import { shortDateTime } from "./time";
import { type CSSProperties, type ReactNode, useEffect, useState } from "react";
import { getPostmortem, type PostmortemData, regeneratePostmortem } from "./api";
import { ActionIcon } from "./icons";
import { Portal } from "./live";
import { useCan } from "./role";

const DURATIONS: [string, string][] = [["to_analysis", "To analysis"], ["to_fix", "To verified fix"], ["to_resolve", "To resolution"]];

function minutes(value: number) {
  return value >= 120 ? `${(value / 60).toFixed(1)} h` : `${Math.round(value)} min`;
}

/** The postmortem of a resolved case, written by the postmortem agent. Until it exists, `fallback` is shown. */
export function PostmortemCard({ caseId, fallback, onError }: { caseId: string; fallback: ReactNode; onError: (message: string) => void }) {
  const can = useCan();
  const [data, setData] = useState<{ postmortem: PostmortemData | null; pending: boolean } | null>(null);
  const [confirm, setConfirm] = useState(false);

  useEffect(() => {
    let stop = false;
    const load = () => getPostmortem(caseId).then((d) => { if (!stop) setData(d); }).catch(() => undefined);
    load();
    const timer = window.setInterval(() => { if (data?.pending !== false || !data?.postmortem) load(); }, 5000);
    return () => { stop = true; window.clearInterval(timer); };
  }, [caseId, data?.pending, Boolean(data?.postmortem)]);

  async function regenerate() {
    setConfirm(false);
    try { await regeneratePostmortem(caseId); setData({ postmortem: data?.postmortem ?? null, pending: true }); }
    catch (caught: unknown) { onError(caught instanceof Error ? caught.message : String(caught)); }
  }

  const p = data?.postmortem;
  if (!p) {
    return (
      <>
        {data?.pending ? (
          <section className="card postmortem pending">
            <span className="section-label">Postmortem</span>
            <p className="tp-stage"><span className="tp-dots" aria-hidden="true"><i /><i /><i /></span><span className="tp-shimmer">Postmortem agent writing</span></p>
          </section>
        ) : null}
        {fallback}
      </>
    );
  }

  return (
    <section className="card postmortem">
      <header className="pm-head">
        <div>
          <span className="section-label">Postmortem</span>
          <h3>{p.title}</h3>
        </div>
        <div className="foot-buttons">
          <a className="secondary pm-link" href={`/api/cases/${caseId}/postmortem.md`} download={`postmortem-${caseId.slice(0, 8)}.md`}><ActionIcon kind="download" size={15} />Markdown</a>
          {can.work ? (
            <button className="secondary" disabled={data?.pending} onClick={() => setConfirm(true)} type="button">
              {data?.pending ? <><ActionIcon kind="loader" />Writing</> : <><ActionIcon kind="reset" size={15} />Regenerate</>}
            </button>
          ) : null}
        </div>
      </header>
      <p className="pm-summary">{p.summary}</p>
      {Object.keys(p.durations).length ? (
        <div className="pm-durations">
          {DURATIONS.filter(([key]) => key in p.durations).map(([key, label], i) => (
            <div key={key} style={{ "--i": i } as CSSProperties}><span>{label}</span><strong>{minutes(p.durations[key])}</strong></div>
          ))}
        </div>
      ) : null}
      {p.warnings.length ? <p className="source-result bad"><ActionIcon kind="fail" size={15} />{p.warnings.join(" ")}</p> : null}
      <div className="pm-sections">
        {([["Impact", p.impact], ["Root cause", p.root_cause], ["Trigger", p.trigger], ["Detection", p.detection], ["Resolution", p.resolution]] as const)
          .filter(([, text]) => text).map(([title, text]) => <div key={title}><h4>{title}</h4><p>{text}</p></div>)}
      </div>
      <div className="pm-columns">
        <div className="pm-list good"><h4><ActionIcon kind="success" size={14} />Went well</h4><ul>{p.went_well.map((x) => <li key={x}>{x}</li>)}</ul></div>
        <div className="pm-list bad"><h4><ActionIcon kind="fail" size={14} />Went badly</h4><ul>{p.went_poorly.map((x) => <li key={x}>{x}</li>)}</ul></div>
      </div>
      {p.action_items.length ? (
        <div className="pm-actions">
          <h4>Action items</h4>
          {p.action_items.map((a) => (
            <div className="pm-action" key={a.item}>
              <span className={`priority-chip ${a.priority.toLowerCase()}`}>{a.priority}</span>
              <span>{a.item}</span>
              <small className="muted">{a.owner}</small>
            </div>
          ))}
        </div>
      ) : null}
      <details className="pm-timeline">
        <summary>Timeline <span className="muted">· {p.timeline.length} events</span></summary>
        <ol>
          {p.timeline.map((e, i) => (
            <li key={`${e.at}-${i}`}>
              <time>{shortDateTime(e.at)}</time>
              <span className={`agent-tag ${e.source}`}>{e.source}</span>
              <span>{e.event}</span>
            </li>
          ))}
        </ol>
      </details>
      {confirm ? (
        <Portal>
          <div className="modal-backdrop" role="presentation" onClick={() => setConfirm(false)}>
            <section aria-label="Regenerate the postmortem" aria-modal="true" className="status-modal confirm" role="alertdialog" onClick={(e) => e.stopPropagation()}>
              <button aria-label="Close" className="modal-close" onClick={() => setConfirm(false)} type="button">×</button>
              <p className="modal-label">Regenerate the postmortem?</p>
              <p>The new postmortem will replace this one. It takes one or two model calls.</p>
              <div className="actions">
                <button className="secondary" onClick={() => setConfirm(false)} type="button">Cancel</button>
                <button className="primary" onClick={regenerate} type="button">Regenerate</button>
              </div>
            </section>
          </div>
        </Portal>
      ) : null}
    </section>
  );
}
