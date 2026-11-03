from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field


CaseStatus = Literal["new", "investigating", "awaiting_approval", "approved", "resolved"]
Severity = Literal["critical", "high", "medium", "low"]
ReviewStatus = Literal["not_ready", "pending_review", "approved"]
SlaState = Literal["on_track", "at_risk", "breached", "met"]


class CaseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    customer: str = Field(min_length=1, max_length=120)
    service: str = Field(min_length=1, max_length=120)
    environment: Literal["production", "staging", "test"] = "production"
    reported_issue: str = Field(min_length=1, max_length=20_000)
    logs: str = Field(default="", max_length=200_000)
    severity: Severity = "medium"


class SupportAnalysis(BaseModel):
    incident_summary: str = ""
    customer_impact: str = ""
    likely_cause: str = ""
    suggested_severity: Severity = "medium"
    diagnostic_steps: list[str] = Field(default_factory=list)
    workaround: str = ""
    information_to_request: list[str] = Field(default_factory=list)
    customer_email_subject: str = ""
    customer_email_body: str = ""


class AnalysisReview(BaseModel):
    analysis: SupportAnalysis
    approve: bool = False


class ResolutionUpdate(BaseModel):
    resolution: str = Field(min_length=1, max_length=10_000)
    preventive_actions: list[Annotated[str, Field(max_length=1_000)]] = Field(default_factory=list, max_length=30)


class PostIncidentReport(BaseModel):
    summary: str = ""
    customer_impact: str = ""
    root_cause: str = ""
    resolution: str = ""
    preventive_actions: list[str] = Field(default_factory=list)


class RunbookRecommendation(BaseModel):
    title: str = ""
    symptoms: list[str] = Field(default_factory=list)
    verification_steps: list[str] = Field(default_factory=list)
    mitigation_steps: list[str] = Field(default_factory=list)
    escalation_guidance: str = ""
    evidence_case_ids: list[str] = Field(default_factory=list)


class TimelineEvent(BaseModel):
    at: datetime
    event: str


class CaseMessage(BaseModel):
    id: str
    case_id: str
    author: str
    content: str
    message_type: Literal["internal", "customer"] = "internal"
    created_at: datetime


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    message_type: Literal["internal", "customer"] = "internal"


class JiraSyncResult(BaseModel):
    imported: int
    skipped: int
    cases: list["SupportCase"]


class AcknowledgeUpdate(BaseModel):
    note: str = Field(default="Customer acknowledgement recorded.", max_length=300)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class AuthUser(BaseModel):
    username: str
    role: Literal["viewer", "engineer", "admin"] = "admin"
    display_name: str = ""
    email: str = ""
    auth: Literal["password", "sso"] = "password"


class PasswordChange(BaseModel):
    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=1, max_length=200)


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=120)
    display_name: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=200)
    role: Literal["viewer", "engineer", "admin"] = "engineer"
    password: str = Field(default="", max_length=200)  # empty: the user signs in with single sign-on, by email


class UserUpdate(BaseModel):
    role: Literal["viewer", "engineer", "admin"] | None = None
    disabled: bool | None = None
    password: str = Field(default="", max_length=200)
    display_name: str | None = Field(default=None, max_length=120)


class LoginResponse(BaseModel):
    access_token: str
    expires_at: datetime
    user: AuthUser


class AuditEvent(BaseModel):
    id: str
    at: datetime
    actor: str
    action: str
    resource_type: str
    resource_id: str
    detail: str = ""


class SupportCase(BaseModel):
    id: str
    title: str
    customer: str
    service: str
    environment: str
    reported_issue: str
    logs: str
    status: CaseStatus
    severity: Severity
    created_at: datetime
    updated_at: datetime
    response_due_at: datetime
    resolution_due_at: datetime
    first_response_at: datetime | None = None
    resolved_at: datetime | None = None  # from the timeline; later notes change updated_at, not this
    sla_state: SlaState = "on_track"
    analysis: SupportAnalysis = Field(default_factory=SupportAnalysis)
    review_status: ReviewStatus = "not_ready"
    approved_at: datetime | None = None
    resolution: str = ""
    preventive_actions: list[str] = Field(default_factory=list)
    post_incident_report: PostIncidentReport = Field(default_factory=PostIncidentReport)
    jira_issue_key: str | None = None
    jira_issue_url: str | None = None
    jira_published_at: datetime | None = None
    gmail_draft_id: str | None = None
    gmail_draft_created_at: datetime | None = None
    jira_source_key: str | None = None
    memory_indexed: bool = False
    runbook: RunbookRecommendation | None = None
    timeline: list[TimelineEvent] = Field(default_factory=list)


class RelatedCase(BaseModel):
    case: SupportCase
    score: float
    match_type: Literal["semantic", "text"]


class MemoryStatus(BaseModel):
    resolved_cases: int
    indexed_cases: int
    embedding_model: str


class OperationsDashboard(BaseModel):
    total_cases: int
    open_cases: int
    urgent_open_cases: int
    awaiting_approval_cases: int
    breached_cases: int
    at_risk_cases: int
    resolved_cases: int
    average_resolution_hours: float | None = None


