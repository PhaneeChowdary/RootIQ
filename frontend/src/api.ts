import type {
  ActionDecision,
  AgentLesson,
  AgentMetrics,
  AgentRun,
  AppState,
  AuditEvent,
  AuthUser,
  CaseCreate,
  CaseMessage,
  Health,
  JiraSyncResult,
  DashboardTrend,
  LiveState,
  LoginResponse,
  UserAccount,
  Role,
  ModelCheckResult,
  ModelOverview,
  MemoryStatus,
  OperationsDashboard,
  ProblemGroup,
  RelatedCase,
  RunbookRecommendation,
  ServiceTrend,
  Severity,
  SupportAnalysis,
  SupportCase,
} from "./types";

let authenticated = false;

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers);
  const response = await fetch(url, { ...options, headers, credentials: "include" });
  if (response.status === 401 && authenticated && url !== "/api/auth/login") {
    authenticated = false;
    window.dispatchEvent(new Event("rootiq:unauthorized"));
  }
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(payload?.detail ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export async function login(username: string, password: string): Promise<LoginResponse> {
  const result = await request<LoginResponse>("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  authenticated = true;
  return result;
}

export async function getCurrentUser(): Promise<AuthUser> {
  const user = await request<AuthUser>("/api/auth/me");
  authenticated = true;
  return user;
}

export async function logout(): Promise<void> {
  const response = await fetch("/api/auth/logout", { method: "POST", credentials: "include" });
  authenticated = false;
  if (!response.ok) throw new Error("Unable to sign out.");
}

export function getAppState(): Promise<AppState> {
  return request<AppState>("/api/state");
}

export function getAuditEvents(): Promise<AuditEvent[]> {
  return request<AuditEvent[]>("/api/audit");
}

export function getHealth(): Promise<Health> {
  return request<Health>("/api/health");
}

export function listCases(): Promise<SupportCase[]> {
  return request<SupportCase[]>("/api/cases");
}

export function getDashboard(): Promise<OperationsDashboard> {
  return request<OperationsDashboard>("/api/dashboard");
}

export function getServiceTrends(): Promise<ServiceTrend[]> {
  return request<ServiceTrend[]>("/api/trends/services");
}

export function getProblemGroups(): Promise<ProblemGroup[]> {
  return request<ProblemGroup[]>("/api/problems");
}

export function searchCases(query: string, status: string): Promise<SupportCase[]> {
  const parameters = new URLSearchParams();
  if (query.trim()) parameters.set("q", query.trim());
  if (status) parameters.set("status", status);
  return request<SupportCase[]>(`/api/cases/search?${parameters.toString()}`);
}

export function createCase(payload: CaseCreate): Promise<SupportCase> {
  return request<SupportCase>("/api/cases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function deleteCase(caseId: string): Promise<void> {
  const response = await fetch(`/api/cases/${caseId}`, { method: "DELETE", credentials: "include" });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(payload?.detail ?? "Unable to delete support case.");
  }
}

export function analyzeCase(caseId: string): Promise<SupportCase> {
  return request<SupportCase>(`/api/cases/${caseId}/analyze`, { method: "POST" });
}

export function saveReview(
  caseId: string,
  analysis: SupportAnalysis,
  approve: boolean,
): Promise<SupportCase> {
  return request<SupportCase>(`/api/cases/${caseId}/review`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ analysis, approve }),
  });
}

export function resolveCase(
  caseId: string,
  resolution: string,
  preventiveActions: string[],
): Promise<SupportCase> {
  return request<SupportCase>(`/api/cases/${caseId}/resolve`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resolution, preventive_actions: preventiveActions }),
  });
}

export function acknowledgeCase(caseId: string): Promise<SupportCase> {
  return request<SupportCase>(`/api/cases/${caseId}/acknowledge`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note: "Initial response acknowledged with customer." }),
  });
}

export function getMemoryStatus(): Promise<MemoryStatus> {
  return request<MemoryStatus>("/api/memory/status");
}

export function indexCaseMemory(caseId: string): Promise<MemoryStatus> {
  return request<MemoryStatus>(`/api/cases/${caseId}/memory/index`, { method: "POST" });
}

export function getRelatedCases(caseId: string): Promise<RelatedCase[]> {
  return request<RelatedCase[]>(`/api/cases/${caseId}/related`);
}

