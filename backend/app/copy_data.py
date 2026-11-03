"""Copy all data from one database to another, for example from the SQLite file to PostgreSQL.

    .venv/bin/python -m backend.app.copy_data --to postgresql://user:password@host:5432/rootiq
    .venv/bin/python -m backend.app.copy_data --from backend/data/rootiq.db --to postgresql://...

The target first gets the newest tables from the migrations. The app keeps the rows that the target already has and
does not overwrite them, so you can run the command again. The app does not copy agent checkpoints: the runs that
wait for approval keep their proposals, but cannot continue. Start a new run for them.
"""

import argparse
import sys

from . import db, migrations

TABLES = ["support_cases", "case_memory", "audit_events", "case_messages", "user_preferences", "agent_runs",
          "agent_lessons", "alert_cases", "users", "sessions", "login_failures", "agent_jobs", "remediation_log",
          "postmortems", "run_ratings"]


def copy(source, target) -> dict[str, int]:
    migrations.upgrade(source)
    migrations.upgrade(target)
    src, dst = db.get(source), db.get(target)
    counts = {}
    for table in TABLES:
        with src.connect() as connection:
            cursor = connection.execute(f"SELECT * FROM {table}")
            names = [d[0] for d in cursor.description]
            rows = [tuple(r) for r in cursor.fetchall()]
        with dst.connect() as connection:
            columns = [n for n in names if n in connection.columns(table)]
            marks = ", ".join("?" for _ in columns)
            sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks}) ON CONFLICT DO NOTHING"
            index = [names.index(c) for c in columns]
            for row in rows:
                connection.execute(sql, tuple(row[i] for i in index))
        counts[table] = len(rows)
    return counts


def main() -> int:
    from .config import Settings

    parser = argparse.ArgumentParser(description="Copy RootIQ data between databases.")
    parser.add_argument("--from", dest="source", default=str(Settings().database_path))
    parser.add_argument("--to", dest="target", required=True)
    args = parser.parse_args()
    if str(args.source) == str(args.target):
        print("The source and the target are the same.")
        return 1
    counts = copy(args.source, args.target)
    for table, n in counts.items():
        print(f"  {table:18} {n} row(s)")
    print(f"Copied to {args.target.split('@')[-1]}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
