import { type CSSProperties, useEffect, useState } from "react";
import { getSources, saveSource, type SourceKind, type SourcesOverview, testSource } from "./api";
import { ActionIcon, type IconKind } from "./icons";

type Kind = "logs" | "metrics" | "changes" | "code" | "database";

const KINDS: { kind: Kind; title: string; icon: IconKind }[] = [
  { kind: "logs", title: "Logs", icon: "file" },
  { kind: "metrics", title: "Metrics", icon: "activity" },
  { kind: "changes", title: "Code changes", icon: "commit" },
  { kind: "code", title: "Code", icon: "code" },
  { kind: "database", title: "Database", icon: "database" },
];

const TYPE_NAMES: Record<string, string> = {
  demo: "Demo", loki: "Grafana Loki", elasticsearch: "Elasticsearch", cloudwatch: "AWS CloudWatch",
  none: "Off", prometheus: "Prometheus", github: "GitHub", local: "Folder", postgres: "PostgreSQL", sqlite: "SQLite",
};

const PLACEHOLDERS: Record<string, string> = {
  url: "https://", label: "service", index: "logs-*", service_field: "service.name", message_field: "message",
  time_field: "@timestamp", level_field: "log.level", region: "us-east-1", group_prefix: "/app/",
  repos: "payments-api = acme/payments\n* = acme/monorepo",
  root: "/Users/you/code", path: "/path/to/file.db", services: "*", refused: "audit_log, invoices",
};


/** Settings > Data sources: where the agents read logs, metrics and code changes. */
export function SourcesPage({ onError, onNotice }: { onError: (message: string) => void; onNotice: (message: string) => void }) {
  const [overview, setOverview] = useState<SourcesOverview | null>(null);
  useEffect(() => { getSources().then(setOverview).catch((e: unknown) => onError(e instanceof Error ? e.message : String(e))); }, []);
  if (!overview) return <div className="provider-grid">{[0, 1, 2].map((i) => <section key={i} className="card provider-card skeleton" />)}</div>;
  return (
    <div className="sources-grid">
      {KINDS.map((k, i) => (
        <SourceCard key={k.kind} index={i} kind={k.kind} title={k.title} icon={k.icon} source={overview[k.kind]}
          onSaved={(next, detail) => { setOverview(next); onNotice(`${k.title} source saved. ${detail}`); }} />
      ))}
    </div>
  );
}

function SourceCard({ kind, title, icon, source, index, onSaved }: {
  kind: Kind; title: string; icon: IconKind; source: SourceKind; index: number;
  onSaved: (next: SourcesOverview, detail: string) => void;
}) {
  const [type, setType] = useState(source.type);
  const [values, setValues] = useState<Record<string, string>>(source.config);
  const [busy, setBusy] = useState<"" | "test" | "save">("");
  const [result, setResult] = useState<{ ok: boolean; detail: string } | null>(null);
  useEffect(() => { setType(source.type); setValues(source.config); }, [source]);
  const fields = source.types[type] ?? [];
  const changed = type !== source.type || fields.some((f) => (values[f.name] ?? "") !== (source.config[f.name] ?? ""));

  async function run(kind_: "test" | "save") {
    setBusy(kind_);
    setResult(null);
    try {
      if (kind_ === "test") setResult(await testSource(kind, type, values));
      else {
        const saved = await saveSource(kind, type, values);
        setResult({ ok: true, detail: saved.detail });
        const { detail, ...next } = saved;
        onSaved(next as SourcesOverview, detail);
      }
    } catch (caught: unknown) {
      setResult({ ok: false, detail: caught instanceof Error ? caught.message : String(caught) });
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="card source-card" style={{ "--i": index } as CSSProperties}>
      <header className="provider-head">
        <span className="source-icon"><ActionIcon kind={icon} size={18} /></span>
        <div><strong>{title}</strong></div>
        <span className={`state-pill ${source.type === "none" ? "" : "on"}`}><i />{TYPE_NAMES[source.type]}</span>
      </header>
      <div className="type-switch" role="radiogroup" aria-label={`${title} source`}>
        {Object.keys(source.types).map((t) => (
          <button aria-checked={type === t} className={type === t ? "active" : ""} key={t} onClick={() => { setType(t); setResult(null); }} role="radio" type="button">
            {TYPE_NAMES[t] ?? t}
          </button>
        ))}
      </div>
      {fields.length ? (
        <div className="source-fields">
          {fields.filter((f) => f.name !== "cloud").map((f) => (
            <label key={f.name} className={f.name === "repos" ? "wide" : ""}>
              <span>{f.label}</span>
              {f.name === "repos" ? (
                <textarea rows={3} placeholder={PLACEHOLDERS.repos} value={values[f.name] ?? ""} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} />
              ) : (
                <input autoComplete="off" spellCheck={false} type={f.secret ? "password" : "text"}
                  placeholder={f.secret && source.saved_secrets.includes(f.name) && type === source.type ? "Saved. Leave empty to keep it." : PLACEHOLDERS[f.name] ?? ""}
                  value={values[f.name] ?? ""} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} />
              )}
            </label>
          ))}
        </div>
      ) : null}
      {fields.some((f) => f.name === "cloud") ? (
        <label className="switch-tile cloud-switch">
          <span><ActionIcon kind="shield" size={13} />{values.cloud === "on" ? "Cloud models can read this code" : "Code stays on this computer"}</span>
          <input checked={values.cloud === "on"} onChange={(e) => setValues({ ...values, cloud: e.target.checked ? "on" : "off" })} role="switch" type="checkbox" />
        </label>
      ) : null}
      {result ? <p className={`source-result ${result.ok ? "ok" : "bad"}`}><ActionIcon kind={result.ok ? "success" : "fail"} size={15} />{result.detail}</p> : null}
      <div className="provider-foot">
        <span />
        <div className="foot-buttons">
          {type !== "none" ? (
            <button className="secondary" disabled={busy !== ""} onClick={() => run("test")} type="button">
              {busy === "test" ? <><ActionIcon kind="loader" />Testing</> : "Test connection"}
            </button>
          ) : null}
          <button className="primary" disabled={busy !== "" || !changed} onClick={() => run("save")} type="button">
            {busy === "save" ? <><ActionIcon kind="loader" />Saving</> : changed ? "Save" : "Saved"}
          </button>
        </div>
      </div>
    </section>
  );
}