export function generateRunbook(caseId: string): Promise<RunbookRecommendation> {
  return request<RunbookRecommendation>(`/api/cases/${caseId}/runbook`, { method: "POST" });
}

export async function downloadReport(caseId: string, filename: string): Promise<void> {
  const response = await fetch(`/api/cases/${caseId}/report.md`, { credentials: "include" });
  if (!response.ok) throw new Error("Unable to download incident report.");
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

export function createJiraEscalation(caseId: string): Promise<SupportCase> {
  return request<SupportCase>(`/api/cases/${caseId}/jira`, { method: "POST" });
}

export function createGmailDraft(caseId: string): Promise<SupportCase> {
  return request<SupportCase>(`/api/cases/${caseId}/gmail-draft`, { method: "POST" });
}

export function getJiraPending(): Promise<{ count: number }> {
  return request<{ count: number }>("/api/jira/pending");
}

export function getCase(caseId: string): Promise<SupportCase> {
  return request<SupportCase>(`/api/cases/${caseId}`);
}

export function syncJiraTickets(): Promise<JiraSyncResult> {
  return request<JiraSyncResult>("/api/jira/sync", { method: "POST" });
}

export function getKBSuggestions(caseId: string): Promise<RelatedCase[]> {
  return request<RelatedCase[]>(`/api/cases/${caseId}/suggestions`);
}

export function getMessageCounts(): Promise<Record<string, number>> {
  return request<Record<string, number>>("/api/messages/counts");
}

export function getMessages(caseId: string): Promise<CaseMessage[]> {
  return request<CaseMessage[]>(`/api/cases/${caseId}/messages`);
}

export function sendMessage(
  caseId: string,
  content: string,
  messageType: "internal" | "customer" = "internal",
): Promise<CaseMessage> {
  return request<CaseMessage>(`/api/cases/${caseId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, message_type: messageType }),
  });
}

export function savePreference(key: string, value: string | null): Promise<Record<string, string>> {
  return request<Record<string, string>>("/api/preferences", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, value }),
  });
}

export function startAgentRun(caseId: string, replace = false): Promise<AgentRun> {
  return request<AgentRun>(`/api/cases/${caseId}/agent-runs${replace ? "?replace=true" : ""}`, { method: "POST" });
}

export function stopAgentRun(runId: string): Promise<AgentRun> {
  return request<AgentRun>(`/api/agent-runs/${runId}/stop`, { method: "POST" });
}

export function listAgentRuns(caseId: string): Promise<AgentRun[]> {
  return request<AgentRun[]>(`/api/cases/${caseId}/agent-runs`);
}

export function getAgentRun(runId: string): Promise<AgentRun> {
  return request<AgentRun>(`/api/agent-runs/${runId}`);
}

export function decideAgentRun(runId: string, decisions: ActionDecision[]): Promise<AgentRun> {
  return request<AgentRun>(`/api/agent-runs/${runId}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decisions }),
  });
}

export function getCaseLesson(caseId: string): Promise<AgentLesson | null> {
  return request<AgentLesson | null>(`/api/cases/${caseId}/agent-lesson`);
}

export function getAgentMetrics(): Promise<AgentMetrics> {
  return request<AgentMetrics>("/api/agent-metrics");
}

export function getModels(): Promise<ModelOverview> {
  return request<ModelOverview>("/api/models");
}

export function saveModelKey(provider: string, apiKey: string): Promise<ModelOverview> {
  return request<ModelOverview>(`/api/models/${provider}/key`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ api_key: apiKey }),
  });
}

export function removeModelKey(provider: string): Promise<ModelOverview> {
  return request<ModelOverview>(`/api/models/${provider}/key`, { method: "DELETE" });
}

export function refreshModels(provider: string): Promise<ModelOverview> {
  return request<ModelOverview>(`/api/models/${provider}/refresh`, { method: "POST" });
}

export function selectModel(provider: string, model: string): Promise<ModelOverview> {
  return request<ModelOverview>(`/api/models/${provider}/selected`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model }),
  });
}

export function checkModel(provider: string, model: string): Promise<ModelCheckResult> {
  return request<ModelCheckResult>(`/api/models/${provider}/check`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model }),
  });
}

