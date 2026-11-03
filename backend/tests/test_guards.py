import tempfile
import unittest

from backend.app.agent.guards import misquoted_lines
from pathlib import Path

from langchain_core.messages import AIMessage

from backend.app.agent.guards import Deadline, unverified_ids
from backend.app.agent.team import run_team
from backend.app.agent.tools import InvestigationToolbox
from backend.app.database import CaseDatabase
from backend.app.models import InvestigationPlan, ReviewVerdict, SupportAnalysis
from backend.seed.seed_demo import seed
from backend.tests.test_agent import LOGS_DIR, KeywordEmbedder, NoJira
from backend.tests.test_team import APPROVE, FINAL, PLAN, FakeTeamLLM, scripts
from backend.tests.test_team import call as call_tool


class UnverifiedIdTests(unittest.TestCase):
    def test_flags_identifiers_no_tool_returned(self):
        known = 'search result {"incident_id": "a473f2aa"} ticket RI-14'
        text = "Matches a473f2aa and RI-14, like INC-1234 and invoice INV-2023-09-15-001, see also 7c1e9b2f."
        self.assertEqual(unverified_ids(text, known), ["INC-1234", "INV-2023-09-15-001", "7c1e9b2f"])

    def test_ignores_standard_names_words_and_numbers(self):
        text = "UTF-8 logs, SHA-256 digest, at 2026-10-02T07:41:17Z the deadbeef handler hit 51175575 requests."
        self.assertEqual(unverified_ids(text, ""), [])

    def test_known_ids_are_case_insensitive(self):
        self.assertEqual(unverified_ids("see sup-206", "Jira ticket: SUP-206"), [])

    def test_deadline(self):
        self.assertFalse(Deadline(None).expired)
        self.assertTrue(Deadline(0).expired)
        self.assertFalse(Deadline(60).expired)


class TeamGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = CaseDatabase(Path(self.tmp.name) / "g.db")
        self.db.initialize()
        seed(self.db, KeywordEmbedder())
        case = next(c for c in self.db.list_cases() if c.title.startswith("Login verification texts"))
        self.box = InvestigationToolbox(case, self.db, KeywordEmbedder(), NoJira(), LOGS_DIR)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reviewer_rejects_findings_that_cite_an_invented_id(self):
        llm = FakeTeamLLM(
            {**scripts(), "history": scripts()["history"] * 2},
            {InvestigationPlan: [PLAN], ReviewVerdict: [APPROVE, APPROVE], SupportAnalysis: [FINAL]},
            reports={"history": ["Same as past incident INC-4242 (Twilio limits).", "Similar to the SMS incident."]},
        )
        result = run_team(self.box, "fake", llm=llm)
        first = result.reviews[0]
        self.assertFalse(first.approved)  # the model approved; the code check overruled it
        self.assertEqual(first.recheck, ["history"])
        self.assertIn("INC-4242", " ".join(first.unsupported_claims))
        self.assertIn("Remove or verify INC-4242", first.instructions)
        self.assertTrue(result.reviews[1].approved)
        self.assertEqual(result.warnings, [])

    def test_an_id_the_model_passed_to_a_failing_tool_is_still_invented(self):
        llm = FakeTeamLLM(
            {**scripts(), "history": [call_tool("get_incident", {"incident_id": "INV-2023-09-15-001"}, "h9"),
                                      AIMessage(content="ok"), *scripts()["history"]]},
            {InvestigationPlan: [PLAN], ReviewVerdict: [APPROVE, APPROVE], SupportAnalysis: [FINAL]},
            reports={"history": ["Tax rate set wrong, see INV-2023-09-15-001.", "No matching past incident."]},
        )
        result = run_team(self.box, "fake", llm=llm)
        self.assertFalse(next(s for s in result.steps if s.tool == "get_incident").ok)  # the made-up ID failed
        self.assertFalse(result.reviews[0].approved)
        self.assertIn("INV-2023-09-15-001", " ".join(result.reviews[0].unsupported_claims))

    def test_ids_beyond_the_shortened_trace_preview_still_count_as_returned(self):
        long_result = "x" * 3000 + " incident_id=c0ffee42"
        self.box.run("list_service_incidents", lambda **_: long_result, "history")
        self.assertNotIn("c0ffee42", self.box.steps[-1].result)  # the stored preview is cut
        llm = FakeTeamLLM(scripts(), {InvestigationPlan: [PLAN], ReviewVerdict: [APPROVE], SupportAnalysis: [FINAL]},
                          reports={"history": ["Matches past incident c0ffee42."]})
        result = run_team(self.box, "fake", llm=llm)
        self.assertTrue(result.reviews[0].approved)

    def test_final_analysis_is_rewritten_without_an_invented_id(self):
        bad = SupportAnalysis(likely_cause="Twilio 429s, as in incident INC-9001.")
        llm = FakeTeamLLM(scripts(), {InvestigationPlan: [PLAN], ReviewVerdict: [APPROVE], SupportAnalysis: [bad, FINAL]})
        result = run_team(self.box, "fake", llm=llm)
        self.assertEqual(result.analysis.likely_cause, FINAL.likely_cause)
        retry = llm.prompts["SupportAnalysis"][1][-1].content
        self.assertIn("INC-9001 is not in the evidence", retry)
        self.assertEqual(result.warnings, [])

    def test_warning_when_the_rewrite_still_cites_it(self):
        bad = SupportAnalysis(likely_cause="Twilio 429s, as in incident INC-9001.")
        llm = FakeTeamLLM(scripts(), {InvestigationPlan: [PLAN], ReviewVerdict: [APPROVE], SupportAnalysis: [bad, bad]})
        result = run_team(self.box, "fake", llm=llm)
        self.assertIn("Unverified evidence in the analysis: INC-9001", result.warnings[0])

    def test_time_limit_skips_review_and_proposals(self):
        llm = FakeTeamLLM(scripts(), {InvestigationPlan: [PLAN], ReviewVerdict: [APPROVE], SupportAnalysis: [FINAL]})
        result = run_team(self.box, "fake", llm=llm, time_limit_seconds=0)
        self.assertNotIn("ReviewVerdict", llm.prompts)  # the reviewer model was never called
        self.assertNotIn("evidence", llm.prompts)  # no specialist made a model call
        self.assertEqual(result.proposals, [])
        self.assertIn("Time limit of 0 s reached", result.warnings[-1])
        self.assertEqual(result.analysis.likely_cause, FINAL.likely_cause)


