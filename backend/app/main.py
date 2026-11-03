import asyncio
import hmac
import os
import shutil
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from .agent.llm import active_model, make_chat, model_label, model_store
from .agent.llm import set_time_zone as set_day_zone
from .agent.llm import use_database as use_shared_usage
from .agent.runner import AgentRunner, RunConflict, RunNotFound
from .agent.store import AgentRunStore
from .config import Settings, zone
from .database import CaseDatabase
from .model_routes import build_router as build_model_router
from .source_routes import build_router as build_source_router
from .automation_routes import build_router as build_automation_router
from .sources import SourceError, active_sources
from .monitoring import build_registry
from .models import (
    AgentLesson,
    AgentMetrics,
    AgentRun,
    AnalysisReview,
    AcknowledgeUpdate,
    DecisionRequest,
    AppState,
    AuditEvent,
    AuthUser,
    CaseCreate,
    CaseMessage,
    HealthResponse,
    JiraSyncResult,
    LoginRequest,
    MemoryStatus,
    MessageCreate,
    OperationsDashboard,
    PasswordChange,
    PreferenceUpdate,
    ProblemGroup,
    RelatedCase,
    IncidentLinkDecision,
    ResolutionUpdate,
    RunbookRecommendation,
    ServiceTrend,
    SupportCase,
    UserCreate,
    UserUpdate,
)
from .services.alerts import AlertLinks, case_for, parse_grafana
from .correlation import IncidentGroups, duplicate_of
from . import backup, db, migrations, retention
from .privacy import masked_counts
from .permissions import allowed, needed_role
from .services.auth import LocalAuthenticator, LoginLocked, UserError
from .services.oidc import OidcClient, OidcConfig, SsoError
from .services.analysis import AnalysisUnavailable, OllamaSupportAnalyst
from .services.gmail import GmailDraftError, GmailDraftPublisher
from .services.jira import JiraEscalationPublisher, JiraPublishError
from .services.memory import EmbeddingUnavailable, LocalEmbedder, cosine_similarity, rank_memory, text_similarity


settings = Settings()
database = CaseDatabase(settings.database_target)
analyst = OllamaSupportAnalyst(
    settings.ollama_url, settings.ollama_model,
    chat_factory=lambda: _analysis_chat(),
)
embedder = LocalEmbedder(settings.ollama_url, settings.embedding_model)
jira_publisher = JiraEscalationPublisher(
    settings.jira_site_url,
    settings.jira_email,
    settings.jira_api_token,
    settings.jira_project_key,
    settings.jira_issue_type,
)
gmail_publisher = GmailDraftPublisher(
    settings.gmail_client_id, settings.gmail_client_secret, settings.gmail_refresh_token
)
settings.data_dir.mkdir(parents=True, exist_ok=True)
authenticator = LocalAuthenticator(
    settings.auth_username, settings.auth_password, settings.auth_session_hours, settings.database_target,
    settings.login_max_failures, settings.login_lock_minutes,
)
oidc = OidcClient(OidcConfig(
    settings.oidc_issuer, settings.oidc_client_id, settings.oidc_client_secret, settings.oidc_redirect_url,
    settings.oidc_name, {d.strip().lower() for d in settings.oidc_allowed_domains.split(",") if d.strip()},
    settings.oidc_default_role,
))
agent_runs = AgentRunStore(settings.database_target)
agent_runner = AgentRunner(settings, database, embedder, jira_publisher, gmail_publisher, agent_runs)


def _count_todays_tokens() -> None:
    from .agent import llm as models

    local = zone(settings.time_zone)
    today = datetime.now(local).date()
    counts: dict[str, int] = {}
    for run in agent_runs.list_all(limit=500):
        for call in run.llm_calls:
            if call.started_at.astimezone(local).date() == today and call.ok:
                counts[call.model] = counts.get(call.model, 0) + call.input_tokens + call.output_tokens
    models.fill_today(counts)


def _analysis_chat():
    """The quick analysis uses the standard chain. None: local only, so the analyst calls Ollama itself."""
    chat = make_chat(settings, json_mode=True)
    return chat.for_agent("analysis") if hasattr(chat, "for_agent") else None


class _WSManager:
    def __init__(self) -> None:
        self._connections: dict[str, list[WebSocket]] = {}

    async def connect(self, case_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.setdefault(case_id, []).append(websocket)

    def disconnect(self, case_id: str, websocket: WebSocket) -> None:
        conns = self._connections.get(case_id, [])
        self._connections[case_id] = [ws for ws in conns if ws is not websocket]

    async def broadcast(self, case_id: str, data: dict) -> None:
        for ws in list(self._connections.get(case_id, [])):
            try:
                await ws.send_json(data)
            except Exception:
                self.disconnect(case_id, ws)


ws_manager = _WSManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    agent_runs.initialize()
    use_shared_usage(database.path)  # token use and model pauses in the database: kept after a restart
    set_day_zone(settings.time_zone)  # "today" of the budgets ends at the user's midnight
    agent_runner.recover(assume_alone=not settings.agent_external_workers)
    with suppress(Exception):  # the daily token budgets count from the start of the day, not of this process
        _count_todays_tokens()
    agent_runner.start_workers()
    watcher = asyncio.create_task(_watch_sla()) if settings.agent_autorun and settings.agent_sla_watch_seconds > 0 else None
    cleaner = asyncio.create_task(_retention_loop())
    backups_task = asyncio.create_task(_backup_loop()) if settings.backup_hours > 0 else None
    with suppress(Exception):  # older plain-text model keys: save them again, encrypted
        store = model_store(settings)
        if store and not store.encrypted():
            store.update(lambda data: None)
    yield
    for task in (watcher, cleaner, backups_task):
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    agent_runner.stop_workers()
    use_shared_usage(None)


async def _backup_loop() -> None:
    """Makes a backup when the newest one is older than ROOTIQ_BACKUP_HOURS. See backup.py."""
    while True:
        with suppress(Exception):  # a failed backup is tried again at the next check
            newest = backup.backups(settings)
            age = (datetime.now(UTC).timestamp() - newest[0].stat().st_mtime) / 3600 if newest else None
            if age is None or age >= settings.backup_hours:
                await asyncio.to_thread(backup.make, settings)
        await asyncio.sleep(1800)


async def _retention_loop() -> None:
    """Applies the retention rules at start and every 6 hours. See retention.py."""
    while True:
        with suppress(Exception):  # a failed cleanup is tried again next time
            await asyncio.to_thread(retention.run, database, agent_runner, settings)
        await asyncio.sleep(retention.INTERVAL_SECONDS)


async def _watch_sla() -> None:
    """Starts agent runs for open cases whose SLA is at risk before anyone has reviewed an analysis."""
    while True:
        await asyncio.sleep(settings.agent_sla_watch_seconds)
        with suppress(Exception):  # the next check tries a failed scan again
            await asyncio.to_thread(agent_runner.check_sla)


app = FastAPI(title="RootIQ API", version="0.1.0", lifespan=lifespan)
ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in settings.allowed_origins.split(",") if o.strip()]
MAX_BODY_BYTES = 2_000_000  # the largest request body: a case with long logs fits
WS_RECHECK_SECONDS = 60  # an open live channel checks its session again after this time
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(build_model_router(lambda: settings, lambda: database))
app.include_router(build_source_router(lambda: settings, lambda: database))
app.include_router(build_automation_router(lambda: settings, lambda: database))
metrics_registry, alerts_received = build_registry(lambda: settings, lambda: database, lambda: agent_runner)


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """Prometheus reads this page. It has counts only: no case text, no keys."""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    return Response(generate_latest(metrics_registry), media_type=CONTENT_TYPE_LATEST)


