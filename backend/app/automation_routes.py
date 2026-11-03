"""API for Settings > Automation: the kill switch, approval modes, limits, verification and the action runner.

The API never sends a runner secret back. A secret field that comes back empty keeps the saved secret.
Every change is in the audit log, with what changed. Never a secret."""

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import automation


class AutomationBody(BaseModel):
    paused: bool
    remediation: bool
    modes: dict[str, str]
    limits: dict[str, int]
    verify: dict[str, int | bool]
    runner: dict = Field(default_factory=dict)


RUNNER_FIELDS = {"demo": [], "webhook": [("url", "Address", False), ("secret", "Signing secret", True)],
                 "github_actions": [("repo", "Repository (owner/repo)", False), ("workflow", "Workflow file", False),
                                    ("ref", "Branch", False), ("token", "Token", True)]}


def build_router(get_settings, get_database) -> APIRouter:
    router = APIRouter(prefix="/api/automation")

    def store():
        return automation.store(get_settings())

    def overview() -> dict:
        data = store().load()
        runner = data["runner"]
        secrets = automation.RUNNER_SECRETS.get(runner["type"], set())
        return {
            **{k: data[k] for k in ("paused", "remediation", "modes", "limits", "verify")},
            "runner": {"type": runner["type"], "config": {k: ("" if k in secrets else v) for k, v in runner["config"].items()},
                       "saved_secrets": sorted(k for k in secrets if runner["config"].get(k))},
            "runner_types": {t: [{"name": n, "label": label, "secret": s} for n, label, s in f] for t, f in RUNNER_FIELDS.items()},
            "actions": [{"id": a, "name": automation.ACTION_NAMES[a], "risk": automation.RISK[a],
                         "remediation": a in automation.REMEDIATIONS} for a in automation.RISK],
            "dry_run": get_settings().agent_dry_run,
        }

    def merged(body: AutomationBody) -> dict:
        current = store().load()
        modes = {}
        for action, mode in body.modes.items():
            if action not in automation.RISK or mode not in automation.MODES:
                raise HTTPException(status_code=422, detail=f"Unknown action or mode: {action} {mode}.")
            if mode == "auto" and automation.RISK[action] != "low":
                raise HTTPException(status_code=422, detail=f"{automation.ACTION_NAMES[action]} has {automation.RISK[action]} risk: it always needs a person.")
            modes[action] = mode
        limits = {"per_case_per_hour": int(body.limits.get("per_case_per_hour", 2)), "per_hour": int(body.limits.get("per_hour", 10))}
        if not (1 <= limits["per_case_per_hour"] <= 10 and 1 <= limits["per_hour"] <= 100):
            raise HTTPException(status_code=422, detail="Limits: 1 to 10 for each case, 1 to 100 in total, in one hour.")
        verify = {"first_check_seconds": int(body.verify.get("first_check_seconds", 90)), "checks": int(body.verify.get("checks", 3)),
                  "auto_resolve": bool(body.verify.get("auto_resolve", False)), "follow_up_runs": int(body.verify.get("follow_up_runs", 1))}
        if not (30 <= verify["first_check_seconds"] <= 3600 and 1 <= verify["checks"] <= 5 and 0 <= verify["follow_up_runs"] <= 3):
            raise HTTPException(status_code=422, detail="Verification: first check 30 to 3600 seconds, 1 to 5 checks, 0 to 3 follow-up runs.")
        rtype = (body.runner or {}).get("type", current["runner"]["type"])
        if rtype not in RUNNER_FIELDS:
            raise HTTPException(status_code=422, detail="Unknown runner.")
        allowed = {n for n, _, _ in RUNNER_FIELDS[rtype]}
        config = {k: str(v).strip() for k, v in ((body.runner or {}).get("config") or {}).items() if k in allowed}
        if rtype == current["runner"]["type"]:
            for name in automation.RUNNER_SECRETS.get(rtype, set()):
                if not config.get(name) and current["runner"]["config"].get(name):
                    config[name] = current["runner"]["config"][name]
        return {"paused": body.paused, "remediation": body.remediation, "modes": {**current["modes"], **modes},
                "limits": limits, "verify": verify, "runner": {"type": rtype, "config": config}}

    @router.get("")
    def get_automation() -> dict:
        return overview()

    @router.put("")
    def save_automation(body: AutomationBody, request: Request) -> dict:
        before = store().load()
        data = merged(body)
        if data["runner"]["type"] != "demo":
            try:  # a runner that cannot even be built is not saved
                automation.make_runner(get_settings(), data)
            except automation.AutomationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        store().save(data)
        changed = [k for k in ("paused", "remediation", "modes", "limits", "verify") if before[k] != data[k]]
        if before["runner"]["type"] != data["runner"]["type"] or before["runner"]["config"] != data["runner"]["config"]:
            changed.append(f"runner ({data['runner']['type']})")
        action = "automation.paused" if data["paused"] and not before["paused"] else (
            "automation.resumed" if before["paused"] and not data["paused"] else "automation.saved")
        get_database().record_audit(request.state.user.username, action, "automation", "settings",
                                    f"Changed: {', '.join(changed) or 'nothing'}.")
        return overview()

    @router.post("/test")
    def test_runner(request: Request) -> dict:
        """Demo: always ready. Webhook: sends a signed "ping" event. GitHub Actions: reads the workflow."""
        data = store().load()
        try:
            runner = automation.make_runner(get_settings(), data)
        except automation.AutomationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(runner, automation.WebhookRunner):
            body = json.dumps({"event": "ping"}).encode()
            try:
                response = runner.client.post(runner.url, content=body, headers={
                    "Content-Type": "application/json", "X-RootIQ-Signature": runner.sign(body)})
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"The webhook did not answer ({type(exc).__name__}).") from exc
            if response.status_code >= 300:
                raise HTTPException(status_code=400, detail=f"The webhook answered HTTP {response.status_code}.")
            detail = "The webhook accepted a signed ping."
        elif isinstance(runner, automation.GitHubActionsRunner):
            response = runner.client.get(f"https://api.github.com/repos/{runner.repo}/actions/workflows/{runner.workflow}")
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail=f"GitHub answered HTTP {response.status_code} for the workflow.")
            detail = f"Workflow found: {response.json().get('name', runner.workflow)}."
        else:
            detail = "The demo environment is ready. Its actions change only the demo service logs."
        get_database().record_audit(request.state.user.username, "automation.runner_tested", "automation", data["runner"]["type"], detail)
        return {"ok": True, "detail": detail}

    return router
