import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient
from mcp import Client

from backend.app import main
from backend.app.mcp_server import create_server
from backend.app.services.auth import LocalAuthenticator
from backend.tests.test_agent import LOGS_DIR, KeywordEmbedder, NoJira
from backend.tests.test_runner import RunnerFixture

WRITE_WORDS = ("propose", "decide", "approve", "send", "delete", "resolve", "create")


class McpServerTests(RunnerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.agent = self.runner()
        main.settings = replace(main.settings, agent_autorun=False, agent_concurrency=0, agent_sla_watch_seconds=0)
        main.database, main.embedder, main.jira_publisher = self.db, KeywordEmbedder(), NoJira()
        main.agent_runs, main.agent_runner = self.agent.store, self.agent
        main.authenticator = LocalAuthenticator("mcp-user", "mcp-password")
        self.api = TestClient(main.app)
        self.api.__enter__()
        settings = SimpleNamespace(auth_username="mcp-user", auth_password="mcp-password", logs_dir=LOGS_DIR,
                                   api_url="http://testserver")
        self.server = create_server(settings, http=self.api, database=self.db, embedder=KeywordEmbedder(), jira=NoJira())

    def tearDown(self):
        self.api.__exit__(None, None, None)
        super().tearDown()

    def call(self, name, args=None):
        async def go():
            async with Client(self.server) as client:
                return await client.call_tool(name, args or {})
        return asyncio.run(go())

    def tools(self):
        async def go():
            async with Client(self.server) as client:
                return (await client.list_tools()).tools
        return asyncio.run(go())

    def test_exposes_no_write_tools_and_marks_read_only_tools(self):
        tools = {t.name: t for t in self.tools()}
        self.assertNotIn("search_jira", tools)  # Jira not configured
        self.assertFalse([n for n in tools if any(w in n for w in WRITE_WORDS)])
        for name in ("list_open_cases", "read_service_logs", "get_investigation", "agent_team_metrics"):
            self.assertTrue(tools[name].annotations.read_only_hint, name)
        self.assertFalse(tools["start_investigation"].annotations.read_only_hint)
        self.assertFalse(tools["start_investigation"].annotations.destructive_hint)

    def test_read_tools(self):
        open_cases = self.call("list_open_cases").structured_content["result"]
        self.assertIn(self.case.id[:8], [c["case_id"] for c in open_cases])
        self.assertTrue(all(c["status"] != "resolved" for c in open_cases))
        matches = self.call("search_similar_incidents", {"query": "SMS otp code"}).structured_content["result"]
        self.assertEqual(matches[0]["title"], "SMS verification codes not arriving for some users")
        logs = self.call("read_service_logs", {"service": "auth-service", "contains": "429"}).structured_content
        self.assertGreater(logs["matched_lines"], 0)
        incident = self.call("get_incident", {"incident_id": matches[0]["incident_id"]}).structured_content
        self.assertIn("Twilio", incident["cause"])

    def test_errors_carry_a_useful_message(self):
        result = self.call("read_service_logs", {"service": "billing"})
        self.assertTrue(result.is_error)
        self.assertIn("Available services: auth-service", result.content[0].text)
        missing = self.call("start_investigation", {"case_id": "zzzz"})
        self.assertIn("No single case matches", missing.content[0].text)

    def test_start_and_follow_an_investigation(self):
        started = self.call("start_investigation", {"case_id": self.case.id[:8]}).structured_content
        self.assertEqual((started["status"], started["queue_position"]), ("queued", 1))
        conflict = self.call("start_investigation", {"case_id": self.case.id[:8]})
        self.assertTrue(conflict.is_error)
        self.assertIn("409", conflict.content[0].text)

        self.agent.process_queue()
        run = self.call("get_investigation", {"run_id": started["run_id"]}).structured_content
        self.assertEqual(run["status"], "awaiting_approval")
        self.assertEqual(run["proposals"], ["Email Northwind Health: Login codes delayed"])
        self.assertIn("engineer must approve", run["next_step"])
        latest = self.call("latest_investigation", {"case_id": self.case.id}).structured_content
        self.assertEqual(latest["run_id"], started["run_id"])
        metrics = self.call("agent_team_metrics").structured_content
        self.assertEqual(metrics["runs_total"], 1)

    def test_case_without_investigation(self):
        result = self.call("latest_investigation", {"case_id": self.case.id[:8]})
        self.assertTrue(result.is_error)
        self.assertIn("not been investigated", result.content[0].text)

    def test_bad_credentials_are_reported(self):
        settings = SimpleNamespace(auth_username="mcp-user", auth_password="wrong", logs_dir=LOGS_DIR, api_url="")
        self.server = create_server(settings, http=self.api, database=self.db, embedder=KeywordEmbedder(), jira=NoJira())
        result = self.call("agent_team_metrics")
        self.assertTrue(result.is_error)
        self.assertIn("rejected the configured credentials", result.content[0].text)


if __name__ == "__main__":
    unittest.main()