@app.post("/api/alerts/grafana")
def grafana_alerts(payload: dict, request: Request, background_tasks: BackgroundTasks) -> dict:
    """The webhook of Grafana. A firing alert opens a case and starts the agent team. See services/alerts.py."""
    if not settings.alert_token:
        raise HTTPException(status_code=503, detail="Alert intake is off. Set ROOTIQ_ALERT_TOKEN in backend/.env.")
    if not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {settings.alert_token}"):
        raise HTTPException(status_code=401, detail="Wrong alert token.")
    links = AlertLinks(settings.database_target)
    links.initialize()
    created: list[str] = []
    noted: list[str] = []
    ignored = 0

    def note(case: SupportCase, text: str) -> None:
        message = database.create_message(case.id, "Grafana", MessageCreate(content=text, message_type="internal"))
        background_tasks.add_task(ws_manager.broadcast, case.id, message.model_dump(mode="json"))
        noted.append(case.id)

    for alert in parse_grafana(payload):
        linked_id = links.case_for_fingerprint(alert.fingerprint)
        linked = database.get_case(linked_id) if linked_id else None
        if alert.status == "resolved":
            if linked and linked.status != "resolved":
                note(linked, f"Grafana says the alert {alert.name} stopped at {_clock_time(alert.ends_at)}. "
                             "Check that the fix works, then resolve the case.")
                links.link(alert, linked.id)
                alerts_received.labels("resolved", "noted").inc()
            else:
                ignored += 1
                alerts_received.labels("resolved", "ignored").inc()
            continue
        if linked and linked.status != "resolved":  # Grafana sends a firing alert again: the case is already open
            ignored += 1
            alerts_received.labels("firing", "repeat").inc()
            continue
        same_rule = next((c for cid in links.cases_for_rule(alert.name, alert.service)
                          if (c := database.get_case(cid)) and c.status != "resolved"), None)
        if same_rule:
            links.link(alert, same_rule.id)
            note(same_rule, f"Another alert for the same rule fired: {alert.summary}.")
            alerts_received.labels("firing", "joined").inc()
            continue
        case = _open_case(case_for(alert, recurred_from=linked.id[:8] if linked else ""), "grafana", "alert", background_tasks)
        links.link(alert, case.id)
        created.append(case.id)
        alerts_received.labels("firing", "case_created").inc()
    return {"created": created, "noted": noted, "ignored": ignored}


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


@app.middleware("http")
async def require_authenticated_session(request: Request, call_next):
    """Request size check, origin check against cross-site requests, sign-in check, role check, then security headers."""
    origin = request.headers.get("origin", "").rstrip("/")
    try:
        size = int(request.headers.get("content-length") or 0)
    except ValueError:
        size = MAX_BODY_BYTES + 1
    if size > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"detail": "The request is too large."})
    if origin and request.method not in ("GET", "HEAD", "OPTIONS") and origin not in ALLOWED_ORIGINS:
        return JSONResponse(status_code=403, content={"detail": "Request from an unknown website. Add it to ROOTIQ_ALLOWED_ORIGINS."})
    response = await _check_session(request, call_next)
    if request.url.path.startswith("/api/"):
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
    return response


async def _check_session(request: Request, call_next):
    public_paths = {"/api/auth/login", "/api/auth/config", "/api/auth/oidc/start", "/api/auth/oidc/callback",
                    "/api/alerts/grafana"}  # the alert intake checks its own token
    if (
        request.method != "OPTIONS"
        and request.url.path.startswith("/api/")
        and request.url.path not in public_paths
    ):
        authorization = request.headers.get("Authorization")
        if not authorization:
            cookie_token = request.cookies.get("rootiq_session")
            if cookie_token:
                authorization = f"Bearer {cookie_token}"
        user = authenticator.authenticate(authorization)
        if user is None:
            return JSONResponse(status_code=401, content={"detail": "Authentication required."})
        if not allowed(user.role, request.method, request.url.path):
            needed = needed_role(request.method, request.url.path)
            return JSONResponse(status_code=403, content={"detail": f"Your role ({user.role}) cannot do this. It needs the {needed} role."})
        request.state.user = user
    return await call_next(request)


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ready",
        ollama_installed=shutil.which("ollama") is not None,
        ollama_model=settings.ollama_model,
        embedding_model=settings.embedding_model,
        jira_configured=jira_publisher.configured,
        jira_site_url=settings.jira_site_url.rstrip("/") if jira_publisher.configured else "",
        gmail_configured=gmail_publisher.configured,
        stored_cases=database.count_cases(),
        agent_model=model_label(settings),
        agent_dry_run=settings.agent_dry_run,
        agent_autorun=settings.agent_autorun,
        grafana_url=settings.grafana_url.rstrip("/"),
        time_zone=settings.time_zone,
    )


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request):
    try:
        session = authenticator.login(payload.username, payload.password, request.client.host if request.client else "")
    except LoginLocked as exc:
        database.record_audit(payload.username[:120], "auth.locked", "session", payload.username[:120], str(exc))
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": str(exc.minutes * 60)}) from exc
    if session is None:
        raise HTTPException(status_code=401, detail="Bad credentials.")
    database.record_audit(session.user.username, "auth.login", "session", session.user.username)
    return _with_session_cookie(JSONResponse(content=session.model_dump(mode="json")), session.access_token)


def _with_session_cookie(response: Response, token: str) -> Response:
    response.set_cookie(
        key="rootiq_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.auth_session_hours * 3600,
        path="/api",
    )
    return response


@app.get("/api/auth/config")
def auth_config() -> dict:
    """What the sign-in page shows: the single sign-on button, if it is on."""
    return {"sso": oidc.config.enabled, "sso_name": settings.oidc_name}


@app.get("/api/auth/oidc/start")
def oidc_start() -> Response:
    if not oidc.config.enabled:
        raise HTTPException(status_code=404, detail="Single sign-on is off.")
    try:
        return RedirectResponse(oidc.start(), status_code=302)
    except SsoError as exc:
        return RedirectResponse(f"/?sso_error={quote(str(exc))}", status_code=302)


