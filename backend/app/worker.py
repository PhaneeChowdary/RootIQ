"""A worker process for the agent team. Run as many as you need, on this computer or on other servers.

    .venv/bin/python -m backend.app.worker                # 2 worker threads
    .venv/bin/python -m backend.app.worker --threads 4

Each worker takes jobs from the queue in the database: agent investigations, approved actions, verification checks,
correlation, postmortems and lessons. Workers in more than one process share one queue safely. This needs PostgreSQL,
ROOTIQ_DATABASE_URL, or one computer with SQLite. For the API, set ROOTIQ_AGENT_CONCURRENCY=0 and
ROOTIQ_AGENT_EXTERNAL_WORKERS=true. Then the API does not run jobs itself, and does not take over the jobs of
running workers at its start.

On SIGTERM or Ctrl+C, the worker stops taking jobs and waits up to --grace seconds for its current jobs. A job that
does not finish in that time continues later on another worker, from its last checkpoint.
"""

import argparse
import signal
import sys
import threading


def main() -> int:
    parser = argparse.ArgumentParser(description="Run agent team workers.")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--grace", type=float, default=60.0, help="seconds to wait for current jobs at a stop")
    args = parser.parse_args()

    from . import migrations
    from .main import _count_todays_tokens, agent_runner, database, settings  # the same objects as the API

    migrations.upgrade(settings.database_target)
    database.initialize()
    from .agent.llm import use_database
    use_database(settings.database_target)  # the same token counts and model pauses as the API
    from .agent.llm import set_time_zone
    set_time_zone(settings.time_zone)
    recovered = agent_runner.recover(assume_alone=False)
    _count_todays_tokens()  # the daily token budgets of llm.py count from the start of the day
    agent_runner.start_workers(max(1, args.threads))
    target = str(settings.database_target).split("@")[-1]
    print(f"Worker {agent_runner.worker_id}: {args.threads} thread(s) on {target}. Recovered: {recovered}.", flush=True)

    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    done.wait()
    print("Stopping: no new jobs. Waiting for the current jobs...", flush=True)
    agent_runner.stop_workers(timeout=args.grace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
