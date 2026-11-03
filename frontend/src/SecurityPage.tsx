import { clockTime } from "./time";
import { type CSSProperties, type ReactNode, useEffect, useState } from "react";
import { getSecurity, runBackup, runCleanup, type SecurityOverview } from "./api";
import { ActionIcon, type IconKind } from "./icons";
import { Portal } from "./live";

const MASK_LABELS: Record<string, string> = { email: "Emails", card: "Card numbers", phone: "Phone numbers", ssn: "SSNs", iban: "Bank accounts", secret: "Secrets", ip: "IP addresses" };

/** Settings > Security: what protects the data. The values come from backend/.env; this page shows them. */
export function SecurityPage({ onError, onNotice }: { onError: (message: string) => void; onNotice: (message: string) => void }) {
  const [data, setData] = useState<SecurityOverview | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [backingUp, setBackingUp] = useState(false);
  useEffect(() => {
    const load = () => getSecurity().then(setData).catch((e: unknown) => onError(e instanceof Error ? e.message : String(e)));
    load();
    const timer = window.setInterval(load, 15000);  // workers and backups change while the page is open
    return () => window.clearInterval(timer);
  }, []);

  async function backupNow() {
    setBackingUp(true);
    try {
      setData(await runBackup());
      onNotice("Backup made.");
    } catch (caught: unknown) {
      onError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBackingUp(false);
    }
  }

  if (!data) return <div className="sources-grid">{[0, 1, 2, 3].map((i) => <section key={i} className="card provider-card skeleton" />)}</div>;
  const masked = Object.entries(data.masking.masked).filter(([, n]) => n > 0);
  const maskedTotal = masked.reduce((sum, [, n]) => sum + n, 0);
  const due = data.retention.due;
  const dueTotal = due.bin + due.resolved + due.audit;
  const masking = data.masking.mode !== "off";

  async function cleanup() {
    setConfirm(false);
    setBusy(true);
    try {
      const result = await runCleanup();
      setData(result);
      const d = result.deleted;
      onNotice(d.bin + d.resolved + d.audit ? `Deleted ${d.bin + d.resolved} case(s) and ${d.audit} audit event(s).` : "Nothing to delete.");
    } catch (caught: unknown) {
      onError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="security-grid">
      <SecurityCard index={0} icon="mask" title="Personal data masking" on={masking} state={masking ? "On" : "Off"}>
        <div className="sec-facts">
          <Fact label="Main model" value={data.masking.model} />
          <Fact label="Masking now" value={!masking ? "Off" : data.masking.cloud_model ? "Active (cloud model)" : "Not needed (local model)"} />
        </div>
        {masked.length ? (
          <div className="mask-counts">
            <small className="muted">Masked since the app started: {maskedTotal}</small>
            <div className="mask-bars">
              {masked.map(([kind, n]) => (
                <div className="mask-bar" key={kind}>
                  <span>{MASK_LABELS[kind] ?? kind}</span>
                  <i style={{ "--w": `${Math.max(6, (n / maskedTotal) * 100)}%` } as CSSProperties} />
                  <strong>{n}</strong>
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </SecurityCard>

      <SecurityCard index={1} icon="lock" title="Encryption" on={data.encryption.model_keys_encrypted} warn={!data.encryption.model_keys_encrypted} state={data.encryption.model_keys_encrypted ? "Encrypted" : "Check"}>
        <div className="sec-facts">
          <Fact label="Saved secrets" value={data.encryption.model_keys_encrypted ? "Encrypted" : "Some are plain text: restart the backend"} />
          <Fact label="Encryption key" value={data.encryption.key_from_env ? "ROOTIQ_SECRET_KEY" : data.encryption.key_file} />
        </div>
      </SecurityCard>

      <SecurityCard index={2} icon="shield" title="Sign-in protection" on warn={data.sign_in.locked.length > 0} state={data.sign_in.locked.length ? `${data.sign_in.locked.length} locked` : "None locked"}>
        <div className="sec-facts">
          <Fact label="Session length" value={`${data.sign_in.session_hours} hours`} />
          <Fact label="Single sign-on" value={data.sign_in.sso ? "On" : "Off"} />
          <Fact label="Secure cookie (HTTPS)" value={data.sign_in.cookie_secure ? "On" : "Off (local use)"} />
          <Fact label="Allowed websites" value={data.origins.length ? `${data.origins.length}` : "None"} />
        </div>
        {data.sign_in.locked.length ? (
          <div className="locked-list">
            {data.sign_in.locked.map((l) => (
              <span className="locked-chip" key={l.key}><ActionIcon kind="lock" size={12} />{l.key.replace(/^user:/, "").replace(/^ip:/, "address ")} until {clockTime(l.locked_until)}</span>
            ))}
          </div>
        ) : null}
      </SecurityCard>

      <SecurityCard index={3} icon="timer" title="Data retention" on warn={dueTotal > 0} state={dueTotal ? `${dueTotal} due` : "Up to date"}>
        <div className="retention-rows">
          <RetentionRow label="Recycle bin" days={data.retention.bin_days} due={due.bin} unit="case" />
          <RetentionRow label="Resolved cases" days={data.retention.resolved_days} due={due.resolved} unit="case" />
          <RetentionRow label="Audit events" days={data.retention.audit_days} due={due.audit} unit="event" />
        </div>
        <div className="provider-foot">
          <span />
          <button className="secondary danger-hover" disabled={busy || dueTotal === 0} onClick={() => setConfirm(true)} type="button">
            {busy ? <><ActionIcon kind="loader" />Cleaning up</> : "Clean up now"}
          </button>
        </div>
      </SecurityCard>

      <SecurityCard index={4} icon="archive" title="Backups" on={data.backups.count > 0} warn={backupOld(data)}
        state={data.backups.latest ? `Last ${timeAgo(data.backups.latest)}` : "None yet"}>
        <div className="sec-facts">
          <Fact label="Backups kept" value={`${data.backups.count} of ${data.backups.keep}`} />
          <Fact label="Automatic" value={data.backups.every_hours > 0 ? `Every ${data.backups.every_hours} hours` : "By the backup service"} />
          <Fact label="Newest" value={data.backups.latest_name ? `${data.backups.latest_kb} KB` : "None"} />
          <Fact label="Folder" value={data.backups.folder} />
        </div>
        <div className="provider-foot">
          <span />
          <button className="secondary" disabled={backingUp} onClick={backupNow} type="button">
            {backingUp ? <><ActionIcon kind="loader" />Backing up</> : "Back up now"}
          </button>
        </div>
      </SecurityCard>

      <SecurityCard index={5} icon="database" title="Database and workers" on={data.system.workers.length > 0 || data.system.api_threads > 0}
        warn={data.system.schema_version < data.system.schema_latest || (data.system.external_workers && data.system.workers.length === 0)}
        state={`${data.system.workers.length} worker${data.system.workers.length === 1 ? "" : "s"}`}>
        <div className="sec-facts">
          <Fact label="Database" value={data.system.database} />
          <Fact label="Schema version" value={`${data.system.schema_version} of ${data.system.schema_latest}`} />
          <Fact label="Runs waiting" value={`${data.system.queued_jobs}`} />
          <Fact label="Workers in the API" value={data.system.api_threads ? `${data.system.api_threads} thread(s)` : "None (worker processes)"} />
        </div>
        {data.system.workers.length ? (
          <div className="worker-list">
            {data.system.workers.map((w) => (
              <div className="worker-row" key={w.id}>
                <i className={`online-dot ${w.jobs ? "busy" : ""}`} />
                <span className="worker-name">{w.host}<small>{w.id.split(":").slice(1).join(" · ")}</small></span>
                <small className="muted">{w.threads} thread{w.threads === 1 ? "" : "s"}</small>
                <strong>{w.jobs ? `${w.jobs} job${w.jobs === 1 ? "" : "s"}` : "Idle"}</strong>
              </div>
            ))}
          </div>
        ) : null}
      </SecurityCard>

      {confirm ? (
        <Portal>
          <div className="modal-backdrop" role="presentation" onClick={() => setConfirm(false)}>
            <section aria-label="Clean up now" aria-modal="true" className="status-modal delete" role="alertdialog" onClick={(e) => e.stopPropagation()}>
              <button aria-label="Close" className="modal-close" onClick={() => setConfirm(false)} type="button">×</button>
              <p className="modal-label">Clean up now</p>
              <p>This will delete {due.bin + due.resolved} case(s) and {due.audit} audit event(s) forever, by the retention rules.</p>
              <p className="muted">It cannot be undone.</p>
              <div className="actions">
                <button className="secondary" onClick={() => setConfirm(false)} type="button">Cancel</button>
                <button className="danger" onClick={cleanup} type="button">Delete forever</button>
              </div>
            </section>
          </div>
        </Portal>
      ) : null}
    </div>
  );
}

function SecurityCard({ index, icon, title, on, state, warn = false, children }: { index: number; icon: IconKind; title: string; on: boolean; state: string; warn?: boolean; children: ReactNode }) {
  return (
    <section className="card source-card security-card" style={{ "--i": index } as CSSProperties}>
      <header className="provider-head">
        <span className={`source-icon ${on ? "on" : "off"}`}><ActionIcon kind={icon} size={18} /></span>
        <div><strong>{title}</strong></div>
        <span className={`state-pill ${warn ? "warn" : on ? "on" : ""}`}><i />{state}</span>
      </header>
      {children}
    </section>
  );
}

function backupOld(data: SecurityOverview) {
  const hours = data.backups.every_hours || 24;
  return !data.backups.latest || Date.now() - new Date(data.backups.latest).getTime() > hours * 2 * 3600 * 1000;
}

function timeAgo(iso: string) {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`;
}

function Fact({ label, value }: { label: string; value: string }) {
  return <div className="sec-fact"><small>{label}</small><strong>{value}</strong></div>;
}

function RetentionRow({ label, days, due, unit }: { label: string; days: number; due: number; unit: string }) {
  return (
    <div className="retention-row">
      <span>{label}</span>
      <strong>{days > 0 ? `${days} days` : "Kept"}</strong>
      <small className={due ? "due" : "muted"}>{days > 0 ? (due ? `${due} ${unit}${due > 1 ? "s" : ""} due` : "Nothing due") : "No limit"}</small>
    </div>
  );
}
