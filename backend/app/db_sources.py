"""Read-only database access for the Evidence agent, on Settings > Data sources > Database.

Types:
- postgres: a PostgreSQL database of a service. Use a database user that can only read: GRANT SELECT, and
  pg_monitor for pg_stat_activity. The app adds its own limits on top.
- sqlite:   a SQLite file, for tests and demos

Safety, in this order. Each step alone stops a write:
1. The statement check: one statement only, which starts with SELECT, WITH, EXPLAIN or SHOW. No INSERT, UPDATE,
   DELETE, DDL, GRANT, COPY, SELECT ... INTO, FOR UPDATE or EXPLAIN ANALYZE, because EXPLAIN ANALYZE runs the
   statement. No dangerous functions such as pg_sleep, pg_terminate_backend, dblink or file access.
2. The app refuses the tables that can hold secrets or personal data: users, sessions, passwords, tokens, secrets,
   api_keys, credentials, and the tables in "Refused tables".
3. A read-only transaction, a statement time limit of 5 s and a lock wait limit of 1 s.
4. At most 100 rows, and at most 300 characters in each value.
Cloud models get the results after the privacy masking of privacy.py, as all text.
"""

import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal

ROW_LIMIT = 100
VALUE_CHARS = 300
STATEMENT_MS = 5000
DEFAULT_REFUSED = {"users", "user", "sessions", "session", "passwords", "password", "tokens", "token", "secrets", "secret",
                   "api_keys", "credentials", "auth_tokens", "refresh_tokens", "payment_methods", "cards"}
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|upsert|drop|alter|create|truncate|grant|revoke|copy|call|do|vacuum|reindex|cluster|"
    r"lock|listen|notify|prepare|execute|deallocate|discard|reset|set|begin|commit|rollback|savepoint|attach|detach|"
    r"pragma|load_extension|into|analyze)\b|\bfor\s+(update|share|no\s+key\s+update|key\s+share)\b", re.I)
FORBIDDEN_FUNCTIONS = re.compile(
    r"\b(pg_sleep\w*|pg_terminate_backend|pg_cancel_backend|pg_reload_conf|pg_rotate_logfile|pg_read_file|pg_read_binary_file|"
    r"pg_ls_dir|pg_stat_file|lo_\w+|dblink\w*|set_config|current_setting|pg_advisory\w*|txid_current|nextval|setval|"
    r"query_to_xml|xpath|copy_\w+)\s*\(", re.I)
STARTS = re.compile(r"^\s*(select|with|explain|show|values|table)\b", re.I)


class DatabaseError(RuntimeError):
    """A refused or failed query. The message says why."""


def check_sql(sql: str, refused: set[str]) -> str:
    """The statement, cleaned, or DatabaseError when it is not a safe read."""
    text = re.sub(r"--[^\n]*", " ", sql)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S).strip().rstrip(";").strip()
    if not text:
        raise DatabaseError("Give one SELECT statement.")
    without_strings = re.sub(r"'(?:[^']|'')*'", "''", text)
    if ";" in without_strings:
        raise DatabaseError("One statement only.")
    if not STARTS.match(without_strings):
        raise DatabaseError("Only SELECT, WITH, EXPLAIN and SHOW statements are allowed.")
    if found := FORBIDDEN.search(without_strings):
        raise DatabaseError(f"'{found.group(0).strip()}' is not allowed: the database tools only read.")
    if found := FORBIDDEN_FUNCTIONS.search(without_strings):
        raise DatabaseError(f"The function {found.group(1)} is not allowed.")
    names = {n.lower().split(".")[-1].strip('"') for n in re.findall(r"\b(?:from|join)\s+([\w.\"]+)", without_strings, re.I)}
    if blocked := sorted(names & refused):
        raise DatabaseError(f"The table {blocked[0]} can hold secrets or personal data. The agents do not read it.")
    return text


def _value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, memoryview)):
        return "<binary>"
    if isinstance(value, str) and len(value) > VALUE_CHARS:
        return value[:VALUE_CHARS] + "…"
    return value


