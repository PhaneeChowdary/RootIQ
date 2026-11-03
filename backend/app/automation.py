"""Automation: what the agents can change, who must approve it, and how the app carries it out.

Risk levels and approval, on Settings > Automation:
- Each action has a risk level: low for an internal Jira comment, medium for a customer update, high for a change to
  a running service.
- Each action has an approval mode: "auto" runs without a person and is for low risk only, "one" needs one engineer
  or admin, and "two" needs two different people. The API checks the mode again at each decision. The page cannot
  skip it.

Remediation catalog. The remediation agent can propose only these actions, for the service of the case:
    rollback_release      put back an earlier release; the version must be one that a tool returned
    restart_service       restart all instances of the service
    scale_service         set the number of instances: 1 to 20, at most double or half of the current number
    purge_cdn             remove cached content under a path
    disable_feature_flag  turn off one feature flag
    revert_config_change  undo one config change; the change id must be one that a tool returned

Safety rules. The code checks them at the proposal and again just before the action runs:
- The kill switch, Pause automation, stops each remediation at once.
- Rate limits: at most N remediations for each case in one hour, and M in total in one hour.
- Each approved remediation runs one time only. An idempotency key in remediation_log makes sure of it.
- In dry run, the app does not call external runners. The demo runner changes only the demo logs, so it always runs.

Runners:
- "demo": the demo environment in backend/seed/logs.
- "webhook": a signed HTTPS call to your automation, for example Rundeck, n8n, a Lambda or a script.
- "github_actions": starts a workflow with workflow_dispatch.
A webhook call has the header X-RootIQ-Signature: t=<unix time>,v1=<HMAC-SHA256 of "<t>.<body>" with the secret>.
Your endpoint must check it, and refuse the calls that are older than 5 minutes.
"""

import hashlib
import hmac
import json
import os
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from . import db
from .crypto import decrypt, encrypt

RISK = {"jira_comment": "low", "customer_update": "medium", "code_change": "medium", "rollback_release": "high", "restart_service": "high",
        "scale_service": "high", "purge_cdn": "medium", "disable_feature_flag": "high", "revert_config_change": "high"}
REMEDIATIONS = ("rollback_release", "revert_config_change", "restart_service", "scale_service", "purge_cdn",
                "disable_feature_flag")
ACTION_NAMES = {"jira_comment": "Jira comment", "customer_update": "Customer update", "code_change": "Code patch", "rollback_release": "Roll back a release",
                "restart_service": "Restart the service", "scale_service": "Scale the service", "purge_cdn": "Purge the CDN",
                "disable_feature_flag": "Turn off a feature flag", "revert_config_change": "Revert a config change"}
MODES = ("auto", "one", "two")
DEFAULTS = {
    "paused": False,
    "remediation": True,
    "modes": {"jira_comment": "one", "customer_update": "one", "code_change": "one", **{a: "one" for a in REMEDIATIONS}},
    "limits": {"per_case_per_hour": 2, "per_hour": 10},
    "verify": {"first_check_seconds": 90, "checks": 3, "auto_resolve": False, "follow_up_runs": 1},
    "runner": {"type": "demo", "config": {}},
}
RUNNER_SECRETS = {"webhook": {"secret"}, "github_actions": {"token"}}
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,80}$")
FLAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}$")


class AutomationError(ValueError):
    """An action that the rules do not allow. The message says which rule."""


# ---------------------------------------------------------------- settings

class AutomationStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.Lock()

    def load(self) -> dict:
        try:
            saved = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            saved = {}
        data = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
        for key in ("modes", "limits", "verify"):
            data[key] = {**DEFAULTS[key], **(saved.get(key) or {})}
        runner = saved.get("runner") or DEFAULTS["runner"]
        secrets = RUNNER_SECRETS.get(runner.get("type"), set())
        data["runner"] = {"type": runner.get("type", "demo"),
                          "config": {k: (decrypt(v) if k in secrets and v else v) for k, v in (runner.get("config") or {}).items()}}
        for action, mode in list(data["modes"].items()):  # never "auto" above low risk, even in a hand-edited file
            if mode not in MODES or (mode == "auto" and RISK.get(action) != "low"):
                data["modes"][action] = "one"
        return data

    def save(self, data: dict) -> None:
        runner = data.get("runner") or DEFAULTS["runner"]
        secrets = RUNNER_SECRETS.get(runner.get("type"), set())
        stored = {**data, "runner": {"type": runner["type"], "config": {
            k: (encrypt(v) if k in secrets and v else v) for k, v in (runner.get("config") or {}).items()}}}
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(stored, indent=2))
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)


