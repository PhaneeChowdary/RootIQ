import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from backend.app import main
from backend.app.agent.runner import AgentRunner, RunConflict
from backend.app.agent.store import AgentRunStore
from backend.app.database import CaseDatabase
from backend.app.models import (
    ActionDecision,
    CaseCreate,
    InvestigationPlan,
    LessonDraft,
    ResolutionUpdate,
    ReviewVerdict,
    SupportAnalysis,
)
from backend.app.services.auth import LocalAuthenticator
from backend.seed.seed_demo import seed
from backend.tests.test_agent import LOGS_DIR, KeywordEmbedder, NoJira
from backend.tests.test_team import APPROVE, FINAL, PLAN, FakeTeamLLM, scripts

LESSON = LessonDraft(verdict="partly_correct", lesson="For SMS otp code delays check the provider 429 rate per sender pool.")


class RecordingGmail:
    configured = True

    def __init__(self):
        self.drafts = []

    def create_draft(self, subject, body):
        self.drafts.append((subject, body))
        return f"draft-{len(self.drafts)}"


class SimulatedCrash(BaseException):
    """Stands in for the process dying mid-run: not an Exception, so nothing in the runner catches it."""


def fake_llm(comms=None):
    script = scripts()
    if comms is not None:
        script["comms"] = comms
    return FakeTeamLLM(script, {
        InvestigationPlan: [PLAN], ReviewVerdict: [APPROVE], SupportAnalysis: [FINAL], LessonDraft: [LESSON],
    })


class CrashInComms(FakeTeamLLM):
    def bind_tools(self, tools):
        if "propose_customer_update" in {t.name for t in tools}:
            raise SimulatedCrash()
        return super().bind_tools(tools)


def reset_postgres(url: str) -> None:
    """An empty database for the next test. Test_postgres.py."""
    import psycopg

    from backend.app import migrations

    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
    migrations._done.discard(url)


class RunnerFixture:
    DATABASE_URL = ""  # test_postgres.py runs the same tests on PostgreSQL

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.target = self.DATABASE_URL or root / "rootiq.db"
        if self.DATABASE_URL:
            reset_postgres(self.DATABASE_URL)
        self.db = CaseDatabase(self.target)
        self.db.initialize()
        seed(self.db, KeywordEmbedder())
        self.case = self.open_case("Login verification texts")
        self.gmail = RecordingGmail()
        self.llms = []

    def tearDown(self):
        self.tmp.cleanup()

    def open_case(self, title_start):
        return next(c for c in self.db.list_cases() if c.title.startswith(title_start))

    def runner(self, dry_run=True, comms=None, llm=None):
        root = Path(self.tmp.name)
        settings = SimpleNamespace(
            agent_model="fake", agent_dry_run=dry_run, logs_dir=LOGS_DIR, ollama_url="",
            checkpoint_path=root / "agent_checkpoints.db", agent_concurrency=0,
            agent_sla_cooldown_minutes=120, agent_autorun=True, agent_sla_watch_seconds=0,
            database_url=self.DATABASE_URL,
        )
        store = AgentRunStore(self.target)
        store.initialize()

        def factory():
            made = llm or fake_llm(comms)
            self.llms.append(made)
            return made

        return AgentRunner(settings, self.db, KeywordEmbedder(), NoJira(), self.gmail, store, llm_factory=factory)

    def paused_run(self, runner, case=None):
        run = runner.start((case or self.case).id, "engineer")
        runner.process_queue()
        return runner.store.get(run.id)

    def approve(self, runner, run):
        decisions = [ActionDecision(index=i, action="approve") for i in range(len(run.proposals))]
        runner.decide(run.id, decisions, "engineer")
        runner.resume(run.id, decisions)
        return runner.store.get(run.id)


