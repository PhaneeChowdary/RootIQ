import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from backend.app import main
from backend.app.agent.correlation_agent import CorrelationLink, CorrelationVerdict, correlate
from backend.app.correlation import IncidentGroups, duplicate_of, linked_notes, log_terms, signals
from backend.app.database import CaseDatabase
from backend.app.models import CaseCreate, ResolutionUpdate
from backend.tests import test_monitoring
from backend.tests.test_monitoring import grafana

# The demo story: EU login texts fail at the SMS provider, users take the email fallback, the email queue grows.
AUTH = ["2026-10-02T08:07:55Z INFO [login] fallback=email offered user=u_44120",
        "2026-10-02T08:12:41Z WARN [otp-sender] provider=twilio pool=shared-eu status=429 Too Many Requests region=eu-west",
        "2026-10-02T08:12:41Z ERROR [otp-sender] otp send failed user=u_50231 region=eu-west, not retrying"]
MAIL = ["2026-10-02T08:00:00Z WARN [consumer] queue=email depth=1904 oldest_age_s=6 note=otp_fallback_email volume up",
        "2026-10-02T08:10:00Z ERROR [consumer] queue=email depth=2210 oldest_age_s=900 template=otp_fallback_email"]
PAY = ['2026-10-02T08:05:00Z ERROR [gateway-client] TLS handshake failed host=pay.gateway.example err="certificate verify failed"']


class FakeLogs:
    def __init__(self, lines):
        self.by_service = lines

    def lines(self, service):
        return self.by_service.get(service, [])


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CaseDatabase(Path(self.tmp.name) / "r.db")
        self.db.initialize()
        self.groups = IncidentGroups(self.db.path)

    def tearDown(self):
        self.tmp.cleanup()

    def case(self, title, service, customer="Acme", minutes_ago=0, issue="Something is wrong for our users."):
        case = self.db.create_case(CaseCreate(title=title, customer=customer, service=service, reported_issue=issue))
        moved = case.model_copy(update={"created_at": datetime.now(UTC) - timedelta(minutes=minutes_ago)})
        self.db._write_case(moved)
        return self.db.get_case(case.id)


class SignalTests(unittest.TestCase):
    def test_shared_names_in_the_logs_link_two_services(self):
        self.assertEqual(log_terms(AUTH) & log_terms(MAIL), {"otp"})  # [otp-sender] and template=otp_fallback_email
        self.assertFalse(log_terms(AUTH) & log_terms(PAY))
        now = datetime.now(UTC)
        a = SimpleNamespace(service="auth-service", created_at=now, title="EU login codes missing", reported_issue="",
                            analysis=SimpleNamespace(likely_cause="SMS provider rate limit in eu-west"))
        b = SimpleNamespace(service="notifications", created_at=now - timedelta(minutes=35), title="Emails delayed",
                            reported_issue="", analysis=SimpleNamespace(likely_cause=""))
        facts = signals(a, b, AUTH, MAIL)
        self.assertEqual((facts["minutes_apart"], facts["same_service"]), (35, False))
        self.assertEqual(facts["shared_log_terms"], ["otp"])


