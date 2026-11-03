"""Chat models for the agents.

Providers: Google Gemini, Groq, Anthropic, OpenAI and xAI in the cloud, and the local Ollama model.

Routing by task: ModelRouter. Each agent task has a level, and each level has a chain of models:
- complex: lead plan, reviewer, final analysis, remediation, verification, correlation. Groq, Gemini, then local.
- standard: evidence and history tool loops, quick analysis, postmortem. Gemini, Groq, then local.
- simple: message drafts, lessons. Local, then Gemini.
- code: the code agent. The code model, then the agent model, both local.
  Code stays on this computer. Cloud models get the code level only when the code source allows it, on
  Settings > Data sources > Code > Cloud models. Then the chain is Groq, Gemini, local.
Thus the hard work goes to the strongest models, and the easy work does not use their quota. The admin can change
the chains on Settings > Models. The app uses only the providers that have a key.

How a chain works:
- Each call goes to the first model of the chain.
- If that call fails, for example for a rate limit, a quota, the network or a timeout, the same call goes to the
  next model.
- If a provider says that its quota is used up for a long time, the app skips it until that time.

The API keys stay in backend/data/model_settings.json, encrypted. Only the owner of the file can read it. The API
never sends a full key back to the browser.
"""

import json
import os
import re
import threading
import time
from pathlib import Path

import httpx

PROVIDERS: dict[str, dict] = {
    "gemini": {"name": "Gemini", "maker": "Google", "keys_url": "https://aistudio.google.com/apikey",
               "usage_url": "https://ai.dev/rate-limit"},
    "anthropic": {"name": "Claude", "maker": "Anthropic", "keys_url": "https://console.anthropic.com/settings/keys",
                  "usage_url": "https://console.anthropic.com/usage"},
    "openai": {"name": "GPT", "maker": "OpenAI", "keys_url": "https://platform.openai.com/api-keys",
               "usage_url": "https://platform.openai.com/usage", "base_url": "https://api.openai.com/v1"},
    "xai": {"name": "Grok", "maker": "xAI", "keys_url": "https://console.x.ai", "usage_url": "https://console.x.ai",
            "base_url": "https://api.x.ai/v1"},
    "groq": {"name": "Groq", "maker": "Groq", "keys_url": "https://console.groq.com/keys",
             "usage_url": "https://console.groq.com/settings/limits", "base_url": "https://api.groq.com/openai/v1"},
}
OPENAI_STYLE = {"openai", "xai", "groq"}  # these providers use the OpenAI API format
ENV_KEYS = {"gemini": "gemini_api_key", "groq": "groq_api_key"}  # keys that can also come from backend/.env
ENV_MODELS = {"gemini": "gemini_model", "groq": "groq_model"}

TIERS = ("complex", "standard", "simple", "code")
AGENT_TIERS = {
    "lead": "complex", "reviewer": "complex", "finalize": "complex", "remediation": "complex", "verifier": "complex",
    "evidence": "standard", "history": "standard", "changes": "standard", "analysis": "standard",
    "postmortem": "standard", "single": "standard", "correlation": "complex",
    "comms": "simple", "reflect": "simple",
    "code": "code",
    "code_patch": "code",  # the Code agent's patch turn: the first model of the code chain; the reading uses the local model
}
DEFAULT_ROUTES = {
    "complex": ["groq", "gemini", "ollama"],
    "standard": ["gemini", "groq", "ollama"],
    "simple": ["ollama", "gemini"],
    "code": ["ollama"],
}
CODE_CLOUD_ROUTE = ["groq", "gemini", "ollama"]
PATCH_PROVIDER = "openai"  # with a key, it writes the patches first: a strong model where it matters, a few calls per run
TIER_INFO = {
    "complex": "Lead plan, reviewer, final analysis, remediation, verification and correlation",
    "standard": "Log and history searches, quick analysis, postmortem",
    "simple": "Message drafts and lessons",
    "code": "Code agent: the local model reads the code; the first model of the chain writes the patch",
}
# Model names that are not for chat: images, audio, embeddings and similar
NOT_CHAT = re.compile(r"embed|tts|audio|image|imagen|vision-preview|whisper|dall-e|transcri|moderation|realtime|orpheus|guard|allam|"
                      r"search|computer|robotics|davinci|babbage|omni-moderation|sora|veo|lyria|live", re.IGNORECASE)


