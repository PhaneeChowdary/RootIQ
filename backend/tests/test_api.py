import tempfile
from datetime import timedelta
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent.runner import AgentRunner
from backend.app.agent.store import AgentRunStore
from backend.app.database import CaseDatabase
from backend.app.models import RunbookRecommendation, SupportAnalysis
from backend.app.services.auth import LocalAuthenticator


class FakeAnalyst:
    def analyze(self, case):
        return SupportAnalysis(
            incident_summary=f"Upload failure reported by {case.customer}.",
            customer_impact="Customers cannot submit recordings.",
            likely_cause="Audio validation failure.",
            suggested_severity="high",
            diagnostic_steps=["Review conversion logs."],
            workaround="Use WAV files temporarily.",
            information_to_request=["Request ID"],
            customer_email_subject="Update on audio upload issue",
            customer_email_body="We are investigating the upload failure.",
        )

    def recommend_runbook(self, case, resolved_cases):
        return RunbookRecommendation(
            title="Audio upload validation runbook",
            symptoms=["Upload conversion failure"],
            verification_steps=["Inspect audio header."],
            mitigation_steps=["Request WAV upload."],
            escalation_guidance="Escalate if valid files continue to fail.",
            evidence_case_ids=[item.id for item in resolved_cases],
        )


class FakeEmbedder:
    def embed(self, text):
        return [1.0, float("upload" in text.lower())]


class FakeJiraPublisher:
    configured = True

    def create_tracking_issue(self, title, customer, service, environment, reported_issue, severity):
        return "SUP-T1", "https://example.atlassian.net/browse/SUP-T1"

    def create_issue(self, case):
        return "SUP-1", "https://example.atlassian.net/browse/SUP-1"

    def __init__(self):
        self.calls = []

    def transition_to_done(self, issue_key):
        self.calls.append(("done", issue_key))

    def transition_to_open(self, issue_key):
        self.calls.append(("open", issue_key))

    def get_comments(self, issue_key):
        return []

    def add_comment(self, issue_key, author, message):
        pass


class FakeGmailPublisher:
    configured = True

    def create_draft(self, subject, body):
        return "draft-1"


class CaseApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        main.database = CaseDatabase(Path(self.temp.name) / "rootiq.db")
        main.analyst = FakeAnalyst()
        main.embedder = FakeEmbedder()
        main.jira_publisher = FakeJiraPublisher()
        main.gmail_publisher = FakeGmailPublisher()
        main.authenticator = LocalAuthenticator("test-agent", "test-password")
        main.settings = replace(main.settings, agent_autorun=False, agent_concurrency=0, agent_sla_watch_seconds=0)
        main.agent_runs = AgentRunStore(Path(self.temp.name) / "rootiq.db")
        main.agent_runner = AgentRunner(
            main.settings, main.database, main.embedder, main.jira_publisher, main.gmail_publisher, main.agent_runs
        )
        self.client = TestClient(main.app)
        self.client.__enter__()
        login = self.client.post(
            "/api/auth/login",
            json={"username": "test-agent", "password": "test-password"},
        )
        self.assertEqual(login.status_code, 200)
        self.client.headers.update(
            {"Authorization": f"Bearer {login.json()['access_token']}"}
        )

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_the_bin_keeps_the_jira_ticket_in_step(self):
        case = self.client.post("/api/cases", json={"title": "Uploads fail", "customer": "Acme", "service": "file-upload",
                                                    "reported_issue": "Uploads fail at 99 percent."}).json()
        self.assertEqual(case["jira_issue_key"], "SUP-T1")
        self.assertEqual(self.client.delete(f"/api/cases/{case['id']}").status_code, 204)
        self.assertIn(("done", "SUP-T1"), main.jira_publisher.calls)  # moved to the bin: Done in Jira
        self.client.post(f"/api/bin/{case['id']}/restore")
        self.assertEqual(main.jira_publisher.calls[-1], ("open", "SUP-T1"))  # back from the bin: To Do in Jira
        actions = [e.action for e in main.database.list_audit_events()]
        self.assertEqual(actions.count("jira.status_synced"), 2)

    def test_a_ticket_closed_in_jira_gives_one_note(self):
        case = self.client.post("/api/cases", json={"title": "Uploads fail", "customer": "Acme", "service": "file-upload",
                                                    "reported_issue": "Uploads fail at 99 percent."}).json()
        main.jira_publisher.search_issues = lambda jql, max_results=50: (
            [{"key": "SUP-T1", "summary": "Uploads fail", "description": "", "status": "Done", "priority": "High"}])
        for _ in range(2):  # a second sync does not repeat the note
            self.assertEqual(self.client.post("/api/jira/sync").status_code, 200)
        notes = [m.content for m in main.database.list_messages(case["id"]) if "closed in Jira" in m.content]
        self.assertEqual(len(notes), 1)
        self.assertNotEqual(main.database.get_case(case["id"]).status, "resolved")  # a person resolves it, not the sync

    def test_authenticated_workflow_records_audit_history(self):
        created = self.client.post(
            "/api/cases",
            json={
                "title": "Audio uploads fail",
                "customer": "Acme",
                "service": "Audio Upload",
                "environment": "production",
                "reported_issue": "Users receive a server error.",
                "logs": "ERROR ffmpeg invalid header",
            },
        )
        self.assertEqual(created.status_code, 200)
        case_id = created.json()["id"]
        self.assertIn(created.json()["sla_state"], ("on_track", "at_risk"))
        self.assertIsNone(created.json()["first_response_at"])

        analyzed = self.client.post(f"/api/cases/{case_id}/analyze")
        self.assertEqual(analyzed.json()["severity"], "high")
        self.assertEqual(analyzed.json()["review_status"], "pending_review")
        acknowledged = self.client.put(
            f"/api/cases/{case_id}/acknowledge",
            json={"note": "Initial response sent to customer."},
        )
        self.assertIsNotNone(acknowledged.json()["first_response_at"])

        approved = self.client.put(
            f"/api/cases/{case_id}/review",
            json={"analysis": analyzed.json()["analysis"], "approve": True},
        )
        self.assertEqual(approved.json()["review_status"], "approved")
        self.assertEqual(approved.json()["status"], "approved")

        self.assertEqual(approved.json()["jira_issue_key"], "SUP-T1")
        self.assertEqual(self.client.post(f"/api/cases/{case_id}/jira").status_code, 409)

        gmail = self.client.post(f"/api/cases/{case_id}/gmail-draft")
        self.assertEqual(gmail.json()["gmail_draft_id"], "draft-1")
        self.assertEqual(self.client.post(f"/api/cases/{case_id}/gmail-draft").status_code, 409)

        resolved = self.client.put(
            f"/api/cases/{case_id}/resolve",
            json={"resolution": "Added audio validation.", "preventive_actions": ["Add alert."]},
        )
        self.assertEqual(resolved.json()["status"], "resolved")
        self.assertEqual(resolved.json()["post_incident_report"]["resolution"], "Added audio validation.")
        self.assertGreaterEqual(len(resolved.json()["timeline"]), 4)
        self.assertEqual(resolved.json()["sla_state"], "met")

        memory = self.client.post(f"/api/cases/{case_id}/memory/index")
        self.assertEqual(memory.json()["indexed_cases"], 1)

        other = self.client.post(
            "/api/cases",
            json={
                "title": "Another upload failure",
                "customer": "Contoso",
                "service": "Audio Upload",
                "reported_issue": "Upload cannot complete.",
            },
        ).json()
        related = self.client.get(f"/api/cases/{other['id']}/related")
        self.assertEqual(related.json()[0]["case"]["id"], case_id)
        runbook = self.client.post(f"/api/cases/{other['id']}/runbook")
        self.assertEqual(runbook.json()["title"], "Audio upload validation runbook")
        self.assertEqual(runbook.json()["evidence_case_ids"], [case_id])
        report = self.client.get(f"/api/cases/{case_id}/report.md")
        self.assertIn("# RootIQ Incident Report: Audio uploads fail", report.text)
        self.assertIn("Added audio validation.", report.text)
        dashboard = self.client.get("/api/dashboard")
        self.assertEqual(dashboard.json()["resolved_cases"], 1)
        self.assertEqual(dashboard.json()["open_cases"], 1)
        trends = self.client.get("/api/trends/services")
        self.assertEqual(trends.json()[0]["service"], "Audio Upload")
        self.assertEqual(trends.json()[0]["total_cases"], 2)

        second_analysis = self.client.post(f"/api/cases/{other['id']}/analyze").json()["analysis"]
        self.client.put(
            f"/api/cases/{other['id']}/review",
            json={"analysis": second_analysis, "approve": True},
        )
        self.client.put(
            f"/api/cases/{other['id']}/resolve",
            json={"resolution": "Rejected invalid file.", "preventive_actions": ["Validate file."]},
        )
        groups = self.client.get("/api/problems")
        self.assertEqual(groups.json()[0]["case_count"], 2)
        audit = self.client.get("/api/audit").json()
        actions = {event["action"] for event in audit}
        self.assertIn("auth.login", actions)
        self.assertIn("case.created", actions)
        self.assertIn("analysis.approved", actions)
        self.assertIn("jira.auto_created", actions)
        self.assertIn("gmail.draft_created", actions)
        self.assertIn("case.resolved", actions)
        self.assertTrue(all(event["actor"] == "test-agent" for event in audit))

    def test_search_cases(self):
        self.client.post(
            "/api/cases",
            json={
                "title": "OAuth error",
                "customer": "Northwind",
                "service": "Email Drafting",
                "reported_issue": "Authentication expired.",
            },
        )
        response = self.client.get("/api/cases/search?q=OAuth")
        self.assertEqual(len(response.json()), 1)

    def test_delete_moves_to_the_recycle_bin_then_restore_or_delete_forever(self):
        case = self.client.post(
            "/api/cases",
            json={
                "title": "Delete me",
                "customer": "Northwind",
                "service": "Support Portal",
                "reported_issue": "Test deletion.",
            },
        ).json()

        response = self.client.delete(f"/api/cases/{case['id']}")

        # Delete moves the case to the recycle bin: it leaves the case list, and it can come back.
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get(f"/api/cases/{case['id']}").status_code, 404)
        self.assertNotIn(case["id"], [c["id"] for c in self.client.get("/api/cases").json()])
        self.assertEqual([b["id"] for b in self.client.get("/api/bin").json()], [case["id"]])
        restored = self.client.post(f"/api/bin/{case['id']}/restore")
        self.assertEqual((restored.status_code, restored.json()["title"]), (200, "Delete me"))
        self.assertEqual(self.client.get(f"/api/cases/{case['id']}").status_code, 200)
        self.assertEqual(self.client.get("/api/bin").json(), [])

        # Delete forever only from the bin.
        self.client.delete(f"/api/cases/{case['id']}")
        self.assertEqual(self.client.delete(f"/api/bin/{case['id']}").status_code, 204)
        self.assertEqual(self.client.get("/api/bin").json(), [])
        self.assertEqual(self.client.post(f"/api/bin/{case['id']}/restore").status_code, 404)
        actions = {event["action"] for event in self.client.get("/api/audit").json()}
        self.assertTrue({"case.moved_to_bin", "case.restored", "case.deleted"} <= actions)

    def test_publication_and_memory_require_approval_or_resolution(self):
        case = self.client.post(
            "/api/cases",
            json={
                "title": "Pending case",
                "customer": "Northwind",
                "service": "Login",
                "reported_issue": "Sign in fails.",
            },
        ).json()
        self.assertEqual(self.client.post(f"/api/cases/{case['id']}/jira").status_code, 400)
        self.assertEqual(self.client.post(f"/api/cases/{case['id']}/gmail-draft").status_code, 400)
        self.assertEqual(self.client.post(f"/api/cases/{case['id']}/memory/index").status_code, 400)

    def test_operational_api_requires_login_and_logout_revokes_session(self):
        client = TestClient(main.app)
        client.__enter__()
        try:
            self.assertEqual(client.get("/api/cases").status_code, 401)
            self.assertEqual(client.get("/api/health").status_code, 401)
            self.assertEqual(
                client.post("/api/auth/login", json={"username": "test-agent", "password": "wrong"}).status_code,
                401,
            )
            token = client.post(
                "/api/auth/login",
                json={"username": "test-agent", "password": "test-password"},
            ).json()["access_token"]
            client.headers.update({"Authorization": f"Bearer {token}"})
            self.assertEqual(client.get("/api/cases").status_code, 200)
            self.assertEqual(client.post("/api/auth/logout").status_code, 204)
            self.assertEqual(client.get("/api/cases").status_code, 401)
        finally:
            client.__exit__(None, None, None)

    def test_cookie_auth_and_logout_clears_cookie(self):
        client = TestClient(main.app)
        client.__enter__()
        try:
            login = client.post(
                "/api/auth/login",
                json={"username": "test-agent", "password": "test-password"},
            )
            self.assertEqual(login.status_code, 200)
            self.assertIn("rootiq_session", login.cookies)
            self.assertEqual(client.get("/api/cases").status_code, 200)
            self.assertEqual(client.post("/api/auth/logout").status_code, 204)
        finally:
            client.__exit__(None, None, None)

    def test_batch_state_endpoint(self):
        self.client.post(
            "/api/cases",
            json={
                "title": "Batch test",
                "customer": "Acme",
                "service": "API",
                "reported_issue": "Failure.",
            },
        )
        state = self.client.get("/api/state")
        self.assertEqual(state.status_code, 200)
        data = state.json()
        self.assertIn("health", data)
        self.assertIn("cases", data)
        self.assertIn("dashboard", data)
        self.assertIn("trends", data)
        self.assertIn("problems", data)
        self.assertIn("audit", data)
        self.assertIn("preferences", data)
        self.assertEqual(len(data["cases"]), 1)

    def test_preferences_persist_in_sqlite(self):
        self.client.put(
            "/api/preferences",
            json={"key": "selected_case_id", "value": "abc-123"},
        )
        prefs = self.client.get("/api/preferences").json()
        self.assertEqual(prefs["selected_case_id"], "abc-123")
        self.client.put(
            "/api/preferences",
            json={"key": "selected_case_id", "value": None},
        )
        prefs = self.client.get("/api/preferences").json()
        self.assertNotIn("selected_case_id", prefs)


