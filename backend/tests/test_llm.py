import unittest
from types import SimpleNamespace

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

from backend.app.agent import llm
from backend.app.agent.llm import FallbackChat, ModelRouter, make_chat, model_label, uses_gemini
from backend.app.agent.telemetry import UsageRecorder
from backend.app.models import InvestigationPlan


SEEN: dict[str, list[str]] = {}  # model name -> the text of each call


class ScriptedChat(BaseChatModel):
    """A chat model that answers with fixed text, or raises if `fail` is set."""

    name_tag: str
    reply: str = "ok"
    fail: bool = False

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _get_ls_params(self, stop=None, **kwargs):
        params = super()._get_ls_params(stop=stop, **kwargs)
        params["ls_model_name"] = self.name_tag
        return params

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        SEEN.setdefault(self.name_tag, []).append(" ".join(str(m.content) for m in messages))
        if self.fail:
            raise RuntimeError("429 quota exceeded")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(self.reply))])

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        def parse(value):
            if self.fail:
                raise RuntimeError("429 quota exceeded")
            return schema(hypotheses=[self.name_tag], evidence_task="e", history_task="h")
        return RunnableLambda(parse)


class FallbackChatTests(unittest.TestCase):
    def test_failed_primary_call_is_answered_by_backup(self) -> None:
        chat = FallbackChat(ScriptedChat(name_tag="gemini", fail=True), ScriptedChat(name_tag="qwen", reply="from backup"))
        self.assertEqual(chat.invoke("hi").content, "from backup")
        self.assertEqual(chat.bind_tools([]).invoke("hi").content, "from backup")
        self.assertEqual(chat.with_structured_output(InvestigationPlan).invoke("hi").hypotheses, ["qwen"])

    def test_healthy_primary_is_used(self) -> None:
        chat = FallbackChat(ScriptedChat(name_tag="gemini", reply="from gemini"), ScriptedChat(name_tag="qwen"))
        self.assertEqual(chat.invoke("hi").content, "from gemini")
        self.assertEqual(chat.with_structured_output(InvestigationPlan).invoke("hi").hypotheses, ["gemini"])

    def test_telemetry_records_the_failed_call_and_the_model_that_answered(self) -> None:
        calls = []
        chat = FallbackChat(ScriptedChat(name_tag="gemini", fail=True), ScriptedChat(name_tag="qwen"))
        chat.invoke("hi", {"callbacks": [UsageRecorder(calls.append)], "metadata": {"agent": "lead"}})
        self.assertEqual([(c.model, c.ok, c.agent) for c in calls], [("gemini", False, "lead"), ("qwen", True, "lead")])


class MaskingTests(unittest.TestCase):
    def setUp(self):
        SEEN.clear()

    def test_cloud_model_gets_masked_text_and_local_backup_gets_the_original(self) -> None:
        chat = FallbackChat(ScriptedChat(name_tag="cloud", fail=True), ScriptedChat(name_tag="local"), mask=True)
        chat.invoke([HumanMessage("Customer ann@acme.io, card 4111 1111 1111 1111")])
        chat.bind_tools([]).invoke("mail bob@acme.io")
        self.assertEqual(SEEN["cloud"], ["Customer [email], card [card]", "mail [email]"])
        self.assertEqual(SEEN["local"], ["Customer ann@acme.io, card 4111 1111 1111 1111", "mail bob@acme.io"])

    def test_no_mask_when_off(self) -> None:
        FallbackChat(ScriptedChat(name_tag="cloud"), ScriptedChat(name_tag="local")).invoke("ann@acme.io")
        self.assertEqual(SEEN["cloud"], ["ann@acme.io"])

    def test_make_chat_masks_unless_turned_off(self) -> None:
        base = dict(agent_provider="gemini", gemini_api_key="key", gemini_model="gemini-x", agent_model="qwen3:8b",
                    ollama_url="http://127.0.0.1:11434")
        self.assertTrue(make_chat(SimpleNamespace(**base)).mask)
        self.assertFalse(make_chat(SimpleNamespace(**base, privacy_masking="off")).mask)