class ChainChat:
    """A chat model that tries each link of a chain in order, one call at a time. A link has a chat model, the
    arguments for structured output, and a mask flag. A masked link is a cloud model: it gets the text after
    privacy.redact_messages."""

    def __init__(self, links: list[tuple], label: str = "", guard=None) -> None:
        self.links = links
        self.label = label
        self.guard = guard  # guard(index) -> reason to skip link `index` now, or ""

    @property
    def mask(self) -> bool:
        return any(link[2] for link in self.links)

    @staticmethod
    def _masked(runnable, mask: bool):
        if not mask:
            return runnable
        from langchain_core.runnables import RunnableLambda

        from ..privacy import redact_messages
        return RunnableLambda(redact_messages, name="mask_personal_data") | runnable

    def _guarded(self, runnable, index: int):
        """Before each call: skip this link if its model was paused or went over its budget during the run."""
        if self.guard is None or index == len(self.links) - 1:
            return runnable  # the last link always answers
        from langchain_core.runnables import RunnableLambda

        def check(value):
            if reason := self.guard(index):
                raise ModelSkipped(reason)
            return value
        return RunnableLambda(check, name="skip_if_paused") | runnable

    def _chain(self, make):
        runnables = [self._guarded(self._masked(make(chat, structured), mask), i)
                     for i, (chat, structured, mask) in enumerate(self.links)]
        return runnables[0].with_fallbacks(runnables[1:]) if len(runnables) > 1 else runnables[0]

    def invoke(self, input, config=None, **kwargs):
        return self._chain(lambda chat, _: chat).invoke(input, config, **kwargs)

    def bind_tools(self, tools, **kwargs):
        return self._chain(lambda chat, _: chat.bind_tools(tools, **kwargs))

    def with_structured_output(self, schema, **kwargs):
        return self._chain(lambda chat, structured: chat.with_structured_output(schema, **{**structured, **kwargs}))


class FallbackChat:
    """A chat model that tries `primary` first and then `backup`, one call at a time.

    It gives the three operations that the team uses: invoke, bind_tools and with_structured_output.
    Each operation puts with_fallbacks of LangChain around the primary, so the tools and schemas are the same on both
    models. `structured` holds extra arguments for with_structured_output of the primary.
    With `mask`, the input to the primary goes through privacy.redact_messages first, because the primary is a cloud
    model. The backup is the local model, so it gets the text as it is.
    """

    def __init__(self, primary, backup, structured: dict | None = None, mask: bool = False) -> None:
        self.primary = primary
        self.backup = backup
        self.structured = structured or {}
        self.mask = mask

    def _masked(self, runnable):
        if not self.mask:
            return runnable
        from langchain_core.runnables import RunnableLambda

        from ..privacy import redact_messages
        return RunnableLambda(redact_messages, name="mask_personal_data") | runnable

    def invoke(self, input, config=None, **kwargs):
        return self._masked(self.primary).with_fallbacks([self.backup]).invoke(input, config, **kwargs)

    def bind_tools(self, tools, **kwargs):
        return self._masked(self.primary.bind_tools(tools, **kwargs)).with_fallbacks([self.backup.bind_tools(tools, **kwargs)])

    def with_structured_output(self, schema, **kwargs):
        return self._masked(self.primary.with_structured_output(schema, **{**self.structured, **kwargs})).with_fallbacks(
            [self.backup.with_structured_output(schema, **kwargs)]
        )


# --- Quota pause -------------------------------------------------------------------------------------------

