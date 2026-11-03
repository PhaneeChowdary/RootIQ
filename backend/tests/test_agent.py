import json
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from backend.app.agent.graph import run_investigation
from backend.app.agent.tools import InvestigationToolbox, ToolFailure, build_tools
from backend.app.database import CaseDatabase
from backend.app.models import SupportAnalysis
from backend.seed.seed_demo import seed

LOGS_DIR = Path(__file__).resolve().parents[1] / "seed" / "logs"


class KeywordEmbedder:
    words = ("sms", "otp", "code", "dashboard", "chunk", "charge", "upload", "email")

    def embed(self, text):
        lower = text.lower()
        return [float(word in lower) for word in self.words]


class NoJira:
    configured = False


class ScriptedPlanner:
    """Stands in for the tool-calling model: returns the scripted replies in order, then stops."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def invoke(self, messages):
        self.seen.append(messages)
        return self.replies.pop(0) if self.replies else AIMessage(content="Done.")


class AlwaysCallsTools:
    def __init__(self):
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return AIMessage(content="", tool_calls=[
            {"name": "read_service_logs", "args": {"service": "auth-service"}, "id": f"c{self.calls}"}
        ])


def call(name, args, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def fake_report(messages):
    fake_report.last_messages = messages
    return SupportAnalysis(likely_cause="SMS provider rate limiting (HTTP 429) on the shared EU pool.")


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CaseDatabase(Path(self.tmp.name) / "agent.db")
        self.db.initialize()
        seed(self.db, KeywordEmbedder())
        self.case = next(c for c in self.db.list_cases() if c.title.startswith("Login verification texts"))

    def tearDown(self):
        self.tmp.cleanup()

    def toolbox(self, case=None):
        return InvestigationToolbox(case or self.case, self.db, KeywordEmbedder(), NoJira(), LOGS_DIR)

    def test_logs_filter_by_level_and_text(self):
        result = self.toolbox().read_service_logs("auth-service", contains="429", level="WARN")
        matches = [line for line in result["lines"] if line.startswith(">> ")]
        self.assertEqual(len(matches), result["matched_lines"])
        self.assertTrue(all("429" in line and " WARN " in line for line in matches))

    def test_filtered_logs_include_surrounding_context(self):
        box = InvestigationToolbox(self.case, self.db, KeywordEmbedder(), NoJira(), LOGS_DIR)
        result = box.read_service_logs("payments-api", contains="duplicate charge", level="ERROR")
        context = [line for line in result["lines"] if not line.startswith(">> ")]
        self.assertTrue(any("renewal-worker" in line and "retrying" in line for line in context))

    def test_external_tool_circuit_breaker(self):
        class BrokenJira:
            configured = True

            def search_issues(self, jql, max_results=5):
                raise RuntimeError("Jira search failed (404).")

        box = InvestigationToolbox(self.case, self.db, KeywordEmbedder(), BrokenJira(), LOGS_DIR)
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "404"):
                box.run("search_jira", box.search_jira, text="sms")
        with self.assertRaisesRegex(ToolFailure, "Do not call it again"):
            box.run("search_jira", box.search_jira, text="sms")
        box.run("read_service_logs", box.read_service_logs, service="auth-service")  # other tools unaffected

    def test_logs_with_no_match_return_a_hint(self):
        result = self.toolbox().read_service_logs("auth-service", contains="We couldn't send your code")
        self.assertEqual(result["matched_lines"], 0)
        self.assertIn("hint", result)

    def test_unknown_service_lists_available_logs(self):
        with self.assertRaises(ToolFailure) as raised:
            self.toolbox().read_service_logs("billing")
        self.assertIn("auth-service", str(raised.exception))

    def test_similar_search_finds_resolved_incident_and_excludes_current_case(self):
        matches = self.toolbox().search_similar_incidents("SMS otp code not arriving")
        self.assertEqual(matches[0]["title"], "SMS verification codes not arriving for some users")
        self.assertNotIn(self.case.id[:8], [m["incident_id"] for m in matches])

    def test_jira_tools_only_offered_when_available(self):
        names = {t.name for t in build_tools(self.toolbox())}
        self.assertNotIn("search_jira", names)
        self.assertNotIn("propose_jira_comment", names)
        with_ticket = self.case.model_copy(update={"jira_source_key": "RI-42"})
        self.assertIn("propose_jira_comment", {t.name for t in build_tools(self.toolbox(with_ticket))})

    def test_full_run_records_trace_and_proposals_without_executing(self):
        planner = ScriptedPlanner([
            call("read_service_logs", {"service": "auth-service", "contains": "429"}, "c1"),
            call("propose_customer_update", {"subject": "Login codes delayed", "body": "We are investigating."}, "c2"),
            AIMessage(content="Rate limiting on the shared EU sender pool."),
        ])
        box = self.toolbox()
        result = run_investigation(box, "fake", max_steps=8, planner=planner, write_report=fake_report)

        self.assertEqual([s.tool for s in result.steps], ["read_service_logs", "propose_customer_update"])
        self.assertTrue(all(s.ok for s in result.steps))
        self.assertEqual(result.tool_rounds, 2)
        self.assertFalse(result.stopped_by_limit)
        self.assertEqual([p.kind for p in result.proposals], ["customer_update"])
        self.assertIn("429", result.analysis.likely_cause)
        tool_results = [m for m in fake_report.last_messages if isinstance(m, ToolMessage)]
        self.assertIn("429", tool_results[0].content)

    def test_step_limit_stops_the_loop(self):
        planner = AlwaysCallsTools()
        result = run_investigation(self.toolbox(), "fake", max_steps=3, planner=planner, write_report=fake_report)
        self.assertTrue(result.stopped_by_limit)
        self.assertEqual(result.tool_rounds, 3)
        self.assertEqual(len(result.steps), 3)
        last = fake_report.last_messages[-2]
        self.assertFalse(isinstance(last, AIMessage) and last.tool_calls, "unexecuted call reached the writer")

    def test_reminds_once_when_required_proposal_is_missing(self):
        planner = ScriptedPlanner([
            call("read_service_logs", {"service": "auth-service"}, "c1"),
            AIMessage(content="Looks like rate limiting."),
            call("propose_customer_update", {"subject": "Update", "body": "Investigating."}, "c2"),
            AIMessage(content="Done."),
        ])
        result = run_investigation(self.toolbox(), "fake", max_steps=8, planner=planner, write_report=fake_report)
        reminders = [m for m in planner.seen[2] if isinstance(m, HumanMessage) and "must call" in m.content]
        self.assertEqual(len(reminders), 1)
        self.assertEqual([p.kind for p in result.proposals], ["customer_update"])

    def test_failed_tool_call_is_traced_and_returned_to_the_model(self):
        planner = ScriptedPlanner([
            call("get_incident", {"incident_id": "zzzz"}, "c1"),
            call("propose_customer_update", {"subject": "Update", "body": "Investigating."}, "c2"),
        ])
        result = run_investigation(self.toolbox(), "fake", max_steps=8, planner=planner, write_report=fake_report)
        self.assertFalse(result.steps[0].ok)
        error_reply = next(m for m in planner.seen[1] if isinstance(m, ToolMessage))
        self.assertIn("No single incident", error_reply.content)

    def test_trace_arguments_are_json_serializable(self):
        box = self.toolbox()
        box.run("read_service_logs", box.read_service_logs, service="auth-service", contains="", level="ERROR")
        json.dumps(box.steps[0].model_dump(mode="json"))


if __name__ == "__main__":
    unittest.main()