_stores: dict[Path, AutomationStore] = {}


def store(settings) -> AutomationStore:
    path = Path(settings.data_dir) / "automation.json"
    return _stores.setdefault(path, AutomationStore(path))


def approvals_needed(policy: dict, action: str) -> int:
    """0: runs without a person, for low risk only. 1 or 2: the number of different people that must approve."""
    mode = policy["modes"].get(action, "one")
    if mode == "auto" and RISK.get(action) == "low":
        return 0
    return 2 if mode == "two" else 1


# ---------------------------------------------------------------- remediation proposals

_DEPLOY = re.compile(r"(?:release|model|version)=([\w.:+@-]+) started\b[^\n\"]*?previous=([\w.:+@-]+)")


def _same_release(a: str, b: str) -> bool:
    return a == b or a.endswith(":" + b) or b.endswith(":" + a)


def _replaced_by(version: str, evidence: str) -> str | None:
    """The release before `version` when the evidence shows a deploy that started `version` and no later deploy that
    replaced it. Then `version` is the release that runs now."""
    deploys = _DEPLOY.findall(evidence)
    if any(_same_release(previous, version) for _, previous in deploys):
        return None  # a later deploy replaced it: it is a real rollback target
    return next((previous for started, previous in deploys if _same_release(started, version)), None)


