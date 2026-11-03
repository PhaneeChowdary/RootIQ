import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
from cryptography.fernet import Fernet

from backend.app import crypto, sources
from backend.app.agent.tools import InvestigationToolbox, build_tools
from backend.app.database import CaseDatabase
from backend.app.models import CaseCreate
from backend.tests.test_agent import LOGS_DIR, KeywordEmbedder, NoJira


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


NOW_NS = int(datetime(2026, 10, 3, 19, 53, 2, tzinfo=UTC).timestamp() * 1e9)


def loki_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/label/service/values"):
        return httpx.Response(200, json={"status": "success", "data": ["payments-api", "web-dashboard"]})
    query = request.url.params["query"]
    if "payments-api" in query:
        return httpx.Response(200, json={"data": {"result": [
            {"stream": {"service": "payments-api"}, "values": [
                [str(NOW_NS + 2_000_000_000), "ERROR [charge] failed status=502"],
                [str(NOW_NS), "2026-10-03T19:53:02Z WARN [gateway-client] TLS handshake failed"]]}]}})
    return httpx.Response(200, json={"data": {"result": []}})


class LokiTests(unittest.TestCase):
    def setUp(self):
        self.loki = sources.LokiLogSource("http://loki", client=client(loki_handler))

    def test_lines_are_oldest_first_and_start_with_a_time(self):
        lines = self.loki.lines("payments-api")
        self.assertEqual(lines[0], "2026-10-03T19:53:02Z WARN [gateway-client] TLS handshake failed")
        self.assertEqual(lines[1], "2026-10-03T19:53:04Z ERROR [charge] failed status=502")
        self.assertEqual(self.loki.services(), ["payments-api", "web-dashboard"])

    def test_unknown_service_lists_the_known_ones(self):
        with self.assertRaisesRegex(sources.SourceError, "payments-api, web-dashboard"):
            self.loki.lines("billing")

    def test_refused_credentials_are_explained(self):
        loki = sources.LokiLogSource("http://loki", client=client(lambda r: httpx.Response(401)))
        with self.assertRaisesRegex(sources.SourceError, "refused the credentials"):
            loki.services()


class ElasticTests(unittest.TestCase):
    def test_lines_from_nested_documents_oldest_first(self):
        def handler(request):
            body = json.loads(request.content)
            if "aggs" in body:
                return httpx.Response(200, json={"aggregations": {"s": {"buckets": [{"key": "payments-api"}]}}})
            return httpx.Response(200, json={"hits": {"hits": [
                {"_source": {"@timestamp": "2026-10-03T19:53:04Z", "log": {"level": "error"}, "message": "charge failed"}},
                {"_source": {"@timestamp": "2026-10-03T19:53:02Z", "log": {"level": "warn"}, "message": "TLS handshake failed"}}]}})
        es = sources.ElasticLogSource("http://es", client=client(handler))
        self.assertEqual(es.lines("payments-api"), ["2026-10-03T19:53:02Z WARN TLS handshake failed",
                                                    "2026-10-03T19:53:04Z ERROR charge failed"])
        self.assertEqual(es.services(), ["payments-api"])


class CloudWatchTests(unittest.TestCase):
    def test_log_groups_become_services_and_events_become_lines(self):
        class FakeLogs:
            def describe_log_groups(self, logGroupNamePrefix):
                return {"logGroups": [{"logGroupName": "/app/payments-api"}]}

            def filter_log_events(self, **args):
                assert args["logGroupName"] == "/app/payments-api"
                return {"events": [{"timestamp": 1791057184000, "message": "ERROR charge failed\n"}]}
        cw = sources.CloudWatchLogSource("us-east-1", client=FakeLogs())
        self.assertEqual(cw.services(), ["payments-api"])
        self.assertEqual(cw.lines("payments-api"), ["2026-10-03T19:53:04Z ERROR charge failed"])


