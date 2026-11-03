import { zoneLabel } from "./time";
import { type CSSProperties, type FormEvent, useEffect, useRef, useState } from "react";
import { checkModel, getLive, getModels, refreshModels, removeModelKey, resetRoutes, saveModelKey, saveRoutes, selectModel, setActiveModel } from "./api";
import claudeLogo from "@lobehub/icons-static-svg/icons/claude-color.svg?raw";
import geminiLogo from "@lobehub/icons-static-svg/icons/gemini-color.svg?raw";
import grokLogo from "@lobehub/icons-static-svg/icons/grok.svg?raw";
import groqLogo from "@lobehub/icons-static-svg/icons/groq.svg?raw";
import ollamaLogo from "@lobehub/icons-static-svg/icons/ollama.svg?raw";
import openaiLogo from "@lobehub/icons-static-svg/icons/openai.svg?raw";
import { ActionIcon } from "./icons";
import { Portal } from "./live";
import type { ModelCheckResult, ModelOverview, ModelProvider } from "./types";

// Provider logos: LobeHub icons , inlined as SVG so they stay sharp at every size. MIT.
const LOGOS: Record<string, string> = { gemini: geminiLogo, anthropic: claudeLogo, openai: openaiLogo, xai: grokLogo,
  groq: groqLogo, ollama: ollamaLogo };

function Monogram({ id, size = 40 }: { id: string; size?: number }) {
  const svg = (LOGOS[id] ?? "").replace(/(width|height)="1em"/g, '$1="100%"').replace(/<title>.*?<\/title>/, "");
  return (
    <span className={`monogram brand brand-${id}`} style={{ width: size, height: size, padding: size * 0.2 }} aria-hidden="true"
      dangerouslySetInnerHTML={{ __html: svg }} />
  );
}

type Act = (work: () => Promise<ModelOverview>, notice?: string) => Promise<boolean>;

