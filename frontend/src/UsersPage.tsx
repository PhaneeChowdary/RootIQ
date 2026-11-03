import { shortDate } from "./time";
import { type CSSProperties, type FormEvent, useEffect, useMemo, useState } from "react";
import { createUser, getCurrentUser, listUsers, removeUser, unlockUser, updateUser } from "./api";
import { ActionIcon } from "./icons";
import { Portal } from "./live";
import { CountUp } from "./motion";
import { ROLE_LABELS } from "./role";
import type { Role, UserAccount } from "./types";

const ROLES: Role[] = ["admin", "engineer", "viewer"];
type Filter = "all" | Role | "disabled";
type Confirm = { title: string; message: string; label: string; notice: string; danger?: boolean; run: () => Promise<UserAccount[]> };

/** A stable color for each person, from their name. */
function hue(name: string) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return h;
}

function Avatar({ user, size = 52 }: { user: Pick<UserAccount, "display_name" | "username"> & Partial<UserAccount>; size?: number }) {
  const h = hue(user.username);
  const state = user.disabled ? "off" : user.locked ? "locked" : user.signed_in ? "online" : "";
  return (
    <span className={`avatar-ring ${state}`} style={{ width: size + 8, height: size + 8 }}>
      <span className="avatar big" style={{ width: size, height: size, fontSize: size * 0.4,
        background: `linear-gradient(135deg, hsl(${h} 72% 58%), hsl(${(h + 48) % 360} 70% 42%))` }}>
        {(user.display_name || user.username).slice(0, 1).toUpperCase()}
      </span>
    </span>
  );
}

function ago(iso: string | null) {
  if (!iso) return "Never";
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 2) return "Just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return days < 30 ? `${days} d ago` : shortDate(iso);
}