class DatabaseSource:
    def __init__(self, kind: str, target: str, services: str = "*", refused: str = "") -> None:
        self.kind = kind
        self.target = target
        self.services = {s.strip() for s in (services or "*").split(",") if s.strip()} or {"*"}
        self.refused = DEFAULT_REFUSED | {t.strip().lower() for t in (refused or "").split(",") if t.strip()}
        self.name = "PostgreSQL" if kind == "postgres" else "SQLite"

    def covers(self, service: str) -> bool:
        return "*" in self.services or service in self.services

    def _need(self, service: str) -> None:
        if not self.covers(service):
            raise DatabaseError(f"No database for service '{service}'. Databases are set for: {', '.join(sorted(self.services))}.")

    def _run(self, sql: str, params: tuple = ()) -> dict:
        if self.kind == "postgres":
            import psycopg

            try:
                with psycopg.connect(self.target, connect_timeout=5, autocommit=False) as conn:
                    conn.read_only = True
                    conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_MS}")
                    conn.execute("SET LOCAL lock_timeout = 1000")
                    cursor = conn.execute(sql, params)
                    columns = [c.name for c in cursor.description or []]
                    rows = cursor.fetchmany(ROW_LIMIT + 1)
                    conn.rollback()
            except psycopg.Error as exc:
                raise DatabaseError(f"The database refused the query: {str(exc).splitlines()[0][:200]}") from exc
        else:
            try:
                conn = sqlite3.connect(f"file:{self.target}?mode=ro", uri=True, timeout=1)
                try:
                    cursor = conn.execute(sql, params)
                    columns = [c[0] for c in cursor.description or []]
                    rows = cursor.fetchmany(ROW_LIMIT + 1)
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                raise DatabaseError(f"The database refused the query: {exc}") from exc
        more = len(rows) > ROW_LIMIT
        return {"columns": columns, "rows": [[_value(v) for v in row] for row in rows[:ROW_LIMIT]],
                "note": f"Only the first {ROW_LIMIT} rows." if more else ""}

    def query(self, service: str, sql: str) -> dict:
        self._need(service)
        return self._run(check_sql(sql, self.refused))

    def schema(self, service: str, table: str = "") -> dict:
        self._need(service)
        if table and table.lower() in self.refused:
            raise DatabaseError(f"The table {table} can hold secrets or personal data. The agents do not read it.")
        if self.kind == "postgres":
            if table:
                return self._run("SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                                 "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') AND table_name = %s "
                                 "ORDER BY ordinal_position", (table,))
            out = self._run("SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema NOT IN "
                            "('pg_catalog', 'information_schema') ORDER BY 1, 2")
        else:
            if table:
                return self._run(f"SELECT name, type, \"notnull\" FROM pragma_table_info(?)", (table,))
            out = self._run("SELECT 'main', name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        out["rows"] = [r for r in out["rows"] if str(r[-1]).lower() not in self.refused]
        return out

    def activity(self, service: str) -> dict:
        """Connections by state and wait, the longest running statements, and the connection limit. PostgreSQL only."""
        self._need(service)
        if self.kind != "postgres":
            raise DatabaseError("Connection activity is for PostgreSQL only.")
        states = self._run("SELECT state, wait_event_type, wait_event, count(*) AS connections, "
                           "max(now() - state_change)::text AS longest_in_state FROM pg_stat_activity "
                           "WHERE datname = current_database() AND pid <> pg_backend_pid() GROUP BY 1, 2, 3 ORDER BY 4 DESC")
        longest = self._run("SELECT pid, state, (now() - query_start)::text AS running_for, wait_event, left(query, 160) AS query "
                            "FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() "
                            "AND state <> 'idle' ORDER BY query_start NULLS LAST LIMIT 10")
        limits = self._run("SELECT current_setting('max_connections')::int AS max_connections, "
                           "(SELECT count(*) FROM pg_stat_activity) AS connections_now")
        return {"by_state": states, "longest_running": longest, "limits": limits}

    def check(self) -> str:
        tables = self.schema(next(iter(self.services)) if "*" not in self.services else "*")["rows"]
        return f"{self.name}: {len(tables)} table(s) readable. Read-only checks are on."
