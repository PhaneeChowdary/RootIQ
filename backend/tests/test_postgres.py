"""The same agent runner tests on PostgreSQL, and tests for the parts that differ between the databases.

They run when ROOTIQ_TEST_DATABASE_URL is set, for example:
    ROOTIQ_TEST_DATABASE_URL=postgresql://rootiq@127.0.0.1:5433/rootiq_test python -m pytest backend/tests
The tests delete everything in that database. Never point it at a real database.
"""
import os
import threading
import unittest

from backend.app import db, migrations
from backend.app.agent.store import AgentRunStore
from backend.app.database import CaseDatabase
from backend.app.models import CaseCreate
from backend.app.services.alerts import Alert, AlertLinks
from backend.app.services.auth import LocalAuthenticator, LoginLocked
from backend.tests import test_runner as runner_tests

URL = os.environ.get("ROOTIQ_TEST_DATABASE_URL", "")
needs_postgres = unittest.skipUnless(URL, "set ROOTIQ_TEST_DATABASE_URL to run the PostgreSQL tests")


class TranslationTests(unittest.TestCase):
    def test_placeholders_and_percent_signs(self):
        self.assertEqual(db.to_postgres("SELECT * FROM t WHERE a = ? AND b LIKE ?"), "SELECT * FROM t WHERE a = %s AND b LIKE %s")
        self.assertEqual(db.to_postgres("SELECT '?', 'a%b', x % 2 FROM t WHERE c = ?"), "SELECT '?', 'a%%b', x %% 2 FROM t WHERE c = %s")
        self.assertTrue(db.is_postgres("postgresql://u@h/d"))
        self.assertFalse(db.is_postgres("/tmp/a.db"))


@needs_postgres
class PostgresRunnerTests(runner_tests.RunnerTests):
    DATABASE_URL = URL


@needs_postgres
class PostgresQueueTests(runner_tests.QueueAndRecoveryTests):
    DATABASE_URL = URL


@needs_postgres
class PostgresCancellationTests(runner_tests.CancellationTests):
    DATABASE_URL = URL


@needs_postgres
class PostgresStopTests(runner_tests.StopAndRestartTests):
    DATABASE_URL = URL


@needs_postgres
class PostgresLessonTests(runner_tests.LessonTests):
    DATABASE_URL = URL


@needs_postgres
class PostgresStorageTests(unittest.TestCase):
    def setUp(self):
        runner_tests.reset_postgres(URL)
        self.cases = CaseDatabase(URL)
        self.cases.initialize()

    def test_migrations_are_recorded_once(self):
        self.assertEqual(migrations.applied(db.get(URL)), [v for v, _, _ in migrations.MIGRATIONS])
        self.assertEqual(migrations.upgrade(URL), [])

    def test_case_round_trip_search_and_bin(self):
        case = self.cases.create_case(CaseCreate(title="Checkout FAILS", customer="Acme", service="payments-api", reported_issue="502 at 100%"))
        self.assertEqual([c.id for c in self.cases.search_cases("checkout fails")], [case.id])  # ignores case
        self.cases.move_to_bin(case.id, "admin")
        binned = self.cases.get_case(case.id, include_deleted=True)
        self.cases._write_case(binned.model_copy(update={"title": "Checkout fails (edited)"}))  # stays in the bin
        self.assertEqual([c.id for c, _, _ in self.cases.list_bin()], [case.id])
        self.cases.delete_case(case.id)
        self.assertIsNone(self.cases.get_case(case.id, include_deleted=True))

    def test_users_sessions_and_lock(self):
        auth = LocalAuthenticator("admin", "pw", db_path=URL, max_failures=2)
        auth.create_user("Ann", "Ann", "ann@acme.io", "engineer", "Correct-horse-9")
        session = auth.login("ann", "Correct-horse-9")  # names ignore case
        self.assertEqual(auth.authenticate(f"Bearer {session.access_token}").role, "engineer")
        auth.login("ann", "x")
        auth.login("ann", "x")
        with self.assertRaises(LoginLocked):
            auth.login("ann", "Correct-horse-9")

    def test_alert_links(self):
        links = AlertLinks(URL)
        links.initialize()
        alert = Alert(fingerprint="f1", status="firing", name="Errors", service="payments-api", severity="high",
                      summary="s", description="d", starts_at="", ends_at="", url="")
        links.link(alert, "case-1")
        links.link(alert, "case-2")
        self.assertEqual(links.case_for_fingerprint("f1"), "case-2")

    def test_eight_workers_never_take_the_same_job(self):
        store = AgentRunStore(URL)
        for i in range(120):
            store.enqueue_job("investigate", f"run-{i}")
        taken, lock = [], threading.Lock()

        def worker(n):
            while (job := store.claim_job(f"w{n}")) is not None:
                with lock:
                    taken.append(job.target)
                store.finish_job(job.id)
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(taken), sorted(f"run-{i}" for i in range(120)))


if __name__ == "__main__":
    unittest.main()