/** Settings > Users: who can sign in, and what each person can do. */
export function UsersPage({ onError, onNotice }: { onError: (message: string) => void; onNotice: (message: string) => void }) {
  const [users, setUsers] = useState<UserAccount[] | null>(null);
  const [me, setMe] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [adding, setAdding] = useState(false);
  const [resetFor, setResetFor] = useState<UserAccount | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [busy, setBusy] = useState("");

  useEffect(() => {
    listUsers().then(setUsers).catch((e: unknown) => onError(message(e)));
    getCurrentUser().then((u) => setMe(u.username)).catch(() => undefined);
  }, []);

  const shown = useMemo(() => (users ?? []).filter((u) => {
    const q = query.trim().toLowerCase();
    const match = !q || [u.display_name, u.username, u.email].some((v) => v.toLowerCase().includes(q));
    const inFilter = filter === "all" ? true : filter === "disabled" ? u.disabled : u.role === filter && !u.disabled;
    return match && inFilter;
  }), [users, query, filter]);

  async function act(key: string, work: () => Promise<UserAccount[]>, notice: string) {
    setBusy(key);
    try {
      setUsers(await work());
      onNotice(notice);
    } catch (caught: unknown) {
      onError(message(caught));
    } finally {
      setBusy("");
    }
  }

  function changeRole(user: UserAccount, role: Role) {
    if (role === user.role) return;
    setConfirm({
      title: `${user.display_name}: ${ROLE_LABELS[role]}`, message: user.signed_in ? "They will be signed out and must sign in again to get the new role." : "",
      label: "Change role", notice: `${user.display_name} is now ${ROLE_LABELS[role].toLowerCase()}.`,
      run: () => updateUser(user.username, { role }),
    });
  }

  function toggleDisabled(user: UserAccount) {
    setConfirm(user.disabled
      ? { title: `Enable ${user.display_name}?`, message: "They will be able to sign in again with their role.", label: "Enable", notice: `${user.display_name} can sign in again.`,
          run: () => updateUser(user.username, { disabled: false }) }
      : { title: `Disable ${user.display_name}?`, message: "They will be signed out at once and cannot sign in until you enable the account.", label: "Disable", danger: true,
          notice: `${user.display_name} is disabled.`, run: () => updateUser(user.username, { disabled: true }) });
  }

  function remove(user: UserAccount) {
    setConfirm({ title: `Remove ${user.display_name}?`, message: "The account will be deleted and its sessions will end. The audit log will keep its name.",
      label: "Remove", danger: true, notice: `${user.display_name} is removed.`, run: () => removeUser(user.username) });
  }

  if (!users) {
    return (
      <div className="team-page">
        <section className="card team-hero skeleton" />
        <div className="member-grid">{[0, 1, 2].map((i) => <section key={i} className="card member-card skeleton" />)}</div>
      </div>
    );
  }

  const active = users.filter((u) => !u.disabled);
  const counts = Object.fromEntries(ROLES.map((r) => [r, active.filter((u) => u.role === r).length])) as Record<Role, number>;
  const online = active.filter((u) => u.signed_in).length;
  const locked = users.filter((u) => u.locked).length;
  const sso = active.filter((u) => u.auth === "sso").length;
  const filters: { id: Filter; label: string; count: number }[] = [
    { id: "all", label: "All", count: users.length },
    ...ROLES.map((r) => ({ id: r as Filter, label: `${ROLE_LABELS[r]}s`, count: counts[r] })),
    { id: "disabled", label: "Disabled", count: users.length - active.length },
  ];

  return (
    <div className="team-page">
      <section className="card team-hero">
        <div className="team-lead">
          <span className="section-label">Team</span>
          <strong className="team-count"><CountUp value={active.length} /><small>{active.length === 1 ? "member" : "members"}</small></strong>
          <div className="avatar-stack" aria-hidden="true">
            {active.slice(0, 6).map((u, i) => <span key={u.username} style={{ "--i": i } as CSSProperties}><Avatar user={u} size={30} /></span>)}
            {active.length > 6 ? <span className="stack-more">+{active.length - 6}</span> : null}
          </div>
        </div>
        <div className="team-roles">
          <div className="role-bar" role="img" aria-label={ROLES.map((r) => `${counts[r]} ${ROLE_LABELS[r]}`).join(", ")}>
            {ROLES.map((r) => counts[r] ? <i key={r} className={r} style={{ flexGrow: counts[r] }} /> : null)}
          </div>
          <div className="role-legend">
            {ROLES.map((r) => <span key={r}><i className={r} />{ROLE_LABELS[r]}<b>{counts[r]}</b></span>)}
          </div>
        </div>
        <div className="team-stats">
          <div><span className="stat-label"><i className="online-dot" />Online</span><strong>{online}</strong></div>
          <div><span className="stat-label"><ActionIcon kind="fingerprint" size={13} />SSO</span><strong>{sso}</strong></div>
          <div className={locked ? "warn" : ""}><span className="stat-label"><ActionIcon kind="lock" size={13} />Locked</span><strong>{locked}</strong></div>
        </div>
        <button className="primary team-add" onClick={() => setAdding(true)} type="button"><ActionIcon kind="userPlus" />Add user</button>
      </section>

      <div className="team-toolbar">
        <label className="team-search">
          <ActionIcon kind="search" size={15} />
          <input aria-label="Search users" placeholder="Search" value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>
        <div className="type-switch team-filters" role="radiogroup" aria-label="Filter users">
          {filters.map((f) => (
            <button aria-checked={filter === f.id} className={filter === f.id ? "active" : ""} key={f.id} onClick={() => setFilter(f.id)} role="radio" type="button">
              {f.label}<span className="filter-count">{f.count}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="member-grid">
        {shown.map((user, i) => {
          const fixed = user.protected || user.username === me;
          return (
            <article className={`card member-card${user.disabled ? " disabled" : ""}${user.locked ? " locked" : ""}`} key={user.username} style={{ "--i": i } as CSSProperties}>
              <header className="member-head">
                <Avatar user={user} />
                <div className="member-name">
                  <strong>{user.display_name}{user.username === me ? <small className="you-chip">You</small> : null}</strong>
                  <span>{user.email || user.username}</span>
                </div>
                {fixed ? <span className={`role-chip ${user.role}`}>{ROLE_LABELS[user.role]}</span> : null}
              </header>
              <div className="member-meta">
                <span><ActionIcon kind={user.auth === "sso" ? "fingerprint" : "key"} size={13} />{user.auth === "sso" ? "SSO" : "Password"}</span>
                <span className={user.signed_in && !user.disabled ? "online" : ""}>
                  {user.disabled ? "Disabled" : user.signed_in ? <><i className="online-dot" />Online</> : ago(user.last_login)}
                </span>
                {user.locked ? <span className="locked-chip"><ActionIcon kind="lock" size={12} />Locked</span> : null}
              </div>
              <footer className="member-actions">
                {fixed ? (
                  <span className="member-fixed"><ActionIcon kind="shield" size={13} />{user.protected ? "Owner account" : "Your account"}</span>
                ) : (
                  <div className="role-segments" role="radiogroup" aria-label={`Role of ${user.display_name}`}>
                    {ROLES.map((r) => (
                      <button aria-checked={user.role === r} className={user.role === r ? `active ${r}` : ""} disabled={busy !== "" || user.disabled}
                        key={r} onClick={() => changeRole(user, r)} role="radio" type="button">{ROLE_LABELS[r]}</button>
                    ))}
                  </div>
                )}
                <div className="member-buttons">
                  {user.locked ? (
                    <button aria-label={`Unlock ${user.display_name}`} className="icon-button" disabled={busy !== ""} title="Unlock"
                      onClick={() => void act("unlock", () => unlockUser(user.username), `${user.display_name} can sign in again.`)} type="button"><ActionIcon kind="unlock" /></button>
                  ) : null}
                  {!user.protected && user.auth === "password" ? (
                    <button aria-label={`New password for ${user.display_name}`} className="icon-button" disabled={busy !== ""} title="New password" onClick={() => setResetFor(user)} type="button"><ActionIcon kind="key" /></button>
                  ) : null}
                  {!fixed ? (
                    <button aria-label={`${user.disabled ? "Enable" : "Disable"} ${user.display_name}`} className={`icon-button ${user.disabled ? "" : "danger-hover"}`}
                      disabled={busy !== ""} title={user.disabled ? "Enable" : "Disable"} onClick={() => toggleDisabled(user)} type="button"><ActionIcon kind="power" /></button>
                  ) : null}
                  {!fixed ? (
                    <button aria-label={`Remove ${user.display_name}`} className="icon-button danger-hover" disabled={busy !== ""} title="Remove"
                      onClick={() => remove(user)} type="button"><ActionIcon kind="trash" /></button>
                  ) : null}
                </div>
              </footer>
            </article>
          );
        })}
        {!shown.length ? <p className="muted team-empty">No users match.</p> : null}
      </div>

      {adding ? <AddUserModal onClose={() => setAdding(false)} onAdded={(next, name) => { setUsers(next); setAdding(false); onNotice(`${name} added.`); }} /> : null}
      {resetFor ? <ResetPasswordModal user={resetFor} onClose={() => setResetFor(null)} onDone={(next) => { setUsers(next); onNotice(`New password set for ${resetFor.display_name}.`); setResetFor(null); }} /> : null}
      {confirm ? (
        <Portal>
          <div className="modal-backdrop" role="presentation" onClick={() => setConfirm(null)}>
            <section aria-label={confirm.title} aria-modal="true" className={`status-modal ${confirm.danger ? "delete" : "confirm"}`} role="alertdialog" onClick={(e) => e.stopPropagation()}>
              <button aria-label="Close" className="modal-close" onClick={() => setConfirm(null)} type="button">×</button>
              <p className="modal-label">{confirm.title}</p>
              {confirm.message ? <p>{confirm.message}</p> : null}
              <div className="actions">
                <button className="secondary" onClick={() => setConfirm(null)} type="button">Cancel</button>
                <button className={confirm.danger ? "danger" : "primary"} onClick={() => { const c = confirm; setConfirm(null); void act("confirm", c.run, c.notice); }} type="button">{confirm.label}</button>
              </div>
            </section>
          </div>
        </Portal>
      ) : null}
    </div>
  );
}

function AddUserModal({ onClose, onAdded }: { onClose: () => void; onAdded: (users: UserAccount[], name: string) => void }) {
  const [form, setForm] = useState({ username: "", display_name: "", email: "", role: "engineer" as Role, password: "" });
  const [method, setMethod] = useState<"password" | "sso">("password");
  const [state, setState] = useState<{ busy?: boolean; error?: string }>({});
  const set = (key: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value });

  async function submit(event: FormEvent) {
    event.preventDefault();
    setState({ busy: true });
    const username = method === "sso" ? (form.username || form.email) : form.username;
    try {
      onAdded(await createUser({ ...form, username, password: method === "password" ? form.password : "" }), form.display_name || username);
    } catch (caught: unknown) {
      setState({ error: message(caught) });
    }
  }

  return (
    <Portal>
      <div className="modal-backdrop" role="presentation" onClick={onClose}>
        <form aria-label="Add user" aria-modal="true" className="status-modal user-form" role="dialog" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
          <button aria-label="Close" className="modal-close" onClick={onClose} type="button">×</button>
          <div className="user-form-head">
            <Avatar user={{ username: form.username || form.email || "new", display_name: form.display_name || "+" }} size={44} />
            <p className="modal-label">Add user</p>
          </div>
          <div className="type-switch" role="radiogroup" aria-label="Sign-in method">
            <button aria-checked={method === "password"} className={method === "password" ? "active" : ""} onClick={() => setMethod("password")} role="radio" type="button"><ActionIcon kind="key" size={14} />Password</button>
            <button aria-checked={method === "sso"} className={method === "sso" ? "active" : ""} onClick={() => setMethod("sso")} role="radio" type="button"><ActionIcon kind="fingerprint" size={14} />Single sign-on</button>
          </div>
          <div className="source-fields">
            <label><span>Name</span><input placeholder="Ann Lee" value={form.display_name} onChange={set("display_name")} /></label>
            <label><span>Email{method === "sso" ? "" : " (optional)"}</span><input placeholder="ann@company.com" required={method === "sso"} type="email" value={form.email} onChange={set("email")} /></label>
            {method === "password" ? (
              <>
                <label><span>Username</span><input autoComplete="off" placeholder="ann" required value={form.username} onChange={set("username")} /></label>
                <label><span>First password</span><input autoComplete="new-password" placeholder="10+ characters, Aa and 1" required type="password" value={form.password} onChange={set("password")} /></label>
              </>
            ) : null}
          </div>
          <div className="role-segments role-choice" role="radiogroup" aria-label="Role">
            {ROLES.map((role) => (
              <button aria-checked={form.role === role} className={form.role === role ? `active ${role}` : ""} key={role} onClick={() => setForm({ ...form, role })} role="radio" type="button">
                {ROLE_LABELS[role]}
              </button>
            ))}
          </div>
          {state.error ? <p className="source-result bad"><ActionIcon kind="fail" size={15} />{state.error}</p> : null}
          <div className="actions">
            <button className="secondary" onClick={onClose} type="button">Cancel</button>
            <button className="primary" disabled={state.busy} type="submit">{state.busy ? "Adding..." : "Add user"}</button>
          </div>
        </form>
      </div>
    </Portal>
  );
}

function ResetPasswordModal({ user, onClose, onDone }: { user: UserAccount; onClose: () => void; onDone: (users: UserAccount[]) => void }) {
  const [password, setPassword] = useState("");
  const [state, setState] = useState<{ busy?: boolean; error?: string }>({});
  async function submit(event: FormEvent) {
    event.preventDefault();
    setState({ busy: true });
    try {
      onDone(await updateUser(user.username, { password }));
    } catch (caught: unknown) {
      setState({ error: message(caught) });
    }
  }
  return (
    <Portal>
      <div className="modal-backdrop" role="presentation" onClick={onClose}>
        <form aria-label="Set a new password" aria-modal="true" className="status-modal user-form" role="dialog" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
          <button aria-label="Close" className="modal-close" onClick={onClose} type="button">×</button>
          <div className="user-form-head">
            <Avatar user={user} size={44} />
            <p className="modal-label">New password for {user.display_name}</p>
          </div>
          <input autoComplete="new-password" placeholder="10+ characters, Aa and 1" required type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
          {state.error ? <p className="source-result bad"><ActionIcon kind="fail" size={15} />{state.error}</p> : null}
          <div className="actions">
            <button className="secondary" onClick={onClose} type="button">Cancel</button>
            <button className="primary" disabled={state.busy || !password} type="submit">{state.busy ? "Saving..." : "Set password"}</button>
          </div>
        </form>
      </div>
    </Portal>
  );
}

function message(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}
