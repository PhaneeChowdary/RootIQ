import { dateTime, shortDateTime } from "./time";
import { type CSSProperties, useContext, useEffect, useRef, useState } from "react";
import { formatSeconds, formatTokens, investigationSeconds, RunPerformance } from "./AgentObservability";
import { UserNameContext, useCan } from "./role";
import { getRunRatings, rateRun, type RatingSummary } from "./api";
import { DiffView } from "./DiffView";
import { ActionIcon } from "./icons";
import { Inline, Markdown, plainText } from "./Markdown";
import { Portal } from "./live";
import { TeamProgress } from "./TeamProgress";
import { decideAgentRun, getAgentRun, getCaseLesson, listAgentRuns, startAgentRun, stopAgentRun } from "./api";
import type { ActionDecision, ActionOutcome, AgentLesson, AgentRun, AgentRunStatus, AgentRunTrigger, Health, ProposedAction, VerificationResult } from "./types";

const POLL_MS = 2000;
const LIVE: AgentRunStatus[] = ["queued", "running", "executing"];
const STATUS_LABELS: Record<AgentRunStatus, string> = {
  queued: "Queued",
  running: "Investigating",
  awaiting_approval: "Awaiting your approval",
  executing: "Executing",
  completed: "Completed",
  failed: "Failed",
  stopped: "Stopped",
};
const RUN_COST = "A run takes 3 to 10 minutes and about 25 to 40 model calls.";
const CONFIRM = {
  run: { title: "Run the agent team", button: "Run", text: `The agents will investigate this case and propose next steps. ${RUN_COST}` },
  again: { title: "Run the agent team again", button: "Run again", text: `A new investigation will start. The earlier run will stay in the run list. ${RUN_COST}` },
  stop: { title: "Stop the investigation", button: "Stop",
    text: "The agents will stop after the step they are on. Their work up to that point will be kept. You can run them again later." },
  restart: { title: "Restart the investigation", button: "Restart",
    text: `This run will stop, and a new investigation will start from the beginning. ${RUN_COST}` },
  rerun: { title: "Run again", button: "Run again",
    text: `The proposals that wait for approval will be discarded, and a new investigation will start. ${RUN_COST}` },
} as const;

const AGENT_LABELS: Record<string, string> = {
  evidence: "Evidence",
  history: "History",
  comms: "Comms",
  agent: "Agent",
};
const TRIGGER_SHORT: Record<AgentRunTrigger, string> = {
  manual: "Started manually",
  case_created: "Started when the case was created",
  jira_import: "Started on Jira import",
  sla_risk: "Started because the SLA was at risk",
  alert: "Started by a Grafana alert",
  verification_failed: "Started because a fix did not work",
};
const VERDICT_LABELS: Record<AgentLesson["verdict"], string> = {
  correct: "Team diagnosis was correct",
  partly_correct: "Team diagnosis was partly correct",
  wrong: "Team diagnosis was wrong",
};
const OUTCOME_LABELS: Record<ActionOutcome["status"], string> = {
  executed: "Executed",
  dry_run: "Dry run",
  skipped: "Skipped",
  failed: "Failed",
};
const FIELD_LABELS: Record<string, string> = { subject: "Subject", body: "Message", comment: "Comment" };
/** The proposal's editable fields as plain text (older runs can still hold Markdown). */
function cleanFields(proposal: ProposedAction): Record<string, string> {
  const fields: Record<string, string> = { ...proposal.payload };
  for (const key of EDITABLE[proposal.kind]) if (typeof fields[key] === "string") fields[key] = plainText(fields[key]);
  return fields;
}

const EDITABLE: Record<ProposedAction["kind"], string[]> = {
  customer_update: ["subject", "body"],
  jira_comment: ["comment"],
  remediation: [],
  code_change: [],
};
const VERIFY_LABELS: Record<VerificationResult["status"], string> = { fixed: "Fixed", not_fixed: "Not fixed", inconclusive: "Not clear yet" };
const RATINGS = [["correct", "Right"], ["partly_correct", "Partly"], ["wrong", "Wrong"]] as const;

type Draft = { approve: boolean; fields: Record<string, string> };