@app.get("/api/auth/oidc/callback")
def oidc_callback(state: str = "", code: str = "", error: str = "") -> Response:
    if error or not state or not code:
        return RedirectResponse(f"/?sso_error={quote('The identity provider did not sign you in.')}", status_code=302)
    try:
        email, name = oidc.finish(state, code)
        session = authenticator.login_sso(email, name, oidc.config.allowed_domains, oidc.config.default_role)
    except (SsoError, UserError) as exc:
        database.record_audit("sso", "auth.sso_refused", "session", "", str(exc))
        return RedirectResponse(f"/?sso_error={quote(str(exc))}", status_code=302)
    database.record_audit(session.user.username, "auth.login", "session", session.user.username, "Single sign-on.")
    return _with_session_cookie(RedirectResponse("/", status_code=302), session.access_token)


@app.put("/api/auth/password", status_code=204)
def change_password(payload: PasswordChange, request: Request) -> Response:
    try:
        authenticator.change_own_password(request.state.user.username, payload.current, payload.new)
    except LoginLocked as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": str(exc.minutes * 60)}) from exc
    except UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    database.record_audit(request.state.user.username, "auth.password_changed", "user", request.state.user.username)
    return Response(status_code=204)


@app.get("/api/security")
def security_overview() -> dict:
    """Settings > Security: what protects the data, and what the next cleanup deletes."""
    todo = retention.due(database, settings)
    provider, model = active_model(settings)
    store = model_store(settings)
    return {
        "masking": {"mode": settings.privacy_masking, "cloud_model": provider != "ollama",
                    "model": model if provider != "ollama" else settings.agent_model, "masked": masked_counts()},
        "encryption": {"key_from_env": bool(os.environ.get("ROOTIQ_SECRET_KEY")),
                       "key_file": str(Path(settings.data_dir) / "secret.key").replace(str(Path.cwd()) + "/", ""),
                       "model_keys_encrypted": store.encrypted() if store else True},
        "sign_in": {"max_failures": settings.login_max_failures, "lock_minutes": settings.login_lock_minutes,
                    "session_hours": settings.auth_session_hours, "sso": oidc.config.enabled,
                    "locked": authenticator.locked_accounts(), "cookie_secure": settings.cookie_secure},
        "retention": {"bin_days": settings.retention_bin_days, "resolved_days": settings.retention_resolved_days,
                      "audit_days": settings.retention_audit_days,
                      "due": {"bin": len(todo["bin"]), "resolved": len(todo["resolved"]), "audit": todo["audit"]}},
        "origins": ALLOWED_ORIGINS,
        "backups": _backup_overview(),
        "system": _system_overview(),
    }


def _backup_overview() -> dict:
    files = backup.backups(settings)
    return {"count": len(files), "every_hours": settings.backup_hours, "keep": settings.backup_keep,
            "latest": datetime.fromtimestamp(files[0].stat().st_mtime, UTC).isoformat() if files else None,
            "latest_name": files[0].name if files else "", "latest_kb": files[0].stat().st_size // 1024 if files else 0,
            "folder": str(backup.backup_dir(settings)).replace(str(Path.cwd()) + "/", ""),
            "key_from_env": bool(os.environ.get("ROOTIQ_SECRET_KEY"))}


def _system_overview() -> dict:
    seen_after = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    target = settings.database_target
    return {"database": "PostgreSQL" if db.is_postgres(target) else "SQLite",
            "database_where": str(target).split("@")[-1] if db.is_postgres(target) else str(target).replace(str(Path.cwd()) + "/", ""),
            "schema_version": max(migrations.applied(db.get(target)), default=0), "schema_latest": migrations.LATEST,
            "workers": agent_runs.workers(seen_after), "queued_jobs": sum(1 for r in agent_runs.with_status("queued")),
            "api_threads": settings.agent_concurrency, "external_workers": settings.agent_external_workers}


@app.get("/healthz", include_in_schema=False)
def liveness() -> dict:
    """The process answers, for Docker and load balancers. No sign-in."""
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readiness() -> JSONResponse:
    """The process can reach its database. No sign-in."""
    try:
        with database._connect() as connection:
            connection.execute("SELECT 1").fetchone()
    except Exception:
        return JSONResponse(status_code=503, content={"status": "database unavailable"})
    return JSONResponse(content={"status": "ready"})


@app.post("/api/security/backup")
def security_backup(request: Request) -> dict:
    try:
        path = backup.make(settings)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"The backup failed: {exc}") from exc
    database.record_audit(request.state.user.username, "backup.created", "data", path.name)
    return security_overview()


@app.post("/api/security/cleanup")
def security_cleanup(request: Request) -> dict:
    counts = retention.run(database, agent_runner, settings, actor=request.state.user.username)
    return {"deleted": counts, **security_overview()}


@app.get("/api/users")
def list_users() -> list[dict]:
    return authenticator.list_users()


@app.post("/api/users", status_code=201)
def create_user(payload: UserCreate, request: Request) -> list[dict]:
    try:
        authenticator.create_user(payload.username, payload.display_name, payload.email, payload.role,
                                  payload.password or None, auth="password" if payload.password else "sso")
    except UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    database.record_audit(request.state.user.username, "user.created", "user", payload.username, f"Role: {payload.role}.")
    return authenticator.list_users()


@app.post("/api/users/{username}/unlock")
def unlock_user(username: str, request: Request) -> list[dict]:
    authenticator.unlock(username)
    database.record_audit(request.state.user.username, "user.unlocked", "user", username)
    return authenticator.list_users()


@app.patch("/api/users/{username}")
def update_user(username: str, payload: UserUpdate, request: Request) -> list[dict]:
    if username == request.state.user.username and (payload.disabled or payload.role not in (None, "admin")):
        raise HTTPException(status_code=400, detail="You cannot disable your own account or remove your own admin role.")
    try:
        authenticator.update_user(username, role=payload.role, disabled=payload.disabled, password=payload.password or None,
                                  display_name=payload.display_name)
    except UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    changed = ", ".join(k for k, v in payload.model_dump().items() if v not in (None, ""))
    database.record_audit(request.state.user.username, "user.updated", "user", username,
                          f"Changed: {changed}." + (f" Role: {payload.role}." if payload.role else ""))
    return authenticator.list_users()


@app.delete("/api/users/{username}")
def remove_user(username: str, request: Request) -> list[dict]:
    if username == request.state.user.username:
        raise HTTPException(status_code=400, detail="You cannot remove your own account.")
    try:
        authenticator.remove_user(username)
    except UserError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    database.record_audit(request.state.user.username, "user.removed", "user", username)
    return authenticator.list_users()


@app.get("/api/auth/me", response_model=AuthUser)
def current_user(request: Request) -> AuthUser:
    return request.state.user