class ProviderSettingsTests(unittest.TestCase):
    def settings(self, **overrides):
        base = dict(agent_provider="gemini", gemini_api_key="key", gemini_model="gemini-x", agent_model="qwen3:8b",
                    ollama_url="http://127.0.0.1:11434")
        return SimpleNamespace(**{**base, **overrides})

    def test_gemini_with_key_falls_back_to_local(self) -> None:
        settings = self.settings()
        self.assertTrue(uses_gemini(settings))
        self.assertIsInstance(make_chat(settings), ModelRouter)
        self.assertEqual(model_label(settings), "by task: gemini-x; backup qwen3:8b")

    def test_without_key_or_with_ollama_provider_the_local_model_is_used(self) -> None:
        for settings in (self.settings(gemini_api_key=""), self.settings(agent_provider="ollama")):
            self.assertFalse(uses_gemini(settings))
            self.assertNotIsInstance(make_chat(settings), ModelRouter)
            self.assertEqual(model_label(settings), "qwen3:8b")


class RouterTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.temp = tempfile.TemporaryDirectory()
        llm._paused.clear()

    def tearDown(self):
        llm._paused.clear()
        self.temp.cleanup()

    def settings(self, **overrides):
        from pathlib import Path

        base = dict(agent_provider="auto", gemini_api_key="g-key", gemini_model="gemini-x", groq_api_key="q-key",
                    groq_model="gpt-oss-120b", agent_model="qwen3:8b", ollama_url="http://127.0.0.1:11434",
                    data_dir=Path(self.temp.name))
        return SimpleNamespace(**{**base, **overrides})

    def models(self, settings, tier):
        return [m for _, m in llm.chain_for(settings, tier)]

    def test_each_task_level_has_its_chain(self):
        settings = self.settings()
        self.assertEqual(self.models(settings, "complex"), ["gpt-oss-120b", "gemini-x", "qwen3:8b"])
        self.assertEqual(self.models(settings, "standard"), ["gemini-x", "gpt-oss-120b", "qwen3:8b"])
        self.assertEqual(self.models(settings, "simple"), ["qwen3:8b", "gemini-x"])
        router = make_chat(settings)
        self.assertEqual(router.for_agent("lead").label, "gpt-oss-120b > gemini-x > qwen3:8b")
        self.assertEqual(router.for_agent("evidence").label, "gemini-x > gpt-oss-120b > qwen3:8b")
        self.assertEqual(router.for_agent("comms").label, "qwen3:8b > gemini-x")
        self.assertEqual([link[2] for link in router.for_agent("comms").links], [False, True])  # only cloud is masked

    def test_paused_or_missing_provider_is_skipped(self):
        llm.note_failure("groq", "gpt-oss-120b", RuntimeError("429 RESOURCE_EXHAUSTED retryDelay': '3600s'"))
        self.assertEqual(self.models(self.settings(), "complex"), ["gemini-x", "qwen3:8b"])
        self.assertEqual(self.models(self.settings(gemini_api_key=""), "standard"), ["qwen3:8b"])

    def test_local_only_and_saved_chains(self):
        self.assertEqual(self.models(self.settings(agent_provider="ollama"), "complex"), ["qwen3:8b"])
        settings = self.settings()
        llm.model_store(settings).update(lambda d: d.__setitem__("routes", {"complex": ["gemini", "ollama"], "standard": ["groq"], "simple": ["ollama"]}))
        self.assertEqual(self.models(settings, "complex"), ["gemini-x", "qwen3:8b"])
        self.assertEqual(self.models(settings, "standard"), ["gpt-oss-120b", "qwen3:8b"])  # local is always last

    def test_openai_writes_only_the_code_patches(self):
        import json

        settings = self.settings(openai_daily_tokens=100_000)
        llm.model_store(settings).update(lambda d: (d.setdefault("keys", {}).__setitem__("openai", "o-key"),
                                                    d.setdefault("selected", {}).__setitem__("openai", "gpt-luna")))
        (settings.data_dir / "data_sources.json").write_text(json.dumps({"code": {"type": "demo", "config": {"cloud": "off"}}}))
        router = make_chat(settings)
        self.assertEqual(router.for_agent("code_patch").label, "qwen3:8b")  # without the Cloud models switch: local
        (settings.data_dir / "data_sources.json").write_text(json.dumps({"code": {"type": "demo", "config": {"cloud": "on"}}}))
        router = make_chat(settings)
        self.assertEqual(router.for_agent("code_patch").label, "gpt-luna > gpt-oss-120b > gemini-x > qwen3:8b")
        self.assertEqual(router.for_agent("code").label, "qwen3:8b")  # the code agent reads with the local model
        for tier in ("complex", "standard", "simple"):  # no other task uses the paid key
            self.assertNotIn("gpt-luna", self.models(settings, tier))
        llm.record_tokens("gpt-luna", 75_000)  # above 70% of the budget: still used for code
        self.assertEqual(llm.held_back(settings, "openai", "gpt-luna", "code"), "")
        llm.record_tokens("gpt-luna", 30_000)
        self.assertEqual(llm.held_back(settings, "openai", "gpt-luna", "code"), "daily token budget used")
        llm._tokens.clear()

    def test_main_model_leads_standard_and_follows_groq_in_complex(self):
        settings = self.settings()
        store = llm.model_store(settings)
        store.update(lambda d: (d.setdefault("keys", {}).__setitem__("anthropic", "a-key"),
                                d.__setitem__("active", {"provider": "anthropic", "model": "claude-x"})))
        self.assertEqual(self.models(settings, "standard"), ["claude-x", "gemini-x", "gpt-oss-120b", "qwen3:8b"])
        self.assertEqual(self.models(settings, "complex"), ["gpt-oss-120b", "claude-x", "gemini-x", "qwen3:8b"])
        store.update(lambda d: d.__setitem__("active", {"provider": "ollama", "model": "qwen3:8b"}))
        self.assertEqual(self.models(settings, "complex"), ["qwen3:8b"])


