import { useEffect, useState } from "react";
import { getAgentMetrics } from "./api";
import { ActionIcon } from "./icons";
import { CountUp } from "./motion";
import type { AgentMetrics, AgentRun } from "./types";

export const AGENT_ORDER = ["lead", "evidence", "history", "changes", "code", "code_patch", "reviewer", "remediation", "comms",
  "finalize", "correlation", "execute"];
export const AGENT_NAMES: Record<string, string> = {
  lead: "Lead",
  evidence: "Evidence",
  history: "History",
  changes: "Changes",
  code: "Code",
  code_patch: "Code patch",
  reviewer: "Reviewer",
  remediation: "Remediation",
  correlation: "Correlation",
  verifier: "Verification",
  postmortem: "Postmortem",
  analysis: "Quick analysis",
  single: "Single agent",
  comms: "Comms",
  finalize: "Final analysis",
  execute: "Execute",
};

export function formatSeconds(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined) return "n/a";
  if (seconds === 0) return "0s";
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
}

/** Investigation spans only: the execute step runs after a human approves, which can be hours later. */
export function investigationSpans(run: AgentRun) {
  return run.spans.filter((s) => s.agent !== "execute");
}

export function investigationSeconds(run: AgentRun) {
  const spans = investigationSpans(run);
  if (!spans.length) return 0;
  return (Math.max(...spans.map((s) => Date.parse(s.started_at) + s.duration_ms)) - Math.min(...spans.map((s) => Date.parse(s.started_at)))) / 1000;
}

export function formatTokens(tokens: number | null | undefined) {
  if (tokens === null || tokens === undefined) return "n/a";
  return tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : `${Math.round(tokens)}`;
}

const percent = (value: number | null | undefined) => (value === null || value === undefined ? "n/a" : `${Math.round(value * 100)}%`);

const order = (agent: string) => { const i = AGENT_ORDER.indexOf(agent); return i < 0 ? AGENT_ORDER.length : i; };