/** The Models page. The admin connects providers with API keys, tests a model and selects the main model. */
export function ModelsPage({ onError, onNotice }: { onError: (message: string) => void; onNotice: (message: string) => void }) {
  const [overview, setOverview] = useState<ModelOverview | null>(null);
  const [removing, setRemoving] = useState<ModelProvider | null>(null);

  useEffect(() => { getModels().then(setOverview).catch((e: unknown) => onError(e instanceof Error ? e.message : String(e))); }, []);
  const beam = useBackupBeam();

  if (!overview) {
    return (
      <section className="models-page">
        <div className="provider-grid">{[0, 1, 2].map((i) => <section key={i} className="card provider-card skeleton" />)}</div>
      </section>
    );
  }

  const { active, backup } = overview;
  const local = active.provider === "ollama";
  const connected = overview.providers.filter((p) => p.connected);
  const available = overview.providers.filter((p) => !p.connected);

  const act: Act = async (work, notice) => {
    try {
      setOverview(await work());
      if (notice) onNotice(notice);
      return true;
    } catch (caught: unknown) {
      onError(caught instanceof Error ? caught.message : String(caught));
      return false;
    }
  };

  return (
    <section className="models-page" ref={beam.ref}>
      {beam.shots.map((shot) => (
        <span className="backup-beam" key={shot.id} style={shot.style} aria-hidden="true"><i /></span>
      ))}
      {beam.label ? <span className="backup-note" style={beam.label} role="status">A call went to the backup</span> : null}

      <RoutingCard overview={overview} act={act} />

      <section className="models-section">
        <h2 className="section-label models-label">Connected</h2>
        <div className="provider-grid">
          {connected.map((provider, i) => (
            <ConnectedCard key={provider.id} index={i} provider={provider} isMain={active.provider === provider.id} mainModel={active.model}
              act={act} onRemove={() => setRemoving(provider)} onError={onError} onQuiet={(o) => setOverview(o)} />
          ))}
          <section className={`card provider-card local${local ? " main" : ""}`} style={{ "--i": connected.length } as CSSProperties}>
            <header className="provider-head">
              <Monogram id="ollama" />
              <div><strong>Ollama</strong><small>On this computer</small></div>
              {local ? <span className="main-pill">Main model</span> : null}
            </header>
            <Usage tokens={backup.tokens_today} budget={0} />
            {!local ? (
              <div className="provider-foot">
                <span />
                <button className="secondary" onClick={() => void act(() => setActiveModel("ollama", backup.model), "The agents now use only the local model.")} type="button">Use local only</button>
              </div>
            ) : null}
          </section>
        </div>
      </section>

      {available.length ? (
        <section className="card add-providers">
          <h2 className="section-label">Add a provider</h2>
          {available.map((provider) => <ProviderRow key={provider.id} provider={provider} act={act} />)}
        </section>
      ) : null}

      {removing ? (
        <div className="modal-backdrop" role="presentation" onClick={() => setRemoving(null)}>
          <section aria-label="Confirm key removal" aria-modal="true" className="status-modal delete" role="alertdialog" onClick={(e) => e.stopPropagation()}>
            <button aria-label="Close" className="modal-close" onClick={() => setRemoving(null)} type="button">×</button>
            <p className="modal-label">Remove key</p>
            <p>Remove the {removing.maker} key from RootIQ?</p>
            {active.provider === removing.id ? <p className="muted">This is the main model. The agents will use the local model until you select another.</p> : null}
            <div className="actions">
              <button className="secondary" onClick={() => setRemoving(null)} type="button">Cancel</button>
              <button className="danger" onClick={() => { const target = removing; setRemoving(null); void act(() => removeModelKey(target.id), `${target.maker} key removed.`); }} type="button">
                <ActionIcon kind="trash" />Remove key
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </section>
  );
}

/** When a running investigation sends a call to the local backup, a light travels from the main model card to the
 *  Ollama card. The page checks /api/live every 3 seconds and compares the number of calls that the backup answered. */
function useBackupBeam() {
  const ref = useRef<HTMLElement>(null);
  const last = useRef<number | null>(null);
  const [shots, setShots] = useState<{ id: number; style: CSSProperties }[]>([]);
  const [label, setLabel] = useState<CSSProperties | null>(null);
  useEffect(() => {
    const timer = window.setInterval(async () => {
      try {
        const { backup_calls: count } = await getLive();
        const before = last.current;
        last.current = count;
        if (before === null || count <= before || !ref.current) return;
        const page = ref.current.getBoundingClientRect();
        const from = ref.current.querySelector(".provider-card.main:not(.local)")?.getBoundingClientRect();
        const to = ref.current.querySelector(".provider-card.local")?.getBoundingClientRect();
        if (!from || !to) return;
        const x1 = from.left + from.width / 2 - page.left, y1 = from.top + from.height / 2 - page.top;
        const x2 = to.left + to.width / 2 - page.left, y2 = to.top + to.height / 2 - page.top;
        const length = Math.hypot(x2 - x1, y2 - y1);
        const angle = Math.atan2(y2 - y1, x2 - x1);
        const id = Date.now();
        setShots((s) => [...s, { id, style: { left: x1, top: y1, width: length, transform: `rotate(${angle}rad)` } as CSSProperties }]);
        setLabel({ left: x2, top: to.top - page.top - 8 } as CSSProperties);
        window.setTimeout(() => setShots((s) => s.filter((shot) => shot.id !== id)), 1800);
        window.setTimeout(() => setLabel(null), 2600);
      } catch { /* the next check tries again */ }
    }, 3000);
    return () => window.clearInterval(timer);
  }, []);
  return { ref, shots, label };
}

function KeyForm({ provider, act, onDone, onCancel, label }: {
  provider: ModelProvider; act: Act; onDone: () => void; onCancel?: () => void; label: string;
}) {
  const [key, setKey] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    const ok = await act(() => saveModelKey(provider.id, key.trim()), `${provider.maker} connected.`);
    setBusy(false);
    if (ok) { setKey(""); onDone(); }
  }
  return (
    <form className="key-form" onSubmit={save}>
      <div className="key-field">
        <ActionIcon kind="key" />
        <input aria-label={`${provider.maker} API key`} autoComplete="off" autoFocus placeholder={`Paste your ${provider.maker} API key`} spellCheck={false}
          type={show ? "text" : "password"} value={key} onChange={(e) => setKey(e.target.value)} />
        <button aria-label={show ? "Hide key" : "Show key"} className="icon-button" onClick={() => setShow(!show)} type="button"><ActionIcon kind={show ? "eyeOff" : "eye"} /></button>
      </div>
      <div className="key-actions">
        <a className="text-action key-link" href={provider.keys_url} rel="noreferrer" target="_blank">Get a key<ActionIcon kind="external" size={13} /></a>
        {onCancel ? <button className="secondary" onClick={onCancel} type="button">Cancel</button> : null}
        <button className="primary" disabled={busy || key.trim().length < 8} type="submit">{busy ? <><ActionIcon kind="loader" />Checking</> : label}</button>
      </div>
    </form>
  );
}

/** A provider without a key: one compact row. The key box opens only after a click on Connect. */
function ProviderRow({ provider, act }: { provider: ModelProvider; act: Act }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`provider-row${open ? " open" : ""}`}>
      <div className="provider-row-head">
        <Monogram id={provider.id} size={34} />
        <div><strong>{provider.name}</strong><small>{provider.maker}</small></div>
        {!open ? <button className="secondary" onClick={() => setOpen(true)} type="button"><ActionIcon kind="plus" />Connect</button> : null}
      </div>
      {open ? <KeyForm provider={provider} act={act} label="Connect" onDone={() => setOpen(false)} onCancel={() => setOpen(false)} /> : null}
    </div>
  );
}

