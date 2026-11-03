"""Test the model settings with small calls. The command sends no case data.

    .venv/bin/python -m backend.app.model_check

The command tests the main model and the local backup. The main model is the model that you selected on the Models
page, or the model in backend/.env. For each model, it does a plain reply, a tool call and a structured reply. These
are the three types of call that the agent team makes.
"""

import sys
import time

from .agent import llm
from .config import Settings

OK, FAIL = "  ok  ", " FAIL "


def show(name: str, checks: list[dict]) -> bool:
    for check in checks:
        print(f"{OK if check['ok'] else FAIL} {name}: {check['name'].lower()} ({check['detail']}, {check['ms']} ms)")
    return all(check["ok"] for check in checks)


def local_checks(settings) -> list[dict]:
    """The same three calls on the local model."""
    from langchain_core.tools import tool

    from .models import InvestigationPlan

    @tool
    def read_service_logs(service: str) -> str:
        """Read recent logs for a service."""
        return "ERROR db timeout"

    chat = llm.ollama_chat(settings.agent_model, settings.ollama_url)
    calls = [
        ("Plain reply", lambda: chat.invoke("Reply with the single word OK.").text.strip()[:40]),
        ("Tool call", lambda: str(chat.bind_tools([read_service_logs]).invoke(
            "Check the logs of the service named billing. Use the tool.").tool_calls[0]["name"])),
        ("Structured reply", lambda: f"{len(chat.with_structured_output(InvestigationPlan).invoke('Test incident: checkout returns HTTP 500 since a deploy at 09:00. Write an investigation plan.').hypotheses)} hypotheses"),
    ]
    checks = []
    for name, call in calls:
        started = time.perf_counter()
        try:
            checks.append({"name": name, "ok": True, "detail": call(), "ms": round((time.perf_counter() - started) * 1000)})
        except Exception as exc:
            checks.append({"name": name, "ok": False, "detail": str(exc)[:160], "ms": round((time.perf_counter() - started) * 1000)})
    return checks


def main() -> int:
    settings = Settings()
    provider, model = llm.active_model(settings)
    print(f"Agents use: {llm.model_label(settings)}\n")
    ok = True
    if provider != "ollama":
        key = llm.api_key(settings, provider)
        try:
            names = llm.list_models(provider, key)
            print(f"{OK} {llm.PROVIDERS[provider]['maker']} key works. It can use {len(names)} chat models.")
            if model not in names:
                ok = False
                print(f"{FAIL} {model} is not in the list of models for this key. Select another model.")
        except llm.ProviderError as exc:
            print(f"{FAIL} {exc}")
            return 1
        ok = show(model, llm.check_model(provider, model, key)) and ok
    ok = show(f"{settings.agent_model} (local)", local_checks(settings)) and ok
    print("\nAll checks passed." if ok else "\nSome checks failed. Read the lines with FAIL.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
