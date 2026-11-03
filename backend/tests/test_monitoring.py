import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent.runner import AgentRunner
from backend.app.agent.store import AgentRunStore
from backend.app.database import CaseDatabase
from backend.app.services.alerts import parse_grafana
from backend.app.services.auth import LocalAuthenticator
from backend.tests.test_api import FakeAnalyst, FakeEmbedder, FakeGmailPublisher

TOKEN = "alert-test-token"


class NoJira:
    configured = False


def grafana(status="firing", fingerprint="fp-1", service="payments-api", name="Service errors rising", severity="critical"):
    return {"status": status, "alerts": [{
        "status": status, "fingerprint": fingerprint, "startsAt": "2026-10-03T15:00:00Z", "endsAt": "2026-10-03T15:20:00Z",
        "labels": {"alertname": name, "service": service, "severity": severity},
        "annotations": {"summary": f"{service}: error lines rising", "description": f"{service} wrote 8 ERROR lines in 5 minutes."},
        "values": {"B": 8}, "generatorURL": "http://127.0.0.1:3000/alerting/grafana/x/view"}]}


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        data = Path(self.temp.name)
        logs = data / "logs"
        logs.mkdir()
        from datetime import UTC, datetime
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        (logs / "payments-api.log").write_text(f"2020-01-01T00:00:00Z ERROR [charge] old failure\n{now} ERROR [charge] failed\n{now} INFO ok\n")
        self.saved = (main.settings, main.database, main.authenticator, main.jira_publisher, main.agent_runner, main.analyst)
        main.settings = replace(main.settings, data_dir=data, logs_dir=logs, alert_token=TOKEN, agent_autorun=True,
                                agent_concurrency=0, agent_sla_watch_seconds=0, gemini_api_key="", agent_provider="ollama")
        main.database = CaseDatabase(data / "rootiq.db")
        main.database.initialize()
        main.authenticator = LocalAuthenticator("admin", "pw")
        main.jira_publisher = NoJira()
        main.analyst = FakeAnalyst()
        store = AgentRunStore(data / "rootiq.db")
        store.initialize()
        main.agent_runner = AgentRunner(main.settings, main.database, FakeEmbedder(), NoJira(), FakeGmailPublisher(), store)
        self.client = TestClient(main.app)

    def tearDown(self):
        main.settings, main.database, main.authenticator, main.jira_publisher, main.agent_runner, main.analyst = self.saved
        self.temp.cleanup()

    def send(self, payload, token=TOKEN):
        return self.client.post("/api/alerts/grafana", json=payload, headers={"Authorization": f"Bearer {token}"})

    def test_metrics_page_has_cases_runs_and_service_errors(self):
        self.send(grafana())
        text = self.client.get("/metrics").text
        self.assertIn('rootiq_cases{severity="critical",status="new"} 1.0', text)
        self.assertIn('rootiq_agent_runs_total{status="queued",trigger="alert"} 1.0', text)
        self.assertIn("rootiq_agent_queue_length 1.0", text)
        self.assertIn('rootiq_service_log_errors_total{service="payments-api"} 2.0', text)
        self.assertIn('rootiq_service_log_errors_5m{service="payments-api"} 1.0', text)  # only the recent line
        self.assertIn('rootiq_alerts_received_total{result="case_created",status="firing"}', text)
        self.assertNotIn("_created{", text)

    def test_wrong_or_missing_token_is_refused(self):
        self.assertEqual(self.send(grafana(), token="nope").status_code, 401)
        main.settings = replace(main.settings, alert_token="")
        self.assertEqual(self.send(grafana()).status_code, 503)

    def test_firing_alert_opens_one_case(self):
        created = self.send(grafana()).json()["created"]
        self.assertEqual(len(created), 1)
        case = main.database.get_case(created[0])
        self.assertEqual((case.service, case.severity, case.customer), ("payments-api", "critical", "Grafana alert"))
        self.assertIn("Service errors rising", case.logs)
        self.assertEqual(self.send(grafana()).json(), {"created": [], "noted": [], "ignored": 1})  # a repeat
        joined = self.send(grafana(fingerprint="fp-2")).json()  # same rule and service, other labels
        self.assertEqual(joined["noted"], created)

    def test_resolved_alert_adds_a_note_and_a_return_opens_a_new_case(self):
        first = self.send(grafana()).json()["created"][0]
        self.assertEqual(self.send(grafana(status="resolved")).json()["noted"], [first])
        self.assertIn("stopped", main.database.list_messages(first)[-1].content)
        from backend.app.models import ResolutionUpdate
        main.database.resolve_case(first, ResolutionUpdate(resolution="Updated the CA bundle."))
        again = self.send(grafana()).json()["created"]
        self.assertEqual(len(again), 1)
        self.assertIn("came back", main.database.get_case(again[0]).reported_issue)


class ParseTests(unittest.TestCase):
    def test_unknown_severity_becomes_high_and_missing_service_unknown(self):
        alert = parse_grafana({"alerts": [{"labels": {"alertname": "X", "severity": "page"}}]})[0]
        self.assertEqual((alert.severity, alert.service, alert.status), ("high", "unknown", "firing"))


if __name__ == "__main__":
    unittest.main()


class LiveEndpointTests(MonitoringTests):
    def login(self):
        token = self.client.post("/api/auth/login", json={"username": "admin", "password": "pw"}).json()["access_token"]
        self.client.headers.update({"Authorization": f"Bearer {token}"})

    def test_live_lists_active_runs_new_alert_cases_and_backup_calls(self):
        created = self.send(grafana()).json()["created"][0]
        self.login()
        live = self.client.get("/api/live").json()
        self.assertEqual(live["active_runs"][created]["status"], "queued")
        self.assertIn(created, live["case_ids"])
        self.assertEqual(live["alert_cases"][0]["id"], created)
        self.assertEqual(live["backup_calls"], 0)

    def test_trend_ends_with_the_counts_of_now(self):
        self.send(grafana())
        self.login()
        trend = self.client.get("/api/dashboard/trend").json()
        dashboard = self.client.get("/api/dashboard").json()
        self.assertEqual(len(trend["open"]), 25)
        self.assertEqual(trend["open"][-1], dashboard["open_cases"])
        self.assertEqual(trend["urgent_open"][-1], dashboard["urgent_open_cases"])
        self.assertEqual(trend["open"][0], 0)  # the case did not exist 24 hours ago
