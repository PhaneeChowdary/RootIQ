"""Users, roles and sessions.

Roles. Each role can do all that the role before it can do:
- viewer:   reads cases, agent runs, Insights and the audit log. Cannot change data.
- engineer: works on the cases: makes and analyzes them, runs the agents, approves or rejects actions, and moves cases
  to the bin.
- admin:    also manages Settings, for example models, data sources and users, and deletes cases forever.

The account in backend/.env, ROOTIQ_AUTH_USERNAME and _PASSWORD, is always an admin. It is the first account, and
the account to use when the other admins cannot sign in. The Users page cannot disable it, remove it or change its role.

The app keeps passwords as scrypt hashes. A session is a random token. The database keeps only a SHA-256 hash of the
token, so a copy of the database does not give working sessions. Sessions stay valid after a restart of the app. A
change of role, a password reset, a disabled account or a removed account ends the sessions of that user at once.
"""

import hashlib
import hmac
import re
import secrets
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import db, migrations
from ..models import AuthUser, LoginResponse

ROLES = ("viewer", "engineer", "admin")
MIN_PASSWORD = 10
USERNAME = re.compile(r"^[A-Za-z0-9._@+-]{2,120}$")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


class UserError(ValueError):
    """A request about a user that the app cannot do. The message tells the admin why."""


class LoginLocked(Exception):
    """Too many failed sign-ins for this account or this address. `minutes` until the next try."""

    def __init__(self, minutes: int) -> None:
        super().__init__(f"Too many failed sign-ins. Try again in {minutes} minute{'s' if minutes != 1 else ''}.")
        self.minutes = minutes


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual.hex(), digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def role_at_least(role: str, needed: str) -> bool:
    return role in ROLES and ROLES.index(role) >= ROLES.index(needed)