def validate(action: str, service: str, params: dict, case_service: str, evidence: str) -> dict:
    """Check a remediation against the catalog and the case. Returns the clean parameters, or raises AutomationError."""
    if action not in REMEDIATIONS:
        raise AutomationError(f"'{action}' is not in the remediation catalog: {', '.join(REMEDIATIONS)}.")
    if service.strip().lower() != case_service.strip().lower():
        raise AutomationError(f"A remediation can change only the service of this case ({case_service}), not '{service}'.")
    clean: dict = {}
    if action == "rollback_release":
        version = str(params.get("to_version", "")).strip()
        if not VERSION.match(version):
            raise AutomationError("rollback_release needs to_version, for example 2026.10.02-4.")
        if version not in evidence:
            raise AutomationError(f"Version {version} is not in the evidence. Use a version that a tool returned "
                                  "(a release from recent_changes or a deploy line in the logs).")
        if before := _replaced_by(version, evidence):  # a rollback to the new release itself would change nothing
            raise AutomationError(f"{version} is the release that the deploy started, not an earlier one. To undo that "
                                  f"deploy, roll back to the release that ran before it: {before}.")
        clean["to_version"] = version
    elif action == "scale_service":
        try:
            replicas = int(params.get("replicas", 0))
            current = int(params.get("current_replicas", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise AutomationError("scale_service needs replicas as a number.") from exc
        if not 1 <= replicas <= 20:
            raise AutomationError("scale_service: replicas must be from 1 to 20.")
        if current and not (current / 2 <= replicas <= current * 2):
            raise AutomationError(f"scale_service: change at most to double or half of the current {current} instances.")
        clean["replicas"] = replicas
        if current:
            clean["current_replicas"] = current
    elif action == "purge_cdn":
        path = str(params.get("path_prefix", "/")).strip() or "/"
        if not PATH.match(path):
            raise AutomationError("purge_cdn: path_prefix must start with / and have only letters, digits and / . _ - ~")
        clean["path_prefix"] = path
    elif action == "disable_feature_flag":
        flag = str(params.get("flag", "")).strip()
        if not FLAG.match(flag):
            raise AutomationError("disable_feature_flag needs the flag name.")
        if flag not in evidence:
            raise AutomationError(f"Flag {flag} is not in the evidence. Name a flag that a tool returned.")
        clean["flag"] = flag
    elif action == "revert_config_change":
        change = str(params.get("change_id", "")).strip()
        if not FLAG.match(change) or len(change) < 4:
            raise AutomationError("revert_config_change needs change_id: the id of the config change (a commit or change id).")
        if change not in evidence:
            raise AutomationError(f"Change {change} is not in the evidence. Use a change id that recent_changes returned.")
        clean["change_id"] = change
    return clean


def describe(action: str, service: str, params: dict) -> str:
    if action == "rollback_release":
        return f"Roll back {service} to release {params['to_version']}"
    if action == "restart_service":
        return f"Restart {service}"
    if action == "scale_service":
        return f"Scale {service} to {params['replicas']} instances"
    if action == "purge_cdn":
        return f"Purge the CDN cache of {service} under {params['path_prefix']}"
    if action == "disable_feature_flag":
        return f"Turn off feature flag {params['flag']} in {service}"
    if action == "revert_config_change":
        return f"Revert config change {params['change_id']} in {service}"
    return f"{action} on {service}"


# ---------------------------------------------------------------- the log of remediations, see migration 6

class RemediationLog:
    def __init__(self, target) -> None:
        self._db = db.get(target)

    def recent(self, since: datetime, case_id: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM remediation_log WHERE created_at >= ? AND status != 'refused'"
        params: tuple = (since.isoformat(),)
        if case_id:
            sql += " AND case_id = ?"
            params += (case_id,)
        with self._db.connect() as connection:
            return connection.execute(sql, params).fetchone()[0]

    def start(self, key: str, case_id: str, run_id: str, action: str, service: str, params: dict, approved_by: str) -> bool:
        """Record the start. False when this key already ran: an approved action never runs two times."""
        with self._db.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO remediation_log (key, case_id, run_id, action, service, params, status, detail, approved_by, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, 'started', '', ?, ?) ON CONFLICT DO NOTHING",
                (key, case_id, run_id, action, service, json.dumps(params), approved_by, datetime.now(UTC).isoformat()))
            return cursor.rowcount == 1

    def finish(self, key: str, status: str, detail: str) -> None:
        with self._db.connect() as connection:
            connection.execute("UPDATE remediation_log SET status = ?, detail = ?, finished_at = ? WHERE key = ?",
                               (status, detail[:500], datetime.now(UTC).isoformat(), key))

    def for_case(self, case_id: str) -> list[dict]:
        with self._db.connect() as connection:
            rows = connection.execute("SELECT key, run_id, action, service, params, status, detail, approved_by, created_at, "
                                      "finished_at FROM remediation_log WHERE case_id = ? ORDER BY created_at", (case_id,)).fetchall()
        return [{"key": r[0], "run_id": r[1], "action": r[2], "service": r[3], "params": json.loads(r[4] or "{}"),
                 "status": r[5], "detail": r[6], "approved_by": r[7], "created_at": r[8], "finished_at": r[9]} for r in rows]


def check_limits(policy: dict, log: RemediationLog, case_id: str, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    hour = now - timedelta(hours=1)
    if policy["paused"]:
        raise AutomationError("Automation is paused (Settings > Automation). No remediation runs.")
    if not policy["remediation"]:
        raise AutomationError("Remediation is off (Settings > Automation).")
    if log.recent(hour, case_id) >= int(policy["limits"]["per_case_per_hour"]):
        raise AutomationError(f"Limit: at most {policy['limits']['per_case_per_hour']} remediations for one case in one hour.")
    if log.recent(hour) >= int(policy["limits"]["per_hour"]):
        raise AutomationError(f"Limit: at most {policy['limits']['per_hour']} remediations in one hour.")


# ---------------------------------------------------------------- runners

class DemoRunner:
    """Acts on the demo environment: it writes what the action does into the demo log of the service.
    If simulate_incident made an incident on that service, the action that fixes it ends the errors. Another action does
    not, and the errors continue. Thus the verification agent sees the real result."""

    name = "Demo environment"
    external = False

    def __init__(self, settings) -> None:
        self.logs = Path(settings.logs_dir)
        self.state_file = Path(settings.data_dir) / "demo_state.json"

    def _state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def run(self, action: str, service: str, params: dict, key: str, extra: dict | None = None) -> tuple[str, str]:
        log = self.logs / f"{service}.log"
        if not log.exists():
            return "failed", f"The demo environment has no service '{service}'."
        now = datetime.now(UTC)
        stamp = lambda s=0: (now + timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        state = self._state()
        incident = state.get(service)
        lines = [f"{stamp()} INFO [ops] {describe(action, service, params).lower()} started by=rootiq key={key[:12]}"]
        fixes = bool(incident) and action in incident.get("fixed_by", {}) and \
            all(str(params.get(k)) == str(v) for k, v in (incident["fixed_by"][action] or {}).items())
        if fixes:
            lines += [f"{stamp(1)} INFO [ops] {action} complete"] + [f"{stamp(2 + i)} {line}" for i, line in enumerate(incident["healthy"])]
            state[service] = {**incident, "fixed_at": now.isoformat(), "fixed_by_action": action}
        else:
            lines.append(f"{stamp(1)} INFO [ops] {action} complete")
            if incident and not incident.get("fixed_at"):  # the action did not address the cause: errors go on
                lines += [f"{stamp(3 + i)} {line}" for i, line in enumerate(incident["still_failing"])]
        with log.open("a") as handle:
            handle.write("\n".join(lines) + "\n")
        self.state_file.write_text(json.dumps(state, indent=2))
        return "executed", f"Demo environment: {describe(action, service, params)}. Done."


class WebhookRunner:
    name = "Webhook"
    external = True

    def __init__(self, url: str, secret: str, client: httpx.Client | None = None) -> None:
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1") and not url.startswith("http://localhost"):
            raise AutomationError("The webhook address must use https:// (http only for 127.0.0.1 or localhost).")
        if len(secret) < 16:
            raise AutomationError("The webhook secret must have 16 or more characters.")
        self.url, self.secret = url, secret
        self.client = client or httpx.Client(timeout=30)

    def sign(self, body: bytes, at: int | None = None) -> str:
        at = at or int(time.time())
        digest = hmac.new(self.secret.encode(), f"{at}.".encode() + body, hashlib.sha256).hexdigest()
        return f"t={at},v1={digest}"

    def run(self, action: str, service: str, params: dict, key: str, extra: dict | None = None) -> tuple[str, str]:
        body = json.dumps({"event": "remediation", "action": action, "service": service, "params": params,
                           "idempotency_key": key, **(extra or {})}, sort_keys=True).encode()
        try:
            response = self.client.post(self.url, content=body, headers={
                "Content-Type": "application/json", "X-RootIQ-Signature": self.sign(body), "Idempotency-Key": key})
        except httpx.HTTPError as exc:
            return "failed", f"The webhook did not answer ({type(exc).__name__})."
        if response.status_code >= 300:
            return "failed", f"The webhook answered HTTP {response.status_code}."
        try:
            detail = str(response.json().get("detail", "")) or "Accepted."
        except ValueError:
            detail = "Accepted."
        return "executed", f"Webhook: {detail[:300]}"


class GitHubActionsRunner:
    name = "GitHub Actions"
    external = True

    def __init__(self, repo: str, workflow: str, ref: str, token: str, client: httpx.Client | None = None) -> None:
        if not re.match(r"^[\w.-]+/[\w.-]+$", repo or "") or not workflow or not token:
            raise AutomationError("GitHub Actions needs the repository (owner/repo), the workflow file and a token.")
        self.repo, self.workflow, self.ref = repo, workflow, ref or "main"
        self.client = client or httpx.Client(timeout=30, headers={"Authorization": f"Bearer {token}",
                                                                   "Accept": "application/vnd.github+json"})

    def run(self, action: str, service: str, params: dict, key: str, extra: dict | None = None) -> tuple[str, str]:
        url = f"https://api.github.com/repos/{self.repo}/actions/workflows/{self.workflow}/dispatches"
        inputs = {"action": action, "service": service, "params": json.dumps(params), "idempotency_key": key}
        try:
            response = self.client.post(url, json={"ref": self.ref, "inputs": inputs})
        except httpx.HTTPError as exc:
            return "failed", f"GitHub did not answer ({type(exc).__name__})."
        if response.status_code != 204:
            return "failed", f"GitHub answered HTTP {response.status_code}: {response.text[:200]}"
        return "executed", f"GitHub workflow {self.workflow} started on {self.repo}@{self.ref}."


def make_runner(settings, policy: dict):
    runner = policy["runner"]
    config = runner.get("config") or {}
    if runner["type"] == "webhook":
        return WebhookRunner(config.get("url", ""), config.get("secret", ""))
    if runner["type"] == "github_actions":
        return GitHubActionsRunner(config.get("repo", ""), config.get("workflow", ""), config.get("ref", "main"), config.get("token", ""))
    return DemoRunner(settings)