_paused: dict[str, tuple[float, str]] = {}  # "provider:model" ->. Resume time, reason.
_paused_lock = threading.Lock()
SHORT_PAUSE_SECONDS = 60  # a rate limit without a wait time: skip the model for this long
BUDGET_RESERVE = 0.7  # above 70% of a daily token budget, only complex tasks use that model


def pause_reason(provider: str, model: str) -> tuple[float, str] | None:
    key = f"{provider}:{model}"
    with _paused_lock:
        entry = _paused.get(key)
        if entry and entry[0] > time.time():
            return entry
        _paused.pop(key, None)
    row = _shared("pause", key, lambda c: c.execute("SELECT until, reason FROM model_pauses WHERE key = ?", (key,)).fetchone())
    if row and float(row[0]) > time.time():  # another process, or this one before a restart, hit the limit
        return float(row[0]), row[1]
    return None


def _wait_seconds(text: str) -> float | None:
    """The wait that the provider asks for: Gemini "retryDelay: '37s'", Groq and OpenAI "try again in 1m2.5s"."""
    if found := re.search(r"retryDelay'?:\s*'?(\d+(?:\.\d+)?)s", text):
        return float(found.group(1))
    if found := re.search(r"try again in\s+(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+(?:\.\d+)?)s)?", text, re.IGNORECASE):
        h, m, s = found.groups()
        if h or m or s:
            return int(h or 0) * 3600 + int(m or 0) * 60 + float(s or 0)
    return None


# --- Shared state: the database keeps token use and pauses, so a restart or another worker process sees them ------

_usage_db = None
_day_zone = None  # the zone of "today" for the budgets, see set_time_zone; None = this computer's zone
_cache: dict[tuple, tuple[float, object]] = {}


def set_time_zone(name: str) -> None:
    """The budgets count a day in this zone, ROOTIQ_TIME_ZONE, so "Today" ends at the user's midnight."""
    global _day_zone
    from ..config import zone

    _day_zone = zone(name) if name else None


def _today() -> str:
    from datetime import datetime

    return datetime.now(_day_zone).strftime("%Y-%m-%d") if _day_zone else time.strftime("%Y-%m-%d")  # -> : the database is read at most every 5 s. Kind, key. Read at, value.


def use_database(target) -> None:
    """Keep the daily token use and the model pauses in this database. Without it, as in the tests,. Migration 9.
    they stay in this process only."""
    global _usage_db
    from .. import db

    _usage_db = db.get(target) if target else None
    _cache.clear()


def _shared(kind: str, key: str, read):
    if _usage_db is None:
        return None
    hit = _cache.get((kind, key))
    if hit and time.time() - hit[0] < 5:
        return hit[1]
    try:
        with _usage_db.connect() as connection:
            value = read(connection)
    except Exception:  # an old database without migration 9, or a database that is down: the process counts alone
        value = None
    _cache[(kind, key)] = (time.time(), value)
    return value


def _write(sql: str, params: tuple) -> None:
    if _usage_db is None:
        return
    try:
        with _usage_db.connect() as connection:
            connection.execute(sql, params)
    except Exception:
        pass


def note_failure(provider: str, model: str, error: BaseException) -> None:
    """Skip a model for a time after a rate limit or a used-up quota. The chains then send the calls straight to
    the next model, without a failed call first."""
    text = str(error)
    if "429" not in text and "RESOURCE_EXHAUSTED" not in text and "quota" not in text.lower() and "rate limit" not in text.lower():
        return
    daily = "PerDay" in text or "per day" in text.lower() or "(TPD)" in text or "(RPD)" in text
    seconds = _wait_seconds(text)
    if seconds is None:
        seconds = 6 * 3600 if daily else SHORT_PAUSE_SECONDS
    reason = "daily quota used up" if daily else "rate limit"
    until = time.time() + min(max(seconds, 20), 24 * 3600)
    with _paused_lock:
        _paused[f"{provider}:{model}"] = (until, reason)
    _write("INSERT INTO model_pauses (key, until, reason) VALUES (?, ?, ?) "
           "ON CONFLICT (key) DO UPDATE SET until = excluded.until, reason = excluded.reason",
           (f"{provider}:{model}", until, reason))
    _cache.pop(("pause", f"{provider}:{model}"), None)