class ServiceTrend(BaseModel):
    service: str
    total_cases: int
    open_cases: int
    resolved_cases: int
    breached_cases: int
    average_resolution_hours: float | None = None


class ProblemGroup(BaseModel):
    service: str
    cause: str
    case_count: int
    cases: list[SupportCase] = Field(default_factory=list)
    known_resolutions: list[str] = Field(default_factory=list)


class IncidentLink(BaseModel):
    """One case in an incident group. A suggested link waits for a person; a confirmed one counts."""
    case_id: str
    status: Literal["suggested", "confirmed"]
    source: Literal["rule", "agent", "person"]
    reason: str = ""
    evidence: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium"] = "medium"
    linked_at: datetime
    linked_by: str


class IncidentGroup(BaseModel):
    """Cases with one root cause. The team works on the primary case; the others follow it."""
    id: str
    primary_case_id: str
    cause: str = ""
    created_at: datetime
    updated_at: datetime
    links: list[IncidentLink] = Field(default_factory=list)


class IncidentLinkDecision(BaseModel):
    action: Literal["confirm", "dismiss", "primary", "ungroup"]


class PreferenceUpdate(BaseModel):
    key: str = Field(min_length=1, max_length=120)
    value: str | None = None


class HealthResponse(BaseModel):
    status: str
    ollama_installed: bool
    ollama_model: str
    embedding_model: str
    jira_configured: bool
    jira_site_url: str = ""
    gmail_configured: bool
    stored_cases: int
    agent_model: str = ""
    agent_dry_run: bool = True
    agent_autorun: bool = True
    grafana_url: str = ""
    time_zone: str = ""  # the zone for the times on the website; empty = the browser's


class AppState(BaseModel):
    health: HealthResponse
    cases: list[SupportCase]
    dashboard: OperationsDashboard
    trends: list[ServiceTrend]
    problems: list[ProblemGroup]
    audit: list[AuditEvent]
    preferences: dict[str, str]


class AgentStep(BaseModel):
    index: int
    agent: str = "agent"
    tool: str
    arguments: dict[str, object] = Field(default_factory=dict)
    result: str = ""
    ok: bool = True
    duration_ms: int = 0


class LLMCall(BaseModel):
    agent: str
    started_at: datetime
    duration_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    ok: bool = True
    model: str = ""  # the model that answered or failed; empty for older runs
    error: str = ""  # why the call failed, in short


class AgentSpan(BaseModel):
    agent: str
    started_at: datetime
    duration_ms: int


class ProposedAction(BaseModel):
    kind: Literal["customer_update", "jira_comment", "remediation", "code_change"]
    summary: str
    payload: dict[str, str] = Field(default_factory=dict)
    risk: Literal["low", "medium", "high"] = "low"
    approvals_needed: int = 1  # 0: runs without a person, for low risk only; 2: two different people


class AgentRunResult(BaseModel):
    case_id: str
    model: str
    analysis: SupportAnalysis
    steps: list[AgentStep] = Field(default_factory=list)
    proposals: list[ProposedAction] = Field(default_factory=list)
    tool_rounds: int = 0
    stopped_by_limit: bool = False


SpecialistName = Literal["evidence", "history", "changes", "code"]


class InvestigationPlan(BaseModel):
    hypotheses: list[str] = Field(description="2-3 candidate causes to confirm or rule out")
    evidence_task: str = Field(description="What the evidence agent should look for in the service logs")
    history_task: str = Field(description="What the history agent should look for in past incidents and Jira")
    changes_task: str = Field(default="", description="What the changes agent should check in recent deploys and commits")
    code_task: str = Field(default="", description="What the code agent should look for in the service's code and config")


class ReviewVerdict(BaseModel):
    approved: bool = Field(description="true only if every claim in the findings is backed by the raw evidence")
    unsupported_claims: list[str] = Field(default_factory=list)
    recheck: list[SpecialistName] = Field(default_factory=list, description="specialists that must redo work")
    instructions: str = Field(default="", description="what the specialists must check on the second pass")


class TeamRunResult(BaseModel):
    case_id: str
    model: str
    plan: InvestigationPlan
    findings: dict[str, str] = Field(default_factory=dict)
    reviews: list[ReviewVerdict] = Field(default_factory=list)
    analysis: SupportAnalysis
    steps: list[AgentStep] = Field(default_factory=list)
    proposals: list[ProposedAction] = Field(default_factory=list)
    severity_note: str = ""
    warnings: list[str] = Field(default_factory=list)


RunStatus = Literal["queued", "running", "awaiting_approval", "executing", "completed", "failed", "stopped"]
RunTrigger = Literal["manual", "case_created", "jira_import", "sla_risk", "alert", "verification_failed"]
DecisionAction = Literal["approve", "edit", "reject"]


class ActionDecision(BaseModel):
    index: int = Field(ge=0)
    action: DecisionAction
    payload: dict[str, Annotated[str, Field(max_length=20_000)]] = Field(default_factory=dict, max_length=20,
                                                                          description="The edited fields of an edit")


class DecisionRequest(BaseModel):
    decisions: list[ActionDecision] = Field(max_length=50)