class GroupTests(Store):
    def test_link_confirm_separate_and_tidy(self):
        first = self.case("EU login codes missing", "auth-service", minutes_ago=40)
        second = self.case("Emails delayed", "notifications")
        group = self.groups.link(second, first, status="suggested", source="agent", by="agent", reason="r", cause="SMS outage")
        self.assertEqual(group.primary_case_id, first.id)  # the first case is the primary
        self.assertEqual({l.status for l in group.links}, {"suggested"})
        group = self.groups.confirm(second.id, "ann")
        self.assertEqual({l.status for l in group.links}, {"confirmed"})
        notes = linked_notes(self.groups, {c.id: c for c in self.db.list_cases()}, second.id)
        self.assertIn("EU login codes missing", notes[0])
        self.assertIn("SMS outage", notes[0])
        self.assertIsNone(self.groups.separate(second.id, "ann"))  # a group of one case is no group
        self.assertIsNone(self.groups.group_for(first.id))
        self.assertIsNone(self.groups.link(first, second, status="suggested", source="agent", by="agent"))  # never again

    def test_a_third_case_joins_and_two_groups_do_not_merge(self):
        a, b, c, d = (self.case(t, "auth-service") for t in "abcd")
        self.groups.link(a, b, status="confirmed", source="person", by="ann")
        self.assertEqual(len(self.groups.link(c, a, status="suggested", source="agent", by="agent").links), 3)
        self.groups.link(d, self.case("e", "x"), status="confirmed", source="person", by="ann")
        self.assertIsNone(self.groups.link(a, d, status="suggested", source="agent", by="agent"))
        self.groups.set_primary(c.id)
        self.groups.forget_case(c.id)  # deleted forever: the oldest case is the primary again
        self.assertEqual(self.groups.group_for(a.id).primary_case_id, a.id)

    def test_duplicate_rule_only_for_alert_cases_of_the_same_service(self):
        report = self.case("Checkout fails", "payments-api", minutes_ago=30)
        alert = self.case("payments-api: errors rising", "payments-api", customer="Grafana alert")
        self.assertEqual(duplicate_of(self.db.list_cases(), alert, self.groups).id, report.id)
        self.assertIsNone(duplicate_of(self.db.list_cases(), report, self.groups))  # a person's case is never a duplicate
        old = self.case("payments-api: latency", "payments-api", customer="Grafana alert")
        self.db._write_case(old.model_copy(update={"created_at": alert.created_at + timedelta(hours=3)}))
        self.assertIsNone(duplicate_of(self.db.list_cases(), self.db.get_case(old.id), self.groups))  # too far apart
        other = self.case("notifications: errors rising", "notifications", customer="Grafana alert")
        self.assertIsNone(duplicate_of(self.db.list_cases(), other, self.groups))


class FakeModel:
    def __init__(self, verdict):
        self.verdict, self.prompts = verdict, []

    def for_agent(self, name):
        assert name == "correlation"
        return self

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages, config=None):
        self.prompts.append(messages[-1].content)
        return self.verdict


