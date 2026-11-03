import tempfile
import threading
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from backend.app.agent.team import run_team
from backend.app.agent.tools import InvestigationToolbox
from backend.app.database import CaseDatabase
from backend.app.models import InvestigationPlan, ReviewVerdict, SupportAnalysis
from backend.seed.seed_demo import seed
from backend.tests.test_agent import LOGS_DIR, KeywordEmbedder, NoJira


def call(name, args, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def _agent(config):
    return ((config or {}).get("metadata") or {}).get("agent", "untagged")


class _Scripted:
    def __init__(self, owner, role):
        self.owner, self.role = owner, role

    def invoke(self, messages, config=None):
        with self.owner.lock:
            self.owner.tags.append((self.role, _agent(config)))
            self.owner.prompts.setdefault(self.role, []).append(messages)
            script = self.owner.scripts.get(self.role, [])
            return script.pop(0) if script else AIMessage(content="Done.")


class _Structured:
    def __init__(self, owner, schema):
        self.owner, self.schema = owner, schema

    def invoke(self, messages, config=None):
        with self.owner.lock:
            self.owner.tags.append((self.schema.__name__, _agent(config)))
            self.owner.prompts.setdefault(self.schema.__name__, []).append(messages)
            return self.owner.structured[self.schema].pop(0)


class FakeTeamLLM:
    """Plays every role on the team. Tool-calling roles are told apart by the tools bound to them."""

    def __init__(self, scripts, structured, reports=None):
        self.scripts, self.structured = scripts, structured
        self.reports = reports or {}  # role -> list of report texts, used in order
        self.prompts = {}
        self.tags = []  #. Role or schema, agent named in the call's metadata.
        self.lock = threading.Lock()

    def bind_tools(self, tools):
        names = {t.name for t in tools}
        role = ("evidence" if "read_service_logs" in names else "history" if "get_incident" in names
                else "changes" if "recent_changes" in names else "remediation" if "propose_remediation" in names else "comms")
        return _Scripted(self, role)

    def with_structured_output(self, schema):
        return _Structured(self, schema)

    def invoke(self, messages, config=None):
        system = next(m.content for m in messages if isinstance(m, SystemMessage))
        role = "evidence" if "evidence agent" in system else "changes" if "changes agent" in system else "history"
        with self.lock:
            self.tags.append((f"{role}-report", _agent(config)))
            if self.reports.get(role):
                return AIMessage(content=self.reports[role].pop(0))
        return AIMessage(content=f"{role} report: SMS provider returned 429 at 2026-10-02T07:41:17Z")


PLAN = InvestigationPlan(
    hypotheses=["SMS provider rate limiting", "EU routing change"],
    evidence_task="Look for provider errors in auth-service logs.",
    history_task="Find past SMS delivery incidents.",
)
APPROVE = ReviewVerdict(approved=True)
REJECT = ReviewVerdict(
    approved=False, unsupported_claims=["no retry logic"], recheck=["evidence"],
    instructions="Confirm whether failed sends are retried.",
)
FINAL = SupportAnalysis(likely_cause="Provider 429s on the shared EU pool.", suggested_severity="low")


def scripts():
    return {
        "evidence": [call("read_service_logs", {"service": "auth-service"}, "e1"), AIMessage(content="ok")],
        "history": [call("search_similar_incidents", {"query": "SMS code"}, "h1"), AIMessage(content="ok")],
        "comms": [call("propose_customer_update", {"subject": "Login codes delayed", "body": "We are investigating."}, "c1")],
    }


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CaseDatabase(Path(self.tmp.name) / "team.db")
        self.db.initialize()
        seed(self.db, KeywordEmbedder())
        self.case = next(c for c in self.db.list_cases() if c.title.startswith("Login verification texts"))
        self.box = InvestigationToolbox(self.case, self.db, KeywordEmbedder(), NoJira(), LOGS_DIR)

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, reviews, script=None):
        llm = FakeTeamLLM(script or scripts(), {
            InvestigationPlan: [PLAN], ReviewVerdict: list(reviews), SupportAnalysis: [FINAL],
        })
        return run_team(self.box, "fake", llm=llm), llm

    def test_specialists_run_and_reviewer_sees_raw_tool_output(self):
        result, llm = self.run_with([APPROVE])
        self.assertEqual(set(result.findings), {"evidence", "history"})
        self.assertEqual({s.agent for s in result.steps}, {"evidence", "history", "comms"})
        review_input = llm.prompts["ReviewVerdict"][0][-1].content
        self.assertIn("RAW TOOL OUTPUT", review_input)
        self.assertIn("status=429", review_input)  # actual log line, not just the specialist's summary
        self.assertEqual(len(result.reviews), 1)

    def test_specialists_get_the_lead_task(self):
        _, llm = self.run_with([APPROVE])
        evidence_task = llm.prompts["evidence"][0][1].content
        self.assertIn(PLAN.evidence_task, evidence_task)

    def test_rejection_reruns_only_named_specialist_with_feedback(self):
        result, llm = self.run_with([REJECT, APPROVE], script={
            **scripts(),
            "evidence": [
                call("read_service_logs", {"service": "auth-service"}, "e1"), AIMessage(content="ok"),
                call("read_service_logs", {"service": "auth-service", "contains": "retry"}, "e2"), AIMessage(content="ok"),
            ],
        })
        evidence_steps = [s for s in result.steps if s.agent == "evidence"]
        history_steps = [s for s in result.steps if s.agent == "history"]
        self.assertEqual(len(evidence_steps), 2)
        self.assertEqual(len(history_steps), 1)
        second_task = next(
            m.content for m in llm.prompts["evidence"][2] if isinstance(m, HumanMessage)
        )
        self.assertIn("Confirm whether failed sends are retried.", second_task)
        self.assertEqual([r.approved for r in result.reviews], [False, True])

    def test_revision_budget_is_enforced(self):
        result, _ = self.run_with([REJECT, REJECT])
        self.assertEqual(len(result.reviews), 2)
        self.assertIsNotNone(result.analysis)

    def test_approval_that_lists_unsupported_claims_counts_as_rejection(self):
        contradictory = ReviewVerdict(approved=True, unsupported_claims=["blamed the checkout client"])
        result, _ = self.run_with([contradictory, APPROVE], script={
            **scripts(),
            "evidence": scripts()["evidence"] * 2,
            "history": scripts()["history"] * 2,
        })
        self.assertFalse(result.reviews[0].approved)
        self.assertEqual(result.reviews[0].recheck, ["evidence", "history"])

    def test_severity_never_lowered_and_email_comes_from_proposal(self):
        result, _ = self.run_with([APPROVE])
        self.assertEqual(result.analysis.suggested_severity, "high")
        self.assertIn("kept the reported high", result.severity_note)
        self.assertEqual(result.analysis.customer_email_subject, "Login codes delayed")
        self.assertEqual([p.kind for p in result.proposals], ["customer_update"])

    def test_comms_is_retried_once_when_it_skips_the_update(self):
        result, llm = self.run_with([APPROVE], script={
            **scripts(),
            "comms": [AIMessage(content="Nothing to send."),
                      call("propose_customer_update", {"subject": "Update", "body": "Investigating."}, "c2")],
        })
        self.assertEqual([p.kind for p in result.proposals], ["customer_update"])
        retry_task = llm.prompts["comms"][1][1].content
        self.assertIn("You must call: propose_customer_update", retry_task)


if __name__ == "__main__":
    unittest.main()