# --- Daily token use for the budgets: ROOTIQ_<PROVIDER>_DAILY_TOKENS -----------------------------------

_tokens: dict[str, int] = {}
_tokens_day = ""


def record_tokens(model: str, count: int) -> None:
    global _tokens_day
    today = _today()
    with _paused_lock:
        if today != _tokens_day:
            _tokens.clear()
            _tokens_day = today
        _tokens[model] = _tokens.get(model, 0) + count
    _write("INSERT INTO model_usage (day, model, tokens) VALUES (?, ?, ?) "
           "ON CONFLICT (day, model) DO UPDATE SET tokens = model_usage.tokens + excluded.tokens", (today, model, int(count)))
    _cache.pop(("tokens", f"{today}:{model}"), None)


def fill_today(counts: dict[str, int]) -> None:
    """At start: today's tokens from the stored runs. With the database, only when it has no count for today yet;
    else a restart would count the same calls again."""
    today = _today()
    has_today = _shared("filled", today, lambda c: c.execute("SELECT 1 FROM model_usage WHERE day = ?", (today,)).fetchone())
    if _usage_db is not None and has_today:
        return
    for model, count in counts.items():
        record_tokens(model, count)


def tokens_today(model: str) -> int:
    """The tokens of this model today: from the database when there is one, so all processes add up. UTC."""
    today = _today()
    row = _shared("tokens", f"{today}:{model}",
                  lambda c: c.execute("SELECT tokens FROM model_usage WHERE day = ? AND model = ?", (today, model)).fetchone())
    if row is not None:
        return int(row[0])
    with _paused_lock:
        own = _tokens.get(model, 0) if _tokens_day == today else 0
    return own


def daily_budget(settings, provider: str) -> int:
    return int(getattr(settings, f"{provider}_daily_tokens", 0) or 0)


def held_back(settings, provider: str, model: str, tier: str) -> str:
    """Why a model is not used for this task now, or "" when it can be used."""
    if provider == "ollama":
        return ""
    if paused := pause_reason(provider, model):
        return paused[1]
    budget = daily_budget(settings, provider)
    used = tokens_today(model)
    if budget and used >= budget:
        return "daily token budget used"
    if budget and tier not in ("complex", "code") and used >= budget * BUDGET_RESERVE:
        return "budget kept for complex tasks"
    return ""


class ModelSkipped(RuntimeError):
    """The chain skips a model that is paused or over its budget. The next model answers the call."""


def _pause_watcher(provider: str, model: str):
    """Watches each call of a cloud model: rate limits pause it, and its tokens count for the daily budget."""
    from langchain_core.callbacks import BaseCallbackHandler

    class PauseWatcher(BaseCallbackHandler):
        def on_llm_error(self, error, **kwargs) -> None:
            note_failure(provider, model, error)

        def on_llm_end(self, response, **kwargs) -> None:
            try:
                usage = response.generations[0][0].message.usage_metadata or {}
            except (AttributeError, IndexError, TypeError):
                usage = {}
            record_tokens(model, int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)))

    return PauseWatcher()


# --- Building chat models ----------------------------------------------------------------------------------

_JSON_CALL = re.compile(r"^\s*(?:```(?:json)?\s*)?(\{.*\})\s*(?:```)?\s*$", re.S)
_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
MAX_RESCUED = 3


