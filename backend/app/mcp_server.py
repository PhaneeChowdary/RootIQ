"""MCP server for RootIQ: the incident tools, for any MCP client such as Claude Desktop, an IDE or another agent.

The read tools read the case store, the incident memory, the service logs and Jira directly. The investigation tools
go through the RootIQ API, so a run that starts here joins the same queue and UI as each other run. There is no
write tool: customer emails and Jira comments are still proposals that an engineer approves in RootIQ.

    python -m backend.app.mcp_server        # stdio transport; the MCP client starts it
"""
import functools
import logging
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .agent.tools import InvestigationToolbox
from .config import Settings
from .sources import active_sources
from .database import CaseDatabase
from .services.jira import JiraEscalationPublisher
from .services.memory import LocalEmbedder

INSTRUCTIONS = """RootIQ is a production-support system. Use these tools to look into customer incidents:
list open cases, search resolved incidents, read service logs, and start or check an agent-team investigation.
Investigations propose customer updates and Jira comments; a support engineer approves them in RootIQ."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
QUEUES_WORK = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


class RootIQApi:
    """Signed-in client for the RootIQ REST API."""

    def __init__(self, http: httpx.Client, username: str, password: str) -> None:
        self.http = http
        self.username = username
        self.password = password
        self.token: str | None = None

    def request(self, method: str, path: str) -> dict | list:
        for attempt in range(2):
            if self.token is None:
                self._login()
            response = self._send(method, path, headers={"Authorization": f"Bearer {self.token}"})
            if response.status_code == 401 and attempt == 0:
                self.token = None  # session expired: sign in again once
                continue
            if response.status_code >= 400:
                detail = response.json().get("detail", response.text) if response.content else response.reason_phrase
                raise RuntimeError(f"RootIQ refused the request ({response.status_code}): {detail}")
            return response.json()
        raise RuntimeError("RootIQ rejected the configured credentials.")

    def _login(self) -> None:
        response = self._send("POST", "/api/auth/login", json={"username": self.username, "password": self.password})
        if response.status_code != 200:
            raise RuntimeError("RootIQ rejected the configured credentials (ROOTIQ_AUTH_USERNAME/PASSWORD).")
        self.token = response.json()["access_token"]

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return self.http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise RuntimeError(f"The RootIQ API is not reachable at {self.http.base_url}. Is the backend running?") from exc


def explained(fn):
    """Give the error message of a tool to the client. Else MCP reports a general failure."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(str(exc)) from exc
    return wrapper


def summarize_run(run: dict) -> dict[str, Any]:
    analysis = run.get("analysis") or {}
    calls = run.get("llm_calls", [])
    return {
        "run_id": run["id"],
        "status": run["status"],
        "stage": run["stage"],
        "queue_position": run.get("queue_position"),
        "trigger": run["trigger"],
        "hypotheses": (run.get("plan") or {}).get("hypotheses", []),
        "reviews": [
            "approved" if r["approved"] else f"sent back to {', '.join(r['recheck'])}: {r['instructions']}"
            for r in run.get("reviews", [])
        ],
        "likely_cause": analysis.get("likely_cause", ""),
        "suggested_severity": analysis.get("suggested_severity", ""),
        "workaround": analysis.get("workaround", ""),
        "proposals": [p["summary"] for p in run.get("proposals", [])],
        "outcomes": [f"{o['status']}: {o['detail']}" for o in run.get("outcomes", [])],
        "tool_calls": len(run.get("steps", [])),
        "model_calls": len(calls),
        "tokens": {"input": sum(c["input_tokens"] for c in calls), "output": sum(c["output_tokens"] for c in calls)},
        "error": run.get("error", ""),
        "next_step": (
            "An engineer must approve, edit or reject the proposals in RootIQ (Agent team tab)."
            if run["status"] == "awaiting_approval" else ""
        ),
    }


