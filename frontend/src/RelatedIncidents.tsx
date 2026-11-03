import { type CSSProperties, useEffect, useState } from "react";
import { decideIncidentLink, getIncidentGroup, type IncidentGroupData, type IncidentLinkData, resolveLinkedCases } from "./api";
import { ActionIcon } from "./icons";
import { Portal } from "./live";
import { useCan } from "./role";

const SOURCE_LABELS: Record<IncidentLinkData["source"], string> = { rule: "Rule", agent: "Agent", person: "Person" };
const STATUS_NAMES: Record<string, string> = { new: "Not investigated", investigating: "Investigating", awaiting_approval: "Awaiting approval", approved: "Approved", resolved: "Resolved" };

type Ask = { title: string; message: string; label: string; danger?: boolean; run: () => Promise<unknown>; notice: string };

/** Cases with the same root cause as this one . Hidden when there is none. */
export function RelatedIncidents({ caseId, caseStatus, onOpenCase, onChanged, onError, onNotice }: {
  caseId: string; caseStatus: string; onOpenCase: (id: string) => void; onChanged: () => void;
  onError: (message: string) => void; onNotice: (message: string) => void;
}) {
  const can = useCan();
  const [data, setData] = useState<IncidentGroupData | null>(null);
  const [busy, setBusy] = useState("");
  const [ask, setAsk] = useState<Ask | null>(null);

  useEffect(() => {
    let stop = false;
    const load = () => getIncidentGroup(caseId).then((d) => { if (!stop) setData(d); }).catch(() => undefined);
    load();
    const timer = window.setInterval(load, 8000);
    return () => { stop = true; window.clearInterval(timer); };
  }, [caseId, caseStatus]);

  async function act(key: string, run: () => Promise<unknown>, notice: string) {
    setBusy(key);
    try {
      await run();
      setData(await getIncidentGroup(caseId));
      onChanged();
      onNotice(notice);
    } catch (caught: unknown) {
      onError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  }

  const group = data?.group;
  if (!group) {
    return data?.pending ? (
      <section className="card related-card pending">
        <span className="section-label">Related incidents</span>
        <p className="tp-stage"><span className="tp-dots" aria-hidden="true"><i /><i /><i /></span><span className="tp-shimmer">Correlation agent checking</span></p>
      </section>
    ) : null;
  }

  const primary = data.cases[group.primary_case_id];
  const open = group.links.filter((l) => l.case_id !== group.primary_case_id && l.status === "confirmed" && data.cases[l.case_id]?.status !== "resolved");
  const ordered = [...group.links].sort((a, b) => (a.case_id === group.primary_case_id ? -1 : b.case_id === group.primary_case_id ? 1 : 0));

  return (
    <section className="card related-card">
      <header className="related-head">
        <span className="related-icon"><ActionIcon kind="link" size={17} /></span>
        <div>
          <span className="section-label">Related incidents</span>
          <h3>{group.cause || "Same root cause"}</h3>
        </div>
        <span className="related-count">{group.links.length}</span>
        {data.pending ? <ActionIcon kind="loader" size={15} /> : null}
      </header>
      <ol className="related-list">
        {ordered.map((link, i) => {
          const item = data.cases[link.case_id];
          if (!item) return null;
          const isPrimary = link.case_id === group.primary_case_id;
          const here = link.case_id === caseId;
          return (
            <li className={`related-row${here ? " here" : ""}${link.status === "suggested" ? " suggested" : ""}`} key={link.case_id} style={{ "--i": i } as CSSProperties}>
              <span className={`related-dot sev-${item.severity}`} aria-hidden="true" />
              <div className="related-main">
                <button className="related-title" disabled={here} onClick={() => onOpenCase(link.case_id)} type="button">{item.title}</button>
                <div className="related-meta">
                  <span>{item.service}</span>
                  <span>{STATUS_NAMES[item.status] ?? item.status}</span>
                  {isPrimary ? <span className="related-chip is-primary"><ActionIcon kind="flag" size={11} />Primary</span> : null}
                  {here ? <span className="related-chip">This case</span> : null}
                  {link.status === "suggested" ? <span className="related-chip suggested">Suggested</span> : null}
                  {!isPrimary ? <span className={`related-chip src-${link.source}`}>{SOURCE_LABELS[link.source]}</span> : null}
                </div>
                {link.reason && !isPrimary ? (
                  <details className="related-why">
                    <summary>{link.reason}</summary>
                    {link.evidence.length ? <pre>{link.evidence.join("\n")}</pre> : null}
                  </details>
                ) : null}
              </div>
              {can.work && !isPrimary ? (
                <div className="related-actions">
                  {link.status === "suggested" ? (
                    <>
                      <button aria-label={`Confirm ${item.title}`} className="icon-button" disabled={busy !== ""} title="Confirm"
                        onClick={() => void act("confirm", () => decideIncidentLink(link.case_id, "confirm"), "Link confirmed.")} type="button"><ActionIcon kind="check" /></button>
                      <button aria-label={`Dismiss ${item.title}`} className="icon-button danger-hover" disabled={busy !== ""} title="Dismiss"
                        onClick={() => setAsk({ title: "Dismiss this link?", message: "The two cases will not be linked again.", label: "Dismiss", danger: true,
                          run: () => decideIncidentLink(link.case_id, "dismiss"), notice: "Link dismissed." })} type="button"><ActionIcon kind="close" /></button>
                    </>
                  ) : (
                    <>
                      <button aria-label={`Make ${item.title} the primary case`} className="icon-button" disabled={busy !== ""} title="Make primary"
                        onClick={() => void act("primary", () => decideIncidentLink(link.case_id, "primary"), "Primary case changed.")} type="button"><ActionIcon kind="flag" /></button>
                      <button aria-label={`Ungroup ${item.title}`} className="icon-button danger-hover" disabled={busy !== ""} title="Ungroup"
                        onClick={() => setAsk({ title: "Ungroup this case?", danger: true, label: "Ungroup", notice: "Case ungrouped.",
                          message: link.source === "rule" ? "The agent team will start its own investigation of this case." : "The two cases will not be linked again.",
                          run: () => decideIncidentLink(link.case_id, "ungroup") })} type="button"><ActionIcon kind="unlink" /></button>
                    </>
                  )}
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
      {can.work && caseId === group.primary_case_id && primary?.status === "resolved" && open.length ? (
        <div className="related-foot">
          <button className="primary" disabled={busy !== ""} type="button"
            onClick={() => setAsk({ title: `Resolve ${open.length} linked case${open.length > 1 ? "s" : ""}?`, label: "Resolve",
              message: "They will get the resolution of this case.", run: () => resolveLinkedCases(caseId),
              notice: `${open.length} linked case${open.length > 1 ? "s" : ""} resolved.` })}>
            <ActionIcon kind="check" />Resolve linked cases
          </button>
        </div>
      ) : null}
      {ask ? (
        <Portal>
          <div className="modal-backdrop" role="presentation" onClick={() => setAsk(null)}>
            <section aria-label={ask.title} aria-modal="true" className="status-modal confirm" role="alertdialog" onClick={(e) => e.stopPropagation()}>
              <button aria-label="Close" className="modal-close" onClick={() => setAsk(null)} type="button">×</button>
              <p className="modal-label">{ask.title}</p>
              <p>{ask.message}</p>
              <div className="actions">
                <button className="secondary" onClick={() => setAsk(null)} type="button">Cancel</button>
                <button className={ask.danger ? "danger" : "primary"} type="button"
                  onClick={() => { const a = ask; setAsk(null); void act("ask", a.run, a.notice); }}>{ask.label}</button>
              </div>
            </section>
          </div>
        </Portal>
      ) : null}
    </section>
  );
}