def rescue_tool_calls(message, tool_names: set[str]):
    """Some local models write a tool call as JSON text: {"name": ..., "arguments": ...}.
    Change that text into real tool calls, but only for the tools that are bound. The whole reply can be one call, or
    each fenced JSON block can be a call, at most MAX_RESCUED. The text around the blocks is the plan of the model."""
    from langchain_core.messages import AIMessage

    if not isinstance(message, AIMessage) or message.tool_calls or not isinstance(message.content, str):
        return message
    found = _JSON_CALL.match(message.content)
    blocks = [found.group(1)] if found else _FENCED_JSON.findall(message.content)
    calls = []
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        calls += data if isinstance(data, list) else [data]
    calls = [c for c in calls if isinstance(c, dict) and c.get("name") in tool_names][:MAX_RESCUED]
    if not calls:
        return message
    rescued = []
    for i, call in enumerate(calls):
        args = call.get("arguments", call.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                return message
        rescued.append({"name": call["name"], "args": args if isinstance(args, dict) else {}, "id": f"rescued-{i}", "type": "tool_call"})
    return message.model_copy(update={"content": "", "tool_calls": rescued})


def ollama_chat(model: str, base_url: str, json_mode: bool = False):
    from langchain_core.runnables import RunnableLambda
    from langchain_ollama import ChatOllama

    class LocalChat(ChatOllama):
        """ChatOllama with the tool-call rescue for models that write tool calls as text."""

        def bind_tools(self, tools, **kwargs):
            names = {getattr(t, "name", None) or getattr(t, "__name__", "") for t in tools}
            bound = super().bind_tools(tools, **kwargs)
            return bound | RunnableLambda(lambda message: rescue_tool_calls(message, names), name="rescue_tool_calls")

    # num_predict limits each reply. The client timeout limits each call. Thus a long reply or a stuck
    # model server cannot keep a run past its time limit.
    return LocalChat(model=model, base_url=base_url, temperature=0, reasoning=False, num_ctx=16384, num_predict=2048,
                      client_kwargs={"timeout": 180}, format="json" if json_mode else None,
                      callbacks=[_pause_watcher("ollama", model)])  # counts its tokens; a local model is never paused


def gemini_chat(model: str, api_key: str, json_mode: bool = False):
    from langchain_google_genai import ChatGoogleGenerativeAI

    # One fast retry. After that, the backup answers: it is faster than a wait for a rate limit.
    return ChatGoogleGenerativeAI(model=model, google_api_key=api_key, temperature=0, max_retries=1, timeout=90,
                                  response_mime_type="application/json" if json_mode else None,
                                  callbacks=[_pause_watcher("gemini", model)])


def provider_chat(provider: str, model: str, api_key: str, json_mode: bool = False):
    """Make the LangChain chat model for one provider."""
    if provider == "gemini":
        return gemini_chat(model, api_key, json_mode)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model, api_key=api_key, max_tokens=4096, timeout=90, max_retries=1,
                             callbacks=[_pause_watcher(provider, model)])
    if provider in OPENAI_STYLE:
        from langchain_openai import ChatOpenAI

        # No temperature: some OpenAI reasoning models do not accept it.
        return ChatOpenAI(model=model, api_key=api_key, base_url=PROVIDERS[provider]["base_url"], timeout=90,
                          max_retries=1, callbacks=[_pause_watcher(provider, model)],
                          model_kwargs={"response_format": {"type": "json_object"}} if json_mode else {})
    raise ValueError(f"Unknown provider: {provider}")


def structured_args(provider: str) -> dict:
    # The strict JSON schema mode of the OpenAI format refuses some schemas of the app. Tool calls accept all of them.
    return {"method": "function_calling"} if provider in OPENAI_STYLE else {}


# --- Settings: which model is the main model, and the saved keys -------------------------------------------

