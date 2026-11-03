import { clockTime, dateTime, dayLabel, setTimeZone, shortDateTime } from "./time";
import { type CSSProperties, type FormEvent, Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgentMetricsCard } from "./AgentObservability";
import { AgentPanel } from "./AgentPanel";
import { ModelsPage } from "./ModelsPage";
import { RoleContext, UserNameContext, useCan } from "./role";
import { UsersPage } from "./UsersPage";
import { SecurityPage } from "./SecurityPage";
import { AutomationPage } from "./AutomationPage";
import { PostmortemCard } from "./PostmortemCard";
import { RelatedIncidents } from "./RelatedIncidents";
import { Warp } from "./Warp";
import { RatingsCard } from "./RatingsCard";
import { SourcesPage } from "./SourcesPage";
import { CountUp, useSlidingIndicator, useScrollMore } from "./motion";
import { AlertToasts, type Notice, Portal, QuickAnalysisProgress, ResolveProgress, Sparkline, type Toast, useFresh } from "./live";
import { readRoute, writeRoute } from "./route";
import { ActionIcon, BrandMark, type IconKind } from "./icons";
import {
  analyzeCase,
  acknowledgeCase,
  createCase,
  createGmailDraft,
  deleteCase,
  downloadReport,
  generateRunbook,
  getAppState,
  getAuditEvents,
  getCase,
  getCurrentUser,
  getHealth,
  getDashboard,
  getJiraPending,
  getMessageCounts,
  getMessages,
  getProblemGroups,
  getRelatedCases,
  getServiceTrends,
  indexCaseMemory,
  login,
  logout,
  resolveCase,
  savePreference,
  saveReview,
  searchCases,
  getLive,
  getBin,
  restoreFromBin,
  deleteForever,
  type BinItem,
  getDashboardTrend,
  sendMessage,
  syncJiraTickets,
  changePassword,
  getAuthConfig, listIncidentGroups, type IncidentGroupMark } from "./api";
import type {
  AuditEvent,
  DashboardTrend,
  LiveState,
  AuthUser,
  CaseCreate,
  CaseMessage,
  Health,
  OperationsDashboard,
  ProblemGroup,
  RelatedCase,
  RunbookRecommendation,
  ServiceTrend,
  Severity,
  SupportAnalysis,
  SupportCase,
} from "./types";

const STATUS_LABELS = {
  new: "Not investigated",
  investigating: "Investigating",
  awaiting_approval: "Awaiting approval",
  approved: "Approved / active",
  resolved: "Resolved",
};

const EMPTY_CASE: CaseCreate = {
  title: "",
  customer: "",
  service: "",
  environment: "production",
  reported_issue: "",
  logs: "",
  severity: "medium",
};

type ConfirmAction = {
  title: string;
  message: string;
  confirmLabel: string;
  onConfirm: () => void;
};

const TAB_ICONS: Record<DetailView, IconKind> = {
  overview: "dashboard",
  agent: "spark",
  analysis: "file",
  publish: "send",
  report: "report",
  similar: "layers",
  messages: "message",
  runbook: "book",
};

const DETAIL_VIEWS = ["overview", "analysis", "publish", "report", "similar", "messages", "runbook", "agent"] as const;
const PAGES = ["dashboard", "insights", "settings", "about"] as const;
const routedPage = (): Page => (PAGES as readonly string[]).includes(readRoute().page) ? readRoute().page as Page : "dashboard";

type DetailView =
  | "overview"
  | "analysis"
  | "publish"
  | "report"
  | "similar"
  | "messages"
  | "runbook"
  | "agent";

