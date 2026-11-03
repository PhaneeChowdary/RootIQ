import { type CSSProperties, type ReactNode, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { getCaseLesson } from "./api";
import { ActionIcon, type IconKind } from "./icons";
import type { AgentLesson, SupportCase } from "./types";

/** Keys that were not there at the first render. The page shows those items with an "arrived now" animation. */
export function useFresh(keys: string[]) {
  const seen = useRef<Set<string> | null>(null);
  if (seen.current === null) seen.current = new Set(keys);
  const fresh = new Set(keys.filter((k) => !seen.current!.has(k)));
  useEffect(() => { keys.forEach((k) => seen.current!.add(k)); });
  return fresh;
}

/** A small line chart that draws itself, for the counter tiles. */
export function Sparkline({ values, tone = "neutral" }: { values: number[]; tone?: string }) {
  if (values.length < 2) return null;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = Math.max(max - min, 1);
  const points = values.map((v, i) => `${(i / (values.length - 1)) * 100},${28 - ((v - min) / span) * 24}`).join(" ");
  return (
    <svg className={`sparkline tone-${tone}`} viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
      <polyline className="spark-area" points={`0,30 ${points} 100,30`} />
      <polyline className="spark-line" points={points} pathLength={100} />
    </svg>
  );
}

export interface Toast { id: string; caseId: string; title: string; service: string }

/** Notices for cases that a Grafana alert opened. Each one has a moving signal line. */
export type Notice = { id: string; text: string };
export const NOTICE_MS = 6000;

export function AlertToasts({ toasts, notices = [], onOpen, onClose, onCloseNotice }: {
  toasts: Toast[]; notices?: Notice[]; onOpen: (caseId: string) => void; onClose: (id: string) => void; onCloseNotice?: (id: string) => void;
}) {
  return (
    <Portal>
      <div className="toast-stack" aria-live="polite">
        {toasts.map((t) => <AlertToast key={t.id} toast={t} onOpen={onOpen} onClose={onClose} />)}
        {notices.map((n) => <NoticeToast key={n.id} notice={n} onClose={() => onCloseNotice?.(n.id)} />)}
      </div>
    </Portal>
  );
}

/** A success message: it slides in, counts down NOTICE_MS (paused while the pointer is on it), then leaves. */
function NoticeToast({ notice, onClose }: { notice: Notice; onClose: () => void }) {
  const [leaving, setLeaving] = useState(false);
  const left = useRef(NOTICE_MS);
  const started = useRef(Date.now());
  const timer = useRef<number | undefined>(undefined);
  const close = () => { setLeaving(true); window.setTimeout(onClose, 260); };
  const run = () => { started.current = Date.now(); timer.current = window.setTimeout(close, left.current); };
  const pause = () => { window.clearTimeout(timer.current); left.current -= Date.now() - started.current; };
  useEffect(() => { run(); return () => window.clearTimeout(timer.current); }, []);
  return (
    <section className={`toast notice-toast${leaving ? " leaving" : ""}`} role="status" onMouseEnter={pause} onMouseLeave={run}
      style={{ "--life": `${NOTICE_MS}ms` } as CSSProperties}>
      <span className="notice-check" aria-hidden="true"><ActionIcon kind="check" size={14} /></span>
      <p className="notice-text">{notice.text}</p>
      <button aria-label="Close message" className="icon-button" onClick={close} type="button"><ActionIcon kind="close" size={14} /></button>
      <i className="notice-life" aria-hidden="true" />
    </section>
  );
}

function AlertToast({ toast, onOpen, onClose }: { toast: Toast; onOpen: (caseId: string) => void; onClose: (id: string) => void }) {
  useEffect(() => {
    const timer = window.setTimeout(() => onClose(toast.id), 14000);
    return () => window.clearTimeout(timer);
  }, [toast.id]);
  return (
    <section className="toast" role="status">
      <div className="toast-signal" aria-hidden="true"><svg viewBox="0 0 120 20" preserveAspectRatio="none"><polyline points="0,10 30,10 38,3 46,17 54,7 60,12 70,10 120,10" /></svg></div>
      <div className="toast-body">
        <span className="toast-label"><i />Grafana alert · case opened</span>
        <strong>{toast.title}</strong>
        <small>The agent team has started on it.</small>
      </div>
      <div className="toast-actions">
        <button className="secondary" onClick={() => { onOpen(toast.caseId); onClose(toast.id); }} type="button">Open</button>
        <button aria-label="Close" className="icon-button" onClick={() => onClose(toast.id)} type="button"><ActionIcon kind="close" size={14} /></button>
      </div>
    </section>
  );
}

/** Shows the work that occurs when a case is resolved, one step at a time, then the graded lesson. */
export function ResolveProgress({ caseItem, onClose }: { caseItem: SupportCase; onClose: () => void }) {
  const [lesson, setLesson] = useState<AgentLesson | null>(null);
  const [waited, setWaited] = useState(false);
  useEffect(() => {
    let tries = 0;
    const load = () => getCaseLesson(caseItem.id).then((found) => { if (found) setLesson(found); return found; }).catch(() => null);
    const timer = window.setInterval(() => {
      tries += 1;
      void load().then((found) => { if (found || tries >= 24) { window.clearInterval(timer); if (!found) setWaited(true); } });
    }, 2500);
    void load();
    return () => window.clearInterval(timer);
  }, [caseItem.id]);
  const jira = caseItem.jira_source_key || caseItem.jira_issue_key;
  const steps: { icon: IconKind; text: string }[] = [
    { icon: "file", text: "Incident report written" },
    { icon: "layers", text: "Fix saved to the knowledge base" },
    ...(jira ? [{ icon: "check" as IconKind, text: `Jira ${jira} moved to Done` }] : []),
  ];
  return (
    <section className="card resolve-progress">
      <div className="section-heading">
        <h3 className="title-icon"><ActionIcon kind="success" size={18} />Case resolved</h3>
        <button aria-label="Close" className="icon-button" onClick={onClose} type="button"><ActionIcon kind="close" size={14} /></button>
      </div>
      <ol className="resolve-steps">
        {steps.map((step, i) => (
          <li key={step.text} style={{ "--i": i } as CSSProperties}>
            <span className="step-mark"><ActionIcon kind="loader" size={14} /><ActionIcon kind="check" size={13} /></span>{step.text}
          </li>
        ))}
        <li className={lesson ? "lesson-done" : "lesson-wait"} style={{ "--i": steps.length } as CSSProperties}>
          <span className="step-mark"><ActionIcon kind="loader" size={14} /><ActionIcon kind="check" size={13} /></span>
          {lesson ? <>Diagnosis graded <span className={`agent-badge verdict-${lesson.verdict}`}>{lesson.verdict.replace("_", " ")}</span></>
            : waited ? "No agent run to grade for this case" : "Grading the agents' diagnosis against the real fix…"}
        </li>
      </ol>
      {lesson ? <p className="lesson-type"><Typewriter text={`Lesson for next time: ${lesson.lesson}`} /></p> : null}
    </section>
  );
}

function Typewriter({ text }: { text: string }) {
  const [shown, setShown] = useState(0);
  useEffect(() => {
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) { setShown(text.length); return; }
    setShown(0);
    const timer = window.setInterval(() => setShown((n) => { if (n >= text.length) { window.clearInterval(timer); return n; } return n + 2; }), 18);
    return () => window.clearInterval(timer);
  }, [text]);
  return <>{text.slice(0, shown)}<span className={shown < text.length ? "type-caret" : "type-caret done"} /></>;
}

/** While the one-prompt analysis runs: placeholder fields and the time so far. One model call has no real stages. */
export function QuickAnalysisProgress() {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return (
    <div className="quick-progress" aria-live="polite">
      <p className="tp-stage"><span className="tp-dots" aria-hidden="true"><i /><i /><i /></span>
        <span className="tp-shimmer">Quick analysis: one model call. It usually takes 10 to 60 seconds.</span>
        <span className="quick-time">{seconds}s</span></p>
      {["Summary", "Likely cause", "Diagnostic steps"].map((label, i) => (
        <div className="quick-field" key={label} style={{ "--i": i } as CSSProperties}>
          <span>{label}</span><i className="shimmer-bar" /><i className="shimmer-bar short" />
        </div>
      ))}
    </div>
  );
}

/** Puts a window (for example a confirmation) on the page itself. A window inside a glass card would stay inside the
 *  card, because the card's blur effect holds fixed elements in it. */
export function Portal({ children }: { children: ReactNode }) {
  return createPortal(children, document.body);
}
