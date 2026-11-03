"""Runs the investigation team for the API: queued, event-driven, durable, and paused for human approval.

    start()        makes a queued run: manual, case created, Jira import or SLA risk.     Any thread.
    workers        take jobs from the queue one at a time and run the team.               Worker threads.
    decide()       checks the choices of the engineer; enqueue_resume() queues actions.   API thread.
    resume()       continues from the checkpoint and carries out the approved actions.    Worker threads.
    check_sla()    starts runs for open cases with an SLA at risk that nobody works on.   SLA watcher.
    reflect()      after resolution, compares the diagnosis with the real fix and stores  Worker threads.
                   a lesson. The lead agent reads the related lessons on later runs.

The queue is a table in the database, agent_jobs. Thus the workers can be in this process or in other processes:
python -m backend.app.worker. A worker marks its jobs with a heartbeat every 15 seconds. A job without a heartbeat
for 90 seconds belongs to a worker that stopped: recover() gives it to another worker. A stop request is a field of
the run. Thus the API can stop a run that a worker in another process runs.

Each run is a LangGraph thread in a checkpointer, SQLite or PostgreSQL. The thread id is the run id. After a restart,
the queued jobs are still in the queue, interrupted investigations continue from their last checkpoint, and runs that
wait for approval wait as before. The app never does approved actions again: an action job that was running when its
worker stopped fails, and the engineer checks Gmail and Jira."""
import json
import os
import socket
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .. import models
from ..models import (
    ActionDecision,
    ApprovalVote,
    ProposedAction,
    AgentLesson,
    AgentMetrics,
    AgentRun,
    AgentSpan,
    AgentStep,
    LessonDraft,
    LLMCall,
    RunTrigger,
    SupportCase,
)
from ..services.memory import EmbeddingUnavailable, cosine_similarity, text_similarity
from .executor import ActionExecutor
from .followup import FollowUpMixin
from .guards import Deadline
from .store import AgentRunStore
from ..sources import active_sources
from .llm import make_chat, model_label
from .team import MAX_REVISIONS, build_team, initial_state
from .telemetry import UsageRecorder, compute_metrics
from .tools import InvestigationToolbox

CHECKPOINT_TYPES = ("InvestigationPlan", "ReviewVerdict", "SupportAnalysis", "ProposedAction",
                    "ActionDecision", "ActionOutcome")
SLA_TRIGGER_STATUSES = ("new", "investigating")  # nobody has a reviewed analysis yet
MAX_LESSONS = 3
LESSON_MIN_SCORE = 0.55
# A lesson from another service must match much better: general incident words give. "server error", "fails".
# unrelated lessons about 0.6, and such a lesson sends the lead after the wrong cause.
LESSON_MIN_SCORE_OTHER_SERVICE = 0.70
SAME_SERVICE_BONUS = 0.15

REFLECT_PROMPT = """You review how an AI investigation team did on a production incident that is now resolved.
Compare the team's likely cause with the confirmed root cause and resolution recorded by the engineer.
verdict: correct only if the team named the same specific mechanism as the confirmed root cause (the thing the
fix changed). partly_correct if it found the right symptom, component or area but not that mechanism, or listed
it only as one of several possibilities. wrong if it pointed somewhere else. Describing the symptom (an error code,
a timeout) is not the cause. lesson: one or two sentences telling future investigations of similar incidents
what to check or what not to assume. Be specific (components, log fields, signals)."""


FOLLOW_UP_PREFIX = "Follow-up: "  # a note for the lead agent in a run that starts after a fix did not work
POLL_SECONDS = 2  # an idle worker checks the queue this often; a job from its own process starts at once
HEARTBEAT_SECONDS = 15
STALE_SECONDS = 90  # a job without a heartbeat for this long belongs to a worker that stopped

class RunConflict(RuntimeError):
    pass


class RunNotFound(LookupError):
    pass


class RunCancelled(RuntimeError):
    """Raised between two steps when a run must end: the case was deleted, or a person stopped it."""

    def __init__(self, message: str, status: str = "failed") -> None:
        super().__init__(message)
        self.status = status