class RunnerTests(RunnerFixture, unittest.TestCase):
    def test_run_pauses_for_approval_and_saves_analysis_to_case(self):
        run = self.paused_run(self.runner())
        self.assertEqual(run.status, "awaiting_approval", run.error)
        self.assertEqual(run.trigger, "manual")
        self.assertEqual(set(run.findings), {"evidence", "history"})
        self.assertEqual({s.agent for s in run.steps}, {"evidence", "history", "comms"})
        self.assertEqual([s.index for s in run.steps], list(range(1, len(run.steps) + 1)))
        case = self.db.get_case(self.case.id)
        self.assertEqual(case.status, "awaiting_approval")
        self.assertEqual(case.analysis.likely_cause, FINAL.likely_cause)
        self.assertEqual(self.gmail.drafts, [])

    def test_dry_run_approval_completes_without_calling_gmail(self):
        runner = self.runner(dry_run=True)
        run = self.approve(runner, self.paused_run(runner))
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual([o.status for o in run.outcomes], ["dry_run"])
        self.assertEqual(self.gmail.drafts, [])
        self.assertIn("agent.action_dry_run", [e.action for e in self.db.list_audit_events()])

    def test_live_edit_uses_engineer_text(self):
        runner = self.runner(dry_run=False)
        run = self.paused_run(runner)
        decisions = [ActionDecision(index=0, action="edit", payload={"subject": "Edited subject", "issue_key": "X-1"})]
        runner.decide(run.id, decisions, "engineer")
        runner.resume(run.id, decisions)
        self.assertEqual([o.status for o in runner.store.get(run.id).outcomes], ["executed"])
        self.assertEqual(self.gmail.drafts, [("Edited subject", "We are investigating.")])

    def test_rejected_action_is_skipped(self):
        runner = self.runner(dry_run=False)
        run = self.paused_run(runner)
        decisions = [ActionDecision(index=0, action="reject")]
        runner.decide(run.id, decisions, "engineer")
        runner.resume(run.id, decisions)
        self.assertEqual([o.status for o in runner.store.get(run.id).outcomes], ["skipped"])
        self.assertEqual(self.gmail.drafts, [])

    def test_paused_run_resumes_after_restart(self):
        run = self.paused_run(self.runner())
        restarted = self.runner()  # new runner, store and checkpointer connection: same files on disk
        self.assertEqual(restarted.recover(), {"requeued": 0, "continued": 0, "failed": 0})
        self.assertEqual(self.approve(restarted, run).status, "completed")

    def test_second_run_on_same_case_conflicts(self):
        runner = self.runner()
        runner.start(self.case.id, "engineer")
        with self.assertRaises(RunConflict):
            runner.start(self.case.id, "engineer")
        self.assertIsNone(runner.start_if_idle(self.case.id, "rootiq-agent", "case_created"))

    def test_decision_validation(self):
        runner = self.runner()
        run = self.paused_run(runner)
        with self.assertRaises(ValueError):
            runner.decide(run.id, [ActionDecision(index=5, action="approve")], "engineer")
        with self.assertRaises(ValueError):
            runner.decide(run.id, [ActionDecision(index=0, action="approve"), ActionDecision(index=0, action="reject")], "e")
        decisions = [ActionDecision(index=0, action="approve")]
        runner.decide(run.id, decisions, "engineer")
        with self.assertRaises(RunConflict):
            runner.decide(run.id, decisions, "engineer")

    def test_run_without_proposals_completes_without_pausing(self):
        run = self.paused_run(self.runner(comms=[AIMessage(content="Nothing to send.")]))
        self.assertEqual(run.status, "completed", run.error)
        self.assertEqual(run.outcomes, [])

    def test_failed_investigation_is_recorded(self):
        runner = self.runner()
        runner.llm_factory = lambda: (_ for _ in ()).throw(RuntimeError("Ollama is not reachable"))
        run = self.paused_run(runner)
        self.assertEqual(run.status, "failed")
        self.assertIn("Ollama is not reachable", run.error)