def create_server(settings: Settings | None = None, http: httpx.Client | None = None,
                  database=None, embedder=None, jira=None) -> MCPServer:
    """The optional arguments replace the real clients in tests. By default, everything comes from the settings."""
    settings = settings or Settings()
    database = database or CaseDatabase(settings.database_target)
    embedder = embedder or LocalEmbedder(settings.ollama_url, settings.embedding_model)
    jira = jira or JiraEscalationPublisher(settings.jira_site_url, settings.jira_email, settings.jira_api_token,
                                           settings.jira_project_key, settings.jira_issue_type)
    api = RootIQApi(http or httpx.Client(base_url=settings.api_url, timeout=30),
                       settings.auth_username, settings.auth_password)
    server = MCPServer("rootiq", instructions=INSTRUCTIONS, version="1.0.0")

    def toolbox() -> InvestigationToolbox:
        sources = active_sources(settings)
        return InvestigationToolbox(None, database, embedder, jira, sources.logs, metrics=sources.metrics, changes=sources.changes)

    def resolve_case_id(case_id: str) -> str:
        prefix = case_id.strip().lower()
        matches = [c.id for c in database.list_cases() if c.id.lower().startswith(prefix)] if prefix else []
        if len(matches) != 1:
            raise RuntimeError(f"No single case matches '{case_id}'. Use a case_id from list_open_cases.")
        return matches[0]

    @server.tool(annotations=READ_ONLY)
    @explained
    def list_open_cases() -> list[dict[str, Any]]:
        """Open support cases that are not resolved, most recently updated first, with severity, status and SLA state."""
        return [
            {"case_id": c.id[:8], "title": c.title, "customer": c.customer, "service": c.service,
             "severity": c.severity, "status": c.status, "sla_state": c.sla_state}
            for c in database.list_cases() if c.status != "resolved"
        ]

    @server.tool(annotations=READ_ONLY)
    @explained
    def get_incident(incident_id: str) -> dict[str, Any]:
        """Full details of one case or past incident: reported issue, cause, workaround, resolution, preventive actions."""
        return toolbox().get_incident(incident_id)

    @server.tool(annotations=READ_ONLY)
    @explained
    def search_similar_incidents(query: str) -> list[dict[str, Any]]:
        """Semantic search over resolved past incidents; returns the closest matches with cause and resolution."""
        return toolbox().search_similar_incidents(query)

    @server.tool(annotations=READ_ONLY)
    @explained
    def list_service_incidents(service: str) -> list[dict[str, Any]]:
        """Incidents recorded for one service, newest first. Useful for spotting recurring problems."""
        return toolbox().list_service_incidents(service)

    @server.tool(annotations=READ_ONLY)
    @explained
    def read_service_logs(service: str, contains: str = "", level: str = "") -> dict[str, Any]:
        """Recent server log lines for a service, optionally filtered by text and level. ERROR, WARN, INFO.
        Filtered results include surrounding lines for context; matching lines start with '>>'."""
        return toolbox().read_service_logs(service, contains=contains, level=level)

    if jira.configured:
        @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
        @explained
        def search_jira(text: str) -> list[dict[str, Any]]:
            """Search Jira tickets in the configured project by free text. Read-only."""
            return toolbox().search_jira(text)

    @server.tool(annotations=QUEUES_WORK)
    @explained
    def start_investigation(case_id: str) -> dict[str, Any]:
        """Queue an investigation of a case by the agent team.
        Returns at once; poll get_investigation with the run_id. Nothing goes to customers or Jira without the approval of an
        engineer in RootIQ."""
        return summarize_run(api.request("POST", f"/api/cases/{resolve_case_id(case_id)}/agent-runs"))

    @server.tool(annotations=READ_ONLY)
    @explained
    def get_investigation(run_id: str) -> dict[str, Any]:
        """Status and results of an agent-team investigation: stage, hypotheses, reviewer verdicts, likely cause,
        proposed actions waiting for approval, and model/tool usage."""
        return summarize_run(api.request("GET", f"/api/agent-runs/{run_id.strip()}"))

    @server.tool(annotations=READ_ONLY)
    @explained
    def latest_investigation(case_id: str) -> dict[str, Any]:
        """The most recent agent-team investigation of a case, if any."""
        runs = api.request("GET", f"/api/cases/{resolve_case_id(case_id)}/agent-runs")
        if not runs:
            raise RuntimeError("This case has not been investigated yet. Use start_investigation.")
        return summarize_run(runs[0])

    @server.tool(annotations=READ_ONLY)
    @explained
    def agent_team_metrics() -> dict[str, Any]:
        """How the agent team performs across runs: timing, tokens, reviewer rejection rate, engineer decisions,
        and how often its diagnosis matched the confirmed fix."""
        return api.request("GET", "/api/agent-metrics")

    return server


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keep the client's server log readable
    create_server().run("stdio")


if __name__ == "__main__":
    main()