class AgentTests(Store):
    def test_only_links_with_real_log_lines_are_saved(self):
        auth = self.case("EU login verification texts missing", "auth-service", minutes_ago=50,
                         issue="Customers in Germany get no SMS code at sign-in.")
        mail = self.case("Email notifications delayed", "notifications", minutes_ago=10,
                         issue="Order emails arrive two hours late.")
        pay = self.case("Card payments fail", "payments-api", issue="Checkout returns 502 for card payments.")
        verdict = CorrelationVerdict(shared_cause="Twilio rate-limits EU texts; the email fallback floods the queue.", links=[
            CorrelationLink(case_id=mail.id[:8], confidence="high", reason="Failed EU texts move users to email.",
                            evidence=[MAIL[1], "2026-10-02T08:11:00Z ERROR [smtp] made-up line that is not in any log"]),
            CorrelationLink(case_id=pay.id[:8], confidence="medium", reason="Both fail.", evidence=["ERROR something invented here"]),
            CorrelationLink(case_id="ffffffff", confidence="high", reason="x", evidence=[AUTH[2]]),
        ])
        # a model's non-breaking hyphens in a quoted line: still the real line
        verdict.links[0].evidence.append(AUTH[1].replace("shared-eu", "shared\u2011eu").replace("eu-west", "eu\u2011west"))
        model = FakeModel(verdict)
        runner = SimpleNamespace(database=self.db, settings=None, llm_factory=lambda: model)
        kept = correlate(runner, auth.id, logs=FakeLogs({"auth-service": AUTH, "notifications": MAIL, "payments-api": PAY}))
        self.assertEqual([k.case_id for k in kept], [mail.id])
        self.assertEqual(kept[0].evidence, [MAIL[1], AUTH[1]])  # the invented line is dropped
        group = self.groups.group_for(auth.id)
        self.assertEqual((group.primary_case_id, group.cause[:6]), (auth.id, "Twilio"))
        self.assertEqual({l.status for l in group.links}, {"suggested"})
        self.assertNotIn(pay.id[:8], model.prompts[0])  # no shared facts: payments was never a candidate
        # numbered and sorted: the WARN and ERROR lines, and the INFO line where the login offers the email fallback
        #. A bridge line: "email" and "fallback" come from the errors of the notifications service.
        self.assertIn(f"L1 {AUTH[0]}\nL2 {AUTH[2]}\nL3 {AUTH[1]}", model.prompts[0])

    def test_a_link_that_says_unrelated_or_has_no_connecting_line_is_dropped(self):
        auth = self.case("EU login texts fail", "auth-service", issue="German customers get no SMS login code.")
        pay = self.case("Card payments fail for EU customers", "payments-api", issue="German customers see 502 at checkout login.")
        logs = FakeLogs({"auth-service": AUTH, "payments-api": PAY})
        said_unrelated = CorrelationLink(case_id=pay.id[:8], confidence="medium", evidence=[PAY[0]],
                                         reason="The TLS failures are unrelated; there is no direct connection to the texts.")
        no_bridge = CorrelationLink(case_id=pay.id[:8], confidence="high", evidence=[PAY[0]], reason="Both started this morning.")
        only_numbers = no_bridge.model_copy(update={"reason": "L1, L2, L3", "evidence": [AUTH[1], PAY[0]]})
        for link in (said_unrelated, no_bridge, only_numbers, said_unrelated.model_copy(update={"same_cause": False, "reason": "Same."})):
            with self.subTest(reason=link.reason):
                runner = SimpleNamespace(database=self.db, settings=None, llm_factory=lambda: FakeModel(CorrelationVerdict(links=[link])))
                self.assertEqual(correlate(runner, auth.id, logs=logs), [])
        self.assertIsNone(self.groups.group_for(auth.id))

    def test_line_numbers_are_evidence(self):
        auth = self.case("EU login texts fail", "auth-service", issue="German customers get no SMS login code.")
        mail = self.case("Email queue backlog", "notifications", issue="Order emails two hours late.")
        model = FakeModel(CorrelationVerdict(links=[CorrelationLink(case_id=mail.id[:8], confidence="high",
                                                                    reason="Failed texts move users to the email fallback.", evidence=["L2", "5", "line 99"])]))
        runner = SimpleNamespace(database=self.db, settings=None, llm_factory=lambda: model)
        kept = correlate(runner, auth.id, logs=FakeLogs({"auth-service": AUTH, "notifications": MAIL}))
        self.assertEqual(kept[0].evidence, [AUTH[2], MAIL[1]])  # L99 does not exist

    def test_low_confidence_and_resolved_cases(self):
        auth = self.case("EU login texts", "auth-service")
        mail = self.case("Email queue backlog", "notifications")
        runner = SimpleNamespace(database=self.db, settings=None, llm_factory=lambda: FakeModel(CorrelationVerdict(links=[
            CorrelationLink(case_id=mail.id[:8], confidence="low", reason="Maybe.", evidence=[MAIL[1]])])))
        logs = FakeLogs({"auth-service": AUTH, "notifications": MAIL})
        self.assertEqual(correlate(runner, auth.id, logs=logs), [])
        self.db.resolve_case(auth.id, ResolutionUpdate(resolution="Raised the Twilio limit."))
        self.assertEqual(correlate(runner, auth.id, logs=logs), [])  # a resolved case is not checked


