import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.agent import llm
from backend.app.database import CaseDatabase
from backend.app.services.auth import LocalAuthenticator

KEY = "sk-test-0123456789abcd"


def fake_list(provider, key):
    if key.startswith("bad"):
        raise llm.ProviderError("The provider rejected this key.")
    return ["model-b", "model-a"]


class ModelRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.saved = (main.settings, main.database, main.authenticator)
        main.settings = replace(main.settings, data_dir=self.data, gemini_api_key="", groq_api_key="", agent_provider="ollama",
                                agent_autorun=False, agent_concurrency=0, agent_sla_watch_seconds=0)
        main.database = CaseDatabase(self.data / "rootiq.db")
        main.database.initialize()
        main.authenticator = LocalAuthenticator("admin", "pw")
        self.patches = [mock.patch.object(llm, "list_models", fake_list),
                        mock.patch.object(llm, "check_model", lambda p, m, k: [{"name": "Plain reply", "ok": True, "detail": "OK", "ms": 5}])]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(main.app)
        token = self.client.post("/api/auth/login", json={"username": "admin", "password": "pw"}).json()["access_token"]
        self.client.headers.update({"Authorization": f"Bearer {token}"})

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        main.settings, main.database, main.authenticator = self.saved
        self.temp.cleanup()

    def provider(self, body, pid):
        return next(p for p in body["providers"] if p["id"] == pid)

    def test_local_model_is_the_default(self):
        body = self.client.get("/api/models").json()
        self.assertEqual(body["active"]["provider"], "ollama")
        self.assertEqual({p["id"] for p in body["providers"]}, {"gemini", "anthropic", "openai", "xai", "groq"})

    def test_saved_key_is_never_sent_back_in_full(self):
        response = self.client.put("/api/models/openai/key", json={"api_key": KEY})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(KEY, response.text)
        self.assertNotIn(KEY, self.client.get("/api/models").text)
        openai = self.provider(response.json(), "openai")
        self.assertEqual((openai["connected"], openai["key_hint"], openai["models"]), (True, "…abcd", ["model-b", "model-a"]))
        self.assertEqual((self.data / "model_settings.json").stat().st_mode & 0o777, 0o600)
        audit = main.database.list_audit_events() if hasattr(main.database, "list_audit_events") else []
        self.assertFalse(any(KEY in str(event) for event in audit))

    def test_the_chosen_model_is_saved_at_once_and_only_for_code(self):
        self.client.put("/api/models/openai/key", json={"api_key": KEY})
        saved = (self.data / "model_settings.json").read_text()
        self.assertNotIn(KEY, saved)  # encrypted on disk
        self.assertIn('"openai": "enc:', saved)
        body = self.client.put("/api/models/openai/selected", json={"model": "model-a"}).json()
        self.assertEqual(self.provider(body, "openai")["selected_model"], "model-a")
        self.assertEqual(self.client.put("/api/models/openai/selected", json={"model": "gpt-made-up"}).status_code, 422)
        self.assertEqual(self.client.put("/api/models/xai/selected", json={"model": "grok"}).status_code, 400)  # no key
        chains = {t["tier"]: t["chain"] for t in body["routing"]["tiers"]}
        code = next(t for t in body["routing"]["tiers"] if t["tier"] == "code")
        self.assertEqual(code["skipped"], [{"provider": "openai", "reason": "Cloud models is off for the code source"}])
        for tier in ("complex", "standard", "simple"):
            self.assertNotIn("openai", chains[tier])  # a new key does not join the other tasks

    def test_rejected_key_is_not_saved(self):
        response = self.client.put("/api/models/anthropic/key", json={"api_key": "bad-key-123456"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.provider(self.client.get("/api/models").json(), "anthropic")["connected"])

    def test_select_main_model_then_remove_its_key_returns_to_local(self):
        self.client.put("/api/models/xai/key", json={"api_key": KEY})
        body = self.client.put("/api/models/active", json={"provider": "xai", "model": "model-a"}).json()
        self.assertEqual(body["active"]["provider"], "xai")
        self.assertEqual(body["active"]["label"], "by task: model-a; backup qwen3:8b")
        self.assertEqual(llm.active_model(main.settings), ("xai", "model-a"))
        body = self.client.delete("/api/models/xai/key").json()
        self.assertEqual(body["active"]["provider"], "ollama")

    def test_cannot_select_a_provider_without_a_key(self):
        response = self.client.put("/api/models/active", json={"provider": "anthropic", "model": "x"})
        self.assertEqual(response.status_code, 400)

    def test_check_runs_the_model_test(self):
        self.client.put("/api/models/gemini/key", json={"api_key": KEY})
        body = self.client.post("/api/models/gemini/check", json={"model": "model-a"}).json()
        self.assertTrue(body["ok"])


class QuotaPauseTests(unittest.TestCase):
    def test_daily_quota_error_pauses_the_model(self):
        llm.note_failure("gemini", "m-test", RuntimeError("429 RESOURCE_EXHAUSTED ... PerDay ... 'retryDelay': '26103s'"))
        self.assertEqual(llm.pause_reason("gemini", "m-test")[1], "daily quota used up")

    def test_short_rate_limit_pauses_for_the_asked_wait(self):
        llm.note_failure("gemini", "m-short", RuntimeError("429 RESOURCE_EXHAUSTED 'retryDelay': '37s'"))
        until, reason = llm.pause_reason("gemini", "m-short")
        self.assertEqual(reason, "rate limit")
        self.assertAlmostEqual(until - __import__("time").time(), 37, delta=2)

    def test_groq_wait_formats(self):
        llm.note_failure("groq", "g-tpm", RuntimeError("Error code: 429 - Rate limit reached for model on tokens per minute (TPM): Limit 8000. Please try again in 7.5s."))
        self.assertEqual(llm.pause_reason("groq", "g-tpm")[1], "rate limit")
        llm.note_failure("groq", "g-tpd", RuntimeError("Error code: 429 - Rate limit reached on tokens per day (TPD): Limit 200000. Please try again in 12m31.2s."))
        until, reason = llm.pause_reason("groq", "g-tpd")
        self.assertEqual(reason, "daily quota used up")
        self.assertAlmostEqual(until - __import__("time").time(), 751, delta=3)
        llm.note_failure("groq", "g-other", RuntimeError("500 server error"))
        self.assertIsNone(llm.pause_reason("groq", "g-other"))

    def test_budget_keeps_the_rest_for_complex_tasks(self):
        from types import SimpleNamespace

        settings = SimpleNamespace(groq_daily_tokens=1000)
        llm.record_tokens("g-budget", 750)
        self.assertEqual(llm.held_back(settings, "groq", "g-budget", "standard"), "budget kept for complex tasks")
        self.assertEqual(llm.held_back(settings, "groq", "g-budget", "complex"), "")
        llm.record_tokens("g-budget", 300)
        self.assertEqual(llm.held_back(settings, "groq", "g-budget", "complex"), "daily token budget used")

    def test_chain_skips_a_model_paused_during_the_run(self):
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        calls = []

        class Counting(FakeListChatModel):
            def _call(self, *args, **kwargs):
                calls.append(self.responses[0])
                return super()._call(*args, **kwargs)
        paused = {"now": False}
        chain = llm.ChainChat([(Counting(responses=["cloud"]), {}, False), (Counting(responses=["local"]), {}, False)],
                              guard=lambda i: "rate limit" if paused["now"] and i == 0 else "")
        self.assertEqual(chain.invoke("hi").content, "cloud")
        paused["now"] = True
        self.assertEqual(chain.invoke("hi").content, "local")
        self.assertEqual(calls, ["cloud", "local"])  # no call to the paused model


if __name__ == "__main__":
    unittest.main()


class RemovedProviderTests(unittest.TestCase):
    def test_saved_settings_of_a_removed_provider_are_ignored(self):
        import json
        import tempfile
        from pathlib import Path

        from backend.app.agent.llm import ModelStore

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model_settings.json"
            path.write_text(json.dumps({"keys": {"deepseek": "k1", "groq": "k2"}, "models": {"deepseek": ["d"], "groq": ["g"]},
                                        "selected": {"deepseek": "d"}, "active": {"provider": "deepseek", "model": "d"}}))
            data = ModelStore(path).load()
        self.assertEqual((list(data["keys"]), list(data["models"]), data["selected"]), (["groq"], ["groq"], {}))
        self.assertNotIn("active", data)