export function AgentPanel({ caseId, caseStatus, health, onCaseChanged, onError, compact = false, onOpen }: {
  caseId: string;
  caseStatus: string;
  health: Health | null;
  onCaseChanged: () => void;
  onError: (message: string) => void;
  compact?: boolean;
  onOpen?: () => void;
}) {
  const can = useCan();
  const me = useContext(UserNameContext);
  const [ratings, setRatings] = useState<RatingSummary | null>(null);
  const label = (status: AgentRun["status"]) => (status === "awaiting_approval" && !can.work ? "Awaiting approval" : STATUS_LABELS[status]);
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [busy, setBusy] = useState(false);
  const [justDecided, setJustDecided] = useState(false);  // show the actions finish one by one, only right after Submit
  const [lesson, setLesson] = useState<AgentLesson | null>(null);
  const lastStatus = useRef<Record<string, AgentRunStatus>>({});

  const run = runs.find((r) => r.id === selectedId) ?? runs[0] ?? null;
  // The buttons follow the run on screen. The server stops an older run itself when a new one replaces it.
  const [confirm, setConfirm] = useState<null | keyof typeof CONFIRM>(null);

  function remember(next: AgentRun) {
    const previous = lastStatus.current[next.id];
    lastStatus.current[next.id] = next.status;
    if (previous && previous !== next.status && (next.status === "awaiting_approval" || next.status === "completed")) {
      onCaseChanged();
    }
    setRuns((current) => {
      const others = current.filter((r) => r.id !== next.id);
      return [next, ...others].sort((a, b) => b.created_at.localeCompare(a.created_at));
    });
  }

  useEffect(() => {
    setRuns([]);
    setSelectedId(null);
    lastStatus.current = {};
    listAgentRuns(caseId)
      .then((loaded) => {
        loaded.forEach((r) => { lastStatus.current[r.id] = r.status; });
        setRuns(loaded);
      })
      .catch((caught: unknown) => onError(caught instanceof Error ? caught.message : String(caught)));
  }, [caseId]);

  useEffect(() => {
    setLesson(null);
    if (caseStatus !== "resolved") return;
    // the lesson is written shortly after resolution, so look again for a minute if it is not there yet
    let tries = 0;
    const load = () => getCaseLesson(caseId).then((found) => { if (found) setLesson(found); return found; }).catch(() => null);
    const timer = window.setInterval(() => {
      tries += 1;
      void load().then((found) => { if (found || tries >= 20) window.clearInterval(timer); });
    }, 3000);
    void load().then((found) => { if (found) window.clearInterval(timer); });
    return () => window.clearInterval(timer);
  }, [caseId, caseStatus]);

  useEffect(() => {
    if (!run || !LIVE.includes(run.status)) return;
    const timer = window.setInterval(() => {
      getAgentRun(run.id).then(remember).catch(() => {});
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [run?.id, run?.status]);

  useEffect(() => {
    if (!run?.id || !run.analysis) { setRatings(null); return; }
    getRunRatings(run.id).then(setRatings).catch(() => setRatings(null));
  }, [run?.id, Boolean(run?.analysis)]);

  useEffect(() => {
    if (run?.status !== "awaiting_approval") return;
    setDrafts(run.proposals.map((p) => ({ approve: true, fields: cleanFields(p) })));
  }, [run?.id, run?.status]);

  async function start(replace = false) {
    setBusy(true);
    try {
      const created = await startAgentRun(caseId, replace);
      if (replace) listAgentRuns(caseId).then(setRuns).catch(() => {});  // the replaced run is now "stopped"
      lastStatus.current[created.id] = created.status;
      setSelectedId(created.id);
      remember(created);
    } catch (caught: unknown) {
      onError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    if (!run) return;
    setBusy(true);
    try { remember(await stopAgentRun(run.id)); }
    catch (caught: unknown) { onError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(false); }
  }

  async function submit() {
    if (!run) return;
    setJustDecided(true);
    const decisions: ActionDecision[] = run.proposals.map((proposal, index) => {
      const draft = drafts[index];
      if (!draft?.approve) return { index, action: "reject" };
      // Compare with the cleaned text: a cleaned field is not an engineer's edit . The backend cleans it the same way.
      const edited = EDITABLE[proposal.kind].filter((key) => draft.fields[key] !== cleanFields(proposal)[key]);
      return edited.length
        ? { index, action: "edit", payload: Object.fromEntries(edited.map((key) => [key, draft.fields[key]])) }
        : { index, action: "approve" };
    });
    setBusy(true);
    try {
      remember(await decideAgentRun(run.id, decisions));
    } catch (caught: unknown) {
      onError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  const live = run ? LIVE.includes(run.status) : false;
  const tokens = run ? run.llm_calls.reduce((n, c) => n + c.input_tokens + c.output_tokens, 0) : 0;
  const agentSeconds = run ? investigationSeconds(run) : 0;

  const confirmWindow = confirm ? (
        <Portal><div className="modal-backdrop" role="presentation" onClick={() => setConfirm(null)}>
          <section aria-label={CONFIRM[confirm].title} aria-modal="true" className={`status-modal ${confirm === "stop" ? "delete" : "confirm"}`} role="alertdialog" onClick={(e) => e.stopPropagation()}>
            <button aria-label="Close" className="modal-close" onClick={() => setConfirm(null)} type="button">×</button>
            <p className="modal-label">{CONFIRM[confirm].title}</p>
            <p>{CONFIRM[confirm].text}</p>
            <div className="actions">
              <button className="secondary" onClick={() => setConfirm(null)} type="button">Cancel</button>
              <button className={confirm === "stop" ? "danger" : "primary"} type="button"
                onClick={() => { const kind = confirm; setConfirm(null); void (kind === "stop" ? stop() : start(kind === "restart" || kind === "rerun")); }}>
                <ActionIcon kind={confirm === "stop" ? "stop" : confirm === "restart" ? "restart" : confirm === "run" ? "play" : "reset"} />{CONFIRM[confirm].button}
              </button>
            </div>
          </section>
        </div></Portal>
      ) : null;

  if (compact) {
    const needsDecision = run?.status === "awaiting_approval";
    return (
      <section className="card agent-summary">
        {confirmWindow}
        <div className="agent-summary-head">
          <span className="agent-summary-icon"><ActionIcon kind="spark" size={18} /></span>
          <h3>Agent team</h3>
          {run ? <span className={`agent-badge ${run.status}`}>{label(run.status)}</span> : null}
          {!run && !can.work ? null : <button className={needsDecision ? "primary" : "secondary"} disabled={busy} onClick={() => (run ? onOpen?.() : setConfirm("run"))}>
            {!run ? <><ActionIcon kind="play" />Run agent team</> : needsDecision ? <>Review proposals<ActionIcon kind="arrow" /></> : <>Open<ActionIcon kind="arrow" /></>}
          </button>}
        </div>
        {run?.analysis ? (
          <p className="agent-summary-cause"><strong>Likely cause:</strong> {truncate(run.analysis.likely_cause, 280)}</p>
        ) : run && LIVE.includes(run.status) ? (
          <TeamProgress run={run} compact />
        ) : run ? (
          <p className="muted agent-summary-cause">{run.error || "No result yet."}</p>
        ) : (
          <p className="muted agent-summary-cause">No investigation yet. The team reads the logs and past incidents, then proposes next steps for your approval.</p>
        )}
      </section>
    );
  }

  return (
    <section className="card agent-panel">
      {confirmWindow}
      <div className="agent-head">
        <div>
          <h3 className="title-icon"><ActionIcon kind="bot" size={20} />Agent team investigation</h3>
          <p className="muted agent-sub">
            {5 + (run?.team?.length ?? 0)} agents · {health?.agent_model || "local model"}
            {health?.agent_dry_run ? <span className="agent-badge dry_run" title="Approved actions are recorded, not sent.">Dry run</span> : null}
          </p>
        </div>
        <div className="run-controls">
          {!can.work ? null : run && (run.status === "queued" || run.status === "running") ? (
            <>
              <button className="secondary" disabled={busy || run.stage.startsWith("Stopping")} onClick={() => setConfirm("restart")} type="button"><ActionIcon kind="restart" />Restart</button>
              <button className="danger" disabled={busy || run.stage.startsWith("Stopping")} onClick={() => setConfirm("stop")} type="button">
                <ActionIcon kind="stop" />{run.stage.startsWith("Stopping") ? "Stopping…" : "Stop"}
              </button>
            </>
          ) : run?.status === "awaiting_approval" ? (
            <button className="secondary" disabled={busy} onClick={() => setConfirm("rerun")} type="button"><ActionIcon kind="reset" />Run again</button>
          ) : run?.status !== "executing" ? (
            <button className="secondary" disabled={busy} onClick={() => setConfirm(run ? "again" : "run")} type="button"><ActionIcon kind={run ? "reset" : "play"} />{run ? "Run again" : "Run agent team"}</button>
          ) : null}
        </div>
      </div>

      {!run ? <p className="muted">No agent runs yet for this case.</p> : (
        <div className="agent-run">
          <div className="agent-status">
            <span className={`agent-badge ${run.status}`}>{label(run.status)}</span>
            <span className="muted">
              {`${TRIGGER_SHORT[run.trigger]}${run.trigger === "manual" ? ` by ${run.started_by}` : ""}${runs.length > 1 ? "" : ` · ${dateTime(run.created_at)}`}`}
            </span>
            {runs.length > 1 ? (
              <select aria-label="Choose a run" className="agent-run-select" value={run.id} onChange={(e) => setSelectedId(e.target.value)}>
                {runs.map((r) => (
                  <option key={r.id} value={r.id}>{shortDateTime(r.created_at)} · {label(r.status)}</option>
                ))}
              </select>
            ) : null}
          </div>
          {live ? <TeamProgress run={run} /> : null}
          {run.error ? <p className={run.status === "stopped" ? "agent-stopped" : "agent-error"}>{run.status === "stopped" ? <ActionIcon kind="stop" size={14} /> : null}{run.error}{run.status === "stopped" ? " The work up to that point is kept below." : ""}</p> : null}

          {run.analysis ? (
            <div className="agent-result">
              <h4>Likely cause</h4>
              <p><Inline text={run.analysis.likely_cause} /></p>
              {run.severity_note ? <p className="muted">{run.severity_note}</p> : null}
              {(run.warnings ?? []).map((w) => <p key={w} className="agent-warning">{w}</p>)}
              <p className="muted agent-note">Full analysis is on the Analysis tab.</p>
            </div>
          ) : null}

          {run.status === "awaiting_approval" ? (
            <div className="agent-block agent-decision">
              <h4>Proposed actions</h4>
              {run.proposals.map((proposal, index) => {
                const draft = drafts[index];
                if (!draft) return null;
                const votes = (run.approvals ?? []).filter((v) => v.index === index && v.action !== "reject");
                const needed = proposal.approvals_needed ?? 1;
                const mine = votes.some((v) => v.by === me);
                return (
                  <article key={index} className={`agent-proposal ${draft.approve ? "" : "rejected"}${proposal.kind === "remediation" ? " remediation" : ""}`}>
                    <div className="proposal-chips">
                      <span className={`risk-chip ${proposal.risk ?? "low"}`}>{proposal.risk ?? "low"} risk</span>
                      {needed === 0 ? <span className="approval-chip auto">Runs without approval</span>
                        : <span className={`approval-chip${votes.length ? " progress" : ""}`}>
                            {Array.from({ length: needed }, (_, n) => <i className={n < votes.length ? "on" : ""} key={n} />)}
                            {votes.length ? `${votes.map((v) => v.by === me ? "You" : v.by).join(", ")} approved · ${Math.max(0, needed - votes.length)} more` : `${needed} ${needed === 1 ? "approval" : "approvals"}`}
                          </span>}
                    </div>
                    <div className="agent-proposal-head">
                      <strong>{proposal.summary}</strong>
                      <label>
                        <input
                          type="checkbox"
                          disabled={!can.work}
                          checked={draft.approve}
                          onChange={(e) => setDrafts(drafts.map((d, i) => i === index ? { ...d, approve: e.target.checked } : d))}
                        />
                        Approve
                      </label>
                    </div>
                    {proposal.kind === "remediation" ? (
                      <dl className="remediation-facts">
                        <div><dt>Why</dt><dd><Inline text={proposal.payload.reason ?? ""} /></dd></div>
                        <div><dt>Check</dt><dd>{proposal.payload.verify}</dd></div>
                        <div><dt>Undo</dt><dd>{proposal.payload.rollback_plan}</dd></div>
                      </dl>
                    ) : null}
                    {proposal.kind === "code_change" ? <PatchView proposal={proposal} /> : null}
                    {mine && votes.length < needed ? <p className="proposal-wait"><ActionIcon kind="clock" size={13} />Another person must approve</p> : null}
                    {EDITABLE[proposal.kind].map((key) => (
                      <label key={key} className="agent-field">
                        {FIELD_LABELS[key]}
                        <textarea
                          rows={key === "subject" ? 1 : 4}
                          disabled={!draft.approve || !can.work}
                          value={draft.fields[key] ?? ""}
                          onChange={(e) => setDrafts(drafts.map((d, i) => i === index ? { ...d, fields: { ...d.fields, [key]: e.target.value } } : d))}
                        />
                      </label>
                    ))}
                  </article>
                );
              })}
              <div className="actions">
                {can.work
                  ? <button className="primary" disabled={busy} onClick={submit}><ActionIcon kind="check" />Submit decisions</button>
                  : <span className="view-only-note"><ActionIcon kind="eye" size={14} />An engineer or admin approves these actions.</span>}
              </div>
            </div>
          ) : null}

          {run.status === "executing" && !run.outcomes.length ? (
            <div className="agent-block">
              <h4>Doing the approved actions</h4>
              <ul className="agent-outcomes">
                {run.proposals.filter((_, i) => drafts[i]?.approve ?? true).map((p, i) => (
                  <li key={i} className="pending"><span className="step-mark"><ActionIcon kind="loader" size={14} /></span>{p.summary}<small className="muted"> · Sending…</small></li>
                ))}
              </ul>
            </div>
          ) : null}
          {run.outcomes.length ? (
            <div className="agent-block">
              <h4>What happened</h4>
              <ul className="agent-outcomes">
                {run.outcomes.map((o, i) => (
                  <li key={o.index} className={`${o.status}${justDecided ? " fresh" : ""}`} style={{ "--i": i } as CSSProperties}>
                    <span className="step-mark">
                      <ActionIcon kind="loader" size={14} />
                      <ActionIcon kind={o.status === "failed" ? "fail" : o.status === "skipped" ? "close" : "check"} size={13} />
                    </span>
                    <span className="outcome-text"><span className={`agent-badge ${o.status}`}>{OUTCOME_LABELS[o.status]}</span> {o.detail}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : run.status === "completed" && run.proposals.length === 0 ? (
            <p className="muted">The team proposed no actions.</p>
          ) : null}

          {run.status !== "awaiting_approval" && run.proposals.some((p) => p.kind === "code_change") ? (
            <div className="agent-block">
              <h4>Code patch</h4>
              {run.proposals.map((proposal, index) => {
                if (proposal.kind !== "code_change") return null;
                // the decision stays with the patch: the app never applies it, a person applies it in code review
                const decision = run.outcomes.find((o) => o.index === index)?.decision;
                return (
                  <article key={index} className="agent-proposal">
                    <div className="agent-proposal-head">
                      <strong>{proposal.summary}</strong>
                      {decision === "reject" ? <span className="agent-badge skipped">Rejected</span>
                        : decision && decision !== "none" ? <span className="agent-badge executed">Approved · not applied</span> : null}
                    </div>
                    <PatchView proposal={proposal} />
                  </article>
                );
              })}
            </div>
          ) : null}

          {(run.verifications ?? []).length ? (
            <div className="agent-block">
              <h4>Verification</h4>
              <ul className="verify-list">
                {run.verifications.map((v) => (
                  <li className={`verify-${v.status}`} key={`${v.check}-${v.at}`}>
                    <span className={`verify-badge ${v.status}`}>{VERIFY_LABELS[v.status]}</span>
                    <span className="verify-numbers"><b>{v.errors_before}</b> errors before <ActionIcon kind="arrow" size={12} /> <b>{v.errors_after}</b> after<small> · {v.minutes_after} min</small></span>
                    <small className="muted verify-summary">{v.summary.replace(/^\d+ errors in [\d.]+ min before the fix, \d+ in [\d.]+ min after it\.\s*/, "")}</small>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {run.analysis && ["awaiting_approval", "completed", "executing"].includes(run.status) && can.work ? (
            <div className="rating-row">
              <span>Diagnosis</span>
              <div className="role-segments" role="radiogroup" aria-label="Rate the diagnosis">
                {RATINGS.map(([value, label]) => {
                  const chosen = ratings?.ratings.find((r) => r.by === me)?.verdict === value;
                  return (
                    <button aria-checked={chosen} className={chosen ? `active ${value === "wrong" ? "admin" : "engineer"}` : ""} key={value} role="radio" type="button"
                      onClick={async () => { try { setRatings(await rateRun(run.id, value)); } catch (e: unknown) { onError(e instanceof Error ? e.message : String(e)); } }}>{label}</button>
                  );
                })}
              </div>
              {ratings && ratings.ratings.length > 1 ? <small className="muted">{ratings.ratings.length} ratings</small> : null}
            </div>
          ) : null}

          {run.plan || run.steps.length ? (
            <details className="agent-details" open={live} key={`details-${run.id}-${live}`}>
              <summary>
                How the team worked it out
                <span className="muted"> · {run.steps.length} tool call{run.steps.length === 1 ? "" : "s"}{run.reviews.length ? ` · ${run.reviews.length} review${run.reviews.length === 1 ? "" : "s"}` : ""}</span>
              </summary>
              {run.plan ? (
                <div className="agent-block">
                  <h4>Plan</h4>
                  {run.plan.hypotheses.length ? <ul>{run.plan.hypotheses.map((h) => <li key={h}><Inline text={h} /></li>)}</ul> : null}
                  <p className="agent-task"><span className="agent-tag evidence">Evidence</span> <Inline text={run.plan.evidence_task} /></p>
                  <p className="agent-task"><span className="agent-tag history">History</span> <Inline text={run.plan.history_task} /></p>
                </div>
              ) : null}
              {run.lessons_used.length ? (
                <div className="agent-block">
                  <h4>Lessons applied from past investigations</h4>
                  <ul>{run.lessons_used.map((item) => <li key={item}>{item}</li>)}</ul>
                </div>
              ) : null}
              {run.reviews.length ? (
                <div className="agent-block">
                  <h4>Reviewer</h4>
                  {run.reviews.map((review, i) => (
                    <div key={i} className="agent-review">
                      <strong>Pass {i + 1}: {review.approved ? "approved" : review.recheck.length ? `sent back to ${review.recheck.join(" and ")}` : "not approved"}</strong>
                      {review.unsupported_claims.length ? <ul>{review.unsupported_claims.map((c) => <li key={c}><span className="agent-claim-label">Unsupported</span> <Inline text={c} /></li>)}</ul> : null}
                      {!review.approved && review.instructions ? <p className="muted agent-instructions"><Inline text={review.instructions} /></p> : null}
                    </div>
                  ))}
                </div>
              ) : null}
              {Object.keys(run.findings).length ? (
                <div className="agent-block">
                  <h4>Specialist findings</h4>
                  {Object.entries(run.findings).map(([name, text]) => (
                    <details key={name} className="agent-finding">
                      <summary>{AGENT_LABELS[name] ?? name} agent</summary>
                      <Markdown className="agent-text" text={text} />
                    </details>
                  ))}
                </div>
              ) : null}
              <div className="agent-block">
                <h4>Trace</h4>
                {run.steps.length === 0 ? (
                  <p className="muted">{run.status === "queued" ? "Waiting for the run to start." : "Waiting for the first tool call."}</p>
                ) : (
                  <ol className="agent-steps">
                    {run.steps.map((step) => (
                      <li key={step.index} className={step.ok ? "" : "failed"}>
                        <details>
                          <summary>
                            <span className={`agent-tag ${step.agent}`}>{AGENT_LABELS[step.agent] ?? step.agent}</span>
                            <code>{step.tool}</code>
                            <span className="muted">{formatArgs(step.arguments)}</span>
                            <span className="agent-step-meta">{step.ok ? "" : "failed · "}{step.duration_ms} ms</span>
                          </summary>
                          <pre>{step.result}</pre>
                        </details>
                      </li>
                    ))}
                  </ol>
                )}
              </div>
            </details>
          ) : null}

          {run.spans.length ? (
            <details className="agent-details">
              <summary>
                Performance
                <span className="muted"> · {formatSeconds(agentSeconds)} · {run.llm_calls.filter((c) => c.ok).length} model calls · {formatTokens(tokens)} tokens</span>
              </summary>
              <RunPerformance run={run} />
            </details>
          ) : null}
        </div>
      )}

      {lesson ? (
        <div className="agent-block agent-lesson">
          <h4>What the team learned from this case</h4>
          <span className={`agent-badge verdict-${lesson.verdict}`}>{VERDICT_LABELS[lesson.verdict]}</span>
          <p>{lesson.lesson}</p>
          <p className="muted">Future investigations of similar incidents receive this lesson.</p>
        </div>
      ) : null}
    </section>
  );
}

function formatArgs(args: Record<string, unknown>) {
  const shown = Object.entries(args).filter(([, v]) => v !== "" && v !== null && v !== undefined);
  const text = shown.map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v) : String(v)}`).join(", ");
  return text.length > 90 ? text.slice(0, 90) + "…" : text;
}

function truncate(raw: string, max: number) {
  const text = raw.replace(/\*\*|__|`/g, "");  // plain preview: a cut must not leave a stray marker
  return text.length > max ? text.slice(0, max).replace(/\s+\S*$/, "") + "…" : text;
}

/** A proposed patch: the reason and the diff . The app never applies it. */
function PatchView({ proposal }: { proposal: ProposedAction }) {
  return (
    <div className="patch">
      <p className="patch-reason"><Inline text={proposal.payload.reason ?? ""} /></p>
      <DiffView diff={proposal.payload.diff ?? ""} path={proposal.payload.path ?? ""} />
    </div>
  );
}
