import unittest

from backend.evals.run_evals import markdown, score, summarize
from backend.evals.scenarios import SCENARIOS

BY_ID = {s.id: s for s in SCENARIOS}


class ScoringTests(unittest.TestCase):
    def test_correct_partial_wrong(self):
        s = BY_ID["rec-otp-429"]
        self.assertEqual(score(s, "Twilio returned 429 on the shared-uk pool during a campaign")["verdict"], "correct")
        self.assertEqual(score(s, "SMS provider rate limit")["verdict"], "partial")
        self.assertEqual(score(s, "Database outage")["verdict"], "wrong")

    def test_copying_a_lookalike_is_flagged(self):
        s = BY_ID["look-sms-sender-id"]
        result = score(s, "Twilio rate limit (429) like the earlier SMS incident")
        self.assertEqual(result["verdict"], "wrong")
        self.assertTrue(result["copied_lookalike"])
        self.assertFalse(score(s, "Carrier rejected the unregistered sender ID")["copied_lookalike"])

    def test_no_evidence_rewards_saying_unknown(self):
        s = BY_ID["none-search"]
        self.assertEqual(score(s, "The cause is unknown; no logs are available for search-service.")["verdict"], "correct")
        self.assertEqual(score(s, "Elasticsearch heap exhaustion")["verdict"], "wrong")

    def test_evidence_citation(self):
        s = BY_ID["ctx-disk-full"]
        self.assertTrue(score(s, "Disk full at 05:10:00Z after cleanup was disabled")["cites_evidence"])

    def test_every_scenario_has_logs_unless_it_tests_missing_evidence(self):
        for s in SCENARIOS:
            if s.kind != "no_evidence":
                self.assertIn(s.service, s.logs, s.id)
            self.assertTrue(s.cause and all(s.cause), s.id)

    def test_summary_and_report(self):
        rows = [
            {"scenario": "a", "kind": "lookalike", "mode": "team", "verdict": "correct", "copied_lookalike": False,
             "cites_evidence": True, "seconds": 100, "tool_calls": 5, "tokens_in": 9000, "tokens_out": 900},
            {"scenario": "a", "kind": "lookalike", "mode": "single_shot", "verdict": "wrong", "copied_lookalike": True,
             "cites_evidence": False, "seconds": 20, "tool_calls": 0, "tokens_in": 0, "tokens_out": 0},
        ]
        summary = summarize(rows)
        self.assertEqual(summary["team"]["copied_lookalike"], "0/1")
        self.assertEqual(summary["single_shot"]["copied_lookalike"], "1/1")
        report = markdown(rows, {"started": "2026-10-02T00:00:00", "model": "m", "scenarios": 1})
        self.assertIn("| Correct root cause | 0/1 | 1/1 |", report)
        self.assertIn("wrong (copied look-alike)", report)


if __name__ == "__main__":
    unittest.main()