if __name__ == "__main__":
    unittest.main()


class LogTimeGuardTests(unittest.TestCase):
    def test_quoted_time_that_no_log_has_is_flagged(self):
        from backend.app.agent.guards import unverified_log_times
        known = "2026-10-02T07:41:17Z WARN [otp-sender] status=429\n2026-10-02T08:03:12Z WARN [cdn] 404"
        text = ("Line `2026-10-02 07:41:17 WARN [otp-sender] status=429` is real, "
                "`2023-10-05 07:38:12 [rate-limit] EU IP range blocked` is invented, and 2026-10-02T08:03 is real.")
        self.assertEqual(unverified_log_times(text, known), ["2023-10-05T07:38:12"])

    def test_text_without_times_is_fine(self):
        from backend.app.agent.guards import unverified_log_times
        self.assertEqual(unverified_log_times("Twilio returned 429 on the shared pool.", "anything"), [])


class ProposalTests(unittest.TestCase):
    def test_a_second_proposal_of_the_same_kind_replaces_the_first(self):
        from types import SimpleNamespace

        from backend.app.agent.tools import InvestigationToolbox
        case = SimpleNamespace(customer="Acme", jira_source_key="KAN-1", jira_issue_key=None)
        box = InvestigationToolbox.__new__(InvestigationToolbox)
        box.case, box.proposals = case, []
        box.propose_customer_update("Update", "First")
        box.propose_customer_update("Update", "Second")
        box.propose_jira_comment("**Cause**: x")
        self.assertEqual([(p.kind, p.payload.get("body", p.payload.get("comment"))) for p in box.proposals],
                         [("customer_update", "Second"), ("jira_comment", "Cause: x")])


class AlertCaseTests(unittest.TestCase):
    def test_an_alert_case_needs_no_customer_email(self):
        from types import SimpleNamespace

        from backend.app.agent.tools import ALERT_CUSTOMER, InvestigationToolbox
        box = InvestigationToolbox.__new__(InvestigationToolbox)
        box.case = SimpleNamespace(customer=ALERT_CUSTOMER, jira_source_key=None, jira_issue_key="KAN-10")
        box.proposals = []
        self.assertEqual(box.missing_required_actions(), ["propose_jira_comment"])
        box.case = SimpleNamespace(customer="Acme", jira_source_key=None, jira_issue_key="KAN-11")
        self.assertEqual(box.missing_required_actions(), ["propose_customer_update", "propose_jira_comment"])


class MisquoteTests(unittest.TestCase):
    KNOWN = "2026-10-03T20:00:20Z ERROR [api] GET /api/widgets 500 timeout acquiring db connection after 5000ms"

    def test_real_quote_passes(self):
        self.assertEqual(misquoted_lines("First error: `2026-10-03T20:00:20Z ERROR [api] GET /api/widgets 500 timeout acquiring db connection after 5000ms`", self.KNOWN), [])
        self.assertEqual(misquoted_lines('"2026-10-03 20:00:20 ERROR [api] GET /api/widgets 500 timeout acquiring db connection"', self.KNOWN), [])

    def test_made_up_words_or_time_zone_fail(self):
        bad = misquoted_lines("First error `2026-10-03T20:00:20Z ERROR failed to acquire DB connection: pool size 2`", self.KNOWN)
        self.assertEqual(bad, ["2026-10-03T20:00:20 (the words differ from the real line)"])
        bad = misquoted_lines("`2026-10-03T20:00:20-04:00 ERROR [api] GET /api/widgets 500 timeout acquiring db connection`", self.KNOWN)
        self.assertIn("not -04:00", bad[0])

    def test_unquoted_or_unknown_times_are_left_to_the_other_guard(self):
        self.assertEqual(misquoted_lines("At 2026-10-03T20:00:20Z the pool ran out.", self.KNOWN), [])
        self.assertEqual(misquoted_lines("`2026-10-03T21:00:00Z ERROR something else entirely here`", self.KNOWN), [])