function App() {
  useEffect(() => {  // Escape closes the top window, as its close button does
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const windows = document.querySelectorAll<HTMLElement>(".modal-backdrop");
      windows[windows.length - 1]?.querySelector<HTMLButtonElement>(".modal-close")?.click();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const [page, setPage] = useState<Page>(routedPage);
  useEffect(() => { if (readRoute().page !== page) writeRoute(page); }, [page]);  // the page in the address
  useEffect(() => {  // Back and Forward of the browser
    const onPop = () => setPage(routedPage());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const canWork = !!user && user.role !== "viewer";
  useEffect(() => { if (page === "settings" && user && user.role !== "admin") setPage("dashboard"); }, [page, user]);
  const [health, setHealth] = useState<Health | null>(null);
  const [cases, setCases] = useState<SupportCase[]>([]);
  const [groupMarks, setGroupMarks] = useState<IncidentGroupMark[]>([]);
  const [selected, setSelected] = useState<SupportCase | null>(null);
  const selectedRef = useRef<SupportCase | null>(null);
  const [related, setRelated] = useState<RelatedCase[]>([]);
  const [dashboard, setDashboard] = useState<OperationsDashboard | null>(null);
  const [trends, setTrends] = useState<ServiceTrend[]>([]);
  const [problems, setProblems] = useState<ProblemGroup[]>([]);
  const [runbook, setRunbook] = useState<RunbookRecommendation | null>(null);
  const [audit, setAudit] = useState<AuditEvent[]>([]);
  const [messages, setMessages] = useState<CaseMessage[]>([]);
  const [jiraPending, setJiraPending] = useState(0);
  const [msgCounts, setMsgCounts] = useState<Record<string, number>>({});
  const [readCounts, setReadCounts] = useState<Record<string, number>>({});
  const readCountsRef = useRef(readCounts);
  readCountsRef.current = readCounts;
  // Read messages are saved for the user ("read:<case id>" = the number read), so "new" stays correct after a reload.
  const markRead = useCallback((caseId: string, count: number) => {
    if ((readCountsRef.current[caseId] ?? -1) === count) return;
    readCountsRef.current = { ...readCountsRef.current, [caseId]: count };
    setReadCounts((prev) => ({ ...prev, [caseId]: count }));
    void savePreference(`read:${caseId}`, String(count)).catch(() => {});
  }, []);
  // How many messages were new when each case opened. Opening a case marks them read; this keeps them findable.
  const [unreadOnOpen, setUnreadOnOpen] = useState<Record<string, number>>({});
  const [highlightId, setHighlightId] = useState<string | null>(null);
  const [activeTasks, setActiveTasks] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [notices, setNotices] = useState<Notice[]>([]);
  // A success message becomes a toast at the bottom right . Errors stay a window. At most three at a time.
  useEffect(() => {
    if (!notice) return;
    setNotices((current) => [...current.slice(-2), { id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, text: notice }]);
    setNotice("");
  }, [notice]);
  const [confirmLogout, setConfirmLogout] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<SupportCase | null>(null);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);
  const searchSeq = useRef(0);  // live search: only the newest request may update the list
  const [live, setLive] = useState<LiveState | null>(null);
  const [trend, setTrend] = useState<DashboardTrend | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [justResolved, setJustResolved] = useState<string | null>(null);
  const [bin, setBin] = useState<BinItem[]>([]);
  const [binConfirm, setBinConfirm] = useState<null | { kind: "forever"; item: BinItem } | { kind: "empty" }>(null);
  const seenAlerts = useRef<Set<string> | null>(null);
  const knownCases = useRef<Set<string>>(new Set());
  // Live check every 4 s: agents working per case, new cases , backup use. For example from a Grafana alert.
  useEffect(() => {
    if (!user) return;
    let stopped = false;
    const tick = async () => {
      try {
        const next = await getLive();
        if (stopped) return;
        setLive(next);
        if (seenAlerts.current === null) seenAlerts.current = new Set(next.alert_cases.map((a) => a.id));
        const newAlerts = next.alert_cases.filter((a) => !seenAlerts.current!.has(a.id));
        newAlerts.forEach((a) => seenAlerts.current!.add(a.id));
        if (newAlerts.length) setToasts((current) => [...current, ...newAlerts.map((a) => ({ id: a.id, caseId: a.id, title: a.title, service: a.service }))]);
        const unknown = next.case_ids.filter((id) => !knownCases.current.has(id));
        if (unknown.length && knownCases.current.size) {
          const state = await getAppState();  // a case came from outside this page: load it, keep the open case
          setCases(state.cases);
          setDashboard(state.dashboard);
          setHighlightId(unknown[0]);
          window.setTimeout(() => setHighlightId(null), 2400);
        }
      } catch { /* the next tick tries again */ }
    };
    void tick();
    const timer = window.setInterval(tick, 4000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [user]);
  useEffect(() => { knownCases.current = new Set(cases.map((c) => c.id)); }, [cases]);
  // The recycle bin: load it with the cases.
  useEffect(() => { if (user) getBin().then(setBin).catch(() => {}); }, [user, cases.length]);

  // The small line charts in the counter tiles: load them with the counters.
  useEffect(() => {
    if (!user) return;
    getDashboardTrend().then(setTrend).catch(() => {});
  }, [user, dashboard]);

  // The Insights page shows fresh numbers and audit events each time it opens.
  useEffect(() => {
    if (page !== "insights" || !user) return;
    Promise.all([getServiceTrends(), getProblemGroups(), getAuditEvents()])
      .then(([nextTrends, nextProblems, nextAudit]) => { setTrends(nextTrends); setProblems(nextProblems); setAudit(nextAudit); })
      .catch(() => {});
  }, [page]);
  const [showNewCase, setShowNewCase] = useState(false);

  const caseIds = cases.map((c) => c.id + c.status).join();
  useEffect(() => { if (caseIds) listIncidentGroups().then(setGroupMarks).catch(() => undefined); }, [caseIds]);
  const links = useMemo(() => {
    const out: Record<string, { primary: boolean; others: number; suggested: boolean }> = {};
    for (const g of groupMarks) for (const l of g.links) out[l.case_id] = { primary: l.case_id === g.primary_case_id, others: g.links.length - 1, suggested: l.status === "suggested" };
    return out;
  }, [groupMarks]);

  function selectCase(caseItem: SupportCase | null, keepTab = false) {
    // another case opens on Overview: its tab leaves the address before the case view is made . Keyed by the case.
    // After a reload, the saved case keeps the tab in the address.
    if (!keepTab && caseItem?.id !== selectedRef.current?.id && readRoute().page === "dashboard") writeRoute("dashboard", "", true);
    selectedRef.current = caseItem;
    setSelected(caseItem);
    void savePreference("selected_case_id", caseItem?.id ?? null);
  }

  function startTask(key: string) { setActiveTasks(prev => new Set([...prev, key])); }
  function endTask(key: string) { setActiveTasks(prev => { const next = new Set(prev); next.delete(key); return next; }); }
  function isActive(key: string) { return activeTasks.has(key); }

  function updateCaseInPlace(updated: SupportCase) {
    setCases(current => current.map(c => c.id === updated.id ? updated : c));
    setSelected(current => current?.id === updated.id ? updated : current);
  }

  useEffect(() => {
    void getCurrentUser()
      .then(setUser)
      .catch(() => {})
      .finally(() => setAuthReady(true));
  }, []);

  useEffect(() => {
    function handleUnauthorized() {
      setUser(null);
      setCases([]);
      setSelected(null);
      setAudit([]);
      setError("Your session has expired. Sign in again.");
    }
    window.addEventListener("rootiq:unauthorized", handleUnauthorized);
    return () => window.removeEventListener("rootiq:unauthorized", handleUnauthorized);
  }, []);

  useEffect(() => {
    if (!user) return;
    void getAppState()
      .then((state) => {
        setTimeZone(state.health.time_zone);
        setHealth(state.health);
        setDashboard(state.dashboard);
        setTrends(state.trends);
        setProblems(state.problems);
        setAudit(state.audit);
        setCases(state.cases);
        const saved = Object.entries(state.preferences).filter(([key]) => key.startsWith("read:"))
          .map(([key, value]) => [key.slice(5), Number(value) || 0] as const);
        readCountsRef.current = Object.fromEntries(saved);
        setReadCounts(readCountsRef.current);
        const selectedId = state.preferences.selected_case_id;
        selectCase(state.cases.find((caseItem) => caseItem.id === selectedId) ?? state.cases[0] ?? null, true);
        if (state.health.jira_configured) void getJiraPending().then((r) => setJiraPending(r.count)).catch(() => {});
        void getMessageCounts().then(setMsgCounts).catch(() => {});
      })
      .catch((caught: unknown) => setError(readError(caught)));
  }, [user?.username]);

  useEffect(() => {
    if (!user || !health?.jira_configured) return;
    const interval = setInterval(() => {
      void getJiraPending().then((r) => setJiraPending(r.count)).catch(() => {});
      void getMessageCounts().then(setMsgCounts).catch(() => {});
    }, 180000);
    return () => clearInterval(interval);
  }, [user?.username, health?.jira_configured]);

  useEffect(() => {
    if (!selected) {
      setRunbook(null);
      setRelated([]);
      setMessages([]);
      return;
    }
    setRunbook(selected.runbook ?? null);
    void getRelatedCases(selected.id).then(setRelated).catch(() => setRelated([]));
    void getMessages(selected.id).then((msgs) => {
      setMessages(msgs);
      const unread = Math.max(0, msgs.length - (readCountsRef.current[selected.id] ?? 0));
      setUnreadOnOpen((prev) => ({ ...prev, [selected.id]: unread }));
      markRead(selected.id, msgs.length);
    }).catch(() => setMessages([]));
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${protocol}//${window.location.host}/api/cases/${selected.id}/ws`);
    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data) as CaseMessage;
      setMessages((prev) => {
        if (prev.some((m) => m.id === msg.id)) return prev;  // already added from the send response
        const next = [...prev, msg];
        markRead(selected.id, next.length);
        setMsgCounts((mc) => ({ ...mc, [msg.case_id]: next.length }));
        return next;
      });
    };
    return () => ws.close();
  }, [selected?.id]);

  function replaceCase(nextCase: SupportCase) {
    selectCase(nextCase);
    setCases((current) => [nextCase, ...current.filter((entry) => entry.id !== nextCase.id)]);
  }

  async function refreshDashboardData() {
    const state = await getAppState();
    setHealth(state.health);
    setDashboard(state.dashboard);
    setTrends(state.trends);
    setProblems(state.problems);
    setAudit(state.audit);
    setCases(state.cases);
    selectCase(state.cases[0] ?? null);
  }

  async function runInline(taskKey: string, action: () => Promise<SupportCase>, success?: string): Promise<SupportCase | null> {
    startTask(taskKey);
    setError("");
    let updated: SupportCase | null = null;
    try {
      updated = await action();
      replaceCase(updated);
      if (success) setNotice(success);
      const [nextHealth, nextDashboard, nextTrends, nextProblems, nextRelated, nextAudit] = await Promise.all([
        getHealth(), getDashboard(), getServiceTrends(), getProblemGroups(), getRelatedCases(updated.id), getAuditEvents(),
      ]);
      setHealth(nextHealth);
      setDashboard(nextDashboard);
      setTrends(nextTrends);
      setProblems(nextProblems);
      setRelated(nextRelated);
      setAudit(nextAudit);
    } catch (caught: unknown) {
      setError(readError(caught));
    } finally {
      endTask(taskKey);
    }
    return updated;
  }

  function runBackground(taskKey: string, action: () => Promise<SupportCase>, success: string) {
    startTask(taskKey);
    action()
      .then(async (updated) => {
        updateCaseInPlace(updated);
        if (updated.runbook) setRunbook(prev => updated.runbook ?? prev);
        setHighlightId(updated.id);
        setTimeout(() => setHighlightId(null), 2000);
        setNotice(success);
        const [d, a] = await Promise.all([getDashboard(), getAuditEvents()]);
        setDashboard(d);
        setAudit(a);
      })
      .catch((caught: unknown) => setError(readError(caught)))
      .finally(() => endTask(taskKey));
  }

  function awaitAnalysis(caseIds: string[]) {
    const pending = new Set(caseIds);
    let analyzed = 0;
    const poll = setInterval(async () => {
      for (const id of [...pending]) {
        try {
          const updated = await getCase(id);
          if (updated.status !== "new" && updated.status !== "investigating") {
            pending.delete(id);
            analyzed += 1;
            updateCaseInPlace(updated);
            setHighlightId(updated.id);
            setTimeout(() => setHighlightId(null), 2000);
          }
        } catch { pending.delete(id); }  // deleted while waiting: nothing to report
      }
      if (pending.size === 0) {
        clearInterval(poll);
        if (analyzed === 0) return;
        const [h, d, a] = await Promise.all([getHealth(), getDashboard(), getAuditEvents()]);
        setHealth(h); setDashboard(d); setAudit(a);
        setNotice("AI analysis complete. Ready for review.");
      }
    }, 2000);
    setTimeout(() => clearInterval(poll), 15 * 60 * 1000);  // an agent team run can take several minutes
  }

  async function signIn(username: string, password: string) {
    startTask("login");
    setError("");
    try {
      const session = await login(username, password);
      setUser(session.user);
      setNotice("");
    } catch (caught: unknown) {
      setError(readError(caught));
    } finally {
      endTask("login");
    }
  }

  async function signOut() {
    startTask("logout");
    setConfirmLogout(false);
    try {
      await logout();
      setNotice("");
      setError("");
    } catch (caught: unknown) {
      setError(readError(caught));
    } finally {
      setUser(null);
      setPage("dashboard");
      setCases([]);
      setSelected(null);
      setAudit([]);
      endTask("logout");
    }
  }

  function requestSignOut() {
    setError("");
    setNotice("");
    setConfirmLogout(true);
  }

  async function confirmDeleteCase() {
    if (!deleteTarget) return;
    startTask("delete");
    setError("");
    try {
      await deleteCase(deleteTarget.id);
      const state = await getAppState();
      setCases(state.cases);
      selectCase(selected?.id === deleteTarget.id ? state.cases[0] ?? null : selected);
      setHealth(state.health);
      setDashboard(state.dashboard);
      setTrends(state.trends);
      setProblems(state.problems);
      setAudit(state.audit);
      setRelated([]);
      setRunbook(null);
      setNotice("Case moved to the recycle bin.");
      getBin().then(setBin).catch(() => {});
      setDeleteTarget(null);
    } catch (caught: unknown) {
      setError(readError(caught));
    } finally {
      endTask("delete");
    }
  }

  if (!authReady) return <div className="splash">Checking local session...</div>;
  if (page === "about") {
    return (
      <>
        <AboutPage user={user} busy={isActive("logout")} onNavigate={setPage} onSignOut={requestSignOut} />
        {error ? <StatusModal kind="error" message={error} onClose={() => setError("")} /> : null}
        {confirmLogout ? <LogoutModal busy={isActive("logout")} onCancel={() => setConfirmLogout(false)} onConfirm={signOut} /> : null}
      </>
    );
  }
  if (!user) return <LoginScreen busy={isActive("login")} error={error} onAbout={() => setPage("about")} onDismissError={() => setError("")} onLogin={signIn} />;

  return (
    <RoleContext.Provider value={user?.role ?? "viewer"}>
    <UserNameContext.Provider value={user?.username ?? ""}>
    <div className="app">
      <AppHeader page={page} user={user} busy={isActive("logout")} onNavigate={setPage} onSignOut={requestSignOut} />
      <AlertToasts
        toasts={toasts}
        notices={notices}
        onCloseNotice={(id) => setNotices((current) => current.filter((n) => n.id !== id))}
        onClose={(id) => setToasts((current) => current.filter((t) => t.id !== id))}
        onOpen={async (caseId) => {
          setPage("dashboard");
          const found = cases.find((c) => c.id === caseId);
          if (found) { selectCase(found); return; }
          const state = await getAppState();
          setCases(state.cases);
          selectCase(state.cases.find((c) => c.id === caseId) ?? null);
        }}
      />
      {error ? <StatusModal kind="error" message={error} onClose={() => setError("")} /> : null}
      {confirmLogout ? <LogoutModal busy={isActive("logout")} onCancel={() => setConfirmLogout(false)} onConfirm={signOut} /> : null}
      {binConfirm ? (
        <div className="modal-backdrop" role="presentation" onClick={() => setBinConfirm(null)}>
          <section aria-label="Confirm delete forever" aria-modal="true" className="status-modal delete" role="alertdialog" onClick={(e) => e.stopPropagation()}>
            <button aria-label="Close" className="modal-close" onClick={() => setBinConfirm(null)} type="button">×</button>
            <p className="modal-label">{binConfirm.kind === "empty" ? "Empty the recycle bin" : "Delete forever"}</p>
            <p>{binConfirm.kind === "empty" ? `Delete all ${bin.length} cases in the recycle bin forever?` : `Delete "${binConfirm.item.title}" forever?`}</p>
            <p className="muted">The case, its messages, its agent runs and its knowledge-base entry will be removed. This cannot be undone.</p>
            <div className="actions">
              <button className="secondary" onClick={() => setBinConfirm(null)} type="button">Cancel</button>
              <button className="danger" type="button" onClick={async () => {
                const target = binConfirm;
                setBinConfirm(null);
                try {
                  const ids = target.kind === "empty" ? bin.map((b) => b.id) : [target.item.id];
                  for (const id of ids) await deleteForever(id);
                  setBin(await getBin());
                  setNotice(ids.length > 1 ? `${ids.length} cases deleted forever.` : "Case deleted forever.");
                } catch (caught: unknown) { setError(readError(caught)); }
              }}><ActionIcon kind="trash" />{binConfirm.kind === "empty" ? "Empty the bin" : "Delete forever"}</button>
            </div>
          </section>
        </div>
      ) : null}
      {deleteTarget ? <DeleteCaseModal caseItem={deleteTarget} busy={isActive("delete")} onCancel={() => setDeleteTarget(null)} onConfirm={confirmDeleteCase} /> : null}
      {confirmAction ? <ConfirmActionModal action={confirmAction} onCancel={() => setConfirmAction(null)} onConfirm={() => { const action = confirmAction; setConfirmAction(null); action.onConfirm(); }} /> : null}
      {page === "dashboard" ? (
        <>
          <OperationsOverview dashboard={dashboard} cases={cases} trend={trend} />
          <div className="toolbar">
            {canWork ? <button className="primary compact" disabled={isActive("create")} onClick={() => setShowNewCase(true)} type="button"><ActionIcon kind="plus" />New case</button> : null}
            {canWork && health?.jira_configured ? (
              <button className={jiraPending > 0 ? "jira-sync-pending compact" : "secondary compact"} disabled={isActive("jira-sync")} onClick={() => setConfirmAction({
                title: "Sync Jira",
                message: jiraPending > 0
                  ? `This will import ${jiraPending} new Jira ticket${jiraPending > 1 ? "s" : ""}. Each one will become a case, and the agent team will investigate it (3 to 10 minutes and about 25 to 40 model calls each).`
                  : "This will check Jira for new tickets. Each new ticket will become a case, and the agent team will investigate it.",
                confirmLabel: "Sync Jira",
                onConfirm: () => {
                startTask("jira-sync");
                setError("");
                syncJiraTickets()
                  .then(async (result) => {
                    setJiraPending(0);
                    if (result.imported > 0) {
                      await refreshDashboardData();
                      setNotice(`Synced ${result.imported} ticket(s) from Jira. AI analysis running...`);
                      awaitAnalysis(result.cases.map((c) => c.id));
                    } else {
                      setNotice(`No new Jira tickets to import (${result.skipped} already synced).`);
                    }
                  })
                  .catch((caught: unknown) => setError(readError(caught)))
                  .finally(() => endTask("jira-sync"));
              } })} type="button"><ActionIcon kind="automation" />{isActive("jira-sync") ? "Syncing..." : jiraPending > 0 ? `Sync Jira (${jiraPending})` : "Sync Jira"}</button>
            ) : null}
            <CaseSearch
              disabled={isActive("search")}
              onReset={async () => {
                searchSeq.current += 1;
                startTask("search");
                setError("");
                setNotice("");
                try {
                  await refreshDashboardData();
                } catch (caught: unknown) {
                  setError(readError(caught));
                } finally {
                  endTask("search");
                }
              }}
              onSearch={async (query, status) => {
                const seq = ++searchSeq.current;
                startTask("search");
                setError("");
                try {
                  const results = await searchCases(query, status);
                  if (seq !== searchSeq.current) return;
                  setCases(results);
                  selectCase(results.find((c) => c.id === selected?.id) ?? results[0] ?? null);
                } catch (caught: unknown) {
                  setError(readError(caught));
                } finally {
                  endTask("search");
                }
              }}
            />
          </div>
          {showNewCase ? (
            <NewCaseModal
              disabled={isActive("create")}
              onClose={() => setShowNewCase(false)}
              onCreate={(payload) => {
                setShowNewCase(false);
                startTask("create");
                setError("");
                createCase(payload)
                  .then(async (created) => {
                    replaceCase(created);
                    setNotice("Case created. AI analysis running...");
                    const [h, d, a] = await Promise.all([getHealth(), getDashboard(), getAuditEvents()]);
                    setHealth(h); setDashboard(d); setAudit(a);
                    awaitAnalysis([created.id]);
                  })
                  .catch((caught: unknown) => setError(readError(caught)))
                  .finally(() => endTask("create"));
              }}
            />
          ) : null}
        </>
      ) : null}
      {page === "settings" ? (
        <SettingsPage onError={setError} onNotice={setNotice} />
      ) : page === "insights" ? (
        <main className="insights-page">
          {health?.grafana_url ? (
            <div className="page-actions">
              <a className="secondary link-button" href={`${health.grafana_url}/d/rootiq-agents`} target="_blank" rel="noreferrer">
                <ActionIcon kind="gauge" />Open in Grafana<ActionIcon kind="external" size={13} />
              </a>
            </div>
          ) : null}
          <AgentMetricsCard />
          <RatingsCard />
          <OperationsInsights trends={trends} problems={problems} />
          <AuditHistory events={audit} />
        </main>
      ) : (
      <main className="workspace">
        <aside>
          <CaseHistory links={links} bin={bin} onRestore={async (item) => {
            try {
              const restored = await restoreFromBin(item.id);
              const state = await getAppState();
              setCases(state.cases);
              setDashboard(state.dashboard);
              setBin(await getBin());
              selectCase(state.cases.find((c) => c.id === restored.id) ?? null);
              setHighlightId(restored.id);
              window.setTimeout(() => setHighlightId(null), 2400);
            } catch (caught: unknown) { setError(readError(caught)); }
          }} onDeleteForever={(item) => setBinConfirm({ kind: "forever", item })} onEmptyBin={() => setBinConfirm({ kind: "empty" })}
          cases={cases} selected={selected} highlightId={highlightId} msgCounts={msgCounts} readCounts={readCounts} activeRuns={live?.active_runs ?? {}} onDelete={setDeleteTarget} onSelect={selectCase} />
        </aside>
        <section className="detail">
          {selected ? (
            <CaseDetail
              key={selected.id}
              onOpenCase={(id) => { const target = cases.find((c) => c.id === id); if (target) selectCase(target); }}
              onNotice={setNotice}
              onGroupsChanged={() => listIncidentGroups().then(setGroupMarks).catch(() => undefined)}
              caseItem={selected}
              health={health}
              related={related}
              messages={messages}
              runbook={runbook}
              activeTasks={activeTasks}
              onAnalyze={() => {
                const doAnalyze = () => runBackground("analyze:" + selected.id, () => analyzeCase(selected.id), "Analysis generated for review.");
                setConfirmAction({
                  title: "Run a quick analysis",
                  message: selected.review_status !== "not_ready"
                    ? "This will make one model call (10 to 60 seconds). It will replace the current analysis, which will then need a new review."
                    : "This will make one model call (10 to 60 seconds), without the agent team.",
                  confirmLabel: "Run quick analysis",
                  onConfirm: doAnalyze,
                });
              }}
              onReview={(analysis, approve) =>
                runInline("review:" + selected.id, () => saveReview(selected.id, analysis, approve), approve ? "Analysis approved." : "Analysis edits saved.")
              }
              onResolve={(resolution, actions) =>
                void runInline("resolve:" + selected.id, () => resolveCase(selected.id, resolution, actions))
                  .then((updated) => { if (updated?.status === "resolved") setJustResolved(updated.id); })
              }
              justResolved={justResolved === selected.id}
              unread={unreadOnOpen[selected.id] ?? 0}
              onUnreadSeen={() => setUnreadOnOpen((prev) => ({ ...prev, [selected.id]: 0 }))}
              onCloseResolved={() => setJustResolved(null)}
              onAcknowledge={() =>
                runInline("ack:" + selected.id, () => acknowledgeCase(selected.id), "Customer acknowledgement recorded.")
              }
              onIndex={() => {
                const doIndex = () => {
                  const key = "index:" + selected.id;
                  startTask(key);
                  indexCaseMemory(selected.id)
                    .then(async () => {
                      updateCaseInPlace({ ...selected, memory_indexed: true });
                      const [nextRelated, nextAudit] = await Promise.all([getRelatedCases(selected.id), getAuditEvents()]);
                      setRelated(nextRelated);
                      setAudit(nextAudit);
                      setNotice("Added to similar incidents.");
                    })
                    .catch((caught: unknown) => setError(readError(caught)))
                    .finally(() => endTask(key));
                };
                if (selected.memory_indexed) {
                  setConfirmAction({
                    title: "Re-index resolution",
                    message: "This resolution is already indexed. Indexing it again will update the stored entry.",
                    confirmLabel: "Re-index",
                    onConfirm: doIndex,
                  });
                } else {
                  doIndex();
                }
              }}
              onGmail={() => runInline("gmail:" + selected.id, () => createGmailDraft(selected.id), "Gmail customer draft created.")}
              onSendMessage={async (content, messageType) => {
                const key = "message:" + selected.id;
                startTask(key);
                setError("");
                try {
                  const msg = await sendMessage(selected.id, content, messageType);
                  setMessages((prev) => {
                    if (prev.some((m) => m.id === msg.id)) return prev;  // the WebSocket push may arrive first
                    const next = [...prev, msg];
                    markRead(selected.id, next.length);
                    setMsgCounts((mc) => ({ ...mc, [selected.id]: next.length }));
                    return next;
                  });
                  setAudit(await getAuditEvents());
                } catch (caught: unknown) {
                  setError(readError(caught));
                } finally {
                  endTask(key);
                }
              }}
              onRunbook={() => {
                const doRunbook = () => {
                  const key = "runbook:" + selected.id;
                  startTask(key);
                  generateRunbook(selected.id)
                    .then(async (nextRunbook) => {
                      updateCaseInPlace({ ...selected, runbook: nextRunbook });
                      setRunbook(nextRunbook);
                      setAudit(await getAuditEvents());
                      setNotice("Runbook generated.");
                    })
                    .catch((caught: unknown) => setError(readError(caught)))
                    .finally(() => endTask(key));
                };
                setConfirmAction({
                  title: runbook ? "Regenerate the runbook" : "Generate a runbook",
                  message: runbook
                    ? "This will make one model call from similar resolved cases. It will replace the current runbook."
                    : "This will make one model call from similar resolved cases.",
                  confirmLabel: runbook ? "Regenerate" : "Generate",
                  onConfirm: doRunbook,
                });
              }}
              onCaseChanged={() => {
                const caseId = selected.id;
                void Promise.all([getCase(caseId), getDashboard(), getAuditEvents()])
                  .then(([updated, d, a]) => {
                    updateCaseInPlace(updated);
                    setDashboard(d);
                    setAudit(a);
                  })
                  .catch((caught: unknown) => setError(readError(caught)));
              }}
              onError={setError}
              onDownloadReport={async () => {
                const key = "download:" + selected.id;
                startTask(key);
                setError("");
                try {
                  await downloadReport(selected.id, `${selected.title}-incident-report.md`);
                  setAudit(await getAuditEvents());
                  setNotice("Incident report downloaded.");
                } catch (caught: unknown) {
                  setError(readError(caught));
                } finally {
                  endTask(key);
                }
              }}
            />
          ) : (
            <section className="empty">
              <h2>Create your first production incident</h2>
              <p>Add a customer issue and logs to generate a support-ready investigation.</p>
            </section>
          )}
        </section>
      </main>
      )}
    </div>
    </UserNameContext.Provider>
    </RoleContext.Provider>
  );
}

function LoginScreen({ busy, error, onAbout, onDismissError, onLogin }: { busy: boolean; error: string; onAbout: () => void; onDismissError: () => void; onLogin: (username: string, password: string) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [sso, setSso] = useState<{ sso: boolean; sso_name: string } | null>(null);
  const [ssoError, setSsoError] = useState(() => new URLSearchParams(window.location.search).get("sso_error") ?? "");
  useEffect(() => {
    getAuthConfig().then(setSso).catch(() => setSso(null));
    if (ssoError) window.history.replaceState(null, "", window.location.pathname);
  }, []);
  return (
    <main className="login-page">
      <form className="card login" onSubmit={(event) => { event.preventDefault(); onLogin(username, password); }}>
        <div className="login-heading">
          <BrandMark size={40} live />
          <h1>RootIQ</h1>
        </div>
        {error ? <StatusModal kind="error" message={error} onClose={onDismissError} /> : null}
        {ssoError ? <StatusModal kind="error" message={ssoError} onClose={() => setSsoError("")} /> : null}
        {sso?.sso ? (
          <>
            <a className="secondary sso-button" href="/api/auth/oidc/start"><ActionIcon kind="key" />Sign in with {sso.sso_name}</a>
            <div className="login-or"><span>or</span></div>
          </>
        ) : null}
        <label><span>Username</span><input autoComplete="username" required value={username} onChange={(event) => setUsername(event.target.value)} /></label>
        <label><span>Password</span><input autoComplete="current-password" type="password" required value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        <button className="primary" disabled={busy}>{busy ? "Signing in..." : "Sign in"}</button>
        <button className="text-action centered" onClick={onAbout} type="button">About RootIQ</button>
      </form>
    </main>
  );
}

/** The Jira tickets of a case, for the delete dialog: "KAN-4" or "KAN-4 and KAN-11". */
function jiraKeys(caseItem: SupportCase): string {
  return [...new Set([caseItem.jira_source_key, caseItem.jira_issue_key].filter(Boolean))].join(" and ");
}

type Page = "dashboard" | "insights" | "settings" | "about";

const NAV: { page: Page; label: string; icon: IconKind; signedIn: boolean; admin?: boolean }[] = [
  { page: "dashboard", label: "Dashboard", icon: "dashboard", signedIn: true },
  { page: "insights", label: "Insights", icon: "insights", signedIn: true },
  { page: "settings", label: "Settings", icon: "settings", signedIn: true, admin: true },
  { page: "about", label: "About", icon: "about", signedIn: false },
];

/** The title bar. All pages use it, so the links are the same on each page. */
function AppHeader({ page, user, busy, onNavigate, onSignOut }: { page: Page; user: AuthUser | null; busy: boolean; onNavigate: (page: Page) => void; onSignOut: () => void }) {
  const navRef = useRef<HTMLElement>(null);
  useSlidingIndicator(navRef, ".control-button.active", [page, Boolean(user)]);
  return (
    <header className="header-bar">
      <div className="header-brand">
        <BrandMark live />
        <h1 className="header-title">RootIQ</h1>
      </div>
      <nav className="header-nav sliding" aria-label="Account and navigation" ref={navRef}>
        {!user ? <button className="control-button" onClick={() => onNavigate("dashboard")} type="button"><ActionIcon kind="user" /><span className="nav-label">Sign in</span></button> : null}
        {NAV.filter((item) => (user || !item.signedIn) && (!item.admin || user?.role === "admin")).map((item) => (
          <button aria-current={page === item.page ? "page" : undefined} className={page === item.page ? "control-button active" : "control-button"} key={item.page} onClick={() => onNavigate(item.page)} type="button">
            <ActionIcon kind={item.icon} /><span className="nav-label">{item.label}</span>
          </button>
        ))}
      </nav>
      {user ? <AccountMenu user={user} busy={busy} onSignOut={onSignOut} /> : null}
    </header>
  );
}

type SettingsTab = "models" | "sources" | "automation" | "users" | "security";

/** Settings: the models that the agents use and the data sources that they read. Admins only, later with users. */
function SettingsPage({ onError, onNotice }: { onError: (message: string) => void; onNotice: (message: string) => void }) {
  const [tab, setTab] = useState<SettingsTab>(() => {
    const routed = readRoute().tab;
    return (["models", "sources", "automation", "users", "security"] as string[]).includes(routed) ? routed as SettingsTab : "models";
  });
  useEffect(() => writeRoute("settings", tab, true), [tab]);
  const barRef = useRef<HTMLElement>(null);
  useSlidingIndicator(barRef, ".detail-tab.active", [tab]);
  const tabs: { id: SettingsTab; label: string; icon: IconKind }[] = [
    { id: "models", label: "Models", icon: "cpu" },
    { id: "sources", label: "Data sources", icon: "database" },
    { id: "automation", label: "Automation", icon: "zap" },
    { id: "users", label: "Users", icon: "users" },
    { id: "security", label: "Security", icon: "shield" },
  ];
  return (
    <main className="settings-page">
      <nav className="detail-tabbar sliding settings-tabs" aria-label="Settings sections" ref={barRef}>
        {tabs.map((t) => (
          <button aria-current={tab === t.id ? "page" : undefined} className={tab === t.id ? "detail-tab active" : "detail-tab"} key={t.id} onClick={() => setTab(t.id)} type="button">
            <ActionIcon kind={t.icon} size={15} />{t.label}
          </button>
        ))}
      </nav>
      {tab === "models" ? <ModelsPage onError={onError} onNotice={onNotice} />
        : tab === "sources" ? <SourcesPage onError={onError} onNotice={onNotice} />
        : tab === "automation" ? <AutomationPage onError={onError} onNotice={onNotice} />
        : tab === "users" ? <UsersPage onError={onError} onNotice={onNotice} />
        : <SecurityPage onError={onError} onNotice={onNotice} />}
    </main>
  );
}

function AccountMenu({ user, busy, onSignOut }: { user: AuthUser; busy: boolean; onSignOut: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="account">
      <button className="profile-badge" onClick={() => setOpen(true)} title="Your account" type="button">
        <ActionIcon kind="user" /><span className="nav-label">{user.username}</span>
      </button>
      <button aria-label="Sign out" className="icon-button" disabled={busy} onClick={onSignOut} type="button"><ActionIcon kind="logout" /></button>
      {open ? <AccountModal user={user} onClose={() => setOpen(false)} /> : null}
    </div>
  );
}

/** The own account: name, role, and a new password (for password accounts). */
function AccountModal({ user, onClose }: { user: AuthUser; onClose: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [state, setState] = useState<{ busy?: boolean; error?: string; done?: boolean }>({});
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (next !== again) return setState({ error: "The new passwords are not the same." });
    setState({ busy: true });
    try {
      await changePassword(current, next);
      setState({ done: true });
    } catch (caught: unknown) {
      setState({ error: caught instanceof Error ? caught.message : String(caught) });
    }
  }
  return (
    <Portal>
      <div className="modal-backdrop" role="presentation" onClick={onClose}>
        <section aria-label="Your account" aria-modal="true" className="status-modal account-modal" role="dialog" onClick={(e) => e.stopPropagation()}>
          <button aria-label="Close" className="modal-close" onClick={onClose} type="button">×</button>
          <div className="account-head">
            <span className="avatar">{(user.display_name || user.username).slice(0, 1).toUpperCase()}</span>
            <div>
              <strong>{user.display_name || user.username}</strong>
              <small className="muted">{user.email || user.username}</small>
            </div>
          </div>
          {user.auth === "sso" ? (
            <p className="muted">You sign in with single sign-on. Change your password with your identity provider.</p>
          ) : state.done ? (
            <p className="source-result ok"><ActionIcon kind="success" size={15} />Password changed. Other sessions of this account are signed out.</p>
          ) : (
            <form className="password-form" onSubmit={submit}>
              <h4>Change password</h4>
              <input autoComplete="current-password" placeholder="Current password" type="password" value={current} onChange={(e) => setCurrent(e.target.value)} />
              <input autoComplete="new-password" placeholder="New password (10 or more characters)" type="password" value={next} onChange={(e) => setNext(e.target.value)} />
              <input autoComplete="new-password" placeholder="New password again" type="password" value={again} onChange={(e) => setAgain(e.target.value)} />
              {state.error ? <p className="source-result bad"><ActionIcon kind="fail" size={15} />{state.error}</p> : null}
              <button className="primary" disabled={state.busy || !current || !next || !again} type="submit">{state.busy ? "Saving..." : "Change password"}</button>
            </form>
          )}
        </section>
      </div>
    </Portal>
  );
}

function AboutPage({ user, busy, onNavigate, onSignOut }: { user: AuthUser | null; busy: boolean; onNavigate: (page: Page) => void; onSignOut: () => void }) {
  const capabilities = [
    ["Agent team investigation", "A team of AI agents examines each case. They read the logs, metrics, recent changes and code, search past incidents and check each other's work."],
    ["Approval before action", "The team stops before an email, a Jira change, a fix or a code patch. One or two people approve, edit or reject each proposal, by its risk."],
    ["Fix and verify", "An approved fix runs one time, from a list of safe fixes. Then the app compares the errors before and after the fix."],
    ["Related incidents", "The app groups the cases that have the same root cause. An alert for a service that already has an open case does not start a second investigation."],
    ["Your choice of model", "Use Groq, Gemini, Claude, GPT or Grok. Each task level has a chain of models. A local model answers when the cloud models cannot."],
    ["Live progress", "You see each agent start and finish. You see each tool call when it occurs."],
    ["Monitoring alerts", "A Grafana alert opens a case and starts the agents. No person has to create the case. A Grafana dashboard shows how the agents perform."],
    ["Measured performance", "The app records time, model calls and tokens for each agent. It grades each diagnosis against the real fix."],
    ["Incident intake", "Record the customer, service, severity, environment and logs. The app opens a Jira ticket for each case."],
    ["Support operations", "The app tracks response and resolution times. It shows SLA risk, service hotspots and recurring problems."],
    ["Knowledge base", "The app stores each resolved case. It finds similar incidents, and writes runbooks and postmortems."],
    ["Messages", "Write internal notes and customer messages on a case. They appear at once for all users."],
    ["Controlled publishing", "The app makes Jira escalations and Gmail drafts only after approval. It does not make duplicates."],
    ["Accountability", "Each person signs in with their own account and role. The audit log records who did each action."],
  ];
  return (
    <div className="app about-page">
      <AppHeader page="about" user={user} busy={busy} onNavigate={onNavigate} onSignOut={onSignOut} />
      <main className="about-content">
        <h1>AI support work, kept accountable.</h1>
        <p className="about-lead">
          RootIQ changes customer incidents and application logs into diagnoses, response drafts and records. A person approves each step.
        </p>
        <section className="capability-grid" aria-label="RootIQ capabilities">
          {capabilities.map(([title, detail]) => (
            <article key={title}>
              <h2>{title}</h2>
              <p>{detail}</p>
            </article>
          ))}
        </section>
        <p className="about-footnote">Data stays in your database: SQLite on one computer, or PostgreSQL on a server. Before case text goes to a cloud model, the app masks personal data. With local models only, no data leaves this computer.</p>
      </main>
    </div>
  );
}

function StatusModal({ kind, message, onClose }: { kind: "success" | "error"; message: string; onClose: () => void }) {
  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <section
        aria-label={kind === "success" ? "Success" : "Error"}
        aria-modal="true"
        className={`status-modal ${kind}`}
        role="alertdialog"
        onClick={(event) => event.stopPropagation()}
      >
        <button aria-label="Close message" className="modal-close" onClick={onClose} type="button">×</button>
        <p className="modal-label"><ActionIcon kind={kind} />{kind === "success" ? "Success" : "Unable to complete action"}</p>
        <p>{message}</p>
      </section>
    </div>
  );
}

function LogoutModal({ busy, onCancel, onConfirm }: { busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  return (
    <div className="modal-backdrop" role="presentation" onClick={onCancel}>
      <section
        aria-label="Confirm sign out"
        aria-modal="true"
        className="status-modal confirm"
        role="alertdialog"
        onClick={(event) => event.stopPropagation()}
      >
        <p className="modal-label">Confirm</p>
        <p>Sign out of RootIQ?</p>
        <div className="actions">
          <button className="secondary" disabled={busy} onClick={onCancel}>Cancel</button>
          <button className="danger" disabled={busy} onClick={onConfirm}><ActionIcon kind="logout" />{busy ? "Signing out..." : "Sign out"}</button>
        </div>
      </section>
    </div>
  );
}

function DeleteCaseModal({ caseItem, busy, onCancel, onConfirm }: { caseItem: SupportCase; busy: boolean; onCancel: () => void; onConfirm: () => void }) {
  return (
    <div className="modal-backdrop" role="presentation" onClick={onCancel}>
      <section
        aria-label="Confirm case delete"
        aria-modal="true"
        className="status-modal delete"
        role="alertdialog"
        onClick={(event) => event.stopPropagation()}
      >
        <button aria-label="Close delete confirmation" className="modal-close" disabled={busy} onClick={onCancel} type="button">×</button>
        <p className="modal-label">Delete case</p>
        <p>Move "{caseItem.title}" to the recycle bin?</p>
        <p className="muted">Its agent work will stop{jiraKeys(caseItem) ? <>, and {jiraKeys(caseItem)} moves to Done in Jira</> : null}. You can
          restore it from the recycle bin later{jiraKeys(caseItem) ? ", and the ticket goes back to To Do" : ""}.</p>
        <div className="actions">
          <button className="secondary" disabled={busy} onClick={onCancel}>Cancel</button>
          <button className="danger" disabled={busy} onClick={onConfirm}><ActionIcon kind="trash" />{busy ? "Moving..." : "Move to bin"}</button>
        </div>
      </section>
    </div>
  );
}

function ConfirmActionModal({ action, onCancel, onConfirm }: { action: ConfirmAction; onCancel: () => void; onConfirm: () => void }) {
  return (
    <div className="modal-backdrop" role="presentation" onClick={onCancel}>
      <section
        aria-label={action.title}
        aria-modal="true"
        className="status-modal confirm"
        role="alertdialog"
        onClick={(event) => event.stopPropagation()}
      >
        <button aria-label="Close" className="modal-close" onClick={onCancel} type="button">×</button>
        <p className="modal-label">{action.title}</p>
        <p>{action.message}</p>
        <div className="actions">
          <button className="secondary" onClick={onCancel}>Cancel</button>
          <button className="primary" onClick={onConfirm}>{action.confirmLabel}</button>
        </div>
      </section>
    </div>
  );
}

function AuditHistory({ events }: { events: AuditEvent[] }) {
  const [showSignIns, setShowSignIns] = useState(false);
  const [limit, setLimit] = useState(10);
  const shown = events.filter((e) => showSignIns || !e.action.startsWith("auth."));
  return (
    <section className="card insight-card audit">
      <div className="section-heading">
        <h2 className="title-icon"><ActionIcon kind="history" size={18} />Audit history</h2>
        <label className="audit-toggle">
          <input type="checkbox" checked={showSignIns} onChange={(e) => setShowSignIns(e.target.checked)} />
          Sign-ins
        </label>
      </div>
      {shown.length === 0 ? <p className="muted">Actions taken after sign-in appear here.</p> : (
        <>
          <ul className="audit-list">
            {shown.slice(0, limit).map((event) => (
              <li key={event.id}>
                <time>{formatShortDate(event.at)}</time>
                <strong>{formatAction(event.action)}</strong>
                <span className="audit-actor">{event.actor}</span>
                <span className="audit-detail" title={event.detail || event.resource_type}>{event.detail || event.resource_type}</span>
              </li>
            ))}
          </ul>
          {shown.length > 10 ? (
            <div className="show-more-row">
              {shown.length > limit ? <button className="text-action show-more" onClick={() => setLimit(limit + 20)} type="button">Show more ({shown.length - limit})</button> : null}
              {limit > 10 ? <button className="text-action show-more" onClick={() => setLimit(10)} type="button">Show less</button> : null}
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}

function OperationsInsights({ trends, problems }: { trends: ServiceTrend[]; problems: ProblemGroup[] }) {
  const hotspotsRef = useScrollMore<HTMLDivElement>();
  const problemsRef = useScrollMore<HTMLDivElement>();
  return (
    <section className="insights">
      <section className="card insight-card">
        <h2 className="title-icon"><ActionIcon kind="server" size={18} />Service hotspots</h2>
        {trends.length === 0 ? <p className="muted">No incident trends recorded yet.</p> : (
          <div className="insight-scroll" aria-label="Scrollable service hotspots" ref={hotspotsRef}>
            <table className="data-table">
              <thead><tr><th>Service</th><th>Cases</th><th>Open</th><th>Breached</th><th>Avg fix</th></tr></thead>
              <tbody>
                {trends.map((trend) => (
                  <tr key={trend.service}>
                    <td>{trend.service}</td>
                    <td>{trend.total_cases}</td>
                    <td className={trend.open_cases ? "num-warn" : "num-dim"}>{trend.open_cases}</td>
                    <td className={trend.breached_cases ? "num-bad" : "num-dim"}>{trend.breached_cases}</td>
                    <td className="num-dim">{trend.average_resolution_hours === null ? "n/a" : `${trend.average_resolution_hours}h`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="card insight-card">
        <h2 className="title-icon"><ActionIcon kind="layers" size={18} />Recurring problems</h2>
        {problems.length === 0 ? <p className="muted">Resolve repeated incidents to identify recurring problems.</p> : (
          <div className="insight-scroll" aria-label="Scrollable recurring problems" ref={problemsRef}>
            {problems.map((problem) => (
              <article className="problem-row" key={`${problem.service}-${problem.cause}`}>
                <div className="problem-heading">
                  <span className="service-tag">{problem.service}</span>
                  <span className="problem-count" title={`${problem.case_count} occurrence${problem.case_count === 1 ? "" : "s"}`}>{problem.case_count}×</span>
                </div>
                <p className="problem-cause" title={problem.cause}>{problem.cause}</p>
                <p className="problem-fix" title={problem.known_resolutions[0] ?? ""}><ActionIcon kind="check" size={13} />{problem.known_resolutions[0] ?? "No recorded fix yet."}</p>
              </article>
            ))}
          </div>
        )}
      </section>
    </section>
  );
}

function OperationsOverview({ dashboard, cases, trend }: { dashboard: OperationsDashboard | null; cases: SupportCase[]; trend: DashboardTrend | null }) {
  const can = useCan();
  const openBreached = cases.filter((c) => c.status !== "resolved" && c.sla_state === "breached").length;
  const openAtRisk = cases.filter((c) => c.status !== "resolved" && c.sla_state === "at_risk").length;
  const metrics: { label: string; value: number; icon: IconKind; tone: string; series?: number[] }[] = [
    { label: can.work ? "Awaiting your approval" : "Awaiting approval", value: dashboard?.awaiting_approval_cases ?? 0, icon: "inbox", tone: "accent", series: trend?.awaiting_approval },
    { label: "Open incidents", value: dashboard?.open_cases ?? 0, icon: "activity", tone: "neutral", series: trend?.open },
    { label: "Urgent open", value: dashboard?.urgent_open_cases ?? 0, icon: "error", tone: "danger", series: trend?.urgent_open },
    { label: "SLA at risk", value: dashboard?.at_risk_cases ?? 0, icon: "clock", tone: "warning", series: trend?.at_risk },
    { label: "Resolved", value: dashboard?.resolved_cases ?? 0, icon: "success", tone: "success", series: trend?.resolved },
  ];
  return (
    <>
      {openBreached > 0 ? (
        <div className="sla-alert breached" role="alert">
          <ActionIcon kind="error" /><strong>{openBreached} open case{openBreached > 1 ? "s" : ""} breached SLA</strong>
        </div>
      ) : openAtRisk > 0 ? (
        <div className="sla-alert at-risk" role="alert">
          <ActionIcon kind="error" /><strong>{openAtRisk} open case{openAtRisk > 1 ? "s" : ""} approaching SLA deadline</strong>
        </div>
      ) : null}
      <section className="overview" aria-label="Support operations overview">
        {metrics.map((m) => (
          <article key={m.label} className={`kpi kpi-${m.tone}`}>
            <span className="kpi-icon"><ActionIcon kind={m.icon} size={18} /></span>
            <div><span>{m.label}</span><strong><CountUp value={m.value} /></strong></div>
            {m.series ? <Sparkline values={m.series} tone={m.tone} /> : null}
          </article>
        ))}
      </section>
    </>
  );
}

function NewCaseModal({ disabled, onClose, onCreate }: { disabled: boolean; onClose: () => void; onCreate: (payload: CaseCreate) => void }) {
  const [form, setForm] = useState(EMPTY_CASE);
  function submit(event: FormEvent) {
    event.preventDefault();
    onCreate(form);
    setForm(EMPTY_CASE);
  }
  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <form className="new-case-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <button aria-label="Close" className="modal-close" onClick={onClose} type="button">×</button>
        <h2>New customer case</h2>
        <input required placeholder="Case title" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
        <div className="paired">
          <input required placeholder="Customer" value={form.customer} onChange={(e) => setForm({ ...form, customer: e.target.value })} />
          <input required placeholder="Affected service" value={form.service} onChange={(e) => setForm({ ...form, service: e.target.value })} />
        </div>
        <div className="paired">
          <select value={form.environment} onChange={(e) => setForm({ ...form, environment: e.target.value })}>
            <option value="production">Production</option>
            <option value="staging">Staging</option>
            <option value="test">Test</option>
          </select>
          <select value={form.severity} onChange={(e) => setForm({ ...form, severity: e.target.value as Severity })}>
            <option value="critical">Critical</option>
            <option value="high">High</option>
            <option value="medium">Medium</option>
            <option value="low">Low</option>
          </select>
        </div>
        <textarea required rows={3} placeholder="Customer-reported issue" value={form.reported_issue} onChange={(e) => setForm({ ...form, reported_issue: e.target.value })} />
        <textarea rows={3} className="logs" placeholder="Paste logs, error messages, request IDs..." value={form.logs} onChange={(e) => setForm({ ...form, logs: e.target.value })} />
        <div className="actions">
          <button className="secondary" onClick={onClose} type="button">Cancel</button>
          <button disabled={disabled} className="primary action-save"><ActionIcon kind="save" />Create case</button>
        </div>
      </form>
    </div>
  );
}

function CaseSearch({ disabled, onReset, onSearch }: { disabled: boolean; onReset: () => void; onSearch: (query: string, status: string) => void }) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const filtered = Boolean(query || status);
  const latest = useRef({ status, onSearch, onReset });
  latest.current = { status, onSearch, onReset };
  const typed = useRef(false);
  // Results update while typing . Emptying the box with no status filter restores the full list. After a short pause.
  useEffect(() => {
    if (!typed.current) return;
    const timer = setTimeout(() => {
      const { status: current, onSearch: search, onReset: reset } = latest.current;
      if (query.trim() || current) search(query.trim(), current);
      else reset();
    }, 300);
    return () => clearTimeout(timer);
  }, [query]);
  function resetSearch() {
    typed.current = false;
    setQuery("");
    setStatus("");
    onReset();
  }
  return (
    <form className="search-bar" role="search" onSubmit={(event) => { event.preventDefault(); if (query.trim() || status) onSearch(query.trim(), status); else onReset(); }}>
      <div className="search-field">
        <button aria-label="Search" className="search-submit" disabled={disabled} type="submit"><ActionIcon kind="search" /></button>
        <input aria-label="Search cases" placeholder="Search cases" value={query} onChange={(e) => { typed.current = true; setQuery(e.target.value); }} />
        {filtered ? <button aria-label="Clear search" className="search-clear" disabled={disabled} onClick={resetSearch} type="button"><ActionIcon kind="close" size={14} /></button> : null}
      </div>
      <select aria-label="Filter by status" value={status} onChange={(e) => { setStatus(e.target.value); if (query.trim() || e.target.value) onSearch(query.trim(), e.target.value); else onReset(); }}>
        <option value="">All statuses</option>
        <option value="new">New</option>
        <option value="investigating">Investigating</option>
        <option value="awaiting_approval">Awaiting approval</option>
        <option value="approved">Approved / active</option>
        <option value="resolved">Resolved</option>
      </select>
    </form>
  );
}

const AGENTS_AT_WORK = ["queued", "running", "executing"];

/** The case list. Each row shows the title, the service, the severity and (for open cases) the status. The Jira key
 *  and the agent state are on the case itself; a row whose agents are at work has a soft light moving across it. */
function CaseHistory({ bin, onRestore, onDeleteForever, onEmptyBin, cases, selected, highlightId, msgCounts, readCounts, activeRuns, links, onDelete, onSelect }: { links: Record<string, { primary: boolean; others: number; suggested: boolean }>; cases: SupportCase[]; selected: SupportCase | null; highlightId: string | null; msgCounts: Record<string, number>; readCounts: Record<string, number>; activeRuns: LiveState["active_runs"]; bin: BinItem[]; onRestore: (item: BinItem) => void; onDeleteForever: (item: BinItem) => void; onEmptyBin: () => void; onDelete: (value: SupportCase) => void; onSelect: (value: SupportCase) => void }) {
  const can = useCan();
  return (
    <nav className="history case-tabs">
      <h2 className="queue-title">Cases <small>{cases.length}</small></h2>
      {[["Open", cases.filter((c) => c.status !== "resolved")], ["Resolved", cases.filter((c) => c.status === "resolved")]].map(([label, group]) => {
        const items = group as SupportCase[];
        if (!items.length) return null;
        const rows = items.map((item, index) => (
        <article key={item.id} style={{ "--i": index } as CSSProperties} className={`case-row sev-${item.severity}${selected?.id === item.id ? " active" : ""}${highlightId === item.id ? " pulse" : ""}${AGENTS_AT_WORK.includes(activeRuns[item.id]?.status ?? "") ? " working" : ""}`}>
          {AGENTS_AT_WORK.includes(activeRuns[item.id]?.status ?? "") ? <Warp /> : null}
          <button className="case-tab" onClick={() => onSelect(item)} type="button">
            {/* the title and the pills flow as one line; the pills go to the next line only when there is no space */}
            <span className="case-line">
              <strong>{item.title}</strong>
              <small className={`severity ${item.severity}`}>{item.severity}</small>
              {item.status !== "resolved" ? <small className={`status-badge ${item.status}`}>{STATUS_LABELS[item.status]}</small> : null}
              {links[item.id] ? (
                <small className={`link-badge${links[item.id].suggested ? " suggested" : ""}`} title={links[item.id].primary ? `Primary of ${links[item.id].others + 1} related cases` : "Linked to a related case"}>
                  <ActionIcon kind="link" size={11} />{links[item.id].primary ? <span className="link-count">+{links[item.id].others}</span> : null}
                </small>
              ) : null}
              {(msgCounts[item.id] ?? 0) > (readCounts[item.id] ?? 0) ? <small className="unread-badge">{(msgCounts[item.id] ?? 0) - (readCounts[item.id] ?? 0)} new</small> : null}
            </span>
            {/* the service, unless the title already names it (alert cases) or it is unknown */}
            {!item.title.toLowerCase().includes(item.service.toLowerCase()) && item.service !== "unknown" ? <span className="meta-pair"><span>{item.service}</span></span> : null}
          </button>
          {can.work ? <button aria-label={`Delete ${item.title}`} className="case-delete" onClick={() => onDelete(item)} type="button"><ActionIcon kind="trash" /></button> : null}
        </article>
        ));
        return label === "Open" ? (
          <section key="open" className="case-group"><h3 className="case-group-title">Open ({items.length})</h3>{rows}</section>
        ) : (
          <details key="resolved" className="case-group" open={items.some((c) => c.id === selected?.id)}>
            <summary className="case-group-title">Resolved ({items.length})</summary>
            {rows}
          </details>
        );
      })}
          {bin.length ? (
        <details className="case-group bin-group">
          <summary className="case-group-title">Recycle bin ({bin.length})</summary>
          {bin.map((item) => (
            <article key={item.id} className="bin-row">
              <div className="bin-text">
                <span className="case-line"><strong>{item.title}</strong><small className={`severity ${item.severity}`}>{item.severity}</small></span>
                <small className="muted">Deleted {formatShortDate(item.deleted_at)}{item.deleted_by ? ` by ${item.deleted_by}` : ""}</small>
              </div>
              <div className="bin-actions">
                {can.work ? <button className="secondary" onClick={() => onRestore(item)} type="button"><ActionIcon kind="reset" size={14} />Restore</button> : null}
                {can.admin ? <button aria-label={`Delete ${item.title} forever`} className="icon-button danger-hover" onClick={() => onDeleteForever(item)} type="button"><ActionIcon kind="trash" /></button> : null}
              </div>
            </article>
          ))}
          {can.admin ? <button className="text-action empty-bin" onClick={onEmptyBin} type="button">Empty the recycle bin</button> : null}
        </details>
      ) : null}
    </nav>
  );
}

function CaseDetail({ onOpenCase, onNotice, onGroupsChanged, caseItem, health, related, messages, runbook, activeTasks, onAnalyze, onReview, onResolve, onAcknowledge, onIndex, onGmail, onSendMessage, onRunbook, onCaseChanged, onError, onDownloadReport, justResolved, onCloseResolved, unread, onUnreadSeen }: {
  onOpenCase: (id: string) => void; onNotice: (message: string) => void; onGroupsChanged: () => void;
  caseItem: SupportCase;
  health: Health | null;
  related: RelatedCase[];
  messages: CaseMessage[];
  runbook: RunbookRecommendation | null;
  activeTasks: Set<string>;
  onAnalyze: () => void;
  onReview: (analysis: SupportAnalysis, approve: boolean) => void;
  onResolve: (resolution: string, preventiveActions: string[]) => void;
  onAcknowledge: () => void;
  onIndex: () => void;
  onGmail: () => void;
  onSendMessage: (content: string, messageType: "internal" | "customer") => void;
  onRunbook: () => void;
  onCaseChanged: () => void;
  onError: (message: string) => void;
  onDownloadReport: () => void;
  justResolved: boolean;
  onCloseResolved: () => void;
  unread: number;
  onUnreadSeen: () => void;
}) {
  const can = useCan();
  const [draft, setDraft] = useState(caseItem.analysis);
  const [resolution, setResolution] = useState(caseItem.resolution);
  const [actions, setActions] = useState(caseItem.preventive_actions.join("\n"));
  const [detailView, setDetailView] = useState<DetailView>(() => {  // after a reload: the tab in the address
    const routed = readRoute();
    return routed.page === "dashboard" && (DETAIL_VIEWS as readonly string[]).includes(routed.tab) ? routed.tab as DetailView : "overview";
  });
  useEffect(() => { if (readRoute().page === "dashboard") writeRoute("dashboard", detailView === "overview" ? "" : detailView, true); }, [detailView]);
  const tabRef = useRef<HTMLElement>(null);
  const [newCount, setNewCount] = useState(0);  // new messages at open time, kept for this visit (divider in Messages)
  const [newSeen, setNewSeen] = useState(false);
  useEffect(() => { if (unread > 0) { setNewCount(unread); setNewSeen(false); } }, [unread]);
  useEffect(() => { if (detailView === "messages" && newCount > 0 && !newSeen) { setNewSeen(true); onUnreadSeen(); } }, [detailView, newCount]);
  const busy = (action: string) => activeTasks.has(action + ":" + caseItem.id);
  const mutating = busy("review") || busy("resolve") || busy("ack") || busy("jira") || busy("gmail");
  useEffect(() => {
    setDraft(caseItem.analysis);
    setResolution(caseItem.resolution);
    setActions(caseItem.preventive_actions.join("\n"));
  }, [caseItem]);
  const ready = caseItem.review_status !== "not_ready";
  const canPublish = caseItem.review_status === "approved" && caseItem.status !== "resolved";
  const hasReport = caseItem.status === "resolved";
  const hasRunbook = !!runbook;
  const availableViews: DetailView[] = [
    "overview",
    "agent",
    ...(ready ? ["analysis" as DetailView] : []),
    ...(canPublish ? ["publish" as DetailView] : []),
    ...(hasReport ? ["report" as DetailView] : []),
    "similar",
    "messages",
    ...(hasRunbook ? ["runbook" as DetailView] : []),
  ];
  useSlidingIndicator(tabRef, ".detail-tab.active", [detailView, availableViews.join()]);
  const tabLabels: Record<DetailView, string> = {
    overview: "Overview",
    agent: "Agent team",
    analysis: "Analysis",
    publish: "Publish",
    report: "Report",
    similar: "Similar",
    messages: "Messages",
    runbook: "Runbook",
  };
  const current: DetailView = availableViews.includes(detailView) ? detailView : "overview";
  const isVisible = (view: DetailView) => current === view;
  return (
    <>
      <section className="case-header card">
        <div>
          <p className="kicker">{caseItem.environment.toUpperCase()} INCIDENT</p>
          <h2>{caseItem.title}</h2>
          <p className="meta-pair">
            <span className="meta-icon"><ActionIcon kind="building" size={14} />{caseItem.customer}</span>
            <span className="meta-icon"><ActionIcon kind="server" size={14} />{caseItem.service}</span>
            {caseItem.jira_source_key && health?.jira_site_url ? (
              <a className="jira-link" href={`${health.jira_site_url}/browse/${caseItem.jira_source_key}`} target="_blank" rel="noreferrer">Jira {caseItem.jira_source_key}</a>
            ) : caseItem.jira_issue_url ? (
              <a className="jira-link" href={caseItem.jira_issue_url} target="_blank" rel="noreferrer">Jira {caseItem.jira_issue_key}</a>
            ) : null}
            {alertRuleUrl(caseItem) ? (
              <a className="jira-link grafana-link" href={alertRuleUrl(caseItem)} target="_blank" rel="noreferrer">
                <ActionIcon kind="activity" size={13} />View alert in Grafana
              </a>
            ) : null}
          </p>
        </div>
        <div className="case-flags">
          <div className={`severity-pill ${caseItem.severity}`}>{caseItem.severity}</div>
          <div className={`sla-pill ${caseItem.sla_state}`}>SLA {caseItem.sla_state.replace("_", " ")}</div>
        </div>
      </section>
      <nav className="detail-tabbar sliding" aria-label="Incident detail sections" ref={tabRef}>
        {availableViews.map((view) => (
          <button
            aria-current={current === view ? "page" : undefined}
            className={current === view ? "detail-tab active" : "detail-tab"}
            key={view}
            onClick={() => setDetailView(view)}
            type="button"
          >
            <ActionIcon kind={TAB_ICONS[view]} size={15} />
            {tabLabels[view]}
            {view === "messages" && newCount > 0 && !newSeen ? <span className="tab-new">{newCount} new</span> : null}
          </button>
        ))}
      </nav>
      {isVisible("overview") && justResolved && caseItem.status === "resolved" ? <ResolveProgress caseItem={caseItem} onClose={onCloseResolved} /> : null}
      {isVisible("overview") ? <RelatedIncidents caseId={caseItem.id} caseStatus={caseItem.status} onOpenCase={onOpenCase} onError={onError} onNotice={onNotice}
        onChanged={() => { onCaseChanged(); onGroupsChanged(); }} /> : null}
      {isVisible("overview") ? <AgentPanel compact caseId={caseItem.id} caseStatus={caseItem.status} health={health} onCaseChanged={onCaseChanged} onError={onError} onOpen={() => setDetailView("agent")} /> : null}
      {isVisible("overview") ? <section className="card evidence">
        <div className="section-heading">
          <h3 className="title-icon"><ActionIcon kind="message" size={18} />Customer report</h3>
          {can.work ? <button className="secondary" disabled={busy("analyze")} onClick={onAnalyze} title="One-prompt analysis without the agent team">
            <ActionIcon kind="zap" />{busy("analyze") ? "Analyzing..." : "Quick analysis"}
          </button> : null}
        </div>
        <p>{caseItem.reported_issue}</p>
        {busy("analyze") ? <QuickAnalysisProgress /> : null}
        {caseItem.logs ? <pre>{caseItem.logs}</pre> : null}
      </section> : null}
      {isVisible("overview") ? <section className="card sla-panel">
        <div>
          <span>Initial response target</span>
          <strong>{formatDeadline(caseItem.response_due_at)}</strong>
          <small>{caseItem.first_response_at ? `Acknowledged ${formatDeadline(caseItem.first_response_at)}` : "Response not recorded"}</small>
        </div>
        <div>
          <span>Resolution target</span>
          <strong>{formatDeadline(caseItem.resolution_due_at)}</strong>
          <small>{caseItem.status === "resolved" ? "Resolution recorded" : "Incident remains open"}</small>
        </div>
        {caseItem.first_response_at ? (
          <span className="state-badge success"><ActionIcon kind="success" />Customer acknowledged</span>
        ) : can.work ? (
          <button className="secondary action-save" disabled={mutating || caseItem.status === "resolved"} onClick={onAcknowledge}>
            <ActionIcon kind="save" />
            Record acknowledgement
          </button>
        ) : null}
      </section> : null}
      {isVisible("overview") ? <TimelineCard caseItem={caseItem} busy={busy("download")} onDownloadReport={onDownloadReport} /> : null}
      {isVisible("agent") ? <AgentPanel caseId={caseItem.id} caseStatus={caseItem.status} health={health} onCaseChanged={onCaseChanged} onError={onError} /> : null}
      {ready && isVisible("analysis") ? (
        <section className="card analysis">
          <div className="section-heading">
            <h3>Support analysis</h3>
            <select value={draft.suggested_severity} onChange={(e) => setDraft({ ...draft, suggested_severity: e.target.value as Severity })}>
              <option value="critical">Critical</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option>
            </select>
          </div>
          <div className="analysis-grid">
            <Editable label="Incident summary" value={draft.incident_summary} onChange={(value) => setDraft({ ...draft, incident_summary: value })} />
            <Editable label="Customer impact" value={draft.customer_impact} onChange={(value) => setDraft({ ...draft, customer_impact: value })} />
            <Editable label="Likely cause" value={draft.likely_cause} onChange={(value) => setDraft({ ...draft, likely_cause: value })} />
            <Editable label="Workaround" value={draft.workaround} onChange={(value) => setDraft({ ...draft, workaround: value })} />
            <ListEditor label="Diagnostic steps" values={draft.diagnostic_steps} onChange={(values) => setDraft({ ...draft, diagnostic_steps: values })} />
            <ListEditor label="Information to request" values={draft.information_to_request} onChange={(values) => setDraft({ ...draft, information_to_request: values })} />
            <Editable label="Customer email subject" value={draft.customer_email_subject} onChange={(value) => setDraft({ ...draft, customer_email_subject: value })} />
            <Editable label="Customer update draft" value={draft.customer_email_body} rows={5} onChange={(value) => setDraft({ ...draft, customer_email_body: value })} />
          </div>
          {can.work ? <div className="actions">
            <button className="secondary action-save" disabled={mutating} onClick={() => onReview(draft, false)}><ActionIcon kind="save" />Save edits</button>
            <button className="primary action-save" disabled={mutating} onClick={() => onReview(draft, true)}>Approve analysis</button>
          </div> : null}
        </section>
      ) : null}
      {canPublish && can.work && isVisible("publish") ? (
        <>
          <section className="card publishing">
            <h3>Actions</h3>
            <div className="actions left">
              {(() => {
                const jiraKey = caseItem.jira_source_key || caseItem.jira_issue_key;
                const jiraUrl = caseItem.jira_source_key ? `${health?.jira_site_url}/browse/${caseItem.jira_source_key}` : caseItem.jira_issue_url;
                return jiraKey && jiraUrl ? (
                  <a className="secondary jira-link-btn" href={jiraUrl} target="_blank" rel="noreferrer">Open Jira ({jiraKey})</a>
                ) : null;
              })()}
              <button className="secondary" disabled={mutating || !health?.gmail_configured || !!caseItem.gmail_draft_id} onClick={onGmail}>
                {caseItem.gmail_draft_id ? "Gmail draft created" : "Create Gmail draft"}
              </button>
            </div>
            {!health?.gmail_configured ? <small className="muted">Configure Gmail credentials in backend/.env to enable drafts.</small> : null}
          </section>
          <section className="card closure">
            <h3>Resolution record</h3>
            <textarea rows={4} placeholder="Resolution applied and verification performed" value={resolution} onChange={(e) => setResolution(e.target.value)} />
            <textarea rows={3} placeholder="Preventive actions, one per line" value={actions} onChange={(e) => setActions(e.target.value)} />
            <button className="primary action-save" disabled={mutating || !resolution.trim()} onClick={() => onResolve(resolution, lines(actions))}><ActionIcon kind="save" />Mark resolved</button>
          </section>
        </>
      ) : null}
      {hasReport && isVisible("report") ? (
        <PostmortemCard caseId={caseItem.id} onError={onError} fallback={
        <section className="card report">
          <div className="section-heading">
            <h3>Post-incident report</h3>
            <div className="section-heading-actions">
              {caseItem.memory_indexed && can.work ? (
                <>
                  <span className="state-badge success"><ActionIcon kind="success" />Indexed</span>
                  <button className="secondary action-save" disabled={busy("index")} onClick={onIndex}><ActionIcon kind="save" />Re-index</button>
                </>
              ) : null}
            </div>
          </div>
          {([["Summary", caseItem.post_incident_report.summary], ["Impact", caseItem.post_incident_report.customer_impact],
             ["Root cause", caseItem.post_incident_report.root_cause], ["Resolution", caseItem.post_incident_report.resolution],
             ["Prevention", caseItem.post_incident_report.preventive_actions.join("; ")]] as const).map(([label, value]) => (
            <p key={label}><strong>{label}:</strong> {value.trim() ? value : <span className="muted">Not recorded.</span>}</p>
          ))}
        </section>
        } />
      ) : null}
      {isVisible("similar") ? <section className="card related">
        <div className="section-heading related-heading">
          <h3>Similar resolved incidents</h3>
          {can.work ? <button className="secondary" disabled={busy("runbook") || related.length === 0} onClick={onRunbook}>{busy("runbook") ? "Generating..." : "Generate runbook"}</button> : null}
        </div>
        {related.length === 0 ? <p className="muted">No indexed resolution matches yet.</p> : related.map((item) => (
          <article key={item.case.id}>
            <button className="related-title similar-title" onClick={() => onOpenCase(item.case.id)} title="Open this incident" type="button">{item.case.title}</button>
            <span className="meta-pair"><span>{Math.round(item.score * 100)}% {item.match_type} match</span><span>{item.case.service}</span></span>
            {item.case.resolution ? <p className="kb-resolution"><strong>Resolution:</strong> {item.case.resolution}</p> : null}
            {item.case.preventive_actions.length > 0 ? <p className="kb-resolution"><strong>Prevention:</strong> {item.case.preventive_actions.join("; ")}</p> : null}
          </article>
        ))}
      </section> : null}
      {isVisible("messages") ? <MessageThread caseId={caseItem.id} messages={messages} busy={busy("message")} newCount={newCount} onSend={onSendMessage} /> : null}
      {hasRunbook && isVisible("runbook") ? (
        <section className="card runbook">
          <h3>{runbook.title}</h3>
          <ListBlock label="Symptoms to verify" values={runbook.symptoms} />
          <ListBlock label="Verification steps" values={runbook.verification_steps} />
          <ListBlock label="Mitigation steps" values={runbook.mitigation_steps} />
          <p><strong>Escalation guidance:</strong> {runbook.escalation_guidance}</p>
          <small className="muted">Grounded in {runbook.evidence_case_ids.length} resolved incident(s).</small>
        </section>
      ) : null}
    </>
  );
}

function MessageThread({ caseId, messages, busy, newCount, onSend }: { caseId: string; messages: CaseMessage[]; busy: boolean; newCount: number; onSend: (content: string, messageType: "internal" | "customer") => void }) {
  const can = useCan();
  const firstNew = newCount > 0 ? messages.length - newCount : -1;
  const [draft, setDraft] = useState("");
  const [messageType, setMessageType] = useState<"internal" | "customer">("internal");
  const scrollRef = useRef<HTMLDivElement>(null);
  const fresh = useFresh(messages.map((m) => m.id));
  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" }); }, [messages.length, busy]);
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!draft.trim()) return;
    onSend(draft.trim(), messageType);
    setDraft("");
  }
  return (
    <section className="card messages">
      <h3 className="title-icon"><ActionIcon kind="message" size={18} />Messages</h3>
      <div className="message-scroll" ref={scrollRef}>
        {messages.length === 0 ? <p className="muted">No messages yet. Start a conversation to collaborate on this case.</p> : messages.map((msg, index) => (
          <Fragment key={msg.id}>
          {index === firstNew ? <div className="new-divider"><span>New</span></div> : null}
          <article className={`message-bubble ${msg.message_type}${fresh.has(msg.id) ? " fresh" : ""}${firstNew >= 0 && index >= firstNew ? " unread" : ""}`}>
            <span className="message-meta">
              <strong>{msg.author}</strong>
              <small className={`message-type-badge ${msg.message_type}`}>{msg.message_type}</small>
              <time>{dateTime(msg.created_at)}</time>
            </span>
            <p>{msg.content}</p>
          </article>
          </Fragment>
        ))}
        {busy ? <div className="message-sending" aria-label="Sending"><span className="tp-dots"><i /><i /><i /></span>Sending</div> : null}
      </div>
      {can.work ? <form className="message-compose" onSubmit={submit}>
        <select value={messageType} onChange={(e) => setMessageType(e.target.value as "internal" | "customer")}>
          <option value="internal">Internal note</option>
          <option value="customer">Customer message</option>
        </select>
        <input placeholder="Type a message..." value={draft} onChange={(e) => setDraft(e.target.value)} />
        <button className="primary" disabled={busy || !draft.trim()} type="submit">Send</button>
      </form> : null}
    </section>
  );
}

function timelineIcon(event: string): IconKind {
  const text = event.toLowerCase();
  if (text.includes("created")) return "plus";
  if (text.includes("imported") || text.includes("jira")) return "download";
  if (text.includes("resolved")) return "success";
  if (text.includes("approved")) return "check";
  if (text.includes("started")) return "spark";
  if (text.includes("prepared")) return "file";
  if (text.includes("gmail") || text.includes("sent") || text.includes("publish")) return "send";
  if (text.includes("analysis")) return "file";
  if (text.includes("acknowledg")) return "clock";
  if (text.includes("message")) return "message";
  if (text.includes("agent")) return "bot";
  return "activity";
}

const TIMELINE_PREVIEW = 6;

/** Activity feed, newest first. Repeats in a row fold into one entry with a count. */
function TimelineCard({ caseItem, busy, onDownloadReport }: { caseItem: SupportCase; busy: boolean; onDownloadReport: () => void }) {
  const [expanded, setExpanded] = useState(false);
  const entries: { at: string; event: string; count: number }[] = [];
  for (const entry of caseItem.timeline) {
    const last = entries[entries.length - 1];
    if (last && last.event === entry.event) { last.count += 1; last.at = entry.at; }
    else entries.push({ at: entry.at, event: entry.event, count: 1 });
  }
  entries.reverse();
  const fresh = useFresh(entries.map((e) => e.at + e.event));
  const visible = expanded ? entries : entries.slice(0, TIMELINE_PREVIEW);
  let lastDay = "";
  return (
    <section className="card timeline">
      <div className="section-heading">
        <h3 className="title-icon"><ActionIcon kind="history" size={18} />Timeline</h3>
        <button className="export-link" disabled={busy} onClick={onDownloadReport}><ActionIcon kind="download" />Report</button>
      </div>
      <ol className="activity">
        {visible.map((entry) => {
          const at = new Date(entry.at);
          const day = dayLabel(at);
          const showDay = day !== lastDay;
          lastDay = day;
          const kind = timelineIcon(entry.event);
          return (
            <li key={entry.at + entry.event} className={`${showDay ? "new-day" : ""}${fresh.has(entry.at + entry.event) ? " fresh" : ""}`}>
              {showDay ? <span className="activity-day">{day}</span> : null}
              <span className={`activity-icon icon-tone-${kind}`}><ActionIcon kind={kind} size={14} /></span>
              <span className="activity-text">{entry.event.replace(/\.$/, "")}{entry.count > 1 ? <small className="activity-count">×{entry.count}</small> : null}</span>
              <time dateTime={entry.at}>{clockTime(at)}</time>
            </li>
          );
        })}
      </ol>
      {entries.length > TIMELINE_PREVIEW ? (
        <button className="text-action show-more" onClick={() => setExpanded(!expanded)} type="button">
          {expanded ? "Show less" : `Show all ${entries.length} events`}
        </button>
      ) : null}
    </section>
  );
}

function Editable({ label, value, onChange, rows = 3, className = "" }: { label: string; value: string; onChange: (value: string) => void; rows?: number; className?: string }) {
  return <label className={className}><span>{label}</span><textarea rows={rows} value={value} onChange={(e) => onChange(e.target.value)} /></label>;
}

function ListEditor({ label, values, onChange, className = "" }: { label: string; values: string[]; onChange: (values: string[]) => void; className?: string }) {
  return <Editable className={className} label={label} value={values.join("\n")} onChange={(value) => onChange(lines(value))} />;
}

function ListBlock({ label, values }: { label: string; values: string[] }) {
  return (
    <div>
      <strong>{label}</strong>
      <ul>{values.map((value) => <li key={value}>{value}</li>)}</ul>
    </div>
  );
}

function lines(value: string) {
  return value.split("\n").map((entry) => entry.trim()).filter(Boolean);
}

function readError(caught: unknown) {
  return caught instanceof Error ? caught.message : "Request failed.";
}

/** The Grafana rule that opened a case. The alert intake writes it into the case logs as "Rule: <address>". */
function alertRuleUrl(caseItem: SupportCase) {
  const match = caseItem.customer === "Grafana alert" ? caseItem.logs.match(/^Rule: (https?:\/\/\S+)$/m) : null;
  return match ? match[1] : "";
}

function formatShortDate(value: string) {
  return shortDateTime(value);
}

function formatDeadline(value: string) {
  return dateTime(value);
}

function formatAction(action: string) {
  const words = action.replace(/[._]/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export default App;
