"""Stores the agent runs with their status, trace, findings, proposals and outcomes, and the lessons from them.

The LangGraph checkpointer holds the state of the graph for a resume. These tables hold what people look at.
"""
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .. import db, migrations
from ..models import AgentLesson, AgentRun

ACTIVE_STATUSES = ("queued", "running", "awaiting_approval", "executing")


@dataclass
class Job:
    id: str
    kind: str  # investigate, continue, resume, reflect, verify, postmortem or correlate
    target: str  # a run id, or a case id or key for the other kinds
    payload: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentRunStore:
    def __init__(self, path: Path | str) -> None:
        self.path = path
        self._db = db.get(path)
        self._lock = threading.Lock()

    def initialize(self) -> None:
        migrations.upgrade(self._db)

    def save(self, run: AgentRun) -> AgentRun:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_runs (id, case_id, status, created_at, updated_at, data) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at,
                                              data = excluded.data
                """,
                (run.id, run.case_id, run.status, run.created_at.isoformat(), run.updated_at.isoformat(),
                 run.model_dump_json()),
            )
        return run

    def get(self, run_id: str) -> AgentRun | None:
        with self._connect() as connection:
            row = connection.execute("SELECT data FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        return AgentRun.model_validate_json(row[0]) if row else None

    def list_for_case(self, case_id: str, limit: int = 20) -> list[AgentRun]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM agent_runs WHERE case_id = ? ORDER BY created_at DESC LIMIT ?", (case_id, limit)
            ).fetchall()
        return [AgentRun.model_validate_json(row[0]) for row in rows]

    def list_all(self, limit: int = 500) -> list[AgentRun]:
        with self._connect() as connection:
            rows = connection.execute("SELECT data FROM agent_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [AgentRun.model_validate_json(row[0]) for row in rows]

    def active_for_case(self, case_id: str) -> AgentRun | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT data FROM agent_runs WHERE case_id = ? AND status IN ({','.join('?' * len(ACTIVE_STATUSES))}) "
                "ORDER BY created_at DESC LIMIT 1",
                (case_id, *ACTIVE_STATUSES),
            ).fetchone()
        return AgentRun.model_validate_json(row[0]) if row else None

    def with_status(self, *statuses: str) -> list[AgentRun]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT data FROM agent_runs WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY created_at",
                statuses,
            ).fetchall()
        return [AgentRun.model_validate_json(row[0]) for row in rows]

    def started_since(self, case_id: str, since: datetime) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM agent_runs WHERE case_id = ? AND created_at >= ? LIMIT 1", (case_id, since.isoformat())
            ).fetchone()
        return row is not None

    def save_lesson(self, lesson: AgentLesson, vector: list[float]) -> AgentLesson:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_lessons (id, case_id, service, created_at, data, vector) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET id = excluded.id, created_at = excluded.created_at,
                                                   data = excluded.data, vector = excluded.vector
                """,
                (lesson.id, lesson.case_id, lesson.service, lesson.created_at.isoformat(),
                 lesson.model_dump_json(), json.dumps(vector)),
            )
        return lesson

    def lesson_for_case(self, case_id: str) -> AgentLesson | None:
        with self._connect() as connection:
            row = connection.execute("SELECT data FROM agent_lessons WHERE case_id = ?", (case_id,)).fetchone()
        return AgentLesson.model_validate_json(row[0]) if row else None

    def lessons(self) -> list[tuple[AgentLesson, list[float]]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT data, vector FROM agent_lessons ORDER BY created_at DESC").fetchall()
        return [(AgentLesson.model_validate_json(data), json.loads(vector)) for data, vector in rows]

    def delete_for_case(self, case_id: str) -> list[str]:
        """Delete the runs and the lesson of a case. Returns the ids of the deleted runs."""
        with self._connect() as connection:
            run_ids = [r[0] for r in connection.execute("SELECT id FROM agent_runs WHERE case_id = ?", (case_id,))]
            connection.execute("DELETE FROM agent_jobs WHERE target = ? OR target IN (SELECT id FROM agent_runs WHERE case_id = ?)",
                               (case_id, case_id))
            connection.execute("DELETE FROM agent_runs WHERE case_id = ?", (case_id,))
            connection.execute("DELETE FROM agent_lessons WHERE case_id = ?", (case_id,))
            connection.execute("DELETE FROM alert_cases WHERE case_id = ?", (case_id,))
        return run_ids

    # Job queue for all the worker processes. See migration 3.

    def enqueue_job(self, kind: str, target: str, payload: str = "", run_after: datetime | None = None) -> bool:
        """Queue a job. False when the target already has a job: one job for each target, see migration 5.
        run_after: no worker takes the job before that time. The verification checks use it."""
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO agent_jobs (id, kind, target, payload, status, created_at, run_after) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?) ON CONFLICT DO NOTHING",
                (str(uuid4()), kind, target, payload, _now(), run_after.isoformat() if run_after else None))
            return cursor.rowcount == 1

    def take_run(self, run_id: str) -> bool:
        """Move a run from queued to running. Only one worker can do it, so two workers never run the same run."""
        with self._connect() as connection:
            return connection.execute("UPDATE agent_runs SET status = 'running' WHERE id = ? AND status = 'queued'",
                                      (run_id,)).rowcount == 1

    def claim_job(self, worker: str) -> Job | None:
        """Take the next queued job. Short jobs go first: approved actions, correlation, verification, postmortem and
        lessons. They need one or two model calls, and a person often waits for them. Then the oldest job goes first.
        Two workers never get the same job: SQLite allows one writer at a time, and PostgreSQL skips the rows that another
        worker locked."""
        now = _now()
        with self._connect() as connection:
            lock = " FOR UPDATE SKIP LOCKED" if connection.dialect == "postgres" else ""
            row = connection.execute(
                f"""UPDATE agent_jobs SET status = 'running', claimed_by = ?, claimed_at = ?, heartbeat_at = ?
                    WHERE id = (SELECT id FROM agent_jobs WHERE status = 'queued' AND (run_after IS NULL OR run_after <= ?)
                                ORDER BY CASE WHEN kind IN ('investigate', 'continue') THEN 1 ELSE 0 END, created_at, id
                                LIMIT 1{lock})
                      AND status = 'queued'
                    RETURNING id, kind, target, payload""", (worker, now, now, now)).fetchone()
        return Job(row[0], row[1], row[2], row[3]) if row else None

    def finish_job(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM agent_jobs WHERE id = ?", (job_id,))

    def requeue_job(self, job_id: str, kind: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE agent_jobs SET status = 'queued', kind = ?, claimed_by = NULL, claimed_at = NULL, "
                               "heartbeat_at = NULL WHERE id = ?", (kind, job_id))

    def drop_queued_jobs(self, target: str) -> int:
        with self._connect() as connection:
            return connection.execute("DELETE FROM agent_jobs WHERE target = ? AND status = 'queued'", (target,)).rowcount

    def heartbeat(self, job_ids: list[str]) -> None:
        if job_ids:
            marks = ", ".join("?" for _ in job_ids)
            with self._connect() as connection:
                connection.execute(f"UPDATE agent_jobs SET heartbeat_at = ? WHERE id IN ({marks})", (_now(), *job_ids))

    def has_job(self, target: str) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT 1 FROM agent_jobs WHERE target = ?", (target,)).fetchone() is not None

    def running_jobs(self, stale_before: str | None = None) -> list[Job]:
        """Jobs that a worker has taken. With stale_before: only jobs with no heartbeat since that time."""
        sql = "SELECT id, kind, target, payload FROM agent_jobs WHERE status = 'running'"
        params: tuple = ()
        if stale_before:
            sql += " AND (heartbeat_at IS NULL OR heartbeat_at < ?)"
            params = (stale_before,)
        with self._connect() as connection:
            return [Job(*row) for row in connection.execute(sql, params).fetchall()]

    def queue_position(self, run_id: str) -> int | None:
        with self._connect() as connection:
            row = connection.execute("SELECT created_at, id FROM agent_jobs WHERE target = ? AND status = 'queued' "
                                     "AND kind IN ('investigate', 'continue')", (run_id,)).fetchone()
            if row is None:
                return None
            ahead = connection.execute("SELECT COUNT(*) FROM agent_jobs WHERE status = 'queued' AND kind IN "
                                       "('investigate', 'continue') AND (created_at < ? OR (created_at = ? AND id < ?))",
                                       (row[0], row[0], row[1])).fetchone()[0]
        return ahead + 1

    # Ratings of the diagnoses, for the continuous evaluation

    def rate(self, run_id: str, case_id: str, username: str, verdict: str, comment: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO run_ratings (run_id, username, case_id, verdict, comment, created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (run_id, username) DO UPDATE SET verdict = excluded.verdict, comment = excluded.comment, "
                "created_at = excluded.created_at", (run_id, username, case_id, verdict, comment.strip(), _now()))

    def ratings_for_run(self, run_id: str) -> dict:
        with self._connect() as connection:
            rows = connection.execute("SELECT username, verdict, comment, created_at FROM run_ratings WHERE run_id = ? "
                                      "ORDER BY created_at", (run_id,)).fetchall()
        return {"ratings": [{"by": r[0], "verdict": r[1], "comment": r[2], "at": r[3]} for r in rows]}

    def rating_summary(self, days: int) -> dict:
        """How the engineers rated the diagnoses: the counts, a score, and each week. Correct is 1, partly 0.5, wrong 0."""
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._connect() as connection:
            rows = connection.execute("SELECT verdict, created_at FROM run_ratings WHERE created_at >= ? ORDER BY created_at",
                                      (since,)).fetchall()
        points = {"correct": 1.0, "partly_correct": 0.5, "wrong": 0.0}
        counts = {k: sum(1 for r in rows if r[0] == k) for k in points}
        weeks: dict[str, list[float]] = {}
        for verdict, at in rows:
            day = datetime.fromisoformat(at)
            week = (day - timedelta(days=day.weekday())).date().isoformat()
            weeks.setdefault(week, []).append(points[verdict])
        return {"days": days, "count": len(rows), "counts": counts,
                "score": round(sum(points[r[0]] for r in rows) / len(rows), 3) if rows else None,
                "weeks": [{"week": w, "score": round(sum(v) / len(v), 3), "count": len(v)} for w, v in sorted(weeks.items())]}

    # Worker presence

    def worker_alive(self, worker_id: str, host: str, threads: int, jobs: int, started_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO agent_workers (id, host, threads, started_at, heartbeat_at, jobs) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at, jobs = excluded.jobs,
                                                  threads = excluded.threads""",
                (worker_id, host, threads, started_at, _now(), jobs))

    def worker_gone(self, worker_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM agent_workers WHERE id = ?", (worker_id,))

    def workers(self, seen_after: str) -> list[dict]:
        """Worker processes with a heartbeat after the given time. Older rows are deleted."""
        with self._connect() as connection:
            connection.execute("DELETE FROM agent_workers WHERE heartbeat_at < ?", (seen_after,))
            rows = connection.execute("SELECT id, host, threads, started_at, heartbeat_at, jobs FROM agent_workers "
                                      "ORDER BY started_at").fetchall()
        return [{"id": r[0], "host": r[1], "threads": r[2], "started_at": r[3], "heartbeat_at": r[4], "jobs": r[5]} for r in rows]

    # Stop requests: any process can ask; the worker that runs the investigation stops at its next step

    def request_cancel(self, run_id: str, status: str, message: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE agent_runs SET cancel_status = ?, cancel_message = ? WHERE id = ?", (status, message, run_id))

    def cancel_request(self, run_id: str) -> tuple[str, str] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT cancel_status, cancel_message FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        return (row[0], row[1]) if row and row[0] else None

    def clear_cancel(self, run_id: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE agent_runs SET cancel_status = NULL, cancel_message = NULL WHERE id = ?", (run_id,))

    @contextmanager
    def _connect(self):
        with self._db.connect() as connection:
            yield connection