/** Per-run view: a timeline of agent steps and a usage table, drawn from the run's spans, model calls and tool steps. */
export function RunPerformance({ run }: { run: AgentRun }) {
  const spans = investigationSpans(run);
  if (!spans.length) return null;
  const starts = spans.map((s) => Date.parse(s.started_at));
  const t0 = Math.min(...starts);
  const total = Math.max(investigationSeconds(run) * 1000, 1);
  const lanes = [...new Set(spans.map((s) => s.agent))].sort((a, b) => order(a) - order(b));
  // every agent with a step or a model call; the patch call of the Code agent has no step of its own
  const present = new Set([...run.spans.map((s) => s.agent), ...run.llm_calls.map((c) => c.agent)]);
  const agents = [...AGENT_ORDER.filter((a) => present.has(a)), ...[...present].filter((a) => !AGENT_ORDER.includes(a))];
  const tokensIn = run.llm_calls.reduce((n, c) => n + c.input_tokens, 0);
  const tokensOut = run.llm_calls.reduce((n, c) => n + c.output_tokens, 0);
  const failedTools = run.steps.filter((s) => !s.ok).length;
  const retried = run.llm_calls.filter((c) => !c.ok).length;
  const byModel = Object.entries(run.llm_calls.filter((c) => c.ok && c.model).reduce<Record<string, number>>(
    (counts, c) => ({ ...counts, [c.model!]: (counts[c.model!] ?? 0) + 1 }), {}));
  const ticks = [0, 0.25, 0.5, 0.75, 1];

  return (
    <div className="agent-block agent-perf">
      <div className="perf-summary">
        <span><strong>{formatSeconds(total / 1000)}</strong> investigation</span>
        <span><strong>{run.llm_calls.length - retried}</strong> model calls{retried ? ` (${retried} retried on backup)` : ""}</span>
        <span><strong>{formatTokens(tokensIn)}</strong> tokens in · <strong>{formatTokens(tokensOut)}</strong> out</span>
        <span><strong>{run.steps.length}</strong> tool calls{failedTools ? ` (${failedTools} failed)` : ""}</span>
        {byModel.length ? <span>{byModel.map(([model, count]) => `${model} ×${count}`).join(", ")}</span> : null}
      </div>

      <div className="perf-timeline" role="img" aria-label="Timeline of agent steps">
        <div className="perf-axis">
          {ticks.map((t, i) => (
            <span key={t} className={i % 2 ? "minor" : ""} style={{ left: `${t * 100}%` }}>{formatSeconds((total * t) / 1000)}</span>
          ))}
        </div>
        {lanes.map((agent) => (
          <div className="perf-row" key={agent}>
            <span className="perf-label">{AGENT_NAMES[agent] ?? agent}</span>
            <div className="perf-track">
              {ticks.slice(1, -1).map((t) => <i key={t} className="perf-grid" style={{ left: `${t * 100}%` }} />)}
              {spans.map((span, i) => span.agent !== agent ? null : (
                <span
                  key={i}
                  className={`perf-bar ${agent}`}
                  style={{ left: `${((starts[i] - t0) / total) * 100}%`, width: `max(${(span.duration_ms / total) * 100}%, 4px)` }}
                  title={`${AGENT_NAMES[agent] ?? agent}: ${formatSeconds(span.duration_ms / 1000)}`}
                />
              ))}
            </div>
            <span className="perf-value">
              {formatSeconds(spans.filter((s) => s.agent === agent).reduce((n, s) => n + s.duration_ms, 0) / 1000)}
            </span>
          </div>
        ))}
      </div>
      <p className="muted perf-note">The specialists (evidence, history, changes, code) run in parallel. A second bar means the reviewer sent that agent back.</p>

      <div className="perf-table-wrap"><table className="perf-table">
        <thead>
          <tr><th>Agent</th><th>Time</th><th>Model calls</th><th>Tokens in</th><th>Tokens out</th><th>Tool calls</th></tr>
        </thead>
        <tbody>
          {agents.map((agent) => {
            const calls = run.llm_calls.filter((c) => c.agent === agent);
            return (
              <tr key={agent}>
                <td>{AGENT_NAMES[agent] ?? agent}</td>
                <td>{formatSeconds((run.spans.some((s) => s.agent === agent)  // a step's time, else its model calls' time
                  ? run.spans.filter((s) => s.agent === agent).reduce((n, s) => n + s.duration_ms, 0)
                  : calls.reduce((n, c) => n + (c.duration_ms ?? 0), 0)) / 1000)}</td>
                <td>{calls.length}</td>
                <td>{formatTokens(calls.reduce((n, c) => n + c.input_tokens, 0))}</td>
                <td>{formatTokens(calls.reduce((n, c) => n + c.output_tokens, 0))}</td>
                <td>{run.steps.filter((s) => s.agent === agent).length || "n/a"}</td>
              </tr>
            );
          })}
        </tbody>
      </table></div>
    </div>
  );
}

