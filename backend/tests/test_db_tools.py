import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.app.db_sources import DEFAULT_REFUSED, DatabaseError, DatabaseSource, check_sql

ATTACKS = [
    "DELETE FROM widgets",
    "UPDATE widgets SET title = 'x'",
    "SELECT 1; DROP TABLE widgets",
    "select * from widgets; delete from widgets",
    "WITH x AS (DELETE FROM widgets RETURNING *) SELECT * FROM x",
    "SELECT * INTO copy_of_widgets FROM widgets",
    "SELECT * FROM widgets FOR UPDATE",
    "EXPLAIN ANALYZE DELETE FROM widgets",
    "SELECT pg_sleep(30)",
    "SELECT pg_terminate_backend(123)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT * FROM dblink('host=x', 'select 1') AS t(a int)",
    "COPY widgets TO '/tmp/x'",
    "GRANT ALL ON widgets TO public",
    "SET statement_timeout = 0",
    "SELECT * FROM users",
    "SELECT s.* FROM public.sessions s",
    "SELECT w.id FROM widgets w JOIN api_keys k ON k.owner = w.owner_id",
    "VACUUM",
    "/* hi */ DELETE FROM widgets -- bye",
    "",
]


class CheckSqlTests(unittest.TestCase):
    def test_attacks_are_refused(self):
        for sql in ATTACKS:
            with self.subTest(sql=sql), self.assertRaises(DatabaseError):
                check_sql(sql, DEFAULT_REFUSED)

    def test_reads_pass(self):
        for sql in ("SELECT id, title FROM widgets WHERE title = 'update; delete'",
                    "with recent as (select * from widgets) select count(*) from recent",
                    "EXPLAIN SELECT * FROM widgets", "select state, count(*) from pg_stat_activity group by 1;"):
            with self.subTest(sql=sql):
                self.assertTrue(check_sql(sql, DEFAULT_REFUSED))


class SqliteSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "svc.db"
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE widgets (id INTEGER, title TEXT)")
            conn.executemany("INSERT INTO widgets VALUES (?, ?)", [(i, f"w{i}") for i in range(250)])
            conn.execute("CREATE TABLE users (email TEXT)")
        self.db = DatabaseSource("sqlite", str(self.path), "web-dashboard", "audit_trail")

    def tearDown(self):
        self.tmp.cleanup()

    def test_query_limit_schema_and_services(self):
        out = self.db.query("web-dashboard", "SELECT id, title FROM widgets ORDER BY id")
        self.assertEqual((len(out["rows"]), out["note"]), (100, "Only the first 100 rows."))
        self.assertEqual([r[-1] for r in self.db.schema("web-dashboard")["rows"]], ["widgets"])  # users is hidden
        with self.assertRaises(DatabaseError):
            self.db.query("payments-api", "SELECT 1")
        with self.assertRaises(DatabaseError):
            self.db.schema("web-dashboard", "users")

    def test_the_file_is_opened_read_only(self):
        with self.assertRaises(DatabaseError):  # even if a write got past the check, the connection cannot write
            self.db._run("INSERT INTO widgets VALUES (999, 'x')")
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM widgets").fetchone()[0], 250)


@unittest.skipUnless(os.environ.get("ROOTIQ_TEST_DATABASE_URL"), "PostgreSQL tests need ROOTIQ_TEST_DATABASE_URL")
class PostgresSourceTests(unittest.TestCase):
    def test_activity_and_read_only_transaction(self):
        url = os.environ["ROOTIQ_TEST_DATABASE_URL"]
        db = DatabaseSource("postgres", url)
        activity = db.activity("any")
        self.assertIn("max_connections", activity["limits"]["columns"])
        with self.assertRaises(DatabaseError):
            db._run("CREATE TABLE should_not_exist (id int)")  # the transaction is read-only


if __name__ == "__main__":
    unittest.main()