class ApiTests(unittest.TestCase):
    send = test_monitoring.MonitoringTests.send

    def setUp(self):
        test_monitoring.MonitoringTests.setUp(self)
        self.saved_runs = main.agent_runs
        main.agent_runs = main.agent_runner.store
        token = self.client.post("/api/auth/login", json={"username": "admin", "password": "pw"}).json()["access_token"]
        self.client.headers.update({"Authorization": f"Bearer {token}"})

    def tearDown(self):
        main.agent_runs = self.saved_runs
        test_monitoring.MonitoringTests.tearDown(self)

    def test_an_alert_on_the_same_service_follows_the_open_case(self):
        report = self.client.post("/api/cases", json={"title": "Checkout fails", "customer": "Acme", "service": "payments-api",
                                                      "reported_issue": "Card payments fail with 502 since this morning."}).json()
        created = self.send(grafana(name="Latency high", fingerprint="fp-9")).json()["created"]
        self.assertEqual(len(created), 1)
        alert_id = created[0]
        self.assertEqual(main.agent_runs.list_for_case(alert_id), [])  # no second investigation
        self.assertEqual(len(main.agent_runs.list_for_case(report["id"])), 1)
        group = self.client.get(f"/api/cases/{alert_id}/incident-group").json()
        self.assertEqual(group["group"]["primary_case_id"], report["id"])
        self.assertEqual(set(group["cases"]), {report["id"], alert_id})
        self.assertIn("Grouped with case", main.database.list_messages(alert_id)[-1].content)
        marks = self.client.get("/api/incident-groups").json()
        self.assertEqual(len(marks), 1)
        # ungroup: the alert case gets its own investigation, and the rule does not group the pair again
        left = self.client.post(f"/api/cases/{alert_id}/incident-group", json={"action": "ungroup"}).json()
        self.assertIsNone(left["group"])
        self.assertEqual(len(main.agent_runs.list_for_case(alert_id)), 1)

    def test_resolve_the_linked_cases_with_the_primary(self):
        report = self.client.post("/api/cases", json={"title": "Checkout fails", "customer": "Acme", "service": "payments-api",
                                                      "reported_issue": "Card payments fail with 502."}).json()
        alert_id = self.send(grafana(name="Latency high", fingerprint="fp-9")).json()["created"][0]
        self.assertEqual(self.client.post(f"/api/cases/{alert_id}/incident-group/resolve-linked").status_code, 409)  # not the primary
        self.assertEqual(self.client.post(f"/api/cases/{report['id']}/incident-group/resolve-linked").status_code, 409)  # not resolved
        self.client.put(f"/api/cases/{report['id']}/resolve", json={"resolution": "Rolled back to 2026.10.02-4."})
        self.assertIn("is resolved", main.database.list_messages(alert_id)[-1].content)
        done = self.client.post(f"/api/cases/{report['id']}/incident-group/resolve-linked").json()
        self.assertEqual(done["resolved"], [alert_id])
        self.assertIn("Rolled back", main.database.get_case(alert_id).resolution)

    def test_viewers_read_engineers_decide(self):
        report = self.client.post("/api/cases", json={"title": "Checkout fails", "customer": "Acme", "service": "payments-api",
                                                      "reported_issue": "Card payments fail with 502."}).json()
        alert_id = self.send(grafana(name="Latency high", fingerprint="fp-9")).json()["created"][0]
        from fastapi.testclient import TestClient

        self.client.post("/api/users", json={"username": "vic", "role": "viewer", "password": "A-long-password-1"})
        viewer = TestClient(main.app)
        token = viewer.post("/api/auth/login", json={"username": "vic", "password": "A-long-password-1"}).json()["access_token"]
        viewer.headers.update({"Authorization": f"Bearer {token}"})
        self.assertEqual(viewer.get(f"/api/cases/{alert_id}/incident-group").status_code, 200)
        self.assertEqual(viewer.post(f"/api/cases/{alert_id}/incident-group", json={"action": "ungroup"}).status_code, 403)
        self.assertEqual(self.client.post(f"/api/cases/{report['id']}/incident-group", json={"action": "primary"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()


class ReasonTests(unittest.TestCase):
    def test_line_numbers_leave_the_reason(self):
        from backend.app.agent.correlation_agent import _clean_reason

        self.assertEqual(_clean_reason("Rate-limited sends (L8) force the email fallback, flooding the queue (L15-L16)."),
                         "Rate-limited sends force the email fallback, flooding the queue.")
        self.assertEqual(_clean_reason("A surge of 429s (L8\u2011L9) explains the errors (lines L3, L4)."), "A surge of 429s explains the errors.")


class QueueOrderTests(unittest.TestCase):
    def test_short_jobs_go_before_investigations(self):
        from backend.app.agent.store import AgentRunStore

        with tempfile.TemporaryDirectory() as tmp:
            store = AgentRunStore(Path(tmp) / "r.db")
            store.initialize()
            store.enqueue_job("investigate", "run-1")
            store.enqueue_job("investigate", "run-2")
            store.enqueue_job("correlate", "correlate:case-1")
            self.assertEqual([store.claim_job("w").target for _ in range(3)], ["correlate:case-1", "run-1", "run-2"])