export function setActiveModel(provider: string, model: string): Promise<ModelOverview> {
  return request<ModelOverview>("/api/models/active", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider, model }),
  });
}

export function getLive(): Promise<LiveState> {
  return request<LiveState>("/api/live");
}

export function getDashboardTrend(): Promise<DashboardTrend> {
  return request<DashboardTrend>("/api/dashboard/trend");
}

export interface BinItem { id: string; title: string; service: string; severity: Severity; status: string; deleted_at: string; deleted_by: string }

export function getBin(): Promise<BinItem[]> {
  return request<BinItem[]>("/api/bin");
}

export function restoreFromBin(caseId: string): Promise<SupportCase> {
  return request<SupportCase>(`/api/bin/${caseId}/restore`, { method: "POST" });
}

export async function deleteForever(caseId: string): Promise<void> {
  const response = await fetch(`/api/bin/${caseId}`, { method: "DELETE", credentials: "include" });
  if (!response.ok) throw new Error("Unable to delete the case forever.");
}

export interface SourceField { name: string; label: string; secret: boolean }
export interface SourceKind { type: string; config: Record<string, string>; saved_secrets: string[]; types: Record<string, SourceField[]> }
export type SourcesOverview = Record<"logs" | "metrics" | "changes" | "code" | "database", SourceKind>;

export function getSources(): Promise<SourcesOverview> {
  return request<SourcesOverview>("/api/sources");
}

export function testSource(kind: string, type: string, config: Record<string, string>): Promise<{ ok: boolean; detail: string }> {
  return request(`/api/sources/${kind}/test`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type, config }) });
}

export function saveSource(kind: string, type: string, config: Record<string, string>): Promise<SourcesOverview & { detail: string }> {
  return request(`/api/sources/${kind}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type, config }) });
}

export function getAuthConfig(): Promise<{ sso: boolean; sso_name: string }> {
  return request("/api/auth/config");
}

export function changePassword(current: string, next: string): Promise<void> {
  return request("/api/auth/password", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ current, new: next }) });
}

export function listUsers(): Promise<UserAccount[]> {
  return request<UserAccount[]>("/api/users");
}

export function createUser(user: { username: string; display_name: string; email: string; role: Role; password: string }): Promise<UserAccount[]> {
  return request("/api/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(user) });
}

export function updateUser(username: string, change: { role?: Role; disabled?: boolean; password?: string; display_name?: string }): Promise<UserAccount[]> {
  return request(`/api/users/${encodeURIComponent(username)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(change) });
}

export function removeUser(username: string): Promise<UserAccount[]> {
  return request(`/api/users/${encodeURIComponent(username)}`, { method: "DELETE" });
}

export function unlockUser(username: string): Promise<UserAccount[]> {
  return request(`/api/users/${encodeURIComponent(username)}/unlock`, { method: "POST" });
}

export interface SecurityOverview {
  masking: { mode: string; cloud_model: boolean; model: string; masked: Record<string, number> };
  encryption: { key_from_env: boolean; key_file: string; model_keys_encrypted: boolean };
  sign_in: { max_failures: number; lock_minutes: number; session_hours: number; sso: boolean; cookie_secure: boolean; locked: { key: string; locked_until: string }[] };
  retention: { bin_days: number; resolved_days: number; audit_days: number; due: { bin: number; resolved: number; audit: number } };
  origins: string[];
  backups: { count: number; every_hours: number; keep: number; latest: string | null; latest_name: string; latest_kb: number; folder: string; key_from_env: boolean };
  system: {
    database: string; database_where: string; schema_version: number; schema_latest: number; queued_jobs: number;
    api_threads: number; external_workers: boolean;
    workers: { id: string; host: string; threads: number; started_at: string; heartbeat_at: string; jobs: number }[];
  };
}

export function runBackup(): Promise<SecurityOverview> {
  return request("/api/security/backup", { method: "POST" });
}

export function getSecurity(): Promise<SecurityOverview> {
  return request<SecurityOverview>("/api/security");
}

export function runCleanup(): Promise<SecurityOverview & { deleted: { bin: number; resolved: number; audit: number } }> {
  return request("/api/security/cleanup", { method: "POST" });
}

export function saveRoutes(routes: Record<string, string[]>): Promise<ModelOverview> {
  return request("/api/models/routes", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ routes }) });
}