class SharedUsageTests(unittest.TestCase):
    """Token use and model pauses live in the database: a restart or another worker process sees them."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from backend.app import migrations

        self.tmp = tempfile.TemporaryDirectory()
        target = Path(self.tmp.name) / "usage.db"
        migrations.upgrade(target)
        llm.use_database(target)
        self.forget_process_state()

    def tearDown(self):
        llm.use_database(None)
        self.forget_process_state()
        self.tmp.cleanup()

    @staticmethod
    def forget_process_state():
        llm._tokens.clear()
        llm._paused.clear()
        llm._cache.clear()

    def test_tokens_and_pauses_survive_a_restart(self):
        llm.record_tokens("gpt-x", 1200)
        llm.record_tokens("gpt-x", 300)
        llm.note_failure("groq", "oss", RuntimeError("429 Rate limit reached ... tokens per day (TPD) ... try again in 2h"))
        self.forget_process_state()  # a new process
        self.assertEqual(llm.tokens_today("gpt-x"), 1500)
        self.assertEqual(llm.pause_reason("groq", "oss")[1], "daily quota used up")
        llm.fill_today({"gpt-x": 1500})  # the start of the app must not count the same calls again
        llm._cache.clear()
        self.assertEqual(llm.tokens_today("gpt-x"), 1500)


class TimeZoneTests(unittest.TestCase):
    def test_the_postmortem_and_the_day_use_the_team_time_zone(self):
        from datetime import UTC, datetime
        from zoneinfo import ZoneInfo

        from backend.app.agent.postmortem import to_markdown
        from backend.app.models import Postmortem, PostmortemEvent

        at = datetime(2026, 10, 11, 16, 30, tzinfo=UTC)
        p = Postmortem(title="T", summary="S", impact="", root_cause="", trigger="", detection="", resolution="",
                       case_id="c", created_at=at, timeline=[PostmortemEvent(at=at, event="Case opened", source="case")])
        text = to_markdown(p, ZoneInfo("America/New_York"))
        self.assertIn("## Timeline (EDT)", text)
        self.assertIn("- 2026-10-11 12:30:00 (case) Case opened", text)  # 16:30 UTC is 12:30 in Charlotte
        llm.set_time_zone("America/New_York")
        try:
            self.assertEqual(llm._today(), datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d"))
        finally:
            llm.set_time_zone("")


if __name__ == "__main__":
    unittest.main()
