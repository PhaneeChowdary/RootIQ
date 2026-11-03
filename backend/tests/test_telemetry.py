import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from backend.app.agent.team import build_team, initial_state, run_tool_loop, tagged
from backend.app.agent.telemetry import (
    UsageRecorder,
    compute_metrics,
    investigation_seconds,
)
from backend.app.agent.tools import InvestigationToolbox, build_tools
from backend.app.database import CaseDatabase
from backend.app.models import (
    ActionOutcome,
    AgentLesson,
    AgentRun,
    AgentSpan,
    AgentStep,
    InvestigationPlan,
    LLMCall,
    ReviewVerdict,
    SupportAnalysis,
)
from backend.seed.seed_demo import seed
from backend.tests.test_agent import LOGS_DIR, KeywordEmbedder, NoJira
from backend.tests.test_team import APPROVE, FINAL, PLAN, FakeTeamLLM, scripts


class ToolCallingFake(GenericFakeChatModel):
    """A real LangChain chat model that replays scripted messages; tools are accepted and ignored. So callbacks fire."""

    def bind_tools(self, tools, **kwargs):
        return self


def usage(i, o):
    return {"input_tokens": i, "output_tokens": o, "total_tokens": i + o}


class Fixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CaseDatabase(Path(self.tmp.name) / "t.db")
        self.db.initialize()
        seed(self.db, KeywordEmbedder())
        case = next(c for c in self.db.list_cases() if c.title.startswith("Login verification texts"))
        self.box = InvestigationToolbox(case, self.db, KeywordEmbedder(), NoJira(), LOGS_DIR)

    def tearDown(self):
        self.tmp.cleanup()


class UsageRecorderTests(Fixture, unittest.TestCase):
    def test_tool_loop_calls_are_attributed_and_counted(self):
        model = ToolCallingFake(messages=iter([
            AIMessage(content="", tool_calls=[{"name": "read_service_logs", "args": {"service": "auth-service"}, "id": "1"}],
                      usage_metadata=usage(120, 15)),
            AIMessage(content="Done.", usage_metadata=usage(300, 4)),
            AIMessage(content="Report: provider 429s.", usage_metadata=usage(350, 40)),
        ]))
        calls: list[LLMCall] = []
        tools = build_tools(self.box, agent="evidence", only={"read_service_logs"})

        def evidence_step(_):
            messages = run_tool_loop(model, tools, "system", "task", max_rounds=3, agent="evidence")
            model.invoke(messages, config=tagged("evidence"))  # the report call

        RunnableLambda(evidence_step).invoke(None, config={"callbacks": [UsageRecorder(calls.append)]})
        self.assertEqual([c.agent for c in calls], ["evidence"] * 3)
        self.assertEqual(sum(c.input_tokens for c in calls), 770)
        self.assertEqual(sum(c.output_tokens for c in calls), 59)
        self.assertTrue(all(c.ok and c.duration_ms >= 0 for c in calls))

    def test_calls_without_agent_metadata_are_marked_unattributed(self):
        calls = []
        ToolCallingFake(messages=iter([AIMessage(content="x", usage_metadata=usage(1, 1))])).invoke(
            "hi", config={"callbacks": [UsageRecorder(calls.append)]})
        self.assertEqual(calls[0].agent, "unattributed")


class SpanTests(Fixture, unittest.TestCase):
    def test_every_agent_step_is_timed_and_tagged(self):
        spans: list[AgentSpan] = []
        llm = FakeTeamLLM(scripts(), {InvestigationPlan: [PLAN], ReviewVerdict: [APPROVE], SupportAnalysis: [FINAL]})
        build_team(llm, self.box, on_span=spans.append).invoke(initial_state(), {"recursion_limit": 30})
        self.assertEqual({s.agent for s in spans}, {"lead", "evidence", "history", "reviewer", "comms", "finalize"})
        self.assertEqual(spans[0].agent, "lead")
        self.assertEqual(spans[-1].agent, "finalize")
        tags = dict(llm.tags)
        self.assertEqual(tags["InvestigationPlan"], "lead")
        self.assertEqual(tags["ReviewVerdict"], "reviewer")
        self.assertEqual(tags["SupportAnalysis"], "finalize")
        self.assertEqual(tags["evidence-report"], "evidence")


def make_run(n, *, seconds, inp, out, failed_steps=0, rejected=False, decision="approve", trigger="manual"):
    start = datetime(2026, 10, 2, 9, 0, tzinfo=UTC) + timedelta(hours=n)
    spans = [AgentSpan(agent="lead", started_at=start, duration_ms=2000),
             AgentSpan(agent="finalize", started_at=start + timedelta(seconds=seconds - 5), duration_ms=5000)]
    return AgentRun(
        id=f"r{n}", case_id=f"c{n}", model="m", status="completed", trigger=trigger, started_by="e",
        created_at=start, updated_at=start, analysis=SupportAnalysis(), spans=spans,
        llm_calls=[LLMCall(agent="lead", started_at=start, duration_ms=2000, input_tokens=inp, output_tokens=out)],
        steps=[AgentStep(index=i + 1, tool="t", ok=i >= failed_steps) for i in range(4)],
        reviews=[ReviewVerdict(approved=not rejected), APPROVE] if rejected else [APPROVE],
        outcomes=[ActionOutcome(index=0, kind="customer_update", decision=decision, status="dry_run")],
    )


class MetricsTests(unittest.TestCase):
    def test_metrics_across_runs(self):
        runs = [make_run(0, seconds=100, inp=1000, out=100, failed_steps=1, rejected=True, decision="edit"),
                make_run(1, seconds=200, inp=3000, out=300, trigger="sla_risk"),
                make_run(2, seconds=150, inp=2000, out=200, decision="reject")]
        lesson = AgentLesson(id="l", case_id="c0", run_id="r0", service="s", case_title="t", verdict="partly_correct",
                             agent_cause="a", actual_resolution="b", lesson="c", created_at=datetime.now(UTC))
        m = compute_metrics(runs, [lesson])
        self.assertEqual(m.runs_total, 3)
        self.assertEqual(m.by_trigger, {"manual": 2, "sla_risk": 1})
        self.assertEqual(m.median_investigation_seconds, 150)
        self.assertEqual(m.avg_input_tokens, 2000)
        self.assertEqual(m.tool_failure_rate, round(1 / 12, 3))
        self.assertEqual(m.reviewer_rejection_rate, round(1 / 3, 3))
        self.assertEqual(m.decisions, {"edit": 1, "approve": 1, "reject": 1})
        self.assertEqual(m.lesson_verdicts, {"partly_correct": 1})
        self.assertEqual([a.agent for a in m.per_agent], ["lead", "finalize"])

    def test_empty_metrics(self):
        m = compute_metrics([], [])
        self.assertEqual((m.runs_total, m.median_investigation_seconds, m.per_agent), (0, None, []))

    def test_investigation_time_excludes_waiting_for_approval(self):
        run = make_run(0, seconds=90, inp=1, out=1)
        self.assertEqual(investigation_seconds(run), 90)


if __name__ == "__main__":
    unittest.main()