@app.post("/api/auth/logout")
def logout(request: Request, authorization: str | None = Header(default=None)):
    user = request.state.user
    auth = authorization
    if not auth:
        cookie_token = request.cookies.get("rootiq_session")
        if cookie_token:
            auth = f"Bearer {cookie_token}"
    authenticator.logout(auth)
    database.record_audit(user.username, "auth.logout", "session", user.username)
    response = Response(status_code=204)
    response.delete_cookie("rootiq_session", path="/api")
    return response


@app.get("/api/audit", response_model=list[AuditEvent])
def audit_history(limit: int = Query(default=100, ge=1, le=250)) -> list[AuditEvent]:
    return database.list_audit_events(limit)


@app.get("/api/preferences")
def get_preferences(request: Request) -> dict[str, str]:
    return database.get_preferences(request.state.user.username)


@app.put("/api/preferences")
def update_preference(payload: PreferenceUpdate, request: Request) -> dict[str, str]:
    database.set_preference(request.state.user.username, payload.key, payload.value)
    return database.get_preferences(request.state.user.username)


@app.get("/api/state", response_model=AppState)
def app_state(request: Request) -> AppState:
    return AppState(
        health=health(),
        cases=database.list_cases(),
        dashboard=database.dashboard(),
        trends=database.service_trends(),
        problems=database.problem_groups(),
        audit=database.list_audit_events(),
        preferences=database.get_preferences(request.state.user.username),
    )


@app.get("/api/cases", response_model=list[SupportCase])
def list_cases() -> list[SupportCase]:
    return database.list_cases()


@app.get("/api/dashboard", response_model=OperationsDashboard)
def dashboard() -> OperationsDashboard:
    return database.dashboard()


@app.get("/api/dashboard/trend")
def dashboard_trend(hours: int = Query(24, ge=1, le=168)) -> dict[str, list[int]]:
    """The dashboard counters over the last hours, for the small line charts in the counter tiles."""
    return database.dashboard_trend(hours=hours)


@app.get("/api/live")
def live() -> dict:
    """A small, frequent check for the pages: which cases have agents working, new alert cases, backup use."""
    active = agent_runner.store.with_status("queued", "running", "executing", "awaiting_approval")
    runs = agent_runner.store.list_all()
    cases = database.list_cases()
    alert_cases = sorted((c for c in cases if c.customer == "Grafana alert"), key=lambda c: c.created_at, reverse=True)[:5]
    return {
        "active_runs": {r.case_id: {"status": r.status, "stage": r.stage} for r in active},
        "case_ids": [c.id for c in cases],
        "alert_cases": [{"id": c.id, "title": c.title, "service": c.service, "created_at": c.created_at.isoformat()}
                        for c in alert_cases],
        "backup_calls": sum(1 for r in runs for call in r.llm_calls if not call.ok),
    }


@app.get("/api/trends/services", response_model=list[ServiceTrend])
def service_trends() -> list[ServiceTrend]:
    return database.service_trends()


@app.get("/api/problems", response_model=list[ProblemGroup])
def problem_groups() -> list[ProblemGroup]:
    return database.problem_groups()


@app.get("/api/cases/search", response_model=list[SupportCase])
def search_cases(q: str = Query(default=""), status: str = Query(default="")) -> list[SupportCase]:
    return database.search_cases(q, status)


@app.get("/api/cases/{case_id}", response_model=SupportCase)
def get_case(case_id: str) -> SupportCase:
    return _find_case(case_id)


@app.delete("/api/cases/{case_id}", status_code=204)
def delete_case(case_id: str, request: Request, background_tasks: BackgroundTasks) -> None:
    """Move a case to the recycle bin. Its agent work stops, and its Jira tickets move to Done. Restore it or delete
    it forever from the bin."""
    case = _find_case(case_id)
    agent_runner.cancel_for_case(case_id, "Cancelled: the case was moved to the recycle bin.")
    database.move_to_bin(case_id, request.state.user.username)
    _audit(request, "case.moved_to_bin", case, f"Moved to the recycle bin: {case.title}.")
    background_tasks.add_task(_sync_jira_status, case, "done", request.state.user.username)


@app.get("/api/bin")
def recycle_bin() -> list[dict]:
    """The cases in the recycle bin."""
    return [{"id": c.id, "title": c.title, "service": c.service, "severity": c.severity, "status": c.status,
             "deleted_at": deleted_at, "deleted_by": deleted_by} for c, deleted_at, deleted_by in database.list_bin()]