class PrometheusTests(unittest.TestCase):
    def test_query_summary_and_bad_query(self):
        def handler(request):
            if request.url.path.endswith("/label/__name__/values"):
                return httpx.Response(200, json={"data": ["http_errors_total", "up"]})
            if request.url.params["query"] == "bad(":
                return httpx.Response(400, headers={"content-type": "application/json"}, json={"error": "parse error"})
            return httpx.Response(200, headers={"content-type": "application/json"}, json={"data": {"result": [
                {"metric": {"service": "payments-api"}, "values": [[1791057000, "0"], [1791057060, "8"], [1791057120, "3"]]}]}})
        prom = sources.PrometheusMetricSource("http://prom", client=client(handler))
        self.assertEqual(prom.list_metrics("error"), ["http_errors_total"])
        series = prom.query("rate(http_errors_total[5m])", 60)[0]
        self.assertEqual((series["first"], series["last"], series["max"], series["max_at"]), (0, 3, 8, "2026-10-03T19:51:00Z"))
        with self.assertRaisesRegex(sources.SourceError, "parse error"):
            prom.query("bad(")


class GitHubTests(unittest.TestCase):
    def test_commits_and_releases_for_a_service(self):
        def handler(request):
            if request.url.path.endswith("/commits"):
                return httpx.Response(200, json=[{"sha": "abc1234567", "commit": {"message": "Use default CA bundle\nlong text",
                    "author": {"name": "Ann", "date": "2026-10-03T19:50:00Z"}, "committer": {"date": "2026-10-03T19:50:00Z"}}}])
            return httpx.Response(200, json=[{"tag_name": "v2026.10.03-1", "name": "Release", "published_at": "2099-01-01T00:00:00Z"}])
        gh = sources.GitHubChangeSource("t", sources.parse_repos("payments-api = acme/payments\n* = acme/mono"), client=client(handler))
        lines = gh.recent_changes("payments-api")
        self.assertEqual(lines[0], "2026-10-03T19:50:00Z commit abc1234 by Ann: Use default CA bundle")
        self.assertIn("release v2026.10.03-1", lines[1])
        self.assertEqual(gh.repo_for("billing"), "acme/mono")


class SecretTests(unittest.TestCase):
    def setUp(self):
        crypto.reset_for_tests(Fernet.generate_key().decode())

    def tearDown(self):
        crypto.reset_for_tests(None)

    def test_round_trip_and_old_plain_values(self):
        token = crypto.encrypt("s3cret")
        self.assertTrue(token.startswith("enc:v1:"))
        self.assertEqual(crypto.decrypt(token), "s3cret")
        self.assertEqual(crypto.decrypt("plain-old-value"), "plain-old-value")

    def test_store_keeps_secrets_encrypted_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = sources.SourceStore(Path(tmp) / "data_sources.json")
            store.save_kind("logs", "loki", {"url": "http://loki", "password": "hunter2"})
            self.assertNotIn("hunter2", store.path.read_text())
            self.assertEqual(store.get("logs"), ("loki", {"url": "http://loki", "password": "hunter2"}))
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)


class ToolboxWithSourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = CaseDatabase(Path(self.tmp.name) / "t.db")
        db.initialize()
        case = db.create_case(CaseCreate(title="Card payments fail", customer="Acme", service="payments-api", reported_issue="502"))
        self.box = InvestigationToolbox(case, db, KeywordEmbedder(), NoJira(), sources.LokiLogSource("http://loki", client=client(loki_handler)),
                                        metrics=SimpleNamespace(list_metrics=lambda c="": ["up"], query=lambda q, m=60: []))

    def tearDown(self):
        self.tmp.cleanup()

    def test_logs_come_from_the_source_and_metric_tools_appear(self):
        result = json.loads(self.box.run("read_service_logs", self.box.read_service_logs, "evidence", service="payments-api", level="ERROR"))
        self.assertEqual(result["matched_lines"], 1)
        names = {t.name for t in build_tools(self.box)}
        self.assertTrue({"list_metrics", "query_metrics"} <= names)
        self.assertNotIn("recent_changes", names)  # no change source

    def test_a_folder_still_works(self):
        box = InvestigationToolbox(self.box.case, self.box.database, KeywordEmbedder(), NoJira(), LOGS_DIR)
        self.assertIsInstance(box.logs, sources.FileLogSource)


if __name__ == "__main__":
    unittest.main()
