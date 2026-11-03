export type CaseStatus = "new" | "investigating" | "awaiting_approval" | "approved" | "resolved";
export type Severity = "critical" | "high" | "medium" | "low";
export type ReviewStatus = "not_ready" | "pending_review" | "approved";
export type SlaState = "on_track" | "at_risk" | "breached" | "met";

export type Role = "viewer" | "engineer" | "admin";

export interface AuthUser {
  username: string;
  role: Role;
  display_name: string;
  email: string;
  auth: "password" | "sso";
}

export interface LoginResponse {
  access_token: string;
  expires_at: string;
  user: AuthUser;
}

export interface AuditEvent {
  id: string;
  at: string;
  actor: string;
  action: string;
  resource_type: string;
  resource_id: string;
  detail: string;
}

export interface SupportAnalysis {
  incident_summary: string;
  customer_impact: string;
  likely_cause: string;
  suggested_severity: Severity;
  diagnostic_steps: string[];
  workaround: string;
  information_to_request: string[];
  customer_email_subject: string;
  customer_email_body: string;
}

export interface TimelineEvent {
  at: string;
  event: string;
}

export interface PostIncidentReport {
  summary: string;
  customer_impact: string;
  root_cause: string;
  resolution: string;
  preventive_actions: string[];
}

export interface SupportCase {
  id: string;
  title: string;
  customer: string;
  service: string;
  environment: string;
  reported_issue: string;
  logs: string;
  status: CaseStatus;
  severity: Severity;
  created_at: string;
  updated_at: string;
  response_due_at: string;
  resolution_due_at: string;
  first_response_at: string | null;
  sla_state: SlaState;
  analysis: SupportAnalysis;
  review_status: ReviewStatus;
  approved_at: string | null;
  resolution: string;
  preventive_actions: string[];
  post_incident_report: PostIncidentReport;
  jira_issue_key: string | null;
  jira_issue_url: string | null;
  jira_published_at: string | null;
  gmail_draft_id: string | null;
  gmail_draft_created_at: string | null;
  jira_source_key: string | null;
  memory_indexed: boolean;
  runbook: RunbookRecommendation | null;
  timeline: TimelineEvent[];
}

export interface CaseCreate {
  title: string;
  customer: string;
  service: string;
  environment: string;
  reported_issue: string;
  logs: string;
  severity: Severity;
}

export interface Health {
  status: string;
  ollama_installed: boolean;
  ollama_model: string;
  embedding_model: string;
  jira_configured: boolean;
  jira_site_url: string;
  gmail_configured: boolean;
  stored_cases: number;
  agent_model: string;
  agent_dry_run: boolean;
  agent_autorun: boolean;
  grafana_url?: string;
  time_zone?: string;
}

export interface MemoryStatus {
  resolved_cases: number;
  indexed_cases: number;
  embedding_model: string;
}

export interface RelatedCase {
  case: SupportCase;
  score: number;
  match_type: "semantic" | "text";
}

export interface OperationsDashboard {
  total_cases: number;
  open_cases: number;
  urgent_open_cases: number;
  awaiting_approval_cases: number;
  breached_cases: number;
  at_risk_cases: number;
  resolved_cases: number;
  average_resolution_hours: number | null;
}

export interface ServiceTrend {
  service: string;
  total_cases: number;
  open_cases: number;
  resolved_cases: number;
  breached_cases: number;
  average_resolution_hours: number | null;
}

export interface ProblemGroup {
  service: string;
  cause: string;
  case_count: number;
  cases: SupportCase[];
  known_resolutions: string[];
}

export interface RunbookRecommendation {
  title: string;
  symptoms: string[];
  verification_steps: string[];
  mitigation_steps: string[];
  escalation_guidance: string;
  evidence_case_ids: string[];
}

export interface CaseMessage {
  id: string;
  case_id: string;
  author: string;
  content: string;
  message_type: "internal" | "customer";
  created_at: string;
}

export interface JiraSyncResult {
  imported: number;
  skipped: number;
  cases: SupportCase[];
}

export interface AppState {
  health: Health;
  cases: SupportCase[];
  dashboard: OperationsDashboard;
  trends: ServiceTrend[];
  problems: ProblemGroup[];
  audit: AuditEvent[];
  preferences: Record<string, string>;
}

export type AgentRunStatus = "queued" | "running" | "awaiting_approval" | "executing" | "completed" | "failed" | "stopped";
export type AgentRunTrigger = "manual" | "case_created" | "jira_import" | "sla_risk" | "alert" | "verification_failed";
export type DecisionAction = "approve" | "edit" | "reject";

export interface AgentStep {
  index: number;
  agent: string;
  tool: string;
  arguments: Record<string, unknown>;
  result: string;
  ok: boolean;
  duration_ms: number;
}

export interface ProposedAction {
  kind: "customer_update" | "jira_comment" | "remediation" | "code_change";
  summary: string;
  payload: Record<string, string>;
  risk: "low" | "medium" | "high";
  approvals_needed: number;
}