export function resetRoutes(): Promise<ModelOverview> {
  return request("/api/models/routes", { method: "DELETE" });
}

export interface AutomationOverview {
  paused: boolean;
  remediation: boolean;
  modes: Record<string, "auto" | "one" | "two">;
  limits: { per_case_per_hour: number; per_hour: number };
  verify: { first_check_seconds: number; checks: number; auto_resolve: boolean; follow_up_runs: number };
  runner: { type: string; config: Record<string, string>; saved_secrets: string[] };
  runner_types: Record<string, { name: string; label: string; secret: boolean }[]>;
  actions: { id: string; name: string; risk: "low" | "medium" | "high"; remediation: boolean }[];
  dry_run: boolean;
}

export function getAutomation(): Promise<AutomationOverview> {
  return request<AutomationOverview>("/api/automation");
}

export function saveAutomation(body: object): Promise<AutomationOverview> {
  return request("/api/automation", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

export function testAutomationRunner(): Promise<{ ok: boolean; detail: string }> {
  return request("/api/automation/test", { method: "POST" });
}

export interface RatingSummary { ratings: { by: string; verdict: "correct" | "partly_correct" | "wrong"; comment: string; at: string }[] }

export function getRunRatings(runId: string): Promise<RatingSummary> {
  return request(`/api/agent-runs/${runId}/rating`);
}

export function rateRun(runId: string, verdict: string): Promise<RatingSummary> {
  return request(`/api/agent-runs/${runId}/rating`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ verdict }) });
}

export interface PostmortemData {
  case_id: string; title: string; summary: string; impact: string; root_cause: string; trigger: string; detection: string;
  resolution: string; went_well: string[]; went_poorly: string[];
  action_items: { item: string; priority: "P1" | "P2" | "P3"; owner: string }[];
  timeline: { at: string; event: string; source: string }[];
  durations: Record<string, number>; warnings: string[]; model: string; created_at: string;
}

export function getPostmortem(caseId: string): Promise<{ postmortem: PostmortemData | null; pending: boolean }> {
  return request(`/api/cases/${caseId}/postmortem`);
}

export function regeneratePostmortem(caseId: string): Promise<{ pending: boolean }> {
  return request(`/api/cases/${caseId}/postmortem`, { method: "POST" });
}

export interface RatingStats {
  days: number; count: number; score: number | null;
  counts: { correct: number; partly_correct: number; wrong: number };
  weeks: { week: string; score: number; count: number }[];
}

export function getRatingSummary(days = 30): Promise<RatingStats> {
  return request(`/api/agent-ratings?days=${days}`);
}

export interface IncidentLinkData {
  case_id: string;
  status: "suggested" | "confirmed";
  source: "rule" | "agent" | "person";
  reason: string;
  evidence: string[];
  confidence: "high" | "medium";
  linked_at: string;
  linked_by: string;
}

export interface IncidentGroupCase {
  id: string; title: string; service: string; customer: string; status: string; severity: string; created_at: string; likely_cause: string;
}

export interface IncidentGroupData {
  group: { id: string; primary_case_id: string; cause: string; links: IncidentLinkData[] } | null;
  cases: Record<string, IncidentGroupCase>;
  pending: boolean;
}

export interface IncidentGroupMark { id: string; primary_case_id: string; cause: string; links: { case_id: string; status: string; source: string }[] }

export function listIncidentGroups(): Promise<IncidentGroupMark[]> {
  return request("/api/incident-groups");
}

export function getIncidentGroup(caseId: string): Promise<IncidentGroupData> {
  return request(`/api/cases/${caseId}/incident-group`);
}

export function decideIncidentLink(caseId: string, action: "confirm" | "dismiss" | "primary" | "ungroup"): Promise<IncidentGroupData> {
  return request(`/api/cases/${caseId}/incident-group`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action }) });
}

export function checkIncidentGroup(caseId: string): Promise<{ pending: boolean }> {
  return request(`/api/cases/${caseId}/incident-group/check`, { method: "POST" });
}

export function resolveLinkedCases(caseId: string): Promise<{ resolved: string[] }> {
  return request(`/api/cases/${caseId}/incident-group/resolve-linked`, { method: "POST" });
}
