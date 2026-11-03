"""Versioned database changes.

Each change has a number. The table schema_migrations records the changes that a database has. At start, the app
applies the missing changes in number order, each in its own transaction. Two processes that start at the same time
do not apply a change two times: PostgreSQL uses an advisory lock, and SQLite allows one writer at a time.

    .venv/bin/python -m backend.app.migrations            # show the version of the database
    .venv/bin/python -m backend.app.migrations upgrade    # apply the missing changes

To change the schema, add a function at the end of MIGRATIONS with the next number. Do not change a migration that
is already released: a database that has it does not run it again.

Migration 1 makes the tables of the versions before migrations. It is safe on a database of an earlier version: it
adds only what is missing.
"""

import sys
from datetime import UTC, datetime

from . import db

LOCK_ID = 7_301_204  # PostgreSQL advisory lock for migrations


def _add_missing(connection, table: str, columns: dict[str, str]) -> None:
    present = connection.columns(table)
    for name, sql_type in columns.items():
        if name not in present:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def m001_base(connection) -> None:
    """Cases, knowledge base, audit log, messages, preferences, agent runs, lessons, alert links."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS support_cases (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, customer TEXT NOT NULL, service TEXT NOT NULL,
            environment TEXT NOT NULL, reported_issue TEXT NOT NULL, logs TEXT NOT NULL, status TEXT NOT NULL,
            severity TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, response_due_at TEXT,
            resolution_due_at TEXT, first_response_at TEXT, analysis TEXT NOT NULL, review_status TEXT NOT NULL,
            approved_at TEXT, resolution TEXT NOT NULL, preventive_actions TEXT NOT NULL,
            post_incident_report TEXT NOT NULL DEFAULT '{}', jira_issue_key TEXT, jira_issue_url TEXT,
            jira_published_at TEXT, gmail_draft_id TEXT, gmail_draft_created_at TEXT, runbook TEXT,
            timeline TEXT NOT NULL, jira_source_key TEXT, deleted_at TEXT, deleted_by TEXT)""")
    _add_missing(connection, "support_cases", {
        "post_incident_report": "TEXT NOT NULL DEFAULT '{}'", "jira_issue_key": "TEXT", "jira_issue_url": "TEXT",
        "jira_published_at": "TEXT", "gmail_draft_id": "TEXT", "gmail_draft_created_at": "TEXT",
        "jira_source_key": "TEXT", "runbook": "TEXT", "response_due_at": "TEXT", "resolution_due_at": "TEXT",
        "first_response_at": "TEXT", "deleted_at": "TEXT", "deleted_by": "TEXT"})
    connection.execute("UPDATE support_cases SET status = 'approved' WHERE status = 'awaiting_approval' AND review_status = 'approved'")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS case_memory (
            case_id TEXT PRIMARY KEY REFERENCES support_cases(id), content TEXT NOT NULL, vector TEXT NOT NULL,
            embedded_at TEXT NOT NULL)""")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS audit_events (
            id TEXT PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
            resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, detail TEXT NOT NULL)""")
    connection.execute("CREATE INDEX IF NOT EXISTS audit_events_at ON audit_events(at)")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS case_messages (
            id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES support_cases(id), author TEXT NOT NULL,
            content TEXT NOT NULL, message_type TEXT NOT NULL DEFAULT 'internal', created_at TEXT NOT NULL,
            jira_comment_id TEXT)""")
    _add_missing(connection, "case_messages", {"jira_comment_id": "TEXT"})
    connection.execute("CREATE INDEX IF NOT EXISTS case_messages_case ON case_messages(case_id, created_at)")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            username TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (username, key))""")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY, case_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, data TEXT NOT NULL)""")
    connection.execute("CREATE INDEX IF NOT EXISTS agent_runs_case ON agent_runs(case_id, created_at)")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_lessons (
            id TEXT PRIMARY KEY, case_id TEXT NOT NULL UNIQUE, service TEXT NOT NULL, created_at TEXT NOT NULL,
            data TEXT NOT NULL, vector TEXT NOT NULL)""")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS alert_cases (
            fingerprint TEXT PRIMARY KEY, case_id TEXT NOT NULL, alert_name TEXT NOT NULL,
            service TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL)""")


def m002_users(connection) -> None:
    """Users with roles, sessions as token hashes, and sign-in limits."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL, password_hash TEXT NOT NULL DEFAULT '', auth TEXT NOT NULL DEFAULT 'password',
            disabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_login TEXT)""")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email ON users(email) WHERE email != ''")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL)""")
    connection.execute("CREATE INDEX IF NOT EXISTS sessions_user ON sessions(username)")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS login_failures (
            key TEXT PRIMARY KEY, failures INTEGER NOT NULL, first_at TEXT NOT NULL, locked_until TEXT)""")


def m003_job_queue(connection) -> None:
    """The agent job queue in the database, so several worker processes can share it, and stop requests."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_jobs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, created_at TEXT NOT NULL, claimed_by TEXT, claimed_at TEXT, heartbeat_at TEXT)""")
    connection.execute("CREATE INDEX IF NOT EXISTS agent_jobs_status ON agent_jobs(status, created_at)")
    _add_missing(connection, "agent_runs", {"cancel_status": "TEXT", "cancel_message": "TEXT"})


def m004_workers(connection) -> None:
    """Worker processes report that they are alive, so the Settings page can show them."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_workers (
            id TEXT PRIMARY KEY, host TEXT NOT NULL, threads INTEGER NOT NULL, started_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL, jobs INTEGER NOT NULL DEFAULT 0)""")


