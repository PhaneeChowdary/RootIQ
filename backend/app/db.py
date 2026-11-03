"""One database layer for two databases.

- SQLite, the default: one file, backend/data/rootiq.db. Nothing to install. Good for one computer.
- PostgreSQL: set ROOTIQ_DATABASE_URL=postgresql://user:password@host:5432/rootiq. Use it on a server, with
  more than one API or worker process. The app keeps a pool of connections.

The code writes SQL one time, in the SQLite style, with "?" for each value. For PostgreSQL, this module changes "?"
to "%s". Rows work as in sqlite3: row["title"] and row[0] both work.

Rules for portable SQL in this project:
- Use INSERT ... ON CONFLICT ... DO UPDATE or DO NOTHING, not INSERT OR REPLACE.
- Use lower(x) LIKE lower(?) for a search that ignores case. LIKE ignores case in SQLite, but not in PostgreSQL.
- Keep times as ISO text in UTC, so that text order is time order in both databases."""

import atexit
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

try:
    import psycopg
    from psycopg_pool import ConnectionPool
except ImportError:  # SQLite only
    psycopg = None
    ConnectionPool = None

POSTGRES_PREFIXES = ("postgres://", "postgresql://")
IntegrityError: tuple = (sqlite3.IntegrityError,) + ((psycopg.IntegrityError,) if psycopg else ())


def is_postgres(target) -> bool:
    return str(target).startswith(POSTGRES_PREFIXES)


def safe_url(target) -> str:
    """The address without the user and the password, for messages and logs."""
    return str(target).split("@")[-1] if is_postgres(target) else str(target)


def hide_password(text: str, target) -> str:
    try:
        from urllib.parse import urlsplit

        password = urlsplit(str(target)).password
    except ValueError:
        password = None
    return text.replace(password, "***") if password else text


class Row(tuple):
    """A result row, by position such as row[0] or by column name such as row["id"], as sqlite3.Row."""

    def __new__(cls, names: list[str], values):
        row = super().__new__(cls, values)
        row._names = {name: i for i, name in enumerate(names)}
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            return super().__getitem__(self._names[key])
        return super().__getitem__(key)

    def keys(self) -> list[str]:
        return list(self._names)


def _pg_row_factory(cursor):
    names = [c.name for c in cursor.description or []]
    return lambda values: Row(names, values)


def to_postgres(sql: str) -> str:
    """Change "?" to "%s", and "%" to "%%", outside quoted text."""
    out, quote = [], None
    for ch in sql:
        if quote:
            out.append("%%" if ch == "%" else ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    return "".join(out)


class Connection:
    """The same small interface on both databases: execute(), columns() and the dialect name."""

    def __init__(self, raw, dialect: str) -> None:
        self.raw = raw
        self.dialect = dialect

    def execute(self, sql: str, params=()):
        if self.dialect == "postgres":
            return self.raw.execute(to_postgres(sql), tuple(params))
        return self.raw.execute(sql, tuple(params))

    def columns(self, table: str) -> set[str]:
        if self.dialect == "postgres":
            rows = self.execute("SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,))
            return {r[0] for r in rows.fetchall()}
        return {r["name"] for r in self.raw.execute(f"PRAGMA table_info({table})").fetchall()}


class Database:
    """A database target, a file path or a postgresql:// address, and its connections."""

    def __init__(self, target) -> None:
        self.target = target
        self.dialect = "postgres" if is_postgres(target) else "sqlite"
        self._pool = None
        self._memory = None  # ":memory:" keeps one connection, else every connection is a new empty database
        self._lock = threading.RLock()
        if self.dialect == "postgres" and psycopg is None:
            raise RuntimeError("PostgreSQL needs psycopg: .venv/bin/pip install 'psycopg[binary]' psycopg-pool")

    @contextmanager
    def connect(self):
        """One transaction: commit at the end, rollback on an error."""
        if self.dialect == "postgres":
            with self._get_pool().connection() as raw:  # the pool commits, or rolls back on an error
                raw.row_factory = _pg_row_factory
                yield Connection(raw, "postgres")
            return
        if str(self.target) == ":memory:":
            with self._lock:
                if self._memory is None:
                    self._memory = sqlite3.connect(":memory:", check_same_thread=False)
                    self._memory.row_factory = sqlite3.Row
                try:
                    yield Connection(self._memory, "sqlite")
                    self._memory.commit()
                except BaseException:
                    self._memory.rollback()
                    raise
            return
        Path(self.target).parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(self.target, timeout=15)
        raw.row_factory = sqlite3.Row
        try:
            yield Connection(raw, "sqlite")
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()

    def _get_pool(self):
        with self._lock:
            if self._pool is None:
                try:  # fail at once with the reason, not after the pool's 30-second wait
                    psycopg.connect(str(self.target), connect_timeout=10).close()
                except psycopg.Error as exc:
                    raise RuntimeError(f"Cannot connect to PostgreSQL at {safe_url(self.target)}: "
                                       f"{hide_password(str(exc), self.target)}") from None
                self._pool = ConnectionPool(str(self.target), min_size=1, max_size=10, open=True,
                                            kwargs={"autocommit": False})
            return self._pool

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None


_databases: dict[str, Database] = {}
_registry_lock = threading.Lock()


def get(target) -> Database:
    """The shared Database for a target, so all parts of the app use one pool."""
    key = str(target)
    with _registry_lock:
        if key not in _databases or key == ":memory:":
            database = Database(target)
            if key == ":memory:":
                return database  # in tests, each in-memory user gets its own database
            _databases[key] = database
        return _databases[key]


@atexit.register
def close_all() -> None:
    """Close the PostgreSQL pools when the process ends."""
    for database in list(_databases.values()):
        database.close()
