import hashlib
import hmac
import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
from langchain_core.messages import AIMessage

from backend.app import automation
from backend.app.agent.followup import decide, measure
from backend.app.agent.runner import AgentRunner, FOLLOW_UP_PREFIX
from backend.app.agent.store import AgentRunStore
from backend.app.models import ActionDecision, VerificationVerdict
from backend.seed.seed_demo import seed
from backend.tests.test_agent import LOGS_DIR, KeywordEmbedder, NoJira
from backend.tests.test_runner import RecordingGmail
from backend.tests.test_team import APPROVE, FINAL, PLAN, FakeTeamLLM, call
from backend.app.database import CaseDatabase
from backend.app.models import InvestigationPlan, ReviewVerdict, SupportAnalysis


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class ValidateTests(unittest.TestCase):
    EVIDENCE = "deploy release=payments-api:2026.10.03-1 previous=2026.10.02-4 flag checkout_v2 change e7a90c3"

    def check(self, action, params, service="payments-api"):
        return automation.validate(action, service, params, "payments-api", self.EVIDENCE)

    def test_catalog_service_and_parameters(self):
        self.assertEqual(self.check("rollback_release", {"to_version": "2026.10.02-4"}), {"to_version": "2026.10.02-4"})
        self.assertEqual(self.check("revert_config_change", {"change_id": "e7a90c3"}), {"change_id": "e7a90c3"})
        self.assertEqual(self.check("scale_service", {"replicas": 6, "current_replicas": 3}), {"replicas": 6, "current_replicas": 3})
        bad = [("drop_database", {}), ("restart_service", {}, "billing"), ("rollback_release", {"to_version": "9.9.9"}),
               ("rollback_release", {"to_version": "x; rm -rf /"}), ("scale_service", {"replicas": 50}),
               ("scale_service", {"replicas": 10, "current_replicas": 3}), ("disable_feature_flag", {"flag": "made_up"}),
               ("purge_cdn", {"path_prefix": "../etc"}), ("revert_config_change", {"change_id": "abcdef1"})]
        for case in bad:
            with self.subTest(case=case), self.assertRaises(automation.AutomationError):
                self.check(*case)

    def test_no_rollback_to_the_release_that_runs_now(self):
        evidence = ("2026-10-10T22:53:25Z INFO [deploy] model=triage-ft:2026.10.10 started previous=triage-ft:2026.10.03 "
                    "schema=v2")
        validate = lambda version: automation.validate("rollback_release", "triagelm", {"to_version": version}, "triagelm", evidence)  # noqa: E731
        with self.assertRaisesRegex(automation.AutomationError, "roll back to the release that ran before it: triage-ft:2026.10.03"):
            validate("triage-ft:2026.10.10")
        self.assertEqual(validate("triage-ft:2026.10.03"), {"to_version": "triage-ft:2026.10.03"})
        later = evidence + "\n2026-10-10T23:30:00Z INFO [deploy] model=triage-ft:2026.10.11 started previous=triage-ft:2026.10.10"
        self.assertEqual(automation.validate("rollback_release", "triagelm", {"to_version": "triage-ft:2026.10.10"}, "triagelm", later),
                         {"to_version": "triage-ft:2026.10.10"})  # a later deploy replaced it: now it is a real target

    def test_auto_only_for_low_risk_even_in_a_hand_edited_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "automation.json"
            path.write_text(json.dumps({"modes": {"rollback_release": "auto", "jira_comment": "auto"}}))
            policy = automation.AutomationStore(path).load()
            self.assertEqual(automation.approvals_needed(policy, "rollback_release"), 1)
            self.assertEqual(automation.approvals_needed(policy, "jira_comment"), 0)

    def test_runner_secret_is_encrypted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = automation.AutomationStore(Path(tmp) / "automation.json")
            store.save({**automation.DEFAULTS, "runner": {"type": "webhook", "config": {"url": "https://ops.example/hook", "secret": "s" * 20}}})
            self.assertNotIn("s" * 20, store.path.read_text())
            self.assertEqual(store.load()["runner"]["config"]["secret"], "s" * 20)


