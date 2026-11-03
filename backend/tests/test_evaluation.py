import unittest

from backend.evals.run_evals import OfflineEmbedder, gate, team_score


class GateTests(unittest.TestCase):
    def test_score_and_gate(self):
        results = [{"mode": "team", "verdict": v} for v in ("correct", "partial", "wrong", "correct")] + [{"mode": "agent", "verdict": "wrong"}]
        self.assertEqual(team_score(results), 0.625)
        self.assertTrue(gate(0.80, {"team_score": 0.85})[0])  # within the tolerance
        ok, message = gate(0.70, {"team_score": 0.85})
        self.assertFalse(ok)
        self.assertIn("below the baseline", message)
        self.assertFalse(gate(None, {"team_score": 0.85})[0])

    def test_offline_embedder_is_stable_and_similar_for_similar_text(self):
        e = OfflineEmbedder()
        a, b, c = e.embed("SMS codes delayed for EU users"), e.embed("EU users get SMS codes late"), e.embed("disk full on reports")
        dot = lambda x, y: sum(i * j for i, j in zip(x, y))  # noqa: E731
        self.assertEqual(a, e.embed("SMS codes delayed for EU users"))
        self.assertGreater(dot(a, b), dot(a, c))


if __name__ == "__main__":
    unittest.main()