class LocalAuthenticator:
    """Users and sessions in the database, see db.py. Without a database path, everything stays in memory, for tests."""

    def __init__(self, username: str, password: str, session_hours: int = 8, db_path: Path | str = ":memory:",
                 max_failures: int = 5, lock_minutes: int = 15) -> None:
        self.username = username
        self.session_hours = session_hours
        self.max_failures = max_failures
        self.lock_minutes = lock_minutes
        self._lock = threading.Lock()
        self._database = db.get(db_path)
        migrations.upgrade(self._database)
        self._bootstrap_admin(username, password)

    # ------------------------------------------------------------ setup

    def _bootstrap_admin(self, username: str, password: str) -> None:
        """The .env account: always present, always an admin, password always the one in .env."""
        with self._lock, self._database.connect() as conn:
            row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO users (username, display_name, role, password_hash, created_at) VALUES (?, ?, 'admin', ?, ?)",
                                 (username, "Administrator", hash_password(password), _now().isoformat()))
            else:
                changes = {"role": "admin", "disabled": 0, "auth": "password"}
                if not check_password(password, row["password_hash"]):
                    changes["password_hash"] = hash_password(password)
                    conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
                sets = ", ".join(f"{k} = ?" for k in changes)
                conn.execute(f"UPDATE users SET {sets} WHERE username = ?", (*changes.values(), username))

    # ------------------------------------------------------------ sessions

    def _user(self, row) -> AuthUser:
        return AuthUser(username=row["username"], role=row["role"], display_name=row["display_name"] or row["username"],
                        email=row["email"], auth=row["auth"])

    def _start_session(self, username: str) -> LoginResponse:
        token = secrets.token_urlsafe(32)
        expires_at = _now() + timedelta(hours=self.session_hours)
        with self._lock, self._database.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now().isoformat(),))
            conn.execute("INSERT INTO sessions (token_hash, username, expires_at, created_at) VALUES (?, ?, ?, ?)", (_token_hash(token), username, expires_at.isoformat(), _now().isoformat()))
            conn.execute("UPDATE users SET last_login = ? WHERE username = ?", (_now().isoformat(), username))
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return LoginResponse(access_token=token, expires_at=expires_at, user=self._user(row))

    def login(self, username: str, password: str, address: str = "") -> LoginResponse | None:
        """A session, or None for a wrong name or password. Raises LoginLocked after too many failures:
        max_failures for one account, or four times that number from one network address."""
        keys = [(f"user:{username.strip().lower()}", self.max_failures)]
        if address:
            keys.append((f"ip:{address}", self.max_failures * 4))
        self._check_lock(keys)
        with self._lock, self._database.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE lower(username) = lower(?)", (username.strip(),)).fetchone()
        stored = row["password_hash"] if row else hash_password("not-a-user")  # same work for unknown users
        if not check_password(password, stored) or row is None or row["disabled"] or not row["password_hash"]:
            self._note_failure(keys)
            return None
        with self._lock, self._database.connect() as conn:
            conn.execute("DELETE FROM login_failures WHERE key = ?", (keys[0][0],))
        return self._start_session(row["username"])

    def _check_lock(self, keys: list[tuple[str, int]]) -> None:
        if self.max_failures <= 0:
            return
        now = _now()
        with self._lock, self._database.connect() as conn:
            for key, _ in keys:
                row = conn.execute("SELECT locked_until FROM login_failures WHERE key = ?", (key,)).fetchone()
                if row and row["locked_until"] and row["locked_until"] > now.isoformat():
                    left = datetime.fromisoformat(row["locked_until"]) - now
                    raise LoginLocked(max(1, int(left.total_seconds() // 60) + 1))

    def _note_failure(self, keys: list[tuple[str, int]]) -> None:
        """Count a failure. The count starts again after lock_minutes without a lock."""
        if self.max_failures <= 0:
            return
        now = _now()
        window = timedelta(minutes=self.lock_minutes)
        with self._lock, self._database.connect() as conn:
            for key, limit in keys:
                row = conn.execute("SELECT * FROM login_failures WHERE key = ?", (key,)).fetchone()
                if row is None or datetime.fromisoformat(row["first_at"]) + window < now and not (row["locked_until"] or "") > now.isoformat():
                    failures, first_at = 1, now.isoformat()
                else:
                    failures, first_at = row["failures"] + 1, row["first_at"]
                locked_until = (now + window).isoformat() if failures >= limit else None
                conn.execute("INSERT INTO login_failures (key, failures, first_at, locked_until) VALUES (?, ?, ?, ?) "
                             "ON CONFLICT (key) DO UPDATE SET failures = excluded.failures, first_at = excluded.first_at, "
                             "locked_until = excluded.locked_until", (key, failures, first_at, locked_until))

    def locked_accounts(self) -> list[dict]:
        with self._lock, self._database.connect() as conn:
            rows = conn.execute("SELECT * FROM login_failures WHERE locked_until > ?", (_now().isoformat(),)).fetchall()
        return [{"key": r["key"], "locked_until": r["locked_until"]} for r in rows]

    def unlock(self, username: str) -> None:
        with self._lock, self._database.connect() as conn:
            conn.execute("DELETE FROM login_failures WHERE key = ?", (f"user:{username.strip().lower()}",))

    def login_sso(self, email: str, name: str, allowed_domains: set[str], default_role: str) -> LoginResponse:
        """Sign in a user whom the identity provider has checked. A new user needs an allowed email domain.
        Only a single sign-on account signs in this way: a password account keeps its password."""
        email = email.strip().lower()
        with self._lock, self._database.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row is not None and row["auth"] != "sso":
            raise UserError("This account signs in with a password, not with single sign-on.")
        if row is None:
            domain = email.rsplit("@", 1)[-1]
            if domain not in allowed_domains:
                raise UserError(f"{email} has no account here. Ask an admin to add you.")
            self.create_user(email, name or email, email, default_role if default_role in ROLES else "viewer", None, auth="sso")
            with self._lock, self._database.connect() as conn:
                row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if row["disabled"]:
            raise UserError("This account is disabled. Ask an admin.")
        return self._start_session(row["username"])

    def authenticate(self, header: str | None) -> AuthUser | None:
        if not header or not header.startswith("Bearer "):
            return None
        token = header.removeprefix("Bearer ").strip()
        with self._lock, self._database.connect() as conn:
            row = conn.execute(
                """SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.username = s.username
                   WHERE s.token_hash = ?""", (_token_hash(token),)).fetchone()
        if row is None or row["disabled"] or row["expires_at"] <= _now().isoformat():
            return None
        return self._user(row)

    def logout(self, header: str | None) -> None:
        if header and header.startswith("Bearer "):
            with self._lock, self._database.connect() as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(header.removeprefix("Bearer ").strip()),))

    # ------------------------------------------------------------ users: Settings > Users

    def list_users(self) -> list[dict]:
        with self._lock, self._database.connect() as conn:
            rows = conn.execute(
                """SELECT u.*, (SELECT COUNT(*) FROM sessions s WHERE s.username = u.username AND s.expires_at > ?) AS sessions
                   FROM users u ORDER BY u.role = 'admin' DESC, u.username""", (_now().isoformat(),)).fetchall()
            locked = {r["key"] for r in conn.execute("SELECT key FROM login_failures WHERE locked_until > ?", (_now().isoformat(),))}
        return [{"username": r["username"], "display_name": r["display_name"] or r["username"], "email": r["email"], "role": r["role"],
                 "auth": r["auth"], "disabled": bool(r["disabled"]), "created_at": r["created_at"], "last_login": r["last_login"],
                 "signed_in": r["sessions"] > 0, "protected": r["username"] == self.username,
                 "locked": f"user:{r['username'].lower()}" in locked} for r in rows]

    def create_user(self, username: str, display_name: str, email: str, role: str, password: str | None, auth: str = "password") -> None:
        username, email = username.strip(), email.strip().lower()
        if not USERNAME.match(username):
            raise UserError("A username has 2 to 120 letters, digits or . _ @ + -")
        if role not in ROLES:
            raise UserError(f"The role must be one of: {', '.join(ROLES)}.")
        if auth == "password" and not password:
            raise UserError("Give a password, or give an email for single sign-on.")
        if password:
            _check_strength(password)
        try:
            with self._lock, self._database.connect() as conn:
                conn.execute(
                    "INSERT INTO users (username, display_name, email, role, password_hash, auth, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (username, display_name.strip(), email, role, hash_password(password) if password else "", auth, _now().isoformat()))
        except db.IntegrityError as exc:
            raise UserError("A user with this username or email exists.") from exc

    def update_user(self, username: str, *, role: str | None = None, disabled: bool | None = None,
                    password: str | None = None, display_name: str | None = None) -> None:
        if username == self.username and (role not in (None, "admin") or disabled):
            raise UserError("The account in backend/.env is always an active admin.")
        if role is not None and role not in ROLES:
            raise UserError(f"The role must be one of: {', '.join(ROLES)}.")
        if password:
            if username == self.username:
                raise UserError("Change this password in backend/.env (ROOTIQ_AUTH_PASSWORD).")
            _check_strength(password)
        changes: dict[str, object] = {}
        if role is not None:
            changes["role"] = role
        if disabled is not None:
            changes["disabled"] = int(disabled)
        if password:
            changes.update(password_hash=hash_password(password), auth="password")
        if display_name is not None:
            changes["display_name"] = display_name.strip()
        if not changes:
            return
        with self._lock, self._database.connect() as conn:
            row = conn.execute("SELECT role FROM users WHERE username = ?", (username,)).fetchone()
            if row is None:
                raise UserError("No such user.")
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE users SET {sets} WHERE username = ?", (*changes.values(), username))
            if {"role", "disabled", "password_hash"} & changes.keys():
                conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
            if "password_hash" in changes:
                conn.execute("DELETE FROM login_failures WHERE key = ?", (f"user:{username.lower()}",))

    def remove_user(self, username: str) -> None:
        """Delete an account, its sessions and its settings. The audit log keeps the name in its records."""
        if username == self.username:
            raise UserError("The account in backend/.env cannot be removed.")
        with self._lock, self._database.connect() as conn:
            if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone() is None:
                raise UserError("No such user.")
            conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
            conn.execute("DELETE FROM user_preferences WHERE username = ?", (username,))
            conn.execute("DELETE FROM login_failures WHERE key = ?", (f"user:{username.lower()}",))
            conn.execute("DELETE FROM users WHERE username = ?", (username,))

    def change_own_password(self, username: str, current: str, new: str) -> None:
        """A wrong current password counts as a failed sign-in: a stolen session cannot guess the password."""
        keys = [(f"user:{username.strip().lower()}", self.max_failures)]
        self._check_lock(keys)
        with self._lock, self._database.connect() as conn:
            row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
        if row is None or not check_password(current, row["password_hash"]):
            self._note_failure(keys)
            raise UserError("The current password is not correct.")
        self.update_user(username, password=new)


def _check_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise UserError(f"A password has {MIN_PASSWORD} or more characters.")
    if password.lower() == password or password.upper() == password or not any(c.isdigit() for c in password):
        raise UserError("A password has upper-case and lower-case letters and a digit.")