@app.post("/api/bin/{case_id}/restore", response_model=SupportCase)
def restore_from_bin(case_id: str, request: Request, background_tasks: BackgroundTasks) -> SupportCase:
    try:
        case = database.restore_case(case_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Case not found in the recycle bin.") from exc
    _audit(request, "case.restored", case, f"Restored from the recycle bin: {case.title}.")
    if case.status != "resolved":  # a resolved case keeps its Jira tickets at Done
        background_tasks.add_task(_sync_jira_status, case, "new", request.state.user.username)
    return case


CLOSED_IN_JIRA = "was closed in Jira"


def _note_closed_in_jira(background_tasks: BackgroundTasks) -> None:
    """An open case whose Jira ticket a person closed in Jira gets one note. The app does not resolve it: the
    resolution and the lesson need a person here."""
    by_key = {key: case for case in database.list_cases() if case.status != "resolved"
              for key in {case.jira_source_key, case.jira_issue_key} - {None, ""}}
    if not by_key:
        return
    try:
        closed = jira_publisher.search_issues(f"key in ({', '.join(sorted(by_key))}) AND statusCategory = Done", 100)
    except JiraPublishError:
        return
    for ticket in closed:
        case = by_key.get(ticket["key"])
        if case and not any(CLOSED_IN_JIRA in m.content for m in database.list_messages(case.id)):
            _note(case, f"{ticket['key']} {CLOSED_IN_JIRA}. Resolve this case if the work is done, or move it to the "
                        "recycle bin.", background_tasks)


def _sync_jira_status(case: SupportCase, category: str, actor: str) -> None:
    """Keep the Jira tickets of a case in step with the bin: Done when the case goes to the bin, To Do when it comes
    back. In the background: a Jira error never blocks the bin; the audit log records it."""
    if not jira_publisher.configured:
        return
    moved = "moved to the recycle bin" if category == "done" else "restored from the recycle bin"
    for key in sorted({case.jira_source_key, case.jira_issue_key} - {None, ""}):
        try:
            jira_publisher.add_comment(key, "RootIQ", f"The case was {moved} by {actor}.")
            if category == "done":
                jira_publisher.transition_to_done(key)
            else:
                jira_publisher.transition_to_open(key)
            database.record_audit(actor, "jira.status_synced", "case", case.id, f"{key}: {'Done' if category == 'done' else 'To Do'}.")
        except JiraPublishError as exc:
            database.record_audit(actor, "jira.status_failed", "case", case.id, f"{key}: {exc}")


@app.delete("/api/bin/{case_id}", status_code=204)
def delete_forever(case_id: str, request: Request) -> None:
    """Delete a case from the recycle bin forever: the case, its messages and its knowledge-base entry."""
    case = database.get_case(case_id, include_deleted=True)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found in the recycle bin.")
    _delete_forever(case_id)
    database.record_audit(request.state.user.username, "case.deleted", "case", case_id, f"Deleted forever: {case.title}.")


def _delete_forever(case_id: str) -> None:
    """The case, its messages, its knowledge-base entry, its agent runs, checkpoints, lesson and alert links."""
    agent_runner.forget_case(case_id)
    IncidentGroups(database.path).forget_case(case_id)
    database.delete_case(case_id)


@app.post("/api/cases", response_model=SupportCase)
def create_case(payload: CaseCreate, request: Request, background_tasks: BackgroundTasks) -> SupportCase:
    return _open_case(payload, request.state.user.username, "case_created", background_tasks)


def _clock_time(value: str) -> str:
    """A Grafana time such as 2026-10-03T16:30:40-04:00 as "16:30 on 3 Oct"."""
    from datetime import datetime

    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(zone(settings.time_zone))
    except ValueError:
        return value or "an unknown time"
    return f"{moment:%H:%M} {moment.tzname() or ''} on {moment.day} {moment:%b}".replace("  ", " ")


def _open_case(payload: CaseCreate, actor: str, trigger: str, background_tasks: BackgroundTasks) -> SupportCase:
    """Create a case, its Jira ticket and its agent run. A person or a Grafana alert can open a case."""
    case = database.create_case(payload)
    database.record_audit(actor, "case.created", "case", case.id, f"Created case for {case.customer}.")
    if jira_publisher.configured:
        try:
            key, url = jira_publisher.create_tracking_issue(
                case.title, case.customer, case.service, case.environment,
                case.reported_issue, case.severity,
            )
            case = database.save_jira_issue(case.id, key, url)
            database.record_audit(actor, "jira.auto_created", "case", case.id, f"Jira ticket {key} created automatically.")
        except JiraPublishError:
            pass
    if trigger == "alert" and (first := duplicate_of(database.list_cases(), case, groups := IncidentGroups(database.path))):
        # the duplicate rule: the team investigates the first case of this service; this one follows it
        groups.link(first, case, status="confirmed", source="rule", by="rootiq",
                    reason=f"Same service ({case.service}), opened {_minutes_apart(first, case)} after case {first.id[:8]}.")
        _note(case, f"Grouped with case {first.id[:8]} ({first.title}): same service, open at the same time. "
                    "The team investigates that case.", background_tasks)
        _note(first, f"An alert joined this case: {case.title} (case {case.id[:8]}).", background_tasks)
        database.record_audit("rootiq", "incident.grouped", "case", case.id, f"Grouped with case {first.id[:8]} by the duplicate rule.")
        return case
    if settings.agent_autorun:
        agent_runner.start_if_idle(case.id, "rootiq-agent", trigger)
    else:
        background_tasks.add_task(_auto_analyze, case.id, actor)
    return case


def _minutes_apart(a: SupportCase, b: SupportCase) -> str:
    minutes = abs(round((b.created_at - a.created_at).total_seconds() / 60))
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h"


def _note(case: SupportCase, text: str, background_tasks: BackgroundTasks | None = None) -> None:
    """An internal message on the case from RootIQ."""
    message = database.create_message(case.id, "RootIQ", MessageCreate(content=text, message_type="internal"))
    if background_tasks is not None:
        background_tasks.add_task(ws_manager.broadcast, case.id, message.model_dump(mode="json"))


# Incident groups: see correlation.py and agent/correlation_agent.py

def _case_card(case: SupportCase) -> dict:
    return {"id": case.id, "title": case.title, "service": case.service, "customer": case.customer, "status": case.status,
            "severity": case.severity, "created_at": case.created_at.isoformat(), "likely_cause": case.analysis.likely_cause}


@app.get("/api/incident-groups")
def list_incident_groups() -> list[dict]:
    """Every group, short: for the marks in the case list."""
    return [{"id": g.id, "primary_case_id": g.primary_case_id, "cause": g.cause,
             "links": [{"case_id": l.case_id, "status": l.status, "source": l.source} for l in g.links]}
            for g in IncidentGroups(database.path).groups()]


@app.get("/api/cases/{case_id}/incident-group")
def get_incident_group(case_id: str) -> dict:
    _find_case(case_id)
    group = IncidentGroups(database.path).group_for(case_id)
    cases = {}
    for link in group.links if group else []:
        if (other := database.get_case(link.case_id)) is not None:
            cases[other.id] = _case_card(other)
    return {"group": group.model_dump(mode="json") if group else None, "cases": cases,
            "pending": agent_runs.has_job(f"correlate:{case_id}")}


@app.post("/api/cases/{case_id}/incident-group/check", status_code=202)
def check_incident_group(case_id: str, request: Request) -> dict:
    case = _find_case(case_id)
    agent_runner.schedule_correlation(case_id)
    _audit(request, "agent.correlation_requested", case, "Related cases check requested.")
    return {"pending": True}


@app.post("/api/cases/{case_id}/incident-group")
def decide_incident_link(case_id: str, payload: IncidentLinkDecision, request: Request, background_tasks: BackgroundTasks) -> dict:
    case = _find_case(case_id)
    groups = IncidentGroups(database.path)
    group = groups.group_for(case_id)
    if group is None:
        raise HTTPException(status_code=404, detail="The case is not in a group.")
    user = request.state.user.username
    if payload.action == "confirm":
        groups.confirm(case_id, user)
        _audit(request, "incident.link_confirmed", case, f"Same root cause as case {group.primary_case_id[:8]} (confirmed).")
    elif payload.action == "primary":
        groups.set_primary(case_id)
        _audit(request, "incident.primary_set", case, "Made the primary case of its group.")
    else:
        was_rule = any(l.case_id == case_id and l.source == "rule" for l in group.links)
        groups.separate(case_id, user)
        _audit(request, "incident.link_removed", case, "Dismissed as not related." if payload.action == "dismiss" else "Ungrouped.")
        if was_rule and settings.agent_autorun and not agent_runs.list_for_case(case_id, limit=1):
            agent_runner.start_if_idle(case_id, user, "manual")  # it followed another case: now it gets its own investigation
    return get_incident_group(case_id)


@app.post("/api/cases/{case_id}/incident-group/resolve-linked")
def resolve_linked_cases(case_id: str, request: Request, background_tasks: BackgroundTasks) -> dict:
    """The primary case is resolved: resolve the confirmed cases of its group with the same resolution."""
    primary = _find_case(case_id)
    group = IncidentGroups(database.path).group_for(case_id)
    if group is None or group.primary_case_id != case_id:
        raise HTTPException(status_code=409, detail="Only the primary case of a group can resolve the linked cases.")
    if primary.status != "resolved":
        raise HTTPException(status_code=409, detail="Resolve the primary case first.")
    resolved = []
    for link in group.links:
        other = database.get_case(link.case_id)
        if link.case_id == case_id or link.status != "confirmed" or other is None or other.status == "resolved":
            continue
        database.resolve_case(other.id, ResolutionUpdate(resolution=f"Resolved with case {case_id[:8]} (same root cause): {primary.resolution}",
                                                         preventive_actions=primary.preventive_actions))
        _audit(request, "case.resolved", other, f"Resolved with the primary case {case_id[:8]}.")
        if (jira_key := other.jira_source_key or other.jira_issue_key) and jira_publisher.configured:
            try:
                jira_publisher.transition_to_done(jira_key)
            except JiraPublishError:
                pass
        resolved.append(other.id)
    return {"resolved": resolved}


@app.post("/api/cases/{case_id}/analyze", response_model=SupportCase)
def analyze_case(case_id: str, request: Request) -> SupportCase:
    case = _find_case(case_id)
    database.mark_investigating(case_id)
    _audit(request, "analysis.started", case, "Local AI incident analysis started.")
    try:
        analysis = analyst.analyze(case)
    except AnalysisUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    updated = database.save_analysis(case_id, analysis)
    _audit(request, "analysis.generated", updated, "Analysis prepared for human review.")
    return updated


@app.put("/api/cases/{case_id}/review", response_model=SupportCase)
def review_case(case_id: str, payload: AnalysisReview, request: Request) -> SupportCase:
    _find_case(case_id)
    case = database.save_review(case_id, payload.analysis, payload.approve)
    action = "analysis.approved" if payload.approve else "analysis.edited"
    _audit(request, action, case, "Human review decision saved.")
    return case


@app.put("/api/cases/{case_id}/resolve", response_model=SupportCase)
def resolve_case(case_id: str, payload: ResolutionUpdate, request: Request, background_tasks: BackgroundTasks) -> SupportCase:
    _find_case(case_id)
    case = database.resolve_case(case_id, payload)
    _audit(request, "case.resolved", case, "Resolution and preventive actions recorded.")
    try:
        database.save_memory(case_id, embedder.embed(database.memory_content(case)))
        case = database.get_case(case_id) or case
        _audit(request, "memory.indexed", case, "Resolution auto-indexed into knowledge base.")
    except EmbeddingUnavailable:
        pass
    jira_key = case.jira_source_key or case.jira_issue_key
    if jira_key and jira_publisher.configured:
        try:
            jira_publisher.transition_to_done(jira_key)
            _audit(request, "jira.transitioned", case, f"Jira {jira_key} transitioned to Done.")
        except JiraPublishError:
            pass
    background_tasks.add_task(_auto_runbook, case_id, request.state.user.username)
    if (group := IncidentGroups(database.path).group_for(case_id)) and group.primary_case_id == case_id:
        for link in group.links:  # tell the cases that follow this one
            other = database.get_case(link.case_id)
            if link.case_id != case_id and link.status == "confirmed" and other and other.status != "resolved":
                _note(other, f"The primary case {case_id[:8]} is resolved: {case.resolution}", background_tasks)
    agent_runner.schedule_reflection(case_id)  # this does nothing if the agent team did not investigate this case
    agent_runner.schedule_postmortem(case_id)
    return case


@app.get("/api/cases/{case_id}/postmortem")
def get_postmortem(case_id: str) -> dict:
    _find_case(case_id)
    postmortem = database.get_postmortem(case_id)
    return {"postmortem": postmortem.model_dump(mode="json") if postmortem else None,
            "pending": agent_runs.has_job(f"postmortem:{case_id}")}


@app.post("/api/cases/{case_id}/postmortem", status_code=202)
def regenerate_postmortem(case_id: str, request: Request) -> dict:
    case = _find_case(case_id)
    if case.status != "resolved":
        raise HTTPException(status_code=409, detail="The postmortem is written after the case is resolved.")
    agent_runner.schedule_postmortem(case_id)
    _audit(request, "agent.postmortem_requested", case, "Postmortem requested.")
    return {"pending": True}


@app.get("/api/cases/{case_id}/postmortem.md", response_class=PlainTextResponse)
def postmortem_markdown(case_id: str) -> str:
    from .agent.postmortem import to_markdown

    postmortem = database.get_postmortem(case_id)
    if postmortem is None:
        raise HTTPException(status_code=404, detail="No postmortem yet.")
    return to_markdown(postmortem, zone(settings.time_zone))


@app.get("/api/cases/{case_id}/remediations")
def case_remediations(case_id: str) -> list[dict]:
    from .automation import RemediationLog

    _find_case(case_id)
    return RemediationLog(settings.database_target).for_case(case_id)


class RatingBody(BaseModel):
    verdict: Literal["correct", "partly_correct", "wrong"]
    comment: str = Field(default="", max_length=500)


@app.put("/api/agent-runs/{run_id}/rating")
def rate_run(run_id: str, body: RatingBody, request: Request) -> dict:
    """An engineer rates the diagnosis of a run. One rating for each person; a new rating replaces the old one."""
    run = agent_runs.get(run_id)
    if run is None or run.analysis is None:
        raise HTTPException(status_code=404, detail="No diagnosis to rate.")
    agent_runs.rate(run_id, run.case_id, request.state.user.username, body.verdict, body.comment)
    database.record_audit(request.state.user.username, "agent.rated", "case", run.case_id,
                          f"Run {run_id[:8]} diagnosis: {body.verdict.replace('_', ' ')}.")
    return agent_runs.ratings_for_run(run_id)


@app.get("/api/agent-runs/{run_id}/rating")
def get_run_rating(run_id: str) -> dict:
    return agent_runs.ratings_for_run(run_id)


@app.get("/api/agent-ratings")
def rating_summary(days: int = Query(default=30, ge=1, le=365)) -> dict:
    return agent_runs.rating_summary(days)


@app.put("/api/cases/{case_id}/acknowledge", response_model=SupportCase)
def acknowledge_case(case_id: str, payload: AcknowledgeUpdate, request: Request) -> SupportCase:
    _find_case(case_id)
    case = database.acknowledge_case(case_id, payload.note)
    _audit(request, "customer.acknowledged", case, payload.note)
    return case


@app.get("/api/memory/status", response_model=MemoryStatus)
def memory_status() -> MemoryStatus:
    resolved, indexed = database.memory_counts()
    return MemoryStatus(
        resolved_cases=resolved, indexed_cases=indexed, embedding_model=settings.embedding_model
    )


@app.post("/api/cases/{case_id}/memory/index", response_model=MemoryStatus)
def index_case_memory(case_id: str, request: Request) -> MemoryStatus:
    case = _find_case(case_id)
    if case.status != "resolved":
        raise HTTPException(status_code=400, detail="Only resolved cases can be indexed.")
    try:
        database.save_memory(case_id, embedder.embed(database.memory_content(case)))
    except EmbeddingUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    _audit(request, "memory.indexed", case, "Resolved case indexed in local semantic memory.")
    return memory_status()


@app.get("/api/cases/{case_id}/related", response_model=list[RelatedCase])
def related_cases(case_id: str) -> list[RelatedCase]:
    case = _find_case(case_id)
    return rank_memory(database, embedder, database.memory_content(case), exclude_case_id=case_id)


@app.post("/api/cases/{case_id}/runbook", response_model=RunbookRecommendation)
def recommend_runbook(case_id: str, request: Request) -> RunbookRecommendation:
    case = _find_case(case_id)
    related = related_cases(case_id)
    evidence = [item.case for item in related if item.case.status == "resolved"]
    if not evidence:
        evidence = [
            item
            for item in database.list_cases()
            if item.id != case_id and item.status == "resolved" and item.service == case.service
        ][:5]
    if not evidence:
        raise HTTPException(status_code=400, detail="Need at least one resolved similar case.")
    try:
        runbook = analyst.recommend_runbook(case, evidence)
    except AnalysisUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    database.save_runbook(case_id, runbook)
    _audit(request, "runbook.generated", case, f"Grounded in {len(evidence)} resolved case(s).")
    return runbook


@app.post("/api/cases/{case_id}/agent-runs", response_model=AgentRun, status_code=202)
def start_agent_run(case_id: str, request: Request, replace: bool = False) -> AgentRun:
    """Start a run. With replace=true, the current run of the case stops first: Restart, or Run again over waiting
    proposals."""
    try:
        if replace:
            return agent_runner.restart(case_id, request.state.user.username)
        return agent_runner.start(case_id, request.state.user.username)
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/agent-runs/{run_id}/stop", response_model=AgentRun)
def stop_agent_run(run_id: str, request: Request) -> AgentRun:
    """Stop a run: at once when it waits, after the current step when it runs."""
    try:
        return agent_runner.stop(run_id, request.state.user.username)
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/cases/{case_id}/agent-runs", response_model=list[AgentRun])
def list_agent_runs(case_id: str) -> list[AgentRun]:
    _find_case(case_id)
    return [agent_runner.present(run) for run in agent_runs.list_for_case(case_id)]


@app.get("/api/agent-runs/{run_id}", response_model=AgentRun)
def get_agent_run(run_id: str) -> AgentRun:
    run = agent_runs.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found.")
    return agent_runner.present(run)


@app.get("/api/cases/{case_id}/agent-lesson", response_model=AgentLesson | None)
def case_agent_lesson(case_id: str) -> AgentLesson | None:
    _find_case(case_id)
    return agent_runs.lesson_for_case(case_id)


@app.get("/api/agent-metrics", response_model=AgentMetrics)
def agent_metrics() -> AgentMetrics:
    return agent_runner.metrics()


@app.get("/api/agent-lessons", response_model=list[AgentLesson])
def agent_lessons() -> list[AgentLesson]:
    return [lesson for lesson, _ in agent_runs.lessons()]


@app.post("/api/agent-runs/{run_id}/decision", response_model=AgentRun, status_code=202)
def decide_agent_run(run_id: str, payload: DecisionRequest, request: Request, background_tasks: BackgroundTasks) -> AgentRun:
    actor = request.state.user.username
    try:
        run = agent_runner.decide(run_id, payload.decisions, actor)
    except RunNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    summary = ", ".join(f"#{d.index + 1} {d.action}" for d in payload.decisions) or "no actions approved"
    database.record_audit(actor, "agent.decision", "case", run.case_id, f"Run {run_id[:8]}: {summary}.")
    if run.status == "executing":  # every proposal has the approvals that its risk level needs
        final, _ = agent_runner.final_decisions(run)
        agent_runner.enqueue_resume(run_id, final)  # a worker carries out the approved actions
    return agent_runner.present(run)


@app.get("/api/cases/{case_id}/report.md", response_class=PlainTextResponse)
def export_case_report(case_id: str, request: Request) -> str:
    case = _find_case(case_id)
    _audit(request, "report.exported", case, "Markdown incident report downloaded.")
    timeline = "\n".join(f"- {entry.at.isoformat()}: {entry.event}" for entry in case.timeline)
    actions = "\n".join(f"- {item}" for item in case.preventive_actions) or "- None recorded"
    return f"""# RootIQ Incident Report: {case.title}

## Case Details

- Customer: {case.customer}
- Service: {case.service}
- Environment: {case.environment}
- Severity: {case.severity}
- Status: {case.status}
- SLA status: {case.sla_state}
- Created: {case.created_at.isoformat()}
- Initial response target: {case.response_due_at.isoformat()}
- Resolution target: {case.resolution_due_at.isoformat()}

## Customer Report

{case.reported_issue}

## Analysis

**Summary:** {case.analysis.incident_summary}

**Customer impact:** {case.analysis.customer_impact}

**Likely cause:** {case.analysis.likely_cause}

**Workaround:** {case.analysis.workaround}

## Resolution

{case.resolution or "Not yet resolved."}

## Preventive Actions

{actions}

## Timeline

{timeline}
"""


@app.post("/api/cases/{case_id}/jira", response_model=SupportCase)
def publish_jira_escalation(case_id: str, request: Request) -> SupportCase:
    case = _approved_case(case_id)
    if case.jira_issue_key:
        raise HTTPException(status_code=409, detail="Already escalated to Jira.")
    try:
        issue_key, issue_url = jira_publisher.create_issue(case)
    except JiraPublishError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    case = database.save_jira_issue(case_id, issue_key, issue_url)
    _audit(request, "jira.created", case, f"Jira issue {issue_key} created.")
    return case


@app.post("/api/cases/{case_id}/gmail-draft", response_model=SupportCase)
def publish_gmail_draft(case_id: str, request: Request) -> SupportCase:
    case = _approved_case(case_id)
    if case.gmail_draft_id:
        raise HTTPException(status_code=409, detail="Draft already exists.")
    try:
        draft_id = gmail_publisher.create_draft(
            case.analysis.customer_email_subject, case.analysis.customer_email_body
        )
    except GmailDraftError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    case = database.save_gmail_draft(case_id, draft_id)
    _audit(request, "gmail.draft_created", case, "Customer update draft created.")
    return case


@app.get("/api/jira/pending")
def jira_pending() -> dict[str, int]:
    if not jira_publisher.configured:
        return {"count": 0}
    try:
        tickets = jira_publisher.search_issues(settings.jira_sync_jql)
    except JiraPublishError:
        return {"count": 0}
    existing_keys = database.get_imported_jira_keys() | database.get_pushed_jira_keys()
    return {"count": sum(1 for t in tickets if t["key"] not in existing_keys)}


@app.post("/api/jira/sync", response_model=JiraSyncResult)
def sync_jira_tickets(request: Request, background_tasks: BackgroundTasks) -> JiraSyncResult:
    if not jira_publisher.configured:
        raise HTTPException(status_code=400, detail="Jira not configured.")
    try:
        tickets = jira_publisher.search_issues(settings.jira_sync_jql)
    except JiraPublishError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    existing_keys = database.get_imported_jira_keys() | database.get_pushed_jira_keys()
    # Services that the app knows: the services with logs, and the services of earlier cases
    try:
        source_services = set(active_sources(settings).logs.services())
    except SourceError:
        source_services = set()
    known_services = source_services | {p.stem for p in settings.logs_dir.glob("*.log")} | {
        c.service for c in database.list_cases() if c.service and c.service != settings.jira_project_key}
    imported: list[SupportCase] = []
    skipped = 0
    for ticket in tickets:
        if ticket["key"] in existing_keys:
            skipped += 1
            continue
        service, customer = jira_publisher.case_fields(ticket, known_services)
        payload = CaseCreate(
            title=ticket["summary"] or ticket["key"],
            customer=customer,
            service=service,
            environment="production",
            reported_issue=ticket["description"] or ticket["summary"] or "Imported from Jira.",
            logs=f"Jira: {ticket['key']}  Status: {ticket['status']}  Priority: {ticket['priority']}",
            severity=jira_publisher.map_severity(ticket["priority"]),
        )
        case = database.create_case(payload)
        case = database.update_jira_source(case.id, ticket["key"])
        _audit(request, "jira.imported", case, f"Imported from Jira ticket {ticket['key']}.")
        imported.append(case)
    _note_closed_in_jira(background_tasks)
    if imported:
        _audit(request, "jira.sync", imported[0], f"Synced {len(imported)} ticket(s) from Jira.")
        for case in imported:
            if settings.agent_autorun:
                agent_runner.start_if_idle(case.id, "rootiq-agent", "jira_import")
            else:
                background_tasks.add_task(_auto_analyze, case.id, request.state.user.username)
    return JiraSyncResult(imported=len(imported), skipped=skipped, cases=imported)


@app.get("/api/cases/{case_id}/suggestions", response_model=list[RelatedCase])
def kb_suggestions(case_id: str) -> list[RelatedCase]:
    case = _find_case(case_id)
    content = database.memory_content(case)
    records = database.get_memory(exclude_case_id=case_id)
    if not records:
        return []
    try:
        vec = embedder.embed(content)
        matches = [
            RelatedCase(case=r, score=round(cosine_similarity(vec, v), 4), match_type="semantic")
            for r, _, v in records
        ]
    except EmbeddingUnavailable:
        matches = [
            RelatedCase(case=r, score=round(text_similarity(content, c), 4), match_type="text")
            for r, c, _ in records
        ]
    return sorted((m for m in matches if m.score > 0.15), key=lambda m: m.score, reverse=True)[:5]


@app.get("/api/cases/{case_id}/messages", response_model=list[CaseMessage])
def list_messages(case_id: str) -> list[CaseMessage]:
    case = _find_case(case_id)
    jira_key = case.jira_source_key or case.jira_issue_key
    if jira_key and jira_publisher.configured:
        try:
            comments = jira_publisher.get_comments(jira_key)
            database.sync_jira_comments(case_id, comments, "")
        except JiraPublishError:
            pass
    return database.list_messages(case_id)


@app.get("/api/messages/counts")
def message_counts() -> dict[str, int]:
    return database.message_counts()


@app.post("/api/cases/{case_id}/messages", response_model=CaseMessage)
def create_message(
    case_id: str, payload: MessageCreate, request: Request, background_tasks: BackgroundTasks,
) -> CaseMessage:
    case = _find_case(case_id)
    message = database.create_message(case_id, request.state.user.username, payload)
    _audit(request, "message.sent", case, f"{payload.message_type} message.")
    background_tasks.add_task(ws_manager.broadcast, case_id, message.model_dump(mode="json"))
    jira_key = case.jira_source_key or case.jira_issue_key
    if jira_key and jira_publisher.configured:
        background_tasks.add_task(
            _sync_jira_comment, jira_key, request.state.user.username, payload.content,
        )
    return message


@app.websocket("/api/cases/{case_id}/ws")
async def case_websocket(websocket: WebSocket, case_id: str) -> None:
    """Live messages of one case. The browser of a signed-in user on an allowed website only. The session is checked
    again every WS_RECHECK_SECONDS: a signed-out, removed or disabled user loses the channel."""
    origin = websocket.headers.get("origin", "").rstrip("/")
    token = websocket.cookies.get("rootiq_session", "")
    signed_in = lambda: bool(token) and authenticator.authenticate(f"Bearer {token}") is not None  # noqa: E731
    if (origin and origin not in ALLOWED_ORIGINS) or not signed_in() or database.get_case(case_id) is None:
        await websocket.close(code=1008)
        return
    await ws_manager.connect(case_id, websocket)
    try:
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=WS_RECHECK_SECONDS)
            except TimeoutError:
                if not signed_in():
                    await websocket.close(code=1008)
                    break
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(case_id, websocket)