/** A provider with a key: select a model, test it, use it, see usage, replace or remove the key. */
function ConnectedCard({ provider, index, isMain, mainModel, act, onRemove, onError, onQuiet }: {
  provider: ModelProvider; index: number; isMain: boolean; mainModel: string; act: Act;
  onRemove: () => void; onError: (message: string) => void; onQuiet: (overview: ModelOverview) => void;
}) {
  const [replacing, setReplacing] = useState(false);
  const [busy, setBusy] = useState<"" | "test" | "use" | "refresh">("");
  const [model, setModel] = useState(provider.selected_model || provider.models[0] || "");
  const [result, setResult] = useState<ModelCheckResult | null>(null);
  const [confirmTest, setConfirmTest] = useState(false);
  const [confirmMain, setConfirmMain] = useState(false);  // a paid key: ask before it answers most tasks
  const useAsMain = async () => { setBusy("use"); await act(() => setActiveModel(provider.id, model), `Main model is now ${model}.`); setBusy(""); };

  useEffect(() => { setModel(provider.selected_model || provider.models[0] || ""); }, [provider.selected_model, provider.models.join()]);
  // A key from .env has no saved model list yet: load it one time, quietly.
  useEffect(() => { if (!provider.models.length) refreshModels(provider.id).then(onQuiet).catch(() => {}); }, [provider.id]);
  const options = provider.selected_model && !provider.models.includes(provider.selected_model)
    ? [provider.selected_model, ...provider.models] : provider.models;
  const inUse = isMain && mainModel === model;

  async function test() {
    setResult(null);
    setBusy("test");
    try { setResult(await checkModel(provider.id, model)); }
    catch (caught: unknown) { onError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(""); }
  }

  return (
    <section className={`card provider-card${isMain ? " main" : ""}`} style={{ "--i": index } as CSSProperties}>
      <header className="provider-head">
        <Monogram id={provider.id} />
        <div><strong>{provider.name}</strong><small>{provider.maker}</small></div>
        {provider.paused ? <span className="state-pill warn"><i />Paused: {provider.paused}</span>
          : isMain ? <span className="main-pill">Main model</span> : null}
      </header>
      <Usage tokens={provider.tokens_today} budget={provider.daily_budget} />
      {provider.id === "openai" && !isMain ? (
        <p className="muted provider-note">Writes only the Code agent's patches, when Cloud models is on for the code source.
          Use as main model would also use it for most other tasks.</p>
      ) : null}

      {confirmTest ? (
        <Portal><div className="modal-backdrop" role="presentation" onClick={() => setConfirmTest(false)}>
          <section aria-label="Test the model" aria-modal="true" className="status-modal confirm" role="alertdialog" onClick={(e) => e.stopPropagation()}>
            <button aria-label="Close" className="modal-close" onClick={() => setConfirmTest(false)} type="button">×</button>
            <p className="modal-label">Test {model}</p>
            <p>This will make 3 small calls to {provider.maker}: a plain reply, a tool call and a structured reply.</p>
            <p className="muted">They count against your {provider.maker} quota (a free Gemini key allows 20 a day for each model).</p>
            <div className="actions">
              <button className="secondary" onClick={() => setConfirmTest(false)} type="button">Cancel</button>
              <button className="primary" onClick={() => { setConfirmTest(false); void test(); }} type="button">Run the test</button>
            </div>
          </section>
        </div></Portal>
      ) : null}
      {confirmMain ? (
        <Portal><div className="modal-backdrop" role="presentation" onClick={() => setConfirmMain(false)}>
          <section aria-label="Use as main model" aria-modal="true" className="status-modal confirm" role="alertdialog" onClick={(e) => e.stopPropagation()}>
            <button aria-label="Close" className="modal-close" onClick={() => setConfirmMain(false)} type="button">×</button>
            <p className="modal-label">Use {model} as the main model?</p>
            <p>It then answers most tasks of the agents, not only the code patches. Each investigation makes many more calls to {provider.maker}.</p>
            <p className="muted">{provider.maker} charges these calls to your account{provider.daily_budget ? `; the daily budget stops it at ${provider.daily_budget.toLocaleString()} tokens` : ""}.</p>
            <div className="actions">
              <button className="secondary" onClick={() => setConfirmMain(false)} type="button">Cancel</button>
              <button className="primary" onClick={() => { setConfirmMain(false); void useAsMain(); }} type="button">Use as main model</button>
            </div>
          </section>
        </div></Portal>
      ) : null}
      {replacing ? (
        <KeyForm provider={provider} act={act} label="Save key" onDone={() => setReplacing(false)} onCancel={() => setReplacing(false)} />
      ) : (
        <>
          <div className="model-row">
            <select aria-label={`${provider.maker} model`} value={model} onChange={(e) => {
              const next = e.target.value;  // saved at once: the agents use the model that the page shows
              setModel(next); setResult(null);
              selectModel(provider.id, next).then(onQuiet).catch((caught: unknown) => onError(caught instanceof Error ? caught.message : String(caught)));
            }}>
              {!options.length ? <option value="">Loading models</option> : null}
              {options.map((name) => <option key={name} value={name}>{name}</option>)}
            </select>
            <button aria-label="Reload the model list" className="icon-button" disabled={busy !== ""} title="Reload the model list"
              onClick={async () => { setBusy("refresh"); await act(() => refreshModels(provider.id)); setBusy(""); }} type="button">
              <ActionIcon kind={busy === "refresh" ? "loader" : "reset"} />
            </button>
          </div>
          <div className="provider-foot">
            <div className="key-meta">
              <span className="key-chip"><ActionIcon kind="key" size={13} />{provider.key_hint}</span>
              <a className="text-action" href={provider.usage_url} rel="noreferrer" target="_blank">Usage<ActionIcon kind="external" size={12} /></a>
              <button className="text-action" onClick={() => setReplacing(true)} type="button">Replace</button>
              {provider.key_source === "page" ? (
                <button aria-label={`Remove ${provider.maker} key`} className="icon-button danger-hover" onClick={onRemove} type="button"><ActionIcon kind="trash" /></button>
              ) : null}
            </div>
            <div className="foot-buttons">
              <button className="secondary" disabled={busy !== "" || !model} onClick={() => setConfirmTest(true)} type="button">
                {busy === "test" ? <><ActionIcon kind="loader" />Testing</> : "Test"}
              </button>
              {!inUse ? (
                <button className="primary" disabled={busy !== "" || !model} type="button"
                  onClick={() => (provider.id === "openai" ? setConfirmMain(true) : void useAsMain())}>
                  {busy === "use" ? <><ActionIcon kind="loader" />Saving</> : "Use as main model"}
                </button>
              ) : null}
            </div>
          </div>
          {busy === "test" || result ? (
            <ul className="check-list" aria-live="polite">
              {(result?.checks ?? [{ name: "Plain reply" }, { name: "Tool call" }, { name: "Structured reply" }]).map((check, i) => {
                const done = "ok" in check;
                return (
                  <li key={check.name} className={done ? (check.ok ? "ok" : "bad") : "wait"} style={{ "--i": i } as CSSProperties}>
                    <ActionIcon kind={!done ? "loader" : check.ok ? "success" : "fail"} size={15} />
                    <span>{check.name}</span>
                    <small title={done ? check.detail : undefined}>{done ? check.detail : "Waiting"}</small>
                    {done ? <time>{check.ms} ms</time> : null}
                  </li>
                );
              })}
            </ul>
          ) : null}
        </>
      )}
    </section>
  );
}