export interface ApprovalVote { index: number; by: string; action: "approve" | "edit" | "reject"; at: string }

export interface VerificationResult {
  check: number;
  at: string;
  action: string;
  status: "fixed" | "not_fixed" | "inconclusive";
  errors_before: number;
  errors_after: number;
  minutes_before: number;
  minutes_after: number;
  summary: string;
}

export interface ActionDecision {
  index: number;
  action: DecisionAction;
  payload?: Record<string, string>;
}

export interface ActionOutcome {
  index: number;
  kind: string;
  decision: DecisionAction | "none";
  status: "executed" | "dry_run" | "skipped" | "failed";
  detail: string;
}

export interface InvestigationPlan {
  hypotheses: string[];
  evidence_task: string;
  history_task: string;
}

export interface ReviewVerdict {
  approved: boolean;
  unsupported_claims: string[];
  recheck: string[];
  instructions: string;
}

export interface AgentRun {
  id: string;
  case_id: string;
  model: string;
  status: AgentRunStatus;
  stage: string;
  trigger: AgentRunTrigger;
  queue_position: number | null;
  started_by: string;
  created_at: string;
  updated_at: string;
  dry_run: boolean;
  plan: InvestigationPlan | null;
  findings: Record<string, string>;
  reviews: ReviewVerdict[];
  analysis: SupportAnalysis | null;
  severity_note: string;
  warnings: string[];
  steps: AgentStep[];
  proposals: ProposedAction[];
  outcomes: ActionOutcome[];
  lessons_used: string[];
  llm_calls: LLMCall[];
  spans: AgentSpan[];
  decided_by: string;
  error: string;
  approvals: ApprovalVote[];
  verifications: VerificationResult[];
  team: string[];
}

export interface AgentLesson {
  id: string;
  case_id: string;
  run_id: string;
  service: string;
  case_title: string;
  verdict: "correct" | "partly_correct" | "wrong";
  agent_cause: string;
  actual_resolution: string;
  lesson: string;
  created_at: string;
}

export interface LLMCall {
  agent: string;
  started_at: string;
  duration_ms: number;
  input_tokens: number;
  output_tokens: number;
  ok: boolean;
  model?: string;
}

export interface AgentSpan {
  agent: string;
  started_at: string;
  duration_ms: number;
}

export interface AgentUsage {
  agent: string;
  runs: number;
  avg_seconds: number;
  avg_llm_calls: number;
  avg_input_tokens: number;
  avg_output_tokens: number;
}

export interface AgentMetrics {
  runs_total: number;
  by_status: Record<string, number>;
  by_trigger: Record<string, number>;
  median_investigation_seconds: number | null;
  p90_investigation_seconds: number | null;
  avg_input_tokens: number | null;
  avg_output_tokens: number | null;
  avg_tool_calls: number | null;
  tool_failure_rate: number | null;
  reviewer_rejection_rate: number | null;
  decisions: Record<string, number>;
  lesson_verdicts: Record<string, number>;
  per_agent: AgentUsage[];
}

export interface LiveState {
  active_runs: Record<string, { status: AgentRunStatus; stage: string }>;
  case_ids: string[];
  alert_cases: { id: string; title: string; service: string; created_at: string }[];
  backup_calls: number;
}

export type DashboardTrend = Record<"awaiting_approval" | "open" | "urgent_open" | "at_risk" | "resolved", number[]>;

export type ProviderId = "gemini" | "anthropic" | "openai" | "xai" | "groq";

export interface ModelProvider {
  id: ProviderId;
  name: string;
  maker: string;
  keys_url: string;
  usage_url: string;
  connected: boolean;
  key_hint: string;
  key_source: "page" | "env" | "";
  models: string[];
  selected_model: string;
  tokens_today: number;
  daily_budget: number;
  paused: string;
}

export interface ModelOverview {
  active: { provider: ProviderId | "ollama"; model: string; label: string; paused: string };
  backup: { provider: "ollama"; model: string; tokens_today: number };
  providers: ModelProvider[];
  routing: {
    custom: boolean;
    local_only: boolean;
    tiers: { tier: "complex" | "standard" | "simple"; about: string; agents: string[]; chain: string[];
             models: { provider: ProviderId | "ollama"; model: string; paused: boolean }[];
             skipped: { provider: ProviderId; reason: string }[] }[];
    budgets: { provider: ProviderId; model: string; budget: number; used: number }[];
  };
}

export interface ModelCheck {
  name: string;
  ok: boolean;
  detail: string;
  ms: number;
}

export interface ModelCheckResult {
  model: string;
  ok: boolean;
  checks: ModelCheck[];
}

export interface UserAccount {
  username: string;
  display_name: string;
  email: string;
  role: Role;
  auth: "password" | "sso";
  disabled: boolean;
  created_at: string;
  last_login: string | null;
  signed_in: boolean;
  protected: boolean;
  locked: boolean;
}
