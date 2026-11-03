import { Fragment, useEffect, useRef, useState } from "react";
import { formatSeconds, formatTokens } from "./AgentObservability";
import { ActionIcon, type IconKind } from "./icons";
import type { AgentRun } from "./types";

type Node = "lead" | "evidence" | "history" | "changes" | "code" | "reviewer" | "remediation" | "comms" | "finalize" | "execute";
type State = "idle" | "active" | "done";

const NODES: Record<Node, { label: string; icon: IconKind }> = {
  lead: { label: "Lead", icon: "bot" },
  evidence: { label: "Evidence", icon: "search" },
  history: { label: "History", icon: "history" },
  changes: { label: "Changes", icon: "commit" },
  code: { label: "Code", icon: "code" },
  remediation: { label: "Fix", icon: "shield" },
  reviewer: { label: "Reviewer", icon: "report" },
  comms: { label: "Comms", icon: "send" },
  finalize: { label: "Final", icon: "spark" },
  execute: { label: "Execute", icon: "zap" },
};
const ORDER: Node[] = ["lead", "evidence", "history", "changes", "code", "reviewer", "remediation", "comms", "finalize", "execute"];
const SPECIALISTS: Node[] = ["evidence", "history", "changes", "code"];
const OPTIONAL: Node[] = ["changes", "code"];

/** Which agents are working now, read from the run's stage . */
function activeNodes(run: AgentRun): Node[] {
  const stage = run.stage.toLowerCase();
  if (run.status === "executing") return ["execute"];
  if (run.status === "queued") return [];
  const specialists = SPECIALISTS.filter((n) => !OPTIONAL.includes(n) || (run.team ?? []).includes(n));
  if (stage.includes("rechecking")) {
    const recheck = run.reviews[run.reviews.length - 1]?.recheck.filter((a: string): a is Node => specialists.includes(a as Node));
    return recheck?.length ? recheck : specialists;
  }
  if (stage.includes("specialists") || stage.includes("evidence and history")) return specialists;
  if (stage.includes("remediation")) return ["remediation"];
  if (stage.includes("reviewer")) return ["reviewer"];
  if (stage.includes("comms")) return ["comms"];
  if (stage.includes("final")) return ["finalize"];
  if (stage.includes("lead")) return ["lead"];
  const last = run.llm_calls[run.llm_calls.length - 1]?.agent as Node | undefined;
  return last && last in NODES ? [last] : ["lead"];
}

function nodeStates(run: AgentRun): Record<Node, State> {
  const active = activeNodes(run);
  const first = active.length ? Math.min(...active.map((n) => ORDER.indexOf(n))) : -1;
  const finished = new Set(run.spans.map((s) => s.agent));
  return Object.fromEntries(ORDER.map((node) => [node,
    active.includes(node) ? "active" : finished.has(node) || (first >= 0 && ORDER.indexOf(node) < first) ? "done" : "idle",
  ])) as Record<Node, State>;
}

function Pill({ node, state }: { node: Node; state: State }) {
  return (
    <span className={`tp-node ${state}`}>
      <ActionIcon kind={NODES[node].icon} size={14} />
      <span className="tp-label">{NODES[node].label}</span>
      {state === "done" ? <span className="tp-done" aria-hidden="true"><ActionIcon kind="check" size={9} /></span> : null}
    </span>
  );
}

/** Fork and join connectors take the state of the step they lead into: light flows while it works. */
function Branch({ kind, states }: { kind: "fork" | "join"; states: State[] }) {
  const height = 34 * states.length + 10 * (states.length - 1);
  const mid = height / 2;
  return (
    <svg className={`tp-branch n${states.length}`} viewBox={`0 0 24 ${height}`} preserveAspectRatio="none" aria-hidden="true">
      {states.map((state, i) => {
        const y = 17 + 44 * i;
        return <path className={`tp-path ${state}`} d={kind === "fork" ? `M0 ${mid} C14 ${mid} 10 ${y} 24 ${y}` : `M0 ${y} C14 ${y} 10 ${mid} 24 ${mid}`} key={i} />;
      })}
    </svg>
  );
}

