import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from backend.app import crypto, main, retention
from backend.app.agent import llm
from backend.app.agent.runner import AgentRunner
from backend.app.agent.store import AgentRunStore
from backend.app.database import CaseDatabase
from backend.app.models import AgentRun, CaseCreate, ResolutionUpdate
from backend.app.services.auth import LocalAuthenticator, LoginLocked
from backend.tests.test_agent import KeywordEmbedder, NoJira
from backend.tests.test_api import FakeAnalyst, FakeEmbedder, FakeGmailPublisher

CASE = {"title": "Checkout fails", "customer": "Acme", "service": "payments-api", "reported_issue": "502 on pay"}


class LockoutTests(unittest.TestCase):
    def setUp(self):
        self.auth = LocalAuthenticator("admin", "pw", max_failures=3, lock_minutes=15)

    def test_account_locks_after_failures_and_a_good_password_does_not_help(self):
        for _ in range(3):
            self.assertIsNone(self.auth.login("admin", "wrong"))
        with self.assertRaises(LoginLocked) as caught:
            self.auth.login("admin", "pw")
        self.assertIn("15 minutes", str(caught.exception))
        self.assertEqual(self.auth.locked_accounts()[0]["key"], "user:admin")
        self.auth.unlock("admin")
        self.assertIsNotNone(self.auth.login("admin", "pw"))

    def test_success_resets_the_count_and_names_are_not_case_sensitive(self):
        self.auth.login("admin", "wrong")
        self.auth.login("ADMIN", "wrong")
        self.assertIsNotNone(self.auth.login("admin", "pw"))
        self.auth.login("admin", "wrong")
        self.auth.login("admin", "wrong")
        self.assertIsNotNone(self.auth.login("admin", "pw"))  # only 2 failures since the last success

    def test_one_address_trying_many_names_is_locked(self):
        for i in range(12):
            self.auth.login(f"user{i}", "x", address="10.0.0.9")
        with self.assertRaises(LoginLocked):
            self.auth.login("admin", "pw", address="10.0.0.9")
        self.assertIsNotNone(self.auth.login("admin", "pw", address="10.0.0.10"))

    def test_new_password_unlocks(self):
        self.auth.create_user("ann", "", "", "engineer", "Correct-horse-9")
        for _ in range(3):
            self.auth.login("ann", "nope")
        self.auth.update_user("ann", password="Another-pass-7")
        self.assertIsNotNone(self.auth.login("ann", "Another-pass-7"))


class ApiSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        data = Path(self.temp.name)
        self.saved = (main.settings, main.database, main.authenticator, main.agent_runs, main.agent_runner, main.jira_publisher,
                      main.analyst, main.embedder)
        main.analyst, main.embedder = FakeAnalyst(), FakeEmbedder()  # no real model calls in tests
        main.settings = replace(main.settings, data_dir=data, agent_autorun=False, agent_concurrency=0, agent_sla_watch_seconds=0,
                                retention_bin_days=30, retention_resolved_days=0, retention_audit_days=0)
        main.database = CaseDatabase(data / "rootiq.db")
        main.database.initialize()
        main.jira_publisher = NoJira()
        main.agent_runs = AgentRunStore(data / "rootiq.db")
        main.agent_runs.initialize()
        main.agent_runner = AgentRunner(main.settings, main.database, KeywordEmbedder(), NoJira(), FakeGmailPublisher(), main.agent_runs)
        main.authenticator = LocalAuthenticator("admin", "pw", max_failures=2)
        self.client = TestClient(main.app)
        token = self.client.post("/api/auth/login", json={"username": "admin", "password": "pw"}).json()["access_token"]
        self.client.headers.update({"Authorization": f"Bearer {token}"})

    def tearDown(self):
        (main.settings, main.database, main.authenticator, main.agent_runs, main.agent_runner, main.jira_publisher,
         main.analyst, main.embedder) = self.saved
        self.temp.cleanup()

    def test_locked_sign_in_answers_429_and_is_audited(self):
        anon = TestClient(main.app)
        for _ in range(2):
            self.assertEqual(anon.post("/api/auth/login", json={"username": "admin", "password": "x"}).status_code, 401)
        locked = anon.post("/api/auth/login", json={"username": "admin", "password": "pw"})
        self.assertEqual(locked.status_code, 429)
        self.assertIn("Retry-After", locked.headers)
        self.assertIn("auth.locked", [e["action"] for e in self.client.get("/api/audit").json()])

    def test_security_headers_and_origin_check(self):
        response = self.client.get("/api/cases")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        evil = self.client.post("/api/cases", json=CASE, headers={"Origin": "https://evil.example"})
        self.assertEqual(evil.status_code, 403)
        good = self.client.post("/api/cases", json=CASE, headers={"Origin": "http://127.0.0.1:5174"})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(self.client.post("/api/cases", json=CASE).status_code, 200)  # no Origin: a script or MCP

    def test_a_write_keeps_a_case_in_the_bin(self):
        case_id = self.client.post("/api/cases", json=CASE).json()["id"]
        self.client.delete(f"/api/cases/{case_id}")
        binned = main.database.get_case(case_id, include_deleted=True)
        main.database._write_case(binned.model_copy(update={"title": "Edited"}))
        self.assertEqual([c.id for c, _, _ in main.database.list_bin()], [case_id])

    def test_delete_forever_removes_runs_and_lessons(self):
        case_id = self.client.post("/api/cases", json=CASE).json()["id"]
        now = datetime.now(UTC)
        main.agent_runs.save(AgentRun(id="run-1", case_id=case_id, model="m", started_by="admin", status="completed", created_at=now, updated_at=now))
        self.client.delete(f"/api/cases/{case_id}")
        self.assertEqual(self.client.delete(f"/api/bin/{case_id}").status_code, 204)
        self.assertIsNone(main.agent_runs.get("run-1"))

    def test_retention_empties_old_bin_and_reports(self):
        case_id = self.client.post("/api/cases", json=CASE).json()["id"]
        self.client.delete(f"/api/cases/{case_id}")
        overview = self.client.get("/api/security").json()
        self.assertEqual(overview["retention"]["due"]["bin"], 0)
        later = datetime.now(UTC) + timedelta(days=31)
        self.assertEqual(retention.due(main.database, main.settings, later)["bin"], [case_id])
        counts = retention.run(main.database, main.agent_runner, main.settings, now=later)
        self.assertEqual(counts["bin"], 1)
        self.assertIsNone(main.database.get_case(case_id, include_deleted=True))
        self.assertIn("retention.cleanup", [e["action"] for e in self.client.get("/api/audit").json()])

    def test_retention_for_resolved_cases_and_audit_is_off_by_default(self):
        case = main.database.create_case(CaseCreate(**CASE))
        main.database.resolve_case(case.id, ResolutionUpdate(resolution="Fixed", preventive_actions=[]))
        far = datetime.now(UTC) + timedelta(days=3650)
        self.assertEqual(retention.due(main.database, main.settings, far)["resolved"], [])
        main.settings = replace(main.settings, retention_resolved_days=365, retention_audit_days=365)
        todo = retention.due(main.database, main.settings, far)
        self.assertEqual(todo["resolved"], [case.id])
        self.assertGreater(todo["audit"], 0)

    def test_security_overview_is_admin_only(self):
        self.assertEqual(self.client.get("/api/security").status_code, 200)
        main.authenticator.create_user("eng", "", "", "engineer", "Correct-horse-9")
        eng = TestClient(main.app)
        token = eng.post("/api/auth/login", json={"username": "eng", "password": "Correct-horse-9"}).json()["access_token"]
        self.assertEqual(eng.get("/api/security", headers={"Authorization": f"Bearer {token}"}).status_code, 403)


class ModelKeyEncryptionTests(unittest.TestCase):
    def setUp(self):
        crypto.reset_for_tests(Fernet.generate_key().decode())
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "model_settings.json"

    def tearDown(self):
        crypto.reset_for_tests(None)
        self.temp.cleanup()

    def test_keys_are_encrypted_on_disk_and_old_plain_keys_still_work(self):
        self.path.write_text(json.dumps({"keys": {"gemini": "AIza-plain-old"}}))
        store = llm.ModelStore(self.path)
        self.assertFalse(store.encrypted())
        self.assertEqual(store.load()["keys"]["gemini"], "AIza-plain-old")
        store.update(lambda data: data["keys"].__setitem__("anthropic", "sk-ant-new"))
        self.assertTrue(store.encrypted())
        raw = self.path.read_text()
        self.assertNotIn("AIza-plain-old", raw)
        self.assertNotIn("sk-ant-new", raw)
        self.assertEqual(store.load()["keys"], {"gemini": "AIza-plain-old", "anthropic": "sk-ant-new"})


if __name__ == "__main__":
    unittest.main()