class ActionOutcome(BaseModel):
    index: int
    kind: str
    decision: DecisionAction | Literal["none"]
    status: Literal["executed", "dry_run", "skipped", "failed"]
    # remediation: "executed" = the runner did it; the verification agent then checks the result
    detail: str = ""


class ApprovalVote(BaseModel):
    index: int
    by: str
    action: DecisionAction
    payload: dict[str, str] = Field(default_factory=dict)
    at: datetime


class VerificationResult(BaseModel):
    check: int  # 1 = the first check after the fix
    at: datetime
    action: str
    status: Literal["fixed", "not_fixed", "inconclusive"]
    errors_before: int = 0
    errors_after: int = 0
    minutes_before: float = 0
    minutes_after: float = 0
    metric: str = ""
    summary: str = ""


class VerificationVerdict(BaseModel):
    status: Literal["fixed", "not_fixed", "inconclusive"] = Field(description="fixed only if the evidence shows the errors stopped")
    summary: str = Field(description="two sentences for the engineer: what the evidence shows, with numbers")


class AgentRun(BaseModel):
    id: str
    case_id: str
    model: str
    status: RunStatus
    stage: str = ""
    trigger: RunTrigger = "manual"
    queue_position: int | None = None
    started_by: str
    created_at: datetime
    updated_at: datetime
    dry_run: bool = True
    plan: InvestigationPlan | None = None
    findings: dict[str, str] = Field(default_factory=dict)
    reviews: list[ReviewVerdict] = Field(default_factory=list)
    analysis: SupportAnalysis | None = None
    severity_note: str = ""
    warnings: list[str] = Field(default_factory=list)
    steps: list[AgentStep] = Field(default_factory=list)
    proposals: list[ProposedAction] = Field(default_factory=list)
    outcomes: list[ActionOutcome] = Field(default_factory=list)
    lessons_used: list[str] = Field(default_factory=list)
    llm_calls: list[LLMCall] = Field(default_factory=list)
    spans: list[AgentSpan] = Field(default_factory=list)
    decided_by: str = ""
    error: str = ""
    approvals: list[ApprovalVote] = Field(default_factory=list)
    verifications: list[VerificationResult] = Field(default_factory=list)
    team: list[str] = Field(default_factory=list)  # the optional agents in this run: changes, code, remediation


LessonVerdict = Literal["correct", "partly_correct", "wrong"]


class LessonDraft(BaseModel):
    verdict: LessonVerdict = Field(description="how well the agent's likely cause matched the confirmed resolution")
    lesson: str = Field(description="one or two sentences: what future investigations of similar incidents should check")


class AgentLesson(BaseModel):
    id: str
    case_id: str
    run_id: str
    service: str
    case_title: str
    verdict: LessonVerdict
    agent_cause: str
    actual_resolution: str
    lesson: str
    created_at: datetime


class AgentUsage(BaseModel):
    agent: str
    runs: int
    avg_seconds: float
    avg_llm_calls: float
    avg_input_tokens: float
    avg_output_tokens: float


class AgentMetrics(BaseModel):
    runs_total: int
    by_status: dict[str, int] = Field(default_factory=dict)
    by_trigger: dict[str, int] = Field(default_factory=dict)
    median_investigation_seconds: float | None = None
    p90_investigation_seconds: float | None = None
    avg_input_tokens: float | None = None
    avg_output_tokens: float | None = None
    avg_tool_calls: float | None = None
    tool_failure_rate: float | None = None
    reviewer_rejection_rate: float | None = None
    decisions: dict[str, int] = Field(default_factory=dict)
    lesson_verdicts: dict[str, int] = Field(default_factory=dict)
    per_agent: list[AgentUsage] = Field(default_factory=list)


class PostmortemActionItem(BaseModel):
    item: str = Field(description="one concrete action that prevents a repeat or finds it sooner")
    priority: Literal["P1", "P2", "P3"] = Field(description="P1 = do this week")
    owner: str = Field(default="", description="a team or role, not a person's name")


class PostmortemDraft(BaseModel):
    title: str
    summary: str = Field(description="three sentences: what happened, the effect, how it was fixed")
    impact: str = Field(description="who was affected, how, and for how long (use the durations given)")
    root_cause: str = Field(description="the cause, with the evidence (log lines or changes with their times)")
    trigger: str = Field(description="the change or event that started it, if known")
    detection: str = Field(description="how the incident was found: alert, customer report, or agents")
    resolution: str
    went_well: list[str] = Field(default_factory=list)
    went_poorly: list[str] = Field(default_factory=list)
    action_items: list[PostmortemActionItem] = Field(default_factory=list)


class PostmortemEvent(BaseModel):
    at: datetime
    event: str
    source: str  # case, alert, agent, approval, remediation, verification


class Postmortem(PostmortemDraft):
    case_id: str
    timeline: list[PostmortemEvent] = Field(default_factory=list)
    durations: dict[str, float] = Field(default_factory=dict)  # minutes: to_analysis, to_fix, to_resolve
    warnings: list[str] = Field(default_factory=list)
    model: str = ""
    created_at: datetime
