import { type CSSProperties, useEffect, useState } from "react";
import { type AutomationOverview, getAutomation, saveAutomation, testAutomationRunner } from "./api";
import { ActionIcon } from "./icons";
import { Portal } from "./live";

const MODE_LABELS: Record<string, string> = { auto: "Auto", one: "1 person", two: "2 people" };
const RUNNER_NAMES: Record<string, string> = { demo: "Demo", webhook: "Webhook", github_actions: "GitHub Actions" };

type Draft = Pick<AutomationOverview, "paused" | "remediation" | "modes" | "limits" | "verify"> & { runner: { type: string; config: Record<string, string> } };

/** Settings > Automation: the kill switch, who approves each action, the runner, verification and limits. */
export function AutomationPage({ onError, onNotice }: { onError: (message: string) => void; onNotice: (message: string) => void }) {
  const [data, setData] = useState<AutomationOverview | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState<"" | "save" | "test" | "pause">("");
  const [test, setTest] = useState<{ ok: boolean; detail: string } | null>(null);
  const [confirmResume, setConfirmResume] = useState(false);

  function load(next: AutomationOverview) {
    setData(next);
    setDraft({ paused: next.paused, remediation: next.remediation, modes: { ...next.modes }, limits: { ...next.limits },
               verify: { ...next.verify }, runner: { type: next.runner.type, config: { ...next.runner.config } } });
  }
  useEffect(() => { getAutomation().then(load).catch((e: unknown) => onError(e instanceof Error ? e.message : String(e))); }, []);

  if (!data || !draft) return <div className="sources-grid">{[0, 1, 2].map((i) => <section key={i} className="card provider-card skeleton" />)}</div>;
  const changed = JSON.stringify(draft) !== JSON.stringify({ paused: data.paused, remediation: data.remediation, modes: data.modes,
    limits: data.limits, verify: data.verify, runner: { type: data.runner.type, config: data.runner.config } });

  async function save(next: Draft, notice: string, kind: "save" | "pause" = "save") {
    setBusy(kind);
    try {
      load(await saveAutomation(next));
      setTest(null);
      onNotice(notice);
    } catch (caught: unknown) {
      onError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy("");
    }
  }

  async function runTest() {
    setBusy("test");
    setTest(null);
    try { setTest(await testAutomationRunner()); }
    catch (caught: unknown) { setTest({ ok: false, detail: caught instanceof Error ? caught.message : String(caught) }); }
    finally { setBusy(""); }
  }

  const fields = data.runner_types[draft.runner.type] ?? [];
  const setVerify = (key: string, value: number | boolean) => setDraft({ ...draft, verify: { ...draft.verify, [key]: value } });
  const setLimit = (key: string, value: number) => setDraft({ ...draft, limits: { ...draft.limits, [key]: value } });

  return (
    <div className="automation-page">
      <section className={`card automation-hero${data.paused ? " paused" : ""}`}>
        <span className={`hero-orb ${data.paused ? "paused" : "running"}`}><ActionIcon kind={data.paused ? "stop" : "zap"} size={22} /></span>
        <div className="hero-text">
          <span className="section-label">Automation</span>
          <strong className="hero-model">{data.paused ? "Paused" : "Running"}</strong>
        </div>
        <div className="automation-flags">
          <span className={`state-pill ${data.remediation ? "on" : ""}`}><i />Remediation {data.remediation ? "on" : "off"}</span>
          <span className={`state-pill ${data.dry_run ? "warn" : "on"}`}><i />{data.dry_run ? "Dry run" : "Live"}</span>
          <span className="state-pill on"><i />{RUNNER_NAMES[data.runner.type]}</span>
        </div>
        <button className={data.paused ? "primary" : "danger"} disabled={busy !== ""} type="button"
          onClick={() => data.paused ? setConfirmResume(true) : void save({ ...draft, paused: true }, "Automation paused. No fix runs.", "pause")}>
          <ActionIcon kind={data.paused ? "play" : "stop"} />{data.paused ? "Resume" : "Pause all"}
        </button>
      </section>

      <div className="automation-grid">
        <section className="card source-card" style={{ "--i": 0 } as CSSProperties}>
          <header className="provider-head">
            <span className="source-icon"><ActionIcon kind="users" size={18} /></span>
            <div><strong>Approvals</strong></div>
            <label className="switch-row">
              <span>Remediation</span>
              <input checked={draft.remediation} onChange={(e) => setDraft({ ...draft, remediation: e.target.checked })} role="switch" type="checkbox" />
            </label>
          </header>
          <div className="policy-list">
            {data.actions.map((action) => (
              <div className={`policy-row${action.remediation && !draft.remediation ? " off" : ""}`} key={action.id}>
                <span className="policy-name">{action.name}</span>
                <span className={`risk-chip ${action.risk}`}>{action.risk}</span>
                <div className="role-segments" role="radiogroup" aria-label={`Approval for ${action.name}`}>
                  {(["auto", "one", "two"] as const).map((mode) => (
                    <button aria-checked={draft.modes[action.id] === mode} className={draft.modes[action.id] === mode ? "active engineer" : ""}
                      disabled={mode === "auto" && action.risk !== "low"} key={mode} role="radio" type="button"
                      title={mode === "auto" && action.risk !== "low" ? "Only low-risk actions run without a person" : undefined}
                      onClick={() => setDraft({ ...draft, modes: { ...draft.modes, [action.id]: mode } })}>{MODE_LABELS[mode]}</button>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>

        <div className="automation-side">
          <section className="card source-card" style={{ "--i": 1 } as CSSProperties}>
            <header className="provider-head">
              <span className="source-icon"><ActionIcon kind="play" size={18} /></span>
              <div><strong>Runner</strong></div>
              <span className="state-pill on"><i />{RUNNER_NAMES[data.runner.type]}</span>
            </header>
            <div className="type-switch" role="radiogroup" aria-label="Runner">
              {Object.keys(data.runner_types).map((t) => (
                <button aria-checked={draft.runner.type === t} className={draft.runner.type === t ? "active" : ""} key={t} role="radio" type="button"
                  onClick={() => { setDraft({ ...draft, runner: { type: t, config: t === data.runner.type ? { ...data.runner.config } : {} } }); setTest(null); }}>{RUNNER_NAMES[t]}</button>
              ))}
            </div>
            {fields.length ? (
              <div className="source-fields">
                {fields.map((f) => (
                  <label key={f.name}>
                    <span>{f.label}</span>
                    <input autoComplete="off" spellCheck={false} type={f.secret ? "password" : "text"}
                      placeholder={f.secret && data.runner.saved_secrets.includes(f.name) && draft.runner.type === data.runner.type ? "Saved" : f.name === "url" ? "https://" : f.name === "ref" ? "main" : ""}
                      value={draft.runner.config[f.name] ?? ""} onChange={(e) => setDraft({ ...draft, runner: { ...draft.runner, config: { ...draft.runner.config, [f.name]: e.target.value } } })} />
                  </label>
                ))}
              </div>
            ) : null}
            {test ? <p className={`source-result ${test.ok ? "ok" : "bad"}`}><ActionIcon kind={test.ok ? "success" : "fail"} size={15} />{test.detail}</p> : null}
            <div className="provider-foot">
              <span />
              <button className="secondary" disabled={busy !== "" || changed} onClick={runTest} title={changed ? "Save first" : undefined} type="button">
                {busy === "test" ? <><ActionIcon kind="loader" />Testing</> : "Test"}
              </button>
            </div>
          </section>

          <section className="card source-card" style={{ "--i": 2 } as CSSProperties}>
            <header className="provider-head">
              <span className="source-icon"><ActionIcon kind="success" size={18} /></span>
              <div><strong>Verification and limits</strong></div>
            </header>
            <div className="number-grid">
              <NumberField label="First check (s)" min={30} max={3600} value={Number(draft.verify.first_check_seconds)} onChange={(v) => setVerify("first_check_seconds", v)} />
              <NumberField label="Checks" min={1} max={5} value={Number(draft.verify.checks)} onChange={(v) => setVerify("checks", v)} />
              <NumberField label="Follow-up runs a day" min={0} max={3} value={Number(draft.verify.follow_up_runs)} onChange={(v) => setVerify("follow_up_runs", v)} />
              <NumberField label="Fixes per case an hour" min={1} max={10} value={draft.limits.per_case_per_hour} onChange={(v) => setLimit("per_case_per_hour", v)} />
              <NumberField label="Fixes an hour" min={1} max={100} value={draft.limits.per_hour} onChange={(v) => setLimit("per_hour", v)} />
              <label className="switch-tile">
                <span>Resolve when verified</span>
                <input checked={Boolean(draft.verify.auto_resolve)} onChange={(e) => setVerify("auto_resolve", e.target.checked)} role="switch" type="checkbox" />
              </label>
            </div>
          </section>
        </div>
      </div>

      {changed ? (
        <Portal>
          <div className="save-bar" role="region" aria-label="Unsaved changes">
            <span className="save-dot" aria-hidden="true" />
            <span className="save-text">Unsaved changes</span>
            <button className="secondary" onClick={() => load(data)} type="button">Discard</button>
            <button className="primary" disabled={busy !== ""} onClick={() => void save(draft, "Automation settings saved.")} type="button">
              {busy === "save" ? <><ActionIcon kind="loader" />Saving</> : "Save"}
            </button>
          </div>
        </Portal>
      ) : null}

      {confirmResume ? (
        <Portal>
          <div className="modal-backdrop" role="presentation" onClick={() => setConfirmResume(false)}>
            <section aria-label="Resume automation" aria-modal="true" className="status-modal confirm" role="alertdialog" onClick={(e) => e.stopPropagation()}>
              <button aria-label="Close" className="modal-close" onClick={() => setConfirmResume(false)} type="button">×</button>
              <p className="modal-label">Resume automation?</p>
              <p>Approved fixes will run again on {RUNNER_NAMES[data.runner.type]}.</p>
              <div className="actions">
                <button className="secondary" onClick={() => setConfirmResume(false)} type="button">Cancel</button>
                <button className="primary" onClick={() => { setConfirmResume(false); void save({ ...draft, paused: false }, "Automation resumed.", "pause"); }} type="button">Resume</button>
              </div>
            </section>
          </div>
        </Portal>
      ) : null}
    </div>
  );
}

function NumberField({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (value: number) => void }) {
  return (
    <label className="number-field">
      <span>{label}</span>
      <input inputMode="numeric" max={max} min={min} type="number" value={value}
        onChange={(e) => onChange(Math.max(min, Math.min(max, Number(e.target.value) || min)))} />
    </label>
  );
}