function useElapsed(run: AgentRun, live: boolean) {
  const seen = useRef<number>(Date.now());
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [live]);
  const starts = [...run.spans, ...run.llm_calls].map((x) => Date.parse(x.started_at));
  const start = starts.length ? Math.min(...starts) : run.status === "queued" ? null : seen.current;
  return start === null ? null : Math.max(0, (now - start) / 1000);
}

function shortArgs(args: Record<string, unknown>) {
  const text = Object.entries(args).filter(([, v]) => v !== "" && v != null).map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v) : String(v)}`).join(", ");
  return text.length > 70 ? text.slice(0, 70) + "…" : text;
}

/** Live view of a running investigation: the team as a pipeline, the stage, the latest tool calls and counters. */
export function TeamProgress({ run, compact = false }: { run: AgentRun; compact?: boolean }) {
  const states = nodeStates(run);
  const elapsed = useElapsed(run, true);
  const fellBack = run.llm_calls.filter((c) => !c.ok).length;
  const tokens = run.llm_calls.reduce((n, c) => n + c.input_tokens + c.output_tokens, 0);
  const stage = run.status === "queued"
    ? run.queue_position ? `Waiting in the queue, position ${run.queue_position}` : "Starting"
    : run.stage || "Working";
  const team = run.team ?? [];
  const parallel = SPECIALISTS.filter((n) => !OPTIONAL.includes(n) || team.includes(n));
  const chain: Node[] = ["reviewer", ...(team.includes("remediation") ? ["remediation" as Node] : []), "comms", "finalize",
    ...(run.status === "executing" ? ["execute" as Node] : [])];

  const pipeline = (
    <div className="tp-pipeline" role="img" aria-label={`Agent team progress: ${stage}`}>
      <Pill node="lead" state={states.lead} />
      <Branch kind="fork" states={parallel.map((n) => states[n])} />
      <span className="tp-parallel">
        {parallel.map((n) => <Pill key={n} node={n} state={states[n]} />)}
      </span>
      <Branch kind="join" states={parallel.map(() => states.reviewer)} />
      {chain.map((node, i) => (
        <Fragment key={node}>
          {i > 0 ? <span className={`tp-link ${states[node]}`} /> : null}
          <Pill node={node} state={states[node]} />
        </Fragment>
      ))}
    </div>
  );

  if (compact) {
    return (
      <div className="team-progress compact">
        {pipeline}
        <p className="tp-stage"><span className="tp-shimmer">{stage}</span></p>
      </div>
    );
  }

  const recent = run.steps.slice(-3).reverse();
  return (
    <div className="team-progress">
      <div className="tp-aurora" aria-hidden="true" />
      {pipeline}
      <div className="tp-status">
        <p className="tp-stage"><span className="tp-dots" aria-hidden="true"><i /><i /><i /></span><span className="tp-shimmer">{stage}</span></p>
        <span className="tp-stats">
          {elapsed !== null ? <span><ActionIcon kind="clock" size={13} />{formatSeconds(Math.round(elapsed))}</span> : null}
          <span><ActionIcon kind="activity" size={13} />{run.steps.length} tool call{run.steps.length === 1 ? "" : "s"}</span>
          <span><ActionIcon kind="spark" size={13} />{run.llm_calls.length - fellBack} model call{run.llm_calls.length - fellBack === 1 ? "" : "s"}{fellBack ? `, ${fellBack} retried on backup` : ""}</span>
          {tokens ? <span><ActionIcon kind="layers" size={13} />{formatTokens(tokens)} tokens</span> : null}
        </span>
      </div>
      {recent.length ? (
        <ul className="tp-ticker" aria-live="polite">
          {recent.map((step) => (
            <li key={step.index} className={step.ok ? "" : "failed"}>
              <span className={`agent-tag ${step.agent}`}>{NODES[step.agent as Node]?.label ?? step.agent}</span>
              <code>{step.tool}</code>
              <span className="tp-args">{shortArgs(step.arguments)}</span>
              <span className="tp-ms">{step.ok ? `${step.duration_ms} ms` : "failed"}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