class WebhookTests(unittest.TestCase):
    def test_signed_call_and_refusals(self):
        seen = {}

        def handler(request):
            seen["sig"] = request.headers["X-RootIQ-Signature"]
            seen["body"] = request.content
            return httpx.Response(200, json={"detail": "rollback started"})
        runner = automation.WebhookRunner("https://ops.example/hook", "a-long-signing-secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
        status, detail = runner.run("restart_service", "payments-api", {}, "run:0")
        self.assertEqual((status, detail), ("executed", "Webhook: rollback started"))
        at, digest = seen["sig"].removeprefix("t=").split(",v1=")
        expected = hmac.new(b"a-long-signing-secret", f"{at}.".encode() + seen["body"], hashlib.sha256).hexdigest()
        self.assertEqual(digest, expected)
        with self.assertRaises(automation.AutomationError):
            automation.WebhookRunner("http://ops.example/hook", "a-long-signing-secret")  # not https
        with self.assertRaises(automation.AutomationError):
            automation.WebhookRunner("https://ops.example/hook", "short")


class VerificationMathTests(unittest.TestCase):
    def test_decisions(self):
        now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
        fix = now - timedelta(minutes=3)
        errors = [f"{stamp(fix - timedelta(minutes=m))} ERROR boom" for m in (1, 2, 3, 4)]
        quiet = measure(errors + [f"{stamp(fix + timedelta(seconds=30))} INFO ok"], fix, fix - timedelta(minutes=10), now)
        self.assertEqual((quiet["errors_before"], quiet["errors_after"], decide(quiet)), (4, 0, "fixed"))
        still = measure(errors + [f"{stamp(fix + timedelta(seconds=s))} ERROR boom" for s in (10, 50, 90)], fix, fix - timedelta(minutes=10), now)
        self.assertEqual(decide(still), "not_fixed")
        early = measure(errors, fix, fix - timedelta(minutes=10), fix + timedelta(seconds=20))
        self.assertEqual(decide(early), "inconclusive")
        nothing_before = measure([f"{stamp(fix + timedelta(seconds=30))} INFO ok"], fix, fix - timedelta(minutes=10), now)
        self.assertEqual(decide(nothing_before), "inconclusive")


class RemediationFlowTests(unittest.TestCase):
    """The whole loop on the demo environment: propose, two approvals, run once, verify, follow up."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.logs = self.root / "logs"
        shutil.copytree(LOGS_DIR, self.logs)
        self.db = CaseDatabase(self.root / "rootiq.db")
        self.db.initialize()
        seed(self.db, KeywordEmbedder())
        self.case = next(c for c in self.db.list_cases() if c.service == "payments-api" and c.status != "resolved")
        self.now = datetime.now(UTC).replace(microsecond=0)
        incident = [f"{stamp(self.now - timedelta(seconds=240))} INFO [deploy] release=payments-api:2026.10.03-1 started previous=2026.10.02-4"]
        incident += [f"{stamp(self.now - timedelta(seconds=s))} ERROR [gateway-client] TLS handshake failed" for s in (170, 140, 95, 30)]
        with (self.logs / "payments-api.log").open("a") as handle:
            handle.write("\n".join(incident) + "\n")
        self.error_time = stamp(self.now - timedelta(seconds=30))
        (self.root / "demo_state.json").write_text(json.dumps({"payments-api": {
            "scenario": "payments-tls", "fixed_by": {"rollback_release": {"to_version": "2026.10.02-4"}},
            "healthy": ["INFO [gateway-client] TLS handshake ok"], "still_failing": ["ERROR [gateway-client] TLS handshake failed"] * 3}}))
        self.settings = SimpleNamespace(
            agent_model="fake", agent_dry_run=True, logs_dir=self.logs, ollama_url="", data_dir=self.root,
            database_target=self.root / "rootiq.db", database_url="", checkpoint_path=self.root / "ckpt.db",
            agent_concurrency=0, agent_sla_cooldown_minutes=120, agent_autorun=True, agent_sla_watch_seconds=0)
        self.policy = automation.store(self.settings)
        data = self.policy.load()
        data["modes"]["rollback_release"] = "two"
        self.policy.save(data)

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, remediation_args=None):
        args = remediation_args or {"action": "rollback_release", "service": "payments-api", "to_version": "2026.10.02-4",
                                    "reason": f"TLS handshake failed at {self.error_time} after release 2026.10.03-1 replaced 2026.10.02-4.",
                                    "verify": "No TLS errors for 5 minutes.", "rollback_plan": "Deploy 2026.10.03-1 again with the CA bundle."}
        llm = FakeTeamLLM({
            "evidence": [call("read_service_logs", {"service": "payments-api"}, "e1"), AIMessage(content="ok")],
            "history": [AIMessage(content="ok")], "changes": [AIMessage(content="ok")],
            "remediation": [call("propose_remediation", args, "r1"), AIMessage(content="done")],
            "comms": [call("propose_customer_update", {"subject": "Payments update", "body": "We are working on it."}, "c1")],
        }, {InvestigationPlan: [PLAN], ReviewVerdict: [APPROVE, APPROVE], SupportAnalysis: [FINAL],
            VerificationVerdict: [VerificationVerdict(status="fixed", summary="Errors stopped after the rollback.")] * 3},
            reports={"evidence": [f"- {self.error_time} ERROR [gateway-client] TLS handshake failed"] * 2,
                     "history": ["- No similar incident."] * 2,
                     "changes": ["- Release 2026.10.03-1 replaced 2026.10.02-4 just before the errors."] * 2})
        store = AgentRunStore(self.root / "rootiq.db")
        store.initialize()
        return AgentRunner(self.settings, self.db, KeywordEmbedder(), NoJira(), RecordingGmail(), store, llm_factory=lambda: llm)

    def paused(self, runner):
        run = runner.start(self.case.id, "engineer")
        runner.process_queue()
        return runner.store.get(run.id)

    def test_full_loop_two_approvals_run_once_and_verify(self):
        runner = self.runner()
        run = self.paused(runner)
        self.assertEqual(run.status, "awaiting_approval", run.error)
        fix = next(i for i, p in enumerate(run.proposals) if p.kind == "remediation")
        self.assertEqual((run.proposals[fix].risk, run.proposals[fix].approvals_needed), ("high", 2))
        everything = [ActionDecision(index=i, action="approve") for i in range(len(run.proposals))]
        after_one = runner.decide(run.id, everything, "ann")
        self.assertEqual(after_one.status, "awaiting_approval")  # one person is not enough for a high-risk fix
        self.assertEqual(runner.decide(run.id, everything, "ann").status, "awaiting_approval")  # nor the same person twice
        done = runner.decide(run.id, [ActionDecision(index=fix, action="approve")], "ben")
        self.assertEqual((done.status, done.decided_by), ("executing", "ann, ben"))
        final, waiting = runner.final_decisions(done)
        self.assertFalse(waiting)
        runner.resume(run.id, final)
        run = runner.store.get(run.id)
        outcome = run.outcomes[fix]
        self.assertEqual(outcome.status, "executed", outcome.detail)  # the demo runner runs even in dry run
        self.assertIn("TLS handshake ok", (self.logs / "payments-api.log").read_text())
        with runner.store._connect() as connection:
            job = connection.execute("SELECT kind, run_after FROM agent_jobs WHERE target LIKE 'verify:%'").fetchone()
        self.assertEqual(job[0], "verify")
        self.assertGreater(datetime.fromisoformat(job[1]), datetime.now(UTC) + timedelta(seconds=60))
        # the same approval cannot run the fix a second time
        from backend.app.sources import active_sources

        status, detail = runner._remediator(run, self.case, active_sources(self.settings))(
            fix, run.proposals[fix], run.proposals[fix].payload)
        self.assertEqual(status, "skipped")
        self.assertIn("already ran", detail)
        # verification: the action happened 3 minutes ago, errors before, none after
        result = runner.verify({"run_id": run.id, "index": fix, "check": 1,
                                "action_at": (datetime.now(UTC) - timedelta(seconds=5)).isoformat()})
        self.assertIn(result.status, ("fixed", "inconclusive"))

    def test_wrong_fix_is_not_verified_and_a_follow_up_run_starts(self):
        runner = self.runner({"action": "restart_service", "service": "payments-api",
                              "reason": f"TLS handshake failed at {self.error_time}.", "verify": "No errors.", "rollback_plan": "None needed."})
        run = self.paused(runner)
        fix = next(i for i, p in enumerate(run.proposals) if p.kind == "remediation")
        everything = [ActionDecision(index=i, action="approve") for i in range(len(run.proposals))]
        runner.decide(run.id, everything, "ann")
        runner.resume(run.id, everything)
        log = (self.logs / "payments-api.log").read_text()
        self.assertIn("restart_service complete", log)
        self.assertNotIn("TLS handshake ok", log)  # a restart does not fix a broken CA bundle
        action_at = datetime.now(UTC) - timedelta(minutes=2)
        with (self.logs / "payments-api.log").open("a") as handle:
            handle.write("\n".join(f"{stamp(action_at + timedelta(seconds=s))} ERROR [gateway-client] TLS handshake failed" for s in (20, 50, 80)) + "\n")
        result = runner.verify({"run_id": run.id, "index": fix, "check": 1, "action_at": action_at.isoformat()})
        self.assertEqual(result.status, "not_fixed")  # the model said "fixed", the numbers decide
        follow = runner.store.list_for_case(self.case.id)[0]
        self.assertEqual(follow.trigger, "verification_failed")
        self.assertTrue(follow.lessons_used[0].startswith(FOLLOW_UP_PREFIX))
        notes = [m.content for m in self.db.list_messages(self.case.id)]
        self.assertTrue(any(n.startswith("The fix did not work") for n in notes))

    def test_kill_switch_and_limits_block_the_fix_at_run_time(self):
        runner = self.runner()
        run = self.paused(runner)
        fix = next(i for i, p in enumerate(run.proposals) if p.kind == "remediation")
        data = self.policy.load()
        data["paused"] = True
        self.policy.save(data)
        runner.decide(run.id, [ActionDecision(index=fix, action="approve")], "ann")
        runner.decide(run.id, [ActionDecision(index=fix, action="approve")], "ben")
        runner.resume(run.id, [ActionDecision(index=fix, action="approve")])
        outcome = runner.store.get(run.id).outcomes[fix]
        self.assertEqual(outcome.status, "skipped")
        self.assertIn("paused", outcome.detail)

    def test_reason_must_cite_real_evidence(self):
        runner = self.runner({"action": "rollback_release", "service": "payments-api", "to_version": "2026.10.02-4",
                              "reason": "TLS failed at 2026-01-01T00:00:00Z.", "verify": "x", "rollback_plan": "x"})
        run = self.paused(runner)
        self.assertFalse(any(p.kind == "remediation" for p in run.proposals))
        step = next(s for s in run.steps if s.tool == "propose_remediation")
        self.assertIn("no tool returned", step.result)


if __name__ == "__main__":
    unittest.main()