def _find_case(case_id: str) -> SupportCase:
    case = database.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found.")
    return case


def _approved_case(case_id: str) -> SupportCase:
    case = _find_case(case_id)
    if case.review_status != "approved":
        raise HTTPException(status_code=400, detail="Analysis must be approved first.")
    return case


def _audit(request: Request, action: str, case: SupportCase, detail: str = "") -> None:
    database.record_audit(request.state.user.username, action, "case", case.id, detail)


def _auto_runbook(case_id: str, actor: str) -> None:
    case = database.get_case(case_id)
    if case is None or case.runbook is not None:
        return
    records = database.get_memory(exclude_case_id=case_id)
    if not records:
        return
    try:
        vec = embedder.embed(database.memory_content(case))
    except EmbeddingUnavailable:
        return
    evidence = [
        r for r, _, v in records
        if r.status == "resolved" and cosine_similarity(vec, v) > 0.15
    ]
    if not evidence:
        return
    try:
        runbook = analyst.recommend_runbook(case, evidence[:5])
    except AnalysisUnavailable:
        return
    database.save_runbook(case_id, runbook)
    database.record_audit(actor, "runbook.auto_generated", "case", case_id, f"Grounded in {len(evidence[:5])} resolved case(s).")


def _auto_analyze(case_id: str, actor: str) -> None:
    case = database.get_case(case_id)
    if case is None or case.status != "new":
        return
    database.mark_investigating(case_id)
    database.record_audit(actor, "analysis.started", "case", case_id, "Auto-analysis triggered on Jira import.")
    try:
        analysis = analyst.analyze(case)
    except AnalysisUnavailable:
        return
    database.save_analysis(case_id, analysis)
    database.record_audit(actor, "analysis.generated", "case", case_id, "AI analysis ready for review.")


def _sync_jira_comment(jira_key: str, author: str, content: str) -> None:
    try:
        jira_publisher.add_comment(jira_key, author, content)
    except JiraPublishError:
        pass