const TIER_NAMES: Record<string, string> = { complex: "Complex", standard: "Standard", simple: "Simple", code: "Code" };
const AGENT_NAMES: Record<string, string> = {
  lead: "Lead", reviewer: "Reviewer", finalize: "Final analysis", remediation: "Remediation", verifier: "Verification",
  evidence: "Evidence", history: "History", changes: "Changes", analysis: "Quick analysis", postmortem: "Postmortem",
  single: "Single agent", comms: "Messages", reflect: "Lessons", code: "Code agent: reads with the local model; this chain writes the patch", correlation: "Correlation",
};

/** Models by task: each task level has a chain of models. A call goes to the first model; when it fails or its quota is
 * used up, the same call goes to the next. Hard work goes to the strongest models; easy work does not use their quota. */
function RoutingCard({ overview, act }: { overview: ModelOverview; act: Act }) {
  const { routing } = overview;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<string, string[]>>({});
  const [confirmReset, setConfirmReset] = useState(false);
  const options = [...overview.providers.filter((p) => p.connected).map((p) => ({ id: p.id as string, name: `${p.name} · ${p.selected_model || "model"}` })),
    { id: "ollama", name: `Local · ${overview.backup.model}` }];
  const nameOf = (id: string) => (id === "ollama" ? "Local" : overview.providers.find((p) => p.id === id)?.name ?? id);

  function begin() {
    setDraft(Object.fromEntries(routing.tiers.map((t) => [t.tier, t.chain.filter((p) => options.some((o) => o.id === p))])));
    setEditing(true);
  }

  function setSlot(tier: string, index: number, value: string) {
    const chain = [...(draft[tier] ?? [])];
    if (value === "") chain.splice(index, 1);
    else chain[index] = value;
    setDraft({ ...draft, [tier]: chain.filter((p, i) => chain.indexOf(p) === i) });
  }

  async function save() {
    if (await act(() => saveRoutes(draft), "Model chains saved. The next agent run uses them.")) setEditing(false);
  }

  return (
    <section className="card routing-card">
      <header className="routing-head">
        <span className="section-label">Models by task</span>
        <div className="foot-buttons">
          {routing.local_only ? <span className="state-pill"><i />Local only</span> : routing.custom ? <span className="state-pill on"><i />Custom</span> : <span className="state-pill on"><i />Automatic</span>}
          {!editing ? <button className="secondary" onClick={begin} type="button">Change</button> : null}
        </div>
      </header>
      <div className="tier-list">
        {routing.tiers.map((t, i) => (
          <div className={`tier-row tier-${t.tier}`} key={t.tier} style={{ "--i": i } as CSSProperties}>
            <div className="tier-name">
              <strong>{TIER_NAMES[t.tier]}</strong>
              <small>{t.agents.map((a) => AGENT_NAMES[a] ?? a).join(", ")}</small>
            </div>
            {editing ? (
              <div className="chain-edit">
                {[0, 1, 2, 3].slice(0, Math.min(4, (draft[t.tier]?.length ?? 0) + 1)).map((slot) => (
                  <span className="chain-slot" key={slot}>
                    {slot > 0 ? <ActionIcon kind="arrow" size={13} /> : null}
                    <select aria-label={`${TIER_NAMES[t.tier]} choice ${slot + 1}`} value={draft[t.tier]?.[slot] ?? ""} onChange={(e) => setSlot(t.tier, slot, e.target.value)}>
                      <option value="">{slot === 0 ? "Choose" : "None"}</option>
                      {options.filter((o) => o.id === draft[t.tier]?.[slot] || !draft[t.tier]?.includes(o.id)).map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
                    </select>
                  </span>
                ))}
              </div>
            ) : (
              <div className="chain">
                {t.skipped.map((m) => (
                  <span className="chain-chip skipped" key={`skip-${m.provider}`} title={m.reason}>
                    <Monogram id={m.provider} size={20} />
                    <span><b>{nameOf(m.provider)}</b><small>{m.reason}</small></span>
                  </span>
                ))}
                {t.models.map((m, n) => (
                  <span className="chain-step" key={`${m.provider}-${n}`}>
                    {n > 0 ? <ActionIcon kind="arrow" size={13} /> : null}
                    <span className={`chain-chip${m.paused ? " paused" : ""}${n === 0 ? " first" : ""}`} title={m.paused ? "Paused: quota used up" : undefined}>
                      <Monogram id={m.provider} size={20} />
                      <span><b>{nameOf(m.provider)}</b><small>{m.model}</small></span>
                    </span>
                  </span>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
      {editing ? (
        <div className="provider-foot">
          <button className="text-action" onClick={() => setConfirmReset(true)} type="button">Use the automatic chains</button>
          <div className="foot-buttons">
            <button className="secondary" onClick={() => setEditing(false)} type="button">Cancel</button>
            <button className="primary" disabled={routing.tiers.some((t) => !(draft[t.tier]?.length))} onClick={save} type="button">Save chains</button>
          </div>
        </div>
      ) : null}
      {confirmReset ? (
        <Portal>
          <div className="modal-backdrop" role="presentation" onClick={() => setConfirmReset(false)}>
            <section aria-label="Use the automatic chains" aria-modal="true" className="status-modal confirm" role="alertdialog" onClick={(e) => e.stopPropagation()}>
              <button aria-label="Close" className="modal-close" onClick={() => setConfirmReset(false)} type="button">×</button>
              <p className="modal-label">Use the automatic chains</p>
              <p>Complex tasks: Groq, then Gemini. Standard tasks: Gemini, then Groq. Simple tasks: the local model first. The local model is always the last backup.</p>
              <div className="actions">
                <button className="secondary" onClick={() => setConfirmReset(false)} type="button">Cancel</button>
                <button className="primary" onClick={async () => { setConfirmReset(false); if (await act(resetRoutes, "Automatic chains are on.")) setEditing(false); }} type="button">Use automatic</button>
              </div>
            </section>
          </div>
        </Portal>
      ) : null}
    </section>
  );
}

function compact(n: number) {
  return n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k` : `${n}`;
}

/** Tokens used today, against the daily budget when there is one. */
function Usage({ tokens, budget }: { tokens: number; budget: number }) {
  const share = budget ? Math.min(1, tokens / budget) : 0;
  return (
    <div className={`usage${share >= 0.7 ? " high" : ""}`}>
      <div className="usage-numbers">
        <span title="Counted per day in the time zone of the app. Each provider has its own limits; its Usage page shows them.">Today ({zoneLabel()})</span>
        <strong>{compact(tokens)}<small> tokens{budget ? ` of ${compact(budget)}` : ""}</small></strong>
      </div>
      {budget ? <i style={{ "--w": `${Math.max(2, share * 100)}%` } as CSSProperties}><em /></i> : null}
    </div>
  );
}