class _Recorder:
    """Applies progress to one run record from several threads and saves it after every change."""

    def __init__(self, store: AgentRunStore, run: AgentRun) -> None:
        self.store = store
        self.run = run
        self.lock = threading.Lock()

    def update(self, **changes) -> None:
        with self.lock:
            self.run = self.run.model_copy(update={**changes, "updated_at": datetime.now(UTC)})
            self.store.save(self.run)

    def add_llm_call(self, call: LLMCall) -> None:
        with self.lock:
            self.run = self.run.model_copy(update={"llm_calls": [*self.run.llm_calls, call], "updated_at": datetime.now(UTC)})
            self.store.save(self.run)

    def add_span(self, span: AgentSpan) -> None:
        with self.lock:
            self.run = self.run.model_copy(update={"spans": [*self.run.spans, span], "updated_at": datetime.now(UTC)})
            self.store.save(self.run)

    def add_step(self, step: AgentStep) -> None:
        with self.lock:
            numbered = step.model_copy(update={"index": len(self.run.steps) + 1})  # the index stays unique after a resume
            self.run = self.run.model_copy(update={"steps": [*self.run.steps, numbered], "updated_at": datetime.now(UTC)})
            self.store.save(self.run)


class AgentRunner(FollowUpMixin):
    def __init__(self, settings, database, embedder, jira, gmail, store: AgentRunStore,
                 llm_factory: Callable[[], object] | None = None) -> None:
        self.settings = settings
        self.database = database
        self.embedder = embedder
        self.jira = jira
        self.gmail = gmail
        self.store = store
        self.llm_factory = llm_factory or (lambda: make_chat(settings))
        self.serde = JsonPlusSerializer(
            allowed_msgpack_modules=[(models.__name__, name) for name in CHECKPOINT_TYPES]
        )
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:6]}"
        self._workers: list[threading.Thread] = []
        self._wake = threading.Event()  # set when this process queues a job, so its workers start at once
        self._stopping = threading.Event()
        self._held: set[str] = set()  # the jobs that the workers of this process run now, for the heartbeat
        self._held_lock = threading.Lock()
        self._pg_ready = False

    # Starting runs

    def start(self, case_id: str, actor: str, trigger: RunTrigger = "manual", replacing: bool = False,
              notes: list[str] | None = None) -> AgentRun:
        """notes: facts for the lead agent in follow-up runs, for example "the remediation X did not fix it"."""
        if self.database.get_case(case_id) is None:
            raise RunNotFound("Case not found.")
        active = self.store.active_for_case(case_id)
        if active and not (replacing and self.store.cancel_request(active.id)):
            raise RunConflict(f"An agent run is already {active.status.replace('_', ' ')} for this case.")
        now = datetime.now(UTC)
        run = AgentRun(
            id=str(uuid4()), case_id=case_id, model=model_label(self.settings), status="queued", stage="Queued",
            trigger=trigger, started_by=actor, created_at=now, updated_at=now, dry_run=self.settings.agent_dry_run,
            lessons_used=list(notes or []),
        )
        self.store.save(run)
        self.database.record_audit(actor, "agent.run_queued", "case", case_id,
                                   f"Agent team run {run.id[:8]} queued ({trigger.replace('_', ' ')}).")
        self._enqueue("investigate", run.id)
        return self.with_position(run)

    def start_if_idle(self, case_id: str, actor: str, trigger: RunTrigger) -> AgentRun | None:
        """For automatic triggers: a case that already has a run in progress is left alone."""
        try:
            return self.start(case_id, actor, trigger)
        except RunConflict:
            return None

    def check_sla(self, now: datetime | None = None) -> list[AgentRun]:
        now = now or datetime.now(UTC)
        cooldown = now - timedelta(minutes=self.settings.agent_sla_cooldown_minutes)
        started = []
        for case in self.database.list_cases():
            if case.status not in SLA_TRIGGER_STATUSES or case.sla_state not in ("at_risk", "breached"):
                continue
            if self.store.started_since(case.id, cooldown):
                continue
            if run := self.start_if_idle(case.id, "rootiq-agent", "sla_risk"):
                started.append(run)
        return started

    def forget_case(self, case_id: str) -> int:
        """For delete forever: remove every run of the case, its checkpoints and its lesson. Returns the run count."""
        self.cancel_for_case(case_id, "Cancelled: the case was deleted forever.")
        run_ids = self.store.delete_for_case(case_id)
        if run_ids:
            with self._checkpointer() as saver:
                for run_id in run_ids:
                    saver.delete_thread(run_id)
        return len(run_ids)

    def cancel_for_case(self, case_id: str, reason: str) -> int:
        """Stops every unfinished run of a case: queued and paused runs at once, a running one at its next step."""
        cancelled = 0
        for run in self.store.list_for_case(case_id, limit=1000):
            if run.status in ("queued", "awaiting_approval"):
                self.store.drop_queued_jobs(run.id)
                self.store.save(run.model_copy(update={"status": "failed", "stage": "", "error": reason,
                                                       "queue_position": None, "updated_at": datetime.now(UTC)}))
                cancelled += 1
            elif run.status == "running":
                self.store.request_cancel(run.id, "failed", "Cancelled: the case was deleted while the team was investigating.")
                cancelled += 1
        return cancelled

    def stop(self, run_id: str, actor: str) -> AgentRun:
        """A person stops a run. A queued or waiting run stops at once. A running run stops after its current step: the
        work so far stays in the run. A run that carries out approved actions cannot stop, so that no action is half sent."""
        run = self._get(run_id)
        if run.status == "executing":
            raise RunConflict("The approved actions are being carried out. Wait for them to finish.")
        if run.status not in ("queued", "running", "awaiting_approval"):
            raise RunConflict(f"The run is already {run.status}.")
        message = f"Stopped by {actor}."
        if run.status == "running":
            self.store.request_cancel(run.id, "stopped", message)
        else:
            self.store.drop_queued_jobs(run.id)
            run = run.model_copy(update={"status": "stopped", "stage": "", "error": message, "queue_position": None,
                                         "updated_at": datetime.now(UTC)})
            self.store.save(run)
        self.database.record_audit(actor, "agent.stopped", "case", run.case_id, f"Agent team run {run.id[:8]} stopped.")
        return self.present(run)

    def restart(self, case_id: str, actor: str) -> AgentRun:
        """Stop the current run of the case, if there is one, and start a new run. The new run waits in the queue
        while the old run finishes its current step."""
        if self.database.get_case(case_id) is None:
            raise RunNotFound("Case not found.")
        if active := self.store.active_for_case(case_id):
            if not self.store.cancel_request(active.id):
                self.stop(active.id, actor)
        return self.start(case_id, actor, replacing=True)

    def schedule_reflection(self, case_id: str) -> None:
        self._enqueue("reflect", case_id)

    def schedule_correlation(self, case_id: str) -> None:
        """The correlation agent compares the case with the other open cases. See agent/correlation_agent.py."""
        self._enqueue("correlate", f"correlate:{case_id}")

    def enqueue_resume(self, run_id: str, decisions: list[ActionDecision]) -> None:
        """After decide(): a worker continues the run and carries out the approved actions. The investigation job
        of the run can still be in the queue table for a moment after the run paused: wait for it to go."""
        payload = json.dumps([d.model_dump() for d in decisions])
        for _ in range(40):
            if self._enqueue("resume", run_id, payload):
                return
            time.sleep(0.25)
        raise RunConflict("The run is still busy. Try again in a moment.")

    # Queue

    def _enqueue(self, kind: str, target: str, payload: str = "") -> bool:
        added = self.store.enqueue_job(kind, target, payload)
        self._wake.set()
        return added

    def present(self, run: AgentRun) -> AgentRun:
        """The run as the API shows it: its place in the queue, or "Stopping" while it finishes its current step."""
        if run.status == "running" and (self.store.cancel_request(run.id) or ("",))[0] == "stopped":
            return run.model_copy(update={"stage": "Stopping after the current step"})
        return self.with_position(run)

    def with_position(self, run: AgentRun) -> AgentRun:
        if run.status != "queued":
            return run
        return run.model_copy(update={"queue_position": self.store.queue_position(run.id)})

    def start_workers(self, count: int | None = None) -> None:
        """Start the worker threads and the heartbeat. With 0 threads, call process_queue() instead."""
        count = self.settings.agent_concurrency if count is None else count
        self._stopping.clear()
        for n in range(count):
            worker = threading.Thread(target=self._work, name=f"agent-worker-{n}", daemon=True)
            worker.start()
            self._workers.append(worker)
        if count:
            self._threads = count
            self._started_at = datetime.now(UTC).isoformat()
            with suppress(Exception):
                self.store.worker_alive(self.worker_id, socket.gethostname(), count, 0, self._started_at)
            beat = threading.Thread(target=self._heartbeat, name="agent-heartbeat", daemon=True)
            beat.start()
            self._workers.append(beat)

    def stop_workers(self, timeout: float = 2.0) -> None:
        self._stopping.set()
        self._wake.set()
        for worker in self._workers:
            worker.join(timeout)
        if self._workers:
            with suppress(Exception):
                self.store.worker_gone(self.worker_id)
        self._workers = []

    def process_queue(self) -> None:
        """Run each queued job on the calling thread, for tests and scripts."""
        while (job := self.store.claim_job(self.worker_id)) is not None:
            self._run_job(job)

    def _work(self) -> None:
        while not self._stopping.is_set():
            try:
                job = self.store.claim_job(self.worker_id)
            except Exception:  # the database is not reachable: try again soon
                job = None
            if job is None:
                self._wake.wait(POLL_SECONDS)
                self._wake.clear()
                continue
            self._run_job(job)

    def _heartbeat(self) -> None:
        """Every 15 s: mark this process's jobs as alive. Every 30 s: give the jobs of stopped workers to others."""
        ticks = 0
        while not self._stopping.wait(HEARTBEAT_SECONDS):
            ticks += 1
            with suppress(Exception):
                with self._held_lock:
                    held = list(self._held)
                self.store.heartbeat(held)
                self.store.worker_alive(self.worker_id, socket.gethostname(), self._threads, len(held), self._started_at)
                if ticks % 2 == 0:
                    self.recover(assume_alone=False)

    def _run_job(self, job) -> None:
        with self._held_lock:
            self._held.add(job.id)
        try:
            self._process((job.kind, job.target, job.payload))
        finally:
            with self._held_lock:
                self._held.discard(job.id)
            self.store.finish_job(job.id)

    def _process(self, job: tuple[str, str, str]) -> None:
        kind, target, payload = job
        try:  # whatever happens to one job, the worker keeps serving the queue
            if kind == "reflect":
                self.reflect(target)
            elif kind == "resume":
                self.resume(target, [ActionDecision.model_validate(d) for d in json.loads(payload or "[]")])
            elif kind == "verify":
                self.verify(json.loads(payload))
            elif kind == "postmortem":
                self.write_postmortem(target.removeprefix("postmortem:"))
            elif kind == "correlate":
                from .correlation_agent import correlate

                correlate(self, target.removeprefix("correlate:"))
            else:
                self.investigate(target, fresh=(kind == "investigate"))
        except Exception as exc:
            action = "agent.lesson_failed" if kind == "reflect" else "agent.job_failed"
            self.database.record_audit("rootiq-agent", action, "agent", target, str(exc))

    # Calls from the API

    def decide(self, run_id: str, decisions: list[ActionDecision], actor: str) -> AgentRun:
        run = self._get(run_id)
        if run.status != "awaiting_approval":
            raise RunConflict(f"Run is {run.status.replace('_', ' ')}, not awaiting approval.")
        if any(d.index >= len(run.proposals) for d in decisions):
            raise ValueError("A decision refers to a proposal that does not exist.")
        if len({d.index for d in decisions}) != len(decisions):
            raise ValueError("Each proposal can have only one decision.")
        now = datetime.now(UTC)
        votes = list(run.approvals)
        for d in decisions:
            votes = [v for v in votes if not (v.index == d.index and v.by == actor)]  # one vote for each person
            if d.action == "edit":  # the others approved another text: they must approve the edited one
                votes = [v for v in votes if v.index != d.index]
            votes.append(ApprovalVote(index=d.index, by=actor, action=d.action, payload=d.payload, at=now))
        run = run.model_copy(update={"approvals": votes, "updated_at": now})
        final, waiting = self.final_decisions(run)
        if waiting:
            names = ", ".join(sorted({v.by for v in votes if v.action != "reject"})) or "nobody"
            run = run.model_copy(update={"stage": f"Waiting for another approval ({names} approved)"})
            return self.store.save(run)
        approvers = sorted({v.by for v in votes if v.action in ("approve", "edit")}) or [actor]
        run = run.model_copy(update={"status": "executing", "stage": "Executing approved actions",
                                     "decided_by": ", ".join(approvers)})
        return self.store.save(run)

    def approvals_needed(self, proposal: ProposedAction) -> int:
        """The stricter of two numbers: the number saved with the proposal, and the number of the current policy.
        A policy can become stricter."""
        needed = proposal.approvals_needed
        if getattr(self.settings, "data_dir", None) is not None:
            from .. import automation

            key = proposal.payload.get("action", "") if proposal.kind == "remediation" else proposal.kind
            now = automation.approvals_needed(automation.store(self.settings).load(), key)
            needed = max(needed, now) if needed else now
        return needed

    def final_decisions(self, run: AgentRun) -> tuple[list[ActionDecision], bool]:
        """The decision for each proposal from the votes, and True when a proposal still needs another person."""
        final, waiting = [], False
        for i, proposal in enumerate(run.proposals):
            needed = self.approvals_needed(proposal)
            votes = [v for v in run.approvals if v.index == i]
            if any(v.action == "reject" for v in votes):
                final.append(ActionDecision(index=i, action="reject"))
            elif needed == 0:
                final.append(ActionDecision(index=i, action="approve"))
            elif len({v.by for v in votes if v.action in ("approve", "edit")}) >= needed:
                edits = [v for v in votes if v.action == "edit"]
                final.append(ActionDecision(index=i, action="edit", payload=edits[-1].payload) if edits
                             else ActionDecision(index=i, action="approve"))
            else:
                waiting = True
        return final, waiting

    def recover(self, assume_alone: bool = True) -> dict[str, int]:
        """Give back the work of workers that stopped, and queue the runs that lost their job.

        assume_alone=True, at start when this process is the only one with workers: each job marked as taken belongs to
        a process that stopped. assume_alone=False, with more than one process: only the jobs without a heartbeat for
        STALE_SECONDS. Interrupted investigations continue from their last checkpoint. The app never does the approved
        actions of a stopped run again."""
        counts = {"requeued": 0, "continued": 0, "failed": 0}
        stale = None if assume_alone else (datetime.now(UTC) - timedelta(seconds=STALE_SECONDS)).isoformat()
        with self._held_lock:
            mine = set(self._held)
        for job in self.store.running_jobs(stale_before=stale):
            if job.id in mine:
                continue
            run = self.store.get(job.target) if job.kind not in ("reflect", "correlate") else None
            if job.kind in ("reflect", "correlate"):  # safe to do again: they only read and suggest
                self.store.requeue_job(job.id, job.kind)
            elif job.kind == "resume" or run is None or run.status not in ("queued", "running"):
                self.store.finish_job(job.id)  # the "executing" check below fails the run
            else:
                self.store.save(run.model_copy(update={"status": "queued", "stage": "Queued to continue after a restart",
                                                       "updated_at": datetime.now(UTC)}))
                self.store.requeue_job(job.id, "continue")
                counts["continued"] += 1
        for run in self.store.with_status("queued"):
            if not self.store.has_job(run.id):
                self._enqueue("investigate", run.id)
                counts["requeued"] += 1
        for run in self.store.with_status("running"):
            if not self.store.has_job(run.id):
                self.store.save(run.model_copy(update={"status": "queued", "stage": "Queued to continue after a restart",
                                                       "updated_at": datetime.now(UTC)}))
                self._enqueue("continue", run.id)
                counts["continued"] += 1
        for run in self.store.with_status("executing"):
            if self.store.has_job(run.id):
                continue  # the approved actions wait in the queue: they have not started
            # do not do external actions again: some can have completed before the stop
            self.store.save(run.model_copy(update={
                "status": "failed", "updated_at": datetime.now(UTC),
                "error": "The server stopped while approved actions were executing. Check Gmail and Jira for what "
                         "completed before starting a new run.",
            }))
            counts["failed"] += 1
        return counts

    # Workers

    def investigate(self, run_id: str, fresh: bool = True) -> None:
        if not self.store.take_run(run_id):
            return  # cancelled while it waited in the queue, or another worker has it
        recorder = _Recorder(self.store, self._get(run_id))
        try:
            case = self.database.get_case(recorder.run.case_id)
            if case is None:
                raise RunNotFound("The case was deleted.")
            if case.status == "new":
                self.database.mark_investigating(case.id)
            changes: dict = {"status": "running", "queue_position": None,
                             "stage": "Lead agent planning" if fresh else "Continuing from the last checkpoint"}
            if fresh:
                notes = [n for n in recorder.run.lessons_used if n.startswith(FOLLOW_UP_PREFIX)]
                changes["lessons_used"] = notes + self._linked_notes(case.id) + self.select_lessons(case)
            recorder.update(**changes)
            with self._checkpointer() as saver:
                has_checkpoint = bool(saver.get_tuple({"configurable": {"thread_id": run_id}}))
            graph_input = None if (not fresh and has_checkpoint) else initial_state()
            self._stream(recorder, graph_input, on_step=recorder.add_step)
            if self.store.get(run_id).status in ("awaiting_approval", "completed"):
                self.schedule_correlation(case.id)
        except RunCancelled as exc:
            recorder.update(status=exc.status, stage="", error=str(exc))
        except Exception as exc:
            recorder.update(status="failed", stage="", error=f"Investigation failed: {exc}")
        finally:
            self.store.clear_cancel(run_id)

    def resume(self, run_id: str, decisions: list[ActionDecision]) -> None:
        recorder = _Recorder(self.store, self._get(run_id))
        try:
            self._stream(recorder, Command(resume=[d.model_dump() for d in decisions]))
        except Exception as exc:
            recorder.update(status="failed", stage="", error=f"Executing approved actions failed: {exc}")

    # Observability

    def _sync_jira_priority(self, before, after) -> None:
        """When the team raises the severity, change the Jira priority to match. A Jira failure does not stop the run."""
        if before is None or after is None or before.severity == after.severity:
            return
        key = after.jira_source_key or after.jira_issue_key
        if not key or not getattr(self.jira, "configured", False) or not hasattr(self.jira, "set_priority"):
            return
        try:
            self.jira.set_priority(key, after.severity)
            self.database.record_audit("rootiq-agent", "jira.priority", "case", after.id,
                                       f"Jira {key} priority changed to match severity {after.severity}.")
        except Exception:  # noqa: BLE001 - the analysis is still valid without the Jira change
            pass

    def metrics(self) -> AgentMetrics:
        runs = self.store.list_all()
        return compute_metrics(runs, [lesson for lesson, _ in self.store.lessons()])

    # Lessons

    def reflect(self, case_id: str) -> AgentLesson | None:
        case = self.database.get_case(case_id)
        if case is None or case.status != "resolved":
            return None
        run = next((r for r in self.store.list_for_case(case_id) if r.analysis is not None), None)
        if run is None:
            return None  # the team never investigated this case
        prompt = (
            f"Incident: {case.title}\nService: {case.service}\nReported issue: {case.reported_issue}\n\n"
            f"Team's likely cause:\n{run.analysis.likely_cause}\n\n"
            f"Confirmed root cause (engineer-reviewed):\n{case.post_incident_report.root_cause}\n"
            f"Resolution:\n{case.resolution}\nPreventive actions: {'; '.join(case.preventive_actions) or 'none'}"
        )
        llm = self.llm_factory()
        llm = llm.for_agent("reflect") if hasattr(llm, "for_agent") else llm  # a simple task: the local model first
        draft = llm.with_structured_output(LessonDraft).invoke(
            [SystemMessage(REFLECT_PROMPT), HumanMessage(prompt)]
        )
        lesson = AgentLesson(
            id=str(uuid4()), case_id=case.id, run_id=run.id, service=case.service, case_title=case.title,
            verdict=draft.verdict, agent_cause=run.analysis.likely_cause, actual_resolution=case.resolution,
            lesson=draft.lesson.strip(), created_at=datetime.now(UTC),
        )
        try:
            vector = self.embedder.embed(f"{case.service}\n{case.title}\n{lesson.lesson}")
        except EmbeddingUnavailable:
            vector = []
        self.store.save_lesson(lesson, vector)
        self.database.record_audit("rootiq-agent", "agent.lesson_recorded", "case", case.id,
                                   f"Team diagnosis {draft.verdict.replace('_', ' ')}: {lesson.lesson}")
        return lesson

    def _linked_notes(self, case_id: str) -> list[str]:
        """The confirmed cases with the same root cause, for the lead agent."""
        from ..correlation import IncidentGroups, linked_notes

        try:
            return linked_notes(IncidentGroups(self.database.path), {c.id: c for c in self.database.list_cases()}, case_id)
        except Exception:  # a note must not stop an investigation
            return []

    def select_lessons(self, case: SupportCase) -> list[str]:
        candidates = [(lesson, vec) for lesson, vec in self.store.lessons() if lesson.case_id != case.id]
        if not candidates:
            return []
        text = f"{case.service}\n{case.title}\n{case.reported_issue}"
        try:
            query = self.embedder.embed(text)
            scored = [(cosine_similarity(query, vec) if vec else 0.0, lesson) for lesson, vec in candidates]
        except EmbeddingUnavailable:
            scored = [(text_similarity(text, f"{item.case_title} {item.lesson}"), item) for item, _ in candidates]
        same = lambda lesson: lesson.service == case.service  # noqa: E731
        scored = [(score + (SAME_SERVICE_BONUS if same(lesson) else 0.0), lesson) for score, lesson in scored]
        best = sorted((item for item in scored
                       if item[0] >= (LESSON_MIN_SCORE if same(item[1]) else LESSON_MIN_SCORE_OTHER_SERVICE)),
                      key=lambda item: item[0], reverse=True)
        return [
            f"{lesson.lesson} (from \"{lesson.case_title}\", where the team's diagnosis was "
            f"{lesson.verdict.replace('_', ' ')})"
            for _, lesson in best[:MAX_LESSONS]
        ]

    # Internals

    def _stream(self, recorder: _Recorder, graph_input, on_step=None) -> None:
        run = recorder.run
        case = self.database.get_case(run.case_id)
        if case is None:
            raise RunNotFound("The case was deleted.")
        sources = active_sources(self.settings)
        box = InvestigationToolbox(case, self.database, self.embedder, self.jira, sources.logs, on_step=on_step,
                                   metrics=sources.metrics, changes=sources.changes)
        policy = None
        if getattr(self.settings, "data_dir", None) is not None:
            from .. import automation

            policy = automation.store(self.settings).load()
            if policy["remediation"] and not policy["paused"]:
                box.remediation = (policy, automation.RemediationLog(self.settings.database_target))
        executor = ActionExecutor(self.gmail, self.jira, run.dry_run, actor=run.decided_by or "rootiq-agent",
                                  remediate=self._remediator(run, case, sources) if policy else None)
        box.code, box.code_cloud, box.service_db = sources.code, sources.code_cloud, sources.database
        team_members = ((["changes"] if box.changes is not None else []) + (["code"] if box.code is not None else [])
                        + (["remediation"] if box.remediation else []))
        if run.team != team_members and graph_input is not None and not isinstance(graph_input, Command):
            recorder.update(team=team_members)
        config = {"configurable": {"thread_id": run.id}, "recursion_limit": 40,
                  "callbacks": [UsageRecorder(recorder.add_llm_call)]}
        with self._checkpointer() as saver:
            limit = getattr(self.settings, "agent_time_limit_seconds", 0) or None
            team = build_team(self.llm_factory(), box, executor=executor, checkpointer=saver, lessons=run.lessons_used,
                              on_span=recorder.add_span, deadline=Deadline(limit), policy=policy)
            for update in team.stream(graph_input, config, stream_mode="updates"):
                for node, output in update.items():  # keep the step that just finished, then check for a stop
                    if node != "__interrupt__":
                        self._apply(recorder, node, output or {})
                if cancel := self.store.cancel_request(run.id):
                    status, message = cancel
                    raise RunCancelled(message, status)
            paused = bool(team.get_state(config).next)
        if paused:
            recorder.update(status="awaiting_approval", stage="Waiting for engineer approval")
        else:
            recorder.update(status="completed", stage="")
            for outcome in recorder.run.outcomes:
                self.database.record_audit(
                    recorder.run.decided_by or "rootiq-agent", f"agent.action_{outcome.status}", "case",
                    run.case_id, f"{outcome.kind}: {outcome.detail}",
                )
                if outcome.kind == "remediation" and outcome.status == "executed":
                    self.schedule_verification(recorder.run, outcome.index, check=1)

    def _apply(self, recorder: _Recorder, node: str, output: dict) -> None:
        run = recorder.run
        if node == "lead":
            recorder.update(plan=output["plan"], stage="Specialists investigating")
        elif node == "remediation":
            recorder.update(stage="Comms agent drafting proposals")
        elif node in ("evidence", "history", "changes", "code"):
            recorder.update(findings={**run.findings, **output["findings"]}, stage="Reviewer checking findings")
        elif node == "review":
            reviews = [*run.reviews, *output["reviews"]]
            moving_on = reviews[-1].approved or len(reviews) > MAX_REVISIONS
            next_stage = ("Remediation agent checking fixes" if "remediation" in run.team and reviews[-1].approved
                          else "Comms agent drafting proposals")
            recorder.update(reviews=reviews, stage=next_stage if moving_on else "Specialists rechecking after review")
        elif node == "comms":
            recorder.update(stage="Writing final analysis")
        elif node == "finalize":
            recorder.update(analysis=output["analysis"], severity_note=output["severity_note"],
                            warnings=output.get("warnings", []), proposals=output["proposals"],
                            stage="Waiting for engineer approval")
            before = self.database.get_case(run.case_id)
            updated = self.database.save_analysis(run.case_id, output["analysis"])
            self._sync_jira_priority(before, updated)
            self.database.record_audit("rootiq-agent", "agent.analysis_ready", "case", run.case_id,
                                       f"Agent team analysis ready with {len(output['proposals'])} proposed action(s).")
        elif node == "execute":
            recorder.update(outcomes=output["outcomes"])

    @contextmanager
    def _checkpointer(self):
        url = getattr(self.settings, "database_url", "")
        if url:  # PostgreSQL: the checkpoints are in the same database
            from langgraph.checkpoint.postgres import PostgresSaver

            with PostgresSaver.from_conn_string(url) as saver:
                saver.serde = self.serde
                if not self._pg_ready:
                    saver.setup()
                    self._pg_ready = True
                yield saver
            return
        self.settings.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.settings.checkpoint_path, check_same_thread=False)
        try:
            saver = SqliteSaver(connection, serde=self.serde)
            saver.setup()
            yield saver
        finally:
            connection.close()

    def _get(self, run_id: str) -> AgentRun:
        run = self.store.get(run_id)
        if run is None:
            raise RunNotFound("Agent run not found.")
        return run