/** Insights card: how the agent team performs across all runs. */
export function AgentMetricsCard() {
  const [metrics, setMetrics] = useState<AgentMetrics | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    getAgentMetrics().then(setMetrics).catch(() => setFailed(true));
  }, []);

  if (failed) return null;
  if (!metrics) return <section className="card insight-card agent-metrics"><h3 className="title-icon"><ActionIcon kind="gauge" size={18} />Agent team performance</h3><p className="muted">Loading…</p></section>;
  if (metrics.runs_total === 0) {
    return (
      <section className="card insight-card agent-metrics">
        <h3 className="title-icon"><ActionIcon kind="gauge" size={18} />Agent team performance</h3>
        <p className="muted">No agent runs yet. Runs start automatically on new cases.</p>
      </section>
    );
  }
  const decisions = ["approve", "edit", "reject"].map((d) => [d, metrics.decisions[d] ?? 0] as const);
  const decided = decisions.reduce((n, [, c]) => n + c, 0);
  const verdicts = ["correct", "partly_correct", "wrong"].map((v) => [v, metrics.lesson_verdicts[v] ?? 0] as const);
  const graded = verdicts.reduce((n, [, c]) => n + c, 0);
  const timed = Math.max(0, ...metrics.per_agent.map((a) => a.runs));
  const tiles: [string, string, string][] = [
    ["Runs", `${metrics.runs_total}`, Object.entries(metrics.by_status).map(([k, v]) => `${v} ${k.replace("_", " ")}`).join(", ")],
    ["Median run", formatSeconds(metrics.median_investigation_seconds), `p90 ${formatSeconds(metrics.p90_investigation_seconds)}`],
    ["Tokens / run", formatTokens(metrics.avg_input_tokens), `${formatTokens(metrics.avg_output_tokens)} out`],
    ["Tool calls / run", metrics.avg_tool_calls?.toFixed(1) ?? "n/a", `${percent(metrics.tool_failure_rate)} failed`],
    ["Reviewer pushback", percent(metrics.reviewer_rejection_rate), "of reviewed runs"],
  ];

  return (
    <section className="card insight-card agent-metrics">
      <div className="section-heading">
        <h3 className="title-icon"><ActionIcon kind="gauge" size={18} />Agent team performance</h3>
        {timed < metrics.runs_total ? <small className="muted" title="Time, token and per-agent figures use only runs recorded with timing data.">Timing from {timed} of {metrics.runs_total} runs</small> : null}
      </div>
      <div className="metric-tiles">
        {tiles.map(([label, value, note]) => (
          <article key={label}><span>{label}</span><strong>{/^\d+$/.test(value) ? <CountUp value={Number(value)} /> : value}</strong><small>{note}</small></article>
        ))}
      </div>
      <div className="metric-split">
        <div>
          <h4>Engineer decisions</h4>
          {decided ? (
            <>
              <div className="stack-bar" role="img" aria-label="Approved, edited and rejected proposals">
                {decisions.map(([d, c]) => c ? <span key={d} className={`seg ${d}`} style={{ flex: c }} /> : null)}
              </div>
              <p className="stack-legend">
                {decisions.map(([d, c]) => <span key={d}><i className={`dot ${d}`} />{d === "approve" ? "Approved" : d === "edit" ? "Edited" : "Rejected"} {c}</span>)}
              </p>
            </>
          ) : <p className="muted">No decisions yet.</p>}
        </div>
        <div>
          <h4>Diagnosis accuracy</h4>
          {graded ? (
            <>
              <div className="stack-bar" role="img" aria-label="Lesson verdicts">
                {verdicts.map(([v, c]) => c ? <span key={v} className={`seg ${v}`} style={{ flex: c }} /> : null)}
              </div>
              <p className="stack-legend">
                {verdicts.map(([v, c]) => <span key={v}><i className={`dot ${v}`} />{v === "partly_correct" ? "Partly correct" : v[0].toUpperCase() + v.slice(1)} {c}</span>)}
              </p>
            </>
          ) : <p className="muted">Not graded yet. Scored when a case is resolved.</p>}
        </div>
      </div>
      {metrics.per_agent.length ? (
        <details className="agent-details">
        <summary>Per-agent breakdown</summary>
        <div className="perf-table-wrap"><table className="perf-table">
          <thead><tr><th>Agent</th><th>Avg time</th><th>Avg model calls</th><th>Avg tokens in</th><th>Avg tokens out</th></tr></thead>
          <tbody>
            {metrics.per_agent.map((a) => (
              <tr key={a.agent}>
                <td>{AGENT_NAMES[a.agent] ?? a.agent}</td>
                <td>{formatSeconds(a.avg_seconds)}</td>
                <td>{a.avg_llm_calls.toFixed(1)}</td>
                <td>{formatTokens(a.avg_input_tokens)}</td>
                <td>{formatTokens(a.avg_output_tokens)}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
        </details>
      ) : null}
    </section>
  );
}