class QueueAndRecoveryTests(RunnerFixture, unittest.TestCase):
    def test_runs_queue_in_order_and_report_position(self):
        runner = self.runner()
        first = runner.start(self.case.id, "engineer")
        second = runner.start(self.open_case("Dashboard shows blank").id, "engineer")
        self.assertEqual((first.status, first.queue_position), ("queued", 1))
        self.assertEqual(second.queue_position, 2)
        runner.process_queue()
        self.assertEqual(runner.with_position(runner.store.get(second.id)).queue_position, None)
        self.assertEqual(runner.store.get(second.id).status, "awaiting_approval")

    def test_queued_run_is_requeued_after_restart(self):
        run = self.runner().start(self.case.id, "engineer")  # process died before a worker took it
        restarted = self.runner()
        self.assertEqual(restarted.recover()["requeued"], 0)  # the job is still in the queue table
        restarted.process_queue()
        self.assertEqual(restarted.store.get(run.id).status, "awaiting_approval")

    def test_interrupted_investigation_continues_from_checkpoint(self):
        crashing = self.runner(llm=CrashInComms(scripts(), {
            InvestigationPlan: [PLAN], ReviewVerdict: [APPROVE], SupportAnalysis: [FINAL],
        }))
        run = crashing.start(self.case.id, "engineer")
        with self.assertRaises(SimulatedCrash):
            crashing.process_queue()
        self.assertEqual(crashing.store.get(run.id).status, "running")
        steps_before = len(crashing.store.get(run.id).steps)

        restarted = self.runner()
        self.assertEqual(restarted.recover()["continued"], 1)
        restarted.process_queue()
        run = restarted.store.get(run.id)
        self.assertEqual(run.status, "awaiting_approval", run.error)
        resumed_llm = self.llms[-1]
        self.assertNotIn("InvestigationPlan", resumed_llm.prompts)  # lead did not plan again
        self.assertNotIn("evidence", resumed_llm.prompts)  # specialists did not rerun
        self.assertEqual({s.agent for s in run.steps[steps_before:]}, {"comms"})

    def test_two_processes_recovering_at_once_run_each_run_once(self):
        import threading

        first = self.runner()
        run = first.start(self.case.id, "engineer")
        first.store.drop_queued_jobs(run.id)  # as if the run was made before the job table
        a, b = self.runner(), self.runner()
        threads = [threading.Thread(target=r.recover) for r in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with a.store._connect() as connection:
            jobs = connection.execute("SELECT COUNT(*) FROM agent_jobs WHERE target = ?", (run.id,)).fetchone()[0]
        self.assertEqual(jobs, 1)
        made_before = len(self.llms)
        workers = [threading.Thread(target=r.process_queue) for r in (a, b)]
        for t in workers:
            t.start()
        for t in workers:
            t.join()
        self.assertEqual(len(self.llms) - made_before, 1)  # one investigation, not two
        self.assertEqual(a.store.get(run.id).status, "awaiting_approval")

    def test_executing_run_fails_on_restart_instead_of_replaying_actions(self):
        runner = self.runner()
        run = self.paused_run(runner)
        runner.decide(run.id, [ActionDecision(index=0, action="approve")], "engineer")  # resume never ran
        self.assertEqual(self.runner().recover()["failed"], 1)
        self.assertIn("Check Gmail and Jira", self.runner().store.get(run.id).error)


class CancellationTests(RunnerFixture, unittest.TestCase):
    def test_queued_run_of_deleted_case_is_cancelled_and_skipped(self):
        runner = self.runner()
        run = runner.start(self.case.id, "engineer")
        self.assertEqual(runner.cancel_for_case(self.case.id, "Cancelled: the case was deleted."), 1)
        runner.process_queue()
        run = runner.store.get(run.id)
        self.assertEqual((run.status, run.error), ("failed", "Cancelled: the case was deleted."))
        self.assertEqual(self.llms, [])  # the worker never started the team

    def test_paused_run_is_cancelled(self):
        runner = self.runner()
        run = self.paused_run(runner)
        runner.cancel_for_case(self.case.id, "Cancelled: the case was deleted.")
        self.assertEqual(runner.store.get(run.id).status, "failed")

    def test_running_investigation_stops_at_the_next_step(self):
        runner = self.runner()
        case_id = self.case.id

        class CancelDuringPlanning(FakeTeamLLM):
            def with_structured_output(self, schema):
                if schema is InvestigationPlan:
                    runner.cancel_for_case(case_id, "Cancelled: the case was deleted.")
                return super().with_structured_output(schema)

        runner.llm_factory = lambda: CancelDuringPlanning(scripts(), {
            InvestigationPlan: [PLAN], ReviewVerdict: [APPROVE], SupportAnalysis: [FINAL],
        })
        run = runner.start(case_id, "engineer")
        runner.process_queue()
        run = runner.store.get(run.id)
        self.assertEqual(run.status, "failed")
        self.assertIn("deleted while the team was investigating", run.error)
        self.assertEqual(run.steps, [])  # stopped after planning, before the specialists


class TriggerTests(RunnerFixture, unittest.TestCase):
    def at_risk(self):
        cases = self.db.list_cases()
        return lambda: [c.model_copy(update={"sla_state": "at_risk"}) for c in cases]

    def test_sla_watcher_starts_runs_once_per_cooldown(self):
        runner = self.runner()
        new_cases = [c for c in self.db.list_cases() if c.status == "new"]
        runner.database.list_cases = self.at_risk()
        started = runner.check_sla()
        self.assertEqual({r.case_id for r in started}, {c.id for c in new_cases})
        self.assertTrue(all(r.trigger == "sla_risk" for r in started))
        runner.process_queue()
        self.assertEqual(runner.check_sla(), [])  # cooldown: no second run for the same cases

    def test_sla_watcher_skips_cases_already_reviewed_or_resolved(self):
        runner = self.runner()
        reviewed_or_closed = {c.id for c in self.db.list_cases() if c.status not in ("new", "investigating")}
        runner.database.list_cases = self.at_risk()
        started = {r.case_id for r in runner.check_sla()}
        self.assertTrue(reviewed_or_closed)
        self.assertFalse(started & reviewed_or_closed)

    def test_sla_watcher_ignores_on_track_cases(self):
        self.assertEqual(self.runner().check_sla(now=datetime.now(UTC)), [])


class LessonTests(RunnerFixture, unittest.TestCase):
    def resolve_after_run(self, runner):
        self.approve(runner, self.paused_run(runner))
        self.db.resolve_case(self.case.id, ResolutionUpdate(
            resolution="Moved OTP traffic off the shared EU pool.", preventive_actions=["Alert on 429 rate"],
        ))

    def test_resolution_produces_a_lesson(self):
        runner = self.runner()
        self.resolve_after_run(runner)
        runner.schedule_reflection(self.case.id)
        runner.process_queue()
        lesson = runner.store.lesson_for_case(self.case.id)
        self.assertEqual(lesson.verdict, "partly_correct")
        self.assertEqual(lesson.agent_cause, FINAL.likely_cause)
        self.assertIn("429", lesson.lesson)
        self.assertIn("agent.lesson_recorded", [e.action for e in self.db.list_audit_events()])

    def test_no_lesson_without_an_agent_run(self):
        runner = self.runner()
        resolved = next(c for c in self.db.list_cases() if c.status == "resolved")
        self.assertIsNone(runner.reflect(resolved.id))

    def test_lessons_reach_the_lead_on_a_similar_case(self):
        runner = self.runner()
        self.resolve_after_run(runner)
        runner.reflect(self.case.id)
        similar = self.db.create_case(CaseCreate(
            title="SMS otp code missing for Spanish users", customer="Northwind Health", service="auth-service",
            reported_issue="Login code texts are not arriving.", severity="high",
        ))
        run = self.paused_run(runner, similar)
        self.assertEqual(len(run.lessons_used), 1)
        self.assertIn("429 rate per sender pool", run.lessons_used[0])
        lead_briefing = self.llms[-1].prompts["InvestigationPlan"][0][-1].content
        self.assertIn("Lessons from past investigations", lead_briefing)

    def test_unrelated_case_gets_no_lessons(self):
        runner = self.runner()
        self.resolve_after_run(runner)
        runner.reflect(self.case.id)
        self.assertEqual(runner.select_lessons(self.open_case("Some customers double charged")), [])


    def test_a_lesson_from_another_service_needs_a_close_match(self):
        from math import sqrt

        from backend.app.models import AgentLesson

        runner = self.runner()
        case = self.open_case("Some customers double charged")

        def lesson(service, text):
            return AgentLesson(id=text, case_id="old", run_id="r", service=service, case_title=text, verdict="correct",
                               agent_cause="", actual_resolution="", lesson=text, created_at=datetime.now(UTC))
        # cosine 0.62 to the case: enough for a lesson of the same service, too little for another service
        near = [0.62, sqrt(1 - 0.62 ** 2)]
        runner.embedder = SimpleNamespace(embed=lambda text: [1.0, 0.0])
        runner.store = SimpleNamespace(lessons=lambda: [(lesson("web-dashboard", "Check the db pool size"), near),
                                                         (lesson(case.service, "Check retries of the charge"), [0.45, sqrt(1 - 0.45 ** 2)])])
        chosen = runner.select_lessons(case)
        self.assertEqual(len(chosen), 1)
        self.assertIn("Check retries of the charge", chosen[0])  # 0.45 + the same-service bonus 0.15 = 0.60


class AgentRunApiTests(RunnerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.agent = self.runner()
        main.settings = replace(main.settings, agent_autorun=True, agent_concurrency=0, agent_sla_watch_seconds=0)
        main.database = self.db
        main.embedder = KeywordEmbedder()
        main.jira_publisher = NoJira()
        main.gmail_publisher = self.gmail
        main.agent_runs = self.agent.store
        main.agent_runner = self.agent
        main.authenticator = LocalAuthenticator("test-agent", "test-password")
        self.client = TestClient(main.app)
        self.client.__enter__()
        token = self.client.post("/api/auth/login", json={"username": "test-agent", "password": "test-password"})
        self.client.headers.update({"Authorization": f"Bearer {token.json()['access_token']}"})

    def tearDown(self):
        self.client.__exit__(None, None, None)
        super().tearDown()

    def test_start_poll_decide_flow(self):
        started = self.client.post(f"/api/cases/{self.case.id}/agent-runs")
        self.assertEqual(started.status_code, 202)
        self.assertEqual((started.json()["status"], started.json()["queue_position"]), ("queued", 1))
        self.assertEqual(self.client.post(f"/api/cases/{self.case.id}/agent-runs").status_code, 409)
        run_id = started.json()["id"]
        self.agent.process_queue()

        self.assertEqual(self.client.get(f"/api/agent-runs/{run_id}").json()["status"], "awaiting_approval")
        bad = self.client.post(f"/api/agent-runs/{run_id}/decision", json={"decisions": [{"index": 9, "action": "approve"}]})
        self.assertEqual(bad.status_code, 422)
        decided = self.client.post(f"/api/agent-runs/{run_id}/decision",
                                   json={"decisions": [{"index": 0, "action": "approve"}]})
        self.assertEqual(decided.status_code, 202)
        self.agent.process_queue()  # a worker carries out the approved actions
        runs = self.client.get(f"/api/cases/{self.case.id}/agent-runs").json()
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(runs[0]["outcomes"][0]["status"], "dry_run")

    def test_creating_a_case_queues_a_run_automatically(self):
        created = self.client.post("/api/cases", json={
            "title": "SMS codes slow", "customer": "Acme", "service": "auth-service", "reported_issue": "Codes late.",
        }).json()
        runs = self.client.get(f"/api/cases/{created['id']}/agent-runs").json()
        self.assertEqual([(r["trigger"], r["status"]) for r in runs], [("case_created", "queued")])
        self.agent.process_queue()
        self.assertEqual(self.client.get(f"/api/cases/{created['id']}").json()["status"], "awaiting_approval")

    def test_resolving_a_case_records_a_lesson(self):
        run = self.approve(self.agent, self.paused_run(self.agent))
        self.assertEqual(run.status, "completed")
        self.client.put(f"/api/cases/{self.case.id}/review", json={"analysis": FINAL.model_dump(), "approve": True})
        self.client.put(f"/api/cases/{self.case.id}/resolve", json={"resolution": "Dedicated OTP pool."})
        self.agent.process_queue()
        lesson = self.client.get(f"/api/cases/{self.case.id}/agent-lesson").json()
        self.assertEqual(lesson["verdict"], "partly_correct")
        self.assertEqual(len(self.client.get("/api/agent-lessons").json()), 1)

    def test_deleting_a_case_cancels_its_queued_run(self):
        run = self.client.post(f"/api/cases/{self.case.id}/agent-runs").json()
        self.assertEqual(self.client.delete(f"/api/cases/{self.case.id}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/agent-runs/{run['id']}").json()["status"], "failed")

    def test_run_records_spans_and_metrics_endpoint_summarizes_runs(self):
        run = self.approve(self.agent, self.paused_run(self.agent))
        self.assertEqual(run.spans[0].agent, "lead")
        self.assertIn("execute", [s.agent for s in run.spans])
        metrics = self.client.get("/api/agent-metrics").json()
        self.assertEqual(metrics["runs_total"], 1)
        self.assertEqual(metrics["decisions"], {"approve": 1})
        self.assertIsNotNone(metrics["median_investigation_seconds"])

    def test_requires_authentication(self):
        self.client.headers.pop("Authorization")
        self.client.cookies.clear()
        self.assertEqual(self.client.post(f"/api/cases/{self.case.id}/agent-runs").status_code, 401)


class StopAndRestartTests(RunnerFixture, unittest.TestCase):
    def test_a_queued_run_stops_at_once_and_never_starts(self):
        runner = self.runner()
        run = runner.start(self.case.id, "engineer")
        stopped = runner.stop(run.id, "engineer")
        self.assertEqual((stopped.status, stopped.error), ("stopped", "Stopped by engineer."))
        runner.process_queue()
        self.assertEqual(runner.store.get(run.id).status, "stopped")
        self.assertEqual(self.llms, [])

    def test_a_running_run_stops_after_its_current_step(self):
        runner = self.runner()
        holder = {}

        class StopDuringPlanning(FakeTeamLLM):
            def with_structured_output(self, schema):
                if schema is InvestigationPlan:
                    holder["shown"] = runner.stop(holder["id"], "engineer")
                return super().with_structured_output(schema)

        runner.llm_factory = lambda: StopDuringPlanning(scripts(), {
            InvestigationPlan: [PLAN], ReviewVerdict: [APPROVE], SupportAnalysis: [FINAL],
        })
        holder["id"] = runner.start(self.case.id, "engineer").id
        runner.process_queue()
        run = runner.store.get(holder["id"])
        self.assertEqual((run.status, run.error), ("stopped", "Stopped by engineer."))
        self.assertEqual(holder["shown"].stage, "Stopping after the current step")
        self.assertIsNotNone(run.plan)  # the plan from the step that was running is kept
        self.assertEqual(run.steps, [])

    def test_restart_replaces_a_run_that_waits_for_approval(self):
        runner = self.runner()
        old = self.paused_run(runner)
        new = runner.restart(self.case.id, "engineer")
        self.assertEqual(runner.store.get(old.id).status, "stopped")
        self.assertEqual(new.status, "queued")
        self.assertNotEqual(new.id, old.id)

    def test_a_finished_run_cannot_be_stopped(self):
        runner = self.runner()
        run = runner.start(self.case.id, "engineer")
        runner.stop(run.id, "engineer")
        with self.assertRaises(RunConflict):
            runner.stop(run.id, "engineer")


class OrphanRunMigrationTests(unittest.TestCase):
    def test_runs_of_deleted_cases_are_removed_once(self):
        import sqlite3
        import tempfile

        from backend.app import migrations

        with tempfile.TemporaryDirectory() as folder:
            path = f"{folder}/r.db"
            migrations.upgrade(path)
            with sqlite3.connect(path) as c:
                c.execute("DELETE FROM schema_migrations WHERE version = 10")
                c.execute("INSERT INTO support_cases (id, title, customer, service, environment, severity, reported_issue, logs, "
                          "status, created_at, updated_at, analysis, review_status, resolution, preventive_actions, timeline) "
                          "VALUES ('kept', 't', 'c', 's', 'production', 'low', 'r', '', 'new', 'x', 'x', '{}', 'not_ready', '', '[]', '[]')")
                for run, case in (("r1", "kept"), ("r2", "gone")):
                    c.execute("INSERT INTO agent_runs (id, case_id, status, created_at, updated_at, data) VALUES (?, ?, 'failed', 'x', 'x', '{}')",
                              (run, case))
            migrations._done.discard(path)
            self.assertEqual(migrations.upgrade(path), [10])
            with sqlite3.connect(path) as c:
                self.assertEqual([r[0] for r in c.execute("SELECT id FROM agent_runs")], ["r1"])
            migrations._done.discard(path)


if __name__ == "__main__":
    unittest.main()