def m005_one_job_per_target(connection) -> None:
    """At most one job for each run or case. Two processes that queue the same run at the same time get one job."""
    connection.execute("""
        DELETE FROM agent_jobs WHERE EXISTS (
            SELECT 1 FROM agent_jobs b WHERE b.target = agent_jobs.target
              AND (b.created_at < agent_jobs.created_at OR (b.created_at = agent_jobs.created_at AND b.id < agent_jobs.id)))""")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS agent_jobs_target ON agent_jobs(target)")


def m006_remediation(connection) -> None:
    """Remediations, each of which runs at most one time, and jobs that wait until a time, for verification checks."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS remediation_log (
            key TEXT PRIMARY KEY, case_id TEXT NOT NULL, run_id TEXT NOT NULL, action TEXT NOT NULL,
            service TEXT NOT NULL, params TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
            approved_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, finished_at TEXT)""")
    connection.execute("CREATE INDEX IF NOT EXISTS remediation_log_case ON remediation_log(case_id, created_at)")
    _add_missing(connection, "agent_jobs", {"run_after": "TEXT"})


def m007_postmortems_and_ratings(connection) -> None:
    """Postmortems written by the postmortem agent, and the engineers' ratings of each diagnosis."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS postmortems (
            case_id TEXT PRIMARY KEY, data TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS run_ratings (
            run_id TEXT NOT NULL, username TEXT NOT NULL, case_id TEXT NOT NULL, verdict TEXT NOT NULL,
            comment TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, PRIMARY KEY (run_id, username))""")


def m008_incident_groups(connection) -> None:
    """Groups of cases with one root cause, see correlation.py, and the pairs that a person said are not related."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS incident_groups (
            id TEXT PRIMARY KEY, primary_case_id TEXT NOT NULL, cause TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS incident_links (
            case_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '[]', confidence TEXT NOT NULL DEFAULT 'medium',
            linked_at TEXT NOT NULL, linked_by TEXT NOT NULL)""")
    connection.execute("CREATE INDEX IF NOT EXISTS incident_links_group ON incident_links(group_id)")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS incident_unrelated (
            case_a TEXT NOT NULL, case_b TEXT NOT NULL, decided_by TEXT NOT NULL, decided_at TEXT NOT NULL,
            PRIMARY KEY (case_a, case_b))""")


def m009_model_usage_and_jira_ignored(connection) -> None:
    """Token use and pauses of the models, shared by all processes and kept after a restart, and the Jira tickets
    whose cases were deleted forever, so that a sync does not import them again."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS model_usage (
            day TEXT NOT NULL, model TEXT NOT NULL, tokens INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, model))""")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS model_pauses (key TEXT PRIMARY KEY, until REAL NOT NULL, reason TEXT NOT NULL)""")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS jira_ignored (jira_key TEXT PRIMARY KEY, ignored_at TEXT NOT NULL, reason TEXT NOT NULL)""")


def m010_drop_orphan_runs(connection) -> None:
    """Agent runs of cases that no longer exist. Older versions deleted a case forever without its runs; those runs
    still counted on Insights and in Grafana. Delete forever now removes the runs with the case."""
    gone = "case_id NOT IN (SELECT id FROM support_cases)"
    connection.execute(f"DELETE FROM run_ratings WHERE run_id IN (SELECT id FROM agent_runs WHERE {gone})")
    connection.execute(f"DELETE FROM agent_jobs WHERE target IN (SELECT id FROM agent_runs WHERE {gone})")
    connection.execute(f"DELETE FROM agent_runs WHERE {gone}")
    connection.execute(f"DELETE FROM agent_lessons WHERE {gone}")


MIGRATIONS = [
    (1, "Base tables", m001_base),
    (2, "Users, sessions and sign-in limits", m002_users),
    (3, "Agent job queue and stop requests", m003_job_queue),
    (4, "Worker presence", m004_workers),
    (5, "One job for each run", m005_one_job_per_target),
    (6, "Remediation log and delayed jobs", m006_remediation),
    (7, "Postmortems and diagnosis ratings", m007_postmortems_and_ratings),
    (8, "Incident groups", m008_incident_groups),
    (9, "Model usage, model pauses and ignored Jira tickets", m009_model_usage_and_jira_ignored),
    (10, "Remove agent runs of deleted cases", m010_drop_orphan_runs),
]
LATEST = MIGRATIONS[-1][0]
_done: set[str] = set()  # targets already at the latest version in this process


def applied(database: db.Database) -> list[int]:
    with database.connect() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
        return [r[0] for r in connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()]


def upgrade(target) -> list[int]:
    """Apply the missing migrations. Returns the versions applied now."""
    database = target if isinstance(target, db.Database) else db.get(target)
    key = str(database.target)
    if key in _done and key != ":memory:":
        return []
    done_now = []
    for version, name, change in MIGRATIONS:
        with database.connect() as connection:
            if connection.dialect == "postgres":
                connection.execute("SELECT pg_advisory_xact_lock(?)", (LOCK_ID,))
            connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
            if connection.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone():
                continue
            change(connection)
            connection.execute("INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                               (version, name, datetime.now(UTC).isoformat()))
            done_now.append(version)
    _done.add(key)
    return done_now


def main(argv: list[str]) -> int:
    from .config import Settings

    settings = Settings()
    target = settings.database_target
    shown = str(target).split("@")[-1] if db.is_postgres(target) else str(target)  # never print a password
    if argv[:1] == ["upgrade"]:
        now = upgrade(target)
        print(f"{shown}: applied {now or 'nothing'}; version {LATEST}.")
        return 0
    have = applied(db.get(target))
    print(f"{shown}: version {max(have, default=0)} of {LATEST}.")
    for version, name, _ in MIGRATIONS:
        print(f"  {'done   ' if version in have else 'missing'} {version}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