class ResolutionTimeTests(unittest.TestCase):
    """A note after the resolution changes updated_at. It must not change the fix time or the SLA result."""

    def test_late_note_keeps_resolution_time_and_sla(self):
        from backend.app.models import CaseCreate, ResolutionUpdate

        with tempfile.TemporaryDirectory() as tmp:
            db = CaseDatabase(Path(tmp) / "rootiq.db")
            db.initialize()
            case = db.create_case(CaseCreate(title="Uploads fail", customer="Acme", service="file-upload",
                                             reported_issue="Uploads fail at 99 percent."))
            db.acknowledge_case(case.id, "")
            resolved = db.resolve_case(case.id, ResolutionUpdate(resolution="Extended the URL expiry."))
            self.assertEqual(resolved.sla_state, "met")
            resolved_at = resolved.resolved_at
            self.assertIsNotNone(resolved_at)

            db.add_event(case.id, "The primary case 1234abcd is resolved: same cause.")
            with db._connect() as connection:  # the note came a week later, after the resolution deadline
                connection.execute("UPDATE support_cases SET updated_at = ? WHERE id = ?",
                                   ((resolved.resolution_due_at + timedelta(days=7)).isoformat(), case.id))
            later = db.get_case(case.id)
            self.assertEqual(later.resolved_at, resolved_at)
            self.assertEqual(later.sla_state, "met")
            trend = next(t for t in db.service_trends() if t.service == "file-upload")
            self.assertEqual(trend.breached_cases, 0)
            self.assertLess(trend.average_resolution_hours, 1)

    def test_resolution_counts_as_the_first_response(self):
        from backend.app.models import CaseCreate, ResolutionUpdate

        with tempfile.TemporaryDirectory() as tmp:
            db = CaseDatabase(Path(tmp) / "rootiq.db")
            db.initialize()
            case = db.create_case(CaseCreate(title="Codes late", customer="Acme", service="auth-service",
                                             reported_issue="Codes arrive late.", severity="high"))
            resolved = db.resolve_case(case.id, ResolutionUpdate(resolution="Moved to a dedicated sender."))
            self.assertIsNone(resolved.first_response_at)
            later = resolved.response_due_at + timedelta(days=3)  # long after the response target
            self.assertEqual(db._sla_state("resolved", resolved.created_at, resolved.resolved_at, resolved.response_due_at,
                                           resolved.resolution_due_at, None, now=later), "met")

    def test_seeded_history_gets_realistic_times(self):
        from backend.seed.seed_demo import history_times, retime, seed

        class NoEmbedder:
            def embed(self, text):
                from backend.app.services.memory import EmbeddingUnavailable
                raise EmbeddingUnavailable("offline")

        item = {"title": "Card payments failing", "customer": "Acme", "service": "payments-api", "severity": "critical",
                "reported_issue": "502", "cause": "CA bundle", "workaround": "none", "resolution": "Updated the bundle.",
                "preventive_actions": ["Track certificates"]}
        with tempfile.TemporaryDirectory() as tmp:
            db = CaseDatabase(Path(tmp) / "rootiq.db")
            db.initialize()
            seed(db, NoEmbedder(), {"resolved": [item], "open": []})
            case = db.list_cases()[0]
            hours = (case.resolved_at - case.created_at).total_seconds() / 3600
            self.assertTrue(1 <= hours <= 4, hours)  # a critical fix within its 4 h target
            self.assertEqual(case.sla_state, "met")
            self.assertLess(case.created_at, case.first_response_at)
            self.assertEqual([e.at for e in case.timeline], sorted(e.at for e in case.timeline))
            self.assertEqual(retime(db, {"resolved": [item]}), 0)  # real times already
            created, answered, resolved = history_times(item)
            self.assertLess(created, answered)
            self.assertLess(answered, resolved)


class DeletedJiraTicketTests(unittest.TestCase):
    def test_a_case_deleted_forever_is_not_imported_again(self):
        from backend.app.models import CaseCreate

        with tempfile.TemporaryDirectory() as tmp:
            db = CaseDatabase(Path(tmp) / "rootiq.db")
            db.initialize()
            case = db.create_case(CaseCreate(title="Checkout fails", customer="Acme", service="payments-api",
                                             reported_issue="502 on pay"))
            db.update_jira_source(case.id, "KAN-14")
            db.move_to_bin(case.id, "admin")
            self.assertIn("KAN-14", db.get_imported_jira_keys())  # in the bin: still known
            db.delete_case(case.id)
            self.assertIn("KAN-14", db.get_imported_jira_keys())  # deleted forever: remembered, a sync skips it
            db.unignore_jira(["KAN-14"])
            self.assertNotIn("KAN-14", db.get_imported_jira_keys())


if __name__ == "__main__":
    unittest.main()