class ModelStore:
    """Saves the main model choice and the API keys in one JSON file that only the owner can read.
    The file holds the keys encrypted, see crypto.py. load() gives them decrypted."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.Lock()

    def load(self) -> dict:
        from ..crypto import decrypt

        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return {}
        for part in ("keys", "models", "selected"):  # a provider that the app no longer has is ignored
            if isinstance(data.get(part), dict):
                data[part] = {p: v for p, v in data[part].items() if p in PROVIDERS}
        if (data.get("active") or {}).get("provider") not in (*PROVIDERS, "ollama", None):
            data.pop("active", None)
        if data.get("keys"):
            data["keys"] = {provider: decrypt(key) for provider, key in data["keys"].items()}
        return data

    def encrypted(self) -> bool:
        """True when no key in the file is plain text."""
        try:
            keys = json.loads(self.path.read_text()).get("keys", {})
        except (FileNotFoundError, ValueError):
            return True
        return all(str(k).startswith("enc:") for k in keys.values())

    def _save(self, data: dict) -> None:
        from ..crypto import encrypt

        if data.get("keys"):
            data = {**data, "keys": {provider: encrypt(key) for provider, key in data["keys"].items()}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def update(self, change) -> dict:
        with self.lock:
            data = self.load()
            change(data)
            self._save(data)
            return data


_stores: dict[Path, ModelStore] = {}


def model_store(settings) -> ModelStore | None:
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is None:
        return None
    path = Path(data_dir) / "model_settings.json"
    return _stores.setdefault(path, ModelStore(path))


def api_key(settings, provider: str) -> str:
    store = model_store(settings)
    saved = (store.load().get("keys", {}) if store else {}).get(provider, "")
    return saved or (getattr(settings, ENV_KEYS[provider], "") if provider in ENV_KEYS else "")


def provider_model(settings, provider: str) -> str:
    """The model to use for a provider: the one tested on the Models page, else the .env default."""
    if provider == "ollama":
        return settings.agent_model
    store = model_store(settings)
    data = store.load() if store else {}
    active = data.get("active") or {}
    if active.get("provider") == provider:
        return active["model"]
    return data.get("selected", {}).get(provider) or getattr(settings, ENV_MODELS.get(provider, ""), "") or ""


def code_cloud(settings) -> bool:
    """True when the code source allows cloud models: Settings > Data sources > Code > Cloud models."""
    if getattr(settings, "data_dir", None) is None:
        return False
    from ..sources import source_store

    return source_store(settings).get("code")[1].get("cloud", "off") == "on"


def local_only(settings) -> bool:
    """True when the admin chose the local model as the main model, or when ROOTIQ_AGENT_PROVIDER=ollama."""
    store = model_store(settings)
    active = (store.load().get("active") if store else None) or {}
    if active:
        return active.get("provider") == "ollama"
    return getattr(settings, "agent_provider", "auto") == "ollama"


def routes(settings) -> dict[str, list[str]]:
    """The chain of providers for each task level.

    - Chains that the admin saved on the Models page are used as they are.
    - Local model as the main model: local only, for all levels.
    - Else the defaults. A cloud main model leads the standard chain and comes second in the complex chain."""
    store = model_store(settings)
    data = (store.load() if store else None) or {}
    clean = lambda chain: [p for p in chain if p == "ollama" or p in PROVIDERS]  # noqa: E731
    cloud_code = code_cloud(settings)
    default_code = DEFAULT_ROUTES["code"]
    if cloud_code:
        default_code = ([PATCH_PROVIDER] if api_key(settings, PATCH_PROVIDER) else []) + CODE_CLOUD_ROUTE
    if data.get("routes"):
        chains = {tier: clean(data["routes"].get(tier, default_code if tier == "code" else DEFAULT_ROUTES[tier])) for tier in TIERS}
        if not cloud_code:
            chains["code"] = ["ollama"]  # code never goes to a cloud model without the switch
        return chains
    if local_only(settings):
        return {tier: ["ollama"] for tier in TIERS}
    chains = {tier: list(DEFAULT_ROUTES[tier]) for tier in TIERS}
    chains["code"] = list(default_code)
    main = (data.get("active") or {}).get("provider")
    if main and main != "ollama":
        chains["standard"] = [main] + [p for p in chains["standard"] if p != main]
        if main not in chains["complex"]:
            chains["complex"].insert(1, main)
    return chains


def usable(settings, provider: str, tier: str = "complex") -> bool:
    if provider == "ollama":
        return True
    model = provider_model(settings, provider)
    return bool(api_key(settings, provider)) and bool(model) and not held_back(settings, provider, model, tier)


def chain_for(settings, tier: str) -> list[tuple[str, str]]:
    """The provider and the model of each usable link of a tier. The local model is always the last link."""
    chain = [(p, provider_model(settings, p)) for p in routes(settings)[tier] if usable(settings, p, tier)]
    if not any(p == "ollama" for p, _ in chain):
        chain.append(("ollama", settings.agent_model))
    coder = getattr(settings, "code_model", "")
    if tier == "code" and coder and coder != settings.agent_model:  # a different code model first, then the agent model
        at = next(i for i, (p, _) in enumerate(chain) if p == "ollama")
        chain[at:at + 1] = [("ollama", coder), ("ollama", settings.agent_model)]
    return chain


def chat_for_tier(settings, tier: str, json_mode: bool = False) -> ChainChat:
    mask = getattr(settings, "privacy_masking", "cloud") != "off"
    chain = chain_for(settings, tier)
    links = []
    for provider, model in chain:
        if provider == "ollama":
            links.append((ollama_chat(model, settings.ollama_url, json_mode), {}, False))
        else:
            links.append((provider_chat(provider, model, api_key(settings, provider), json_mode),
                          structured_args(provider), mask))
    out = ChainChat(links, " > ".join(m for _, m in chain), guard=lambda i: held_back(settings, chain[i][0], chain[i][1], tier))
    out.providers = [p for p, _ in chain]
    out.models = [m for _, m in chain]
    return out


class ModelRouter:
    """Gives each agent the chain of its task level, see AGENT_TIERS. It also works as a plain chat model with the
    standard chain, for code that does not ask for an agent."""

    def __init__(self, settings, json_mode: bool = False) -> None:
        self.settings = settings
        self.json_mode = json_mode
        self._chains: dict[str, ChainChat] = {}

    def for_tier(self, tier: str) -> ChainChat:
        if tier not in self._chains:
            self._chains[tier] = chat_for_tier(self.settings, tier, self.json_mode)
        return self._chains[tier]

    def for_agent(self, agent: str) -> ChainChat:
        if agent == "code":
            # the Code agent reads and searches with the local links of the code chain: many calls, so no cloud cost.
            # Its patch turn gets the whole chain, with the cloud models first when they are allowed. "code_patch".
            chain = self.for_tier("code")
            pairs = list(zip(chain.links, getattr(chain, "providers", []), getattr(chain, "models", [])))
            local = [(link, model) for link, provider, model in pairs if provider == "ollama"]
            if not local or len(local) == len(chain.links):
                return chain
            return ChainChat([link for link, _ in local], " > ".join(model for _, model in local))
        return self.for_tier(AGENT_TIERS.get(agent, "standard"))

    @property
    def mask(self) -> bool:
        return self.for_tier("standard").mask

    def invoke(self, input, config=None, **kwargs):
        return self.for_tier("standard").invoke(input, config, **kwargs)

    def bind_tools(self, tools, **kwargs):
        return self.for_tier("standard").bind_tools(tools, **kwargs)

    def with_structured_output(self, schema, **kwargs):
        return self.for_tier("standard").with_structured_output(schema, **kwargs)


def uses_gemini(settings) -> bool:
    """True when the .env settings select Gemini. The app uses this when the admin selected no model on the page."""
    provider = getattr(settings, "agent_provider", "ollama")
    return provider == "gemini" and bool(getattr(settings, "gemini_api_key", ""))


def active_model(settings) -> tuple[str, str]:
    """The main model as a provider and a model. The provider "ollama" means local only."""
    store = model_store(settings)
    chosen = store.load().get("active") if store else None
    if chosen and (chosen["provider"] == "ollama" or api_key(settings, chosen["provider"])):
        return chosen["provider"], chosen["model"]
    if uses_gemini(settings):
        return "gemini", settings.gemini_model
    return "ollama", settings.agent_model


def make_chat(settings, json_mode: bool = False):
    """The chat models for the agents and the quick analysis: a router that picks a chain for each task level."""
    router = ModelRouter(settings, json_mode)
    if all(p == "ollama" for tier in TIERS for p, _ in chain_for(settings, tier)):
        return ollama_chat(settings.agent_model, settings.ollama_url, json_mode)  # local only
    return router


def model_label(settings) -> str:
    """The text that the UI shows, for example "by task: openai/gpt-oss-120b, gemini-3.5-flash; backup qwen3:8b"."""
    clouds = []
    for tier in TIERS:
        for provider, model in chain_for(settings, tier):
            if provider != "ollama" and model not in clouds:
                clouds.append(model)
    if not clouds:
        return settings.agent_model
    return f"by task: {', '.join(clouds)}; backup {settings.agent_model}"


# --- Provider API calls for the Models page ----------------------------------------------------------------

class ProviderError(RuntimeError):
    pass


def list_models(provider: str, key: str) -> list[str]:
    """Get the chat models that this key can use. A wrong key gives ProviderError."""
    try:
        if provider == "gemini":
            from google import genai

            client = genai.Client(api_key=key)  # keep a reference: the pager needs the client open
            names = [m.name.removeprefix("models/") for m in client.models.list()
                     if "generateContent" in (m.supported_actions or []) and "gemini" in m.name]
        elif provider == "anthropic":
            response = httpx.get("https://api.anthropic.com/v1/models", params={"limit": 100}, timeout=20,
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            response.raise_for_status()
            names = [m["id"] for m in response.json().get("data", [])]
        elif provider in OPENAI_STYLE:
            response = httpx.get(f"{PROVIDERS[provider]['base_url']}/models", timeout=20,
                                 headers={"Authorization": f"Bearer {key}"})
            response.raise_for_status()
            names = [m["id"] for m in response.json().get("data", [])]
        else:
            raise ProviderError(f"Unknown provider: {provider}")
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        raise ProviderError("The provider rejected this key." if code in (400, 401, 403)
                            else f"The provider answered with error {code}.") from exc
    except httpx.HTTPError as exc:
        raise ProviderError("Cannot reach the provider. Check the network.") from exc
    except ProviderError:
        raise
    except Exception as exc:  # the Google client raises its own error types
        raise ProviderError("The provider rejected this key." if "API_KEY" in str(exc) or "401" in str(exc)
                            or "403" in str(exc) else f"The provider gave an error: {str(exc)[:160]}") from exc
    return sorted({n for n in names if not NOT_CHAT.search(n)}, reverse=True)


def check_model(provider: str, model: str, key: str) -> list[dict]:
    """Do the three types of call that the team makes, with a small test prompt. No case data is sent."""
    from langchain_core.tools import tool

    from ..models import InvestigationPlan

    @tool
    def read_service_logs(service: str) -> str:
        """Read recent logs for a service."""
        return "ERROR db timeout"

    chat = provider_chat(provider, model, key)
    checks = []

    def run(name: str, call) -> None:
        started = time.perf_counter()
        try:
            detail = call()
            checks.append({"name": name, "ok": True, "detail": detail, "ms": round((time.perf_counter() - started) * 1000)})
        except Exception as exc:
            text = str(exc)
            if "429" in text or "RESOURCE_EXHAUSTED" in text:
                text = "Rate limit or quota reached. " + ("Daily quota used up." if "PerDay" in text else "")
            checks.append({"name": name, "ok": False, "detail": text[:220], "ms": round((time.perf_counter() - started) * 1000)})

    run("Plain reply", lambda: chat.invoke("Reply with the single word OK.").text.strip()[:40])

    def tool_call():
        message = chat.bind_tools([read_service_logs]).invoke("Check the logs of the service named billing. Use the tool.")
        if not message.tool_calls:
            raise RuntimeError("The model answered without a call to the tool.")
        return f"{message.tool_calls[0]['name']}({json.dumps(message.tool_calls[0]['args'])})"

    run("Tool call", tool_call)
    run("Structured reply", lambda: f"{len(chat.with_structured_output(InvestigationPlan, **structured_args(provider)).invoke('Test incident: checkout returns HTTP 500 since a deploy at 09:00. Write an investigation plan.').hypotheses)} hypotheses")
    return checks
