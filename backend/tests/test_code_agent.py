import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from backend.app.agent import llm
from backend.app.agent.guards import unverified_code_refs, without_code
from backend.app.agent.tools import InvestigationToolbox, ToolFailure, build_tools
from backend.app.code_sources import CodeError, FolderCodeSource, GitHubCodeSource, find_symbol, read_range
from backend.app.database import CaseDatabase
from backend.app.models import CaseCreate
from backend.tests.test_agent import LOGS_DIR, KeywordEmbedder, NoJira

DEMO = Path(__file__).resolve().parents[1] / "seed" / "code"


class FolderSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "svc" / "src").mkdir(parents=True)
        (root / "svc" / "src" / "app.py").write_text("def handler():\n    return pool.get(timeout=5)\n")
        (root / "svc" / ".env").write_text("DB_PASSWORD=hunter2\n")
        (root / "svc" / "server.key").write_text("-----BEGIN PRIVATE KEY-----\n")
        (root / "outside.txt").write_text("not code of svc")
        self.source = FolderCodeSource(root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_search_read_and_symbols(self):
        self.assertEqual(self.source.list_files("svc"), ["src/app.py"])  # no .env, no key
        self.assertEqual(self.source.search("svc", "timeout")[0], {"path": "src/app.py", "line": 2, "text": "return pool.get(timeout=5)"})
        self.assertIn("    2  ", read_range(self.source, "svc", "src/app.py")["code"])
        self.assertEqual(find_symbol(self.source, "svc", "handler")[0]["line"], 1)

    def test_secrets_and_escapes_are_refused(self):
        for path in (".env", "server.key", "../outside.txt", "/etc/passwd", "src/../../outside.txt"):
            with self.subTest(path=path), self.assertRaises(CodeError):
                self.source.read_lines("svc", path)
        with self.assertRaises(CodeError):
            self.source.list_files("other")


class SecretAndReadOnlyTests(unittest.TestCase):
    """A real project folder: the agent must not see secrets, and nothing it does may change a file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        files = {
            "src/app.py": 'API_KEY = "sk-live-abcdefghijklmnopqrstuv"\nkey = os.environ["API_KEY"]\ndef handler():\n    return 1\n',
            "src/supabase.ts": 'const url = import.meta.env.VITE_SUPABASE_URL;\nconst anonKey = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.c2lnbmF0dXJlMTIz";\n',
            "config/app.yaml": "db_password: hunter22xyz\nmax_size: 40\n",
            ".env.local": "VITE_SUPABASE_ANON_KEY=abc\n", "prod.env": "TOKEN=abc\n", ".envrc": "export X=1\n",
            "token.json": "{}", "gcp-service-account.json": "{}", "secrets/db.yaml": "password: x\n",
            ".git/config": "[remote]\n\turl = https://user:ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/x\n",
            ".venv/lib/site.py": "x = 1\n", "node_modules/pkg/index.js": "x\n", "deploy/server.pem": "-----BEGIN PRIVATE KEY-----\n",
        }
        for rel, text in files.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)
        self.source = FolderCodeSource(folders={"proj": str(self.root)})

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self):
        return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(self.root.rglob("*")) if p.is_file()}

    def test_secret_files_and_closed_folders_are_never_read(self):
        blocked = [".env.local", "prod.env", ".envrc", "token.json", "gcp-service-account.json", "secrets/db.yaml",
                   ".git/config", ".venv/lib/site.py", "node_modules/pkg/index.js", "deploy/server.pem"]
        for path in blocked + ["./.env.local", "./.git/config"]:
            with self.subTest(path=path), self.assertRaisesRegex(CodeError, "secrets|does not read"):
                self.source.read_lines("proj", path)
        self.assertEqual(self.source.list_files("proj"), ["config/app.yaml", "src/app.py", "src/supabase.ts"])
        for word in ("abc", "ghp_", "hunter22xyz", "sk-live", "eyJhbGci", "PRIVATE KEY"):
            with self.subTest(word=word):
                self.assertFalse([h for h in self.source.search("proj", word) if word in h["text"]])

    def test_stack_frames_in_the_logs_become_code_leads(self):
        logs = Path(self.tmp.name) / "logs"
        logs.mkdir()
        (logs / "proj.log").write_text(
            '2026-10-10T10:00:00Z ERROR [uvicorn.error] File "/srv/app/src/app.py", line 3, in handler\n'
            "2026-10-10T10:00:00Z ERROR [uvicorn.error] TypeError: bad value\n")
        db = CaseDatabase(Path(self.tmp.name) / "t.db")
        db.initialize()
        case = db.create_case(CaseCreate(title="Handler fails", customer="Acme", service="proj", reported_issue="500"))
        box = InvestigationToolbox(case, db, KeywordEmbedder(), NoJira(), logs)
        box.code = self.source
        leads = box.code_leads()
        self.assertIn("Top folders and files: src/ (2), config/.", leads)
        self.assertIn('API_KEY = "[secret]"', leads)  # the leads are masked too
        self.assertIn("The error: TypeError: bad value", leads)
        self.assertIn("src/app.py:3 in handler", leads)
        self.assertIn("    3  def handler():", leads)
        self.assertIn(("read_code", "src/app.py"), [(s.tool, s.arguments.get("path")) for s in box.steps])

    def test_a_glob_from_the_root_matches_nested_code(self):
        nested = self.root / "apps" / "web" / "src" / "lib"
        nested.mkdir(parents=True)
        (nested / "calc.ts").write_text("export function averageCalories() { return 0; }\n")
        hits = self.source.search("proj", "averageCalories", "src/*.ts")
        self.assertEqual([h["path"] for h in hits], ["apps/web/src/lib/calc.ts"])

    def test_secrets_in_code_are_masked_but_secret_reads_stay(self):
        app = self.source.read_lines("proj", "src/app.py")
        self.assertEqual(app[:2], ['API_KEY = "[secret]"', 'key = os.environ["API_KEY"]'])
        self.assertEqual(self.source.read_lines("proj", "src/supabase.ts")[1], 'const anonKey = "[secret]";')
        self.assertEqual(self.source.read_lines("proj", "config/app.yaml"), ["db_password: [secret]", "max_size: 40"])

    def test_the_code_tools_never_change_a_file(self):
        db = CaseDatabase(Path(self.tmp.name) / "t.db")
        db.initialize()
        case = db.create_case(CaseCreate(title="Handler fails", customer="Acme", service="proj", reported_issue="500"))
        box = InvestigationToolbox(case, db, KeywordEmbedder(), NoJira(), LOGS_DIR)
        box.code = self.source
        before = self.snapshot()
        box.list_code_files("proj")
        box.search_code("proj", "handler")
        box.read_code("proj", "src/app.py")
        box.find_symbol("proj", "handler")
        box.propose_code_change("proj", "config/app.yaml", 2, 2, "max_size: 40", "max_size: 80",
                                "config/app.yaml:2 caps the pool at 40.")
        self.assertEqual(len(box.proposals), 1)
        self.assertEqual(self.snapshot(), before)  # same bytes and the same modification times


class GitHubSourceTests(unittest.TestCase):
    def test_tree_contents_and_search_with_exact_lines(self):
        content = base64.b64encode(b"db:\n  pool:\n    max_size: 2\n").decode()

        def handler(request):
            path = request.url.path
            if path.endswith("/git/trees/HEAD"):
                return httpx.Response(200, json={"tree": [{"path": "config/production.yaml", "type": "blob"},
                                                          {"path": ".env", "type": "blob"}, {"path": "config", "type": "tree"}]})
            if path.endswith("/contents/config/production.yaml"):
                return httpx.Response(200, json={"type": "file", "size": 40, "content": content})
            if path == "/search/code":
                return httpx.Response(200, json={"items": [{"path": "config/production.yaml"}]})
            return httpx.Response(404)
        gh = GitHubCodeSource("t", {"web": "acme/web"}, client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.assertEqual(gh.list_files("web"), ["config/production.yaml"])
        self.assertEqual(gh.search("web", "max_size"), [{"path": "config/production.yaml", "line": 3, "text": "max_size: 2"}])
        with self.assertRaises(CodeError):
            gh.read_lines("web", ".env")


class CodeToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = CaseDatabase(Path(self.tmp.name) / "t.db")
        db.initialize()
        case = db.create_case(CaseCreate(title="Widgets time out", customer="Globex", service="web-dashboard",
                                         reported_issue="GET /api/widgets 500 timeout acquiring db connection after 5000ms"))
        self.box = InvestigationToolbox(case, db, KeywordEmbedder(), NoJira(), LOGS_DIR)
        self.box.code = FolderCodeSource(DEMO)

    def tearDown(self):
        self.tmp.cleanup()

    def propose(self, **overrides):
        args = {"service": "web-dashboard", "path": "config/production.yaml", "start_line": 9, "end_line": 9,
                "old_code": "    max_size: 2", "new_code": "    max_size: 40",
                "reason": "The pool of 2 matches config/production.yaml:9 and the 500 timeouts."} | overrides
        return self.box.propose_code_change(**args)

    def test_patch_is_a_real_diff_of_the_current_file(self):
        self.assertIn("never applies", self.propose())
        patch = self.box.proposals[0]
        self.assertEqual(patch.kind, "code_change")
        self.assertIn("-    max_size: 2\n+    max_size: 40", patch.payload["diff"])
        self.assertTrue(patch.payload["diff"].startswith("--- a/config/production.yaml\n+++ b/config/production.yaml"))

    def test_patch_tolerates_line_numbers_but_not_wrong_code(self):
        # a small model copies the numbers of read_code and gives a wrong line: the exact code still finds its place
        self.propose(start_line=99, end_line=99, old_code="    9      max_size: 2", new_code="    9      max_size: 40")
        self.assertIn("@@ -6,7 +6,7 @@", self.box.proposals[0].payload["diff"])
        self.assertIn("-    max_size: 2\n+    max_size: 40", self.box.proposals[0].payload["diff"])
        with self.assertRaisesRegex(ToolFailure, "Write the changed lines"):
            self.propose(new_code="  9      max_size: 2")

    def test_bad_patches_are_refused(self):
        cases = [{"old_code": "    max_size: 3"}, {"old_code": ""}, {"service": "payments-api"},
                 {"new_code": "    max_size: 2"}, {"reason": "It is better."}, {"path": "../../app/main.py"}]
        for overrides in cases:
            with self.subTest(overrides=overrides), self.assertRaises(ToolFailure):
                self.propose(**overrides)

    def test_code_tools_appear_only_with_a_code_source(self):
        names = {t.name for t in build_tools(self.box)}
        self.assertTrue({"search_code", "read_code", "propose_code_change"} <= names)
        self.box.code = None
        self.assertFalse({"search_code", "read_code"} & {t.name for t in build_tools(self.box)})

    def test_the_app_finds_leads_from_the_log_tags(self):
        # its own log: the demo logs change when simulate_incident runs, so a test must not depend on them
        logs = Path(self.tmp.name) / "logs"
        logs.mkdir()
        (logs / "payments-api.log").write_text(
            '2026-10-03T19:53:02Z ERROR [gateway-client] TLS handshake failed host=pay.gateway.example err="certificate verify failed"\n'
            "2026-10-03T19:53:02Z ERROR [charge] path=checkout charge failed status=502 upstream=gateway latency_ms=38\n"
            '2026-10-03T19:55:40Z ERROR [gateway-client] TLS handshake failed host=pay.gateway.example err="certificate verify failed"\n')
        case = self.box.case.model_copy(update={"service": "payments-api"})
        self.box = InvestigationToolbox(case, self.box.database, KeywordEmbedder(), NoJira(), logs)
        self.box.code = FolderCodeSource(DEMO)
        leads = self.box.code_leads()
        self.assertIn("gateway-client: src/gateway/client.py:38", leads)
        self.assertIn({"service": "payments-api", "query": "gateway-client", "by": "app"}, [s.arguments for s in self.box.steps])
        self.assertIn("charge: src/charge.py:", leads)  # a term with many hits does not stop the next terms
        self.assertIs(self.box.code_leads(), leads)  # the searches run one time only
        self.box.code = None
        self.box.__dict__.pop("_code_leads")
        self.assertEqual(self.box.code_leads(), "")

    def test_a_file_name_without_its_folder_is_a_real_reference(self):
        exists = self.box.code_line_exists
        self.assertTrue(exists("production.yaml", 9))  # config/production.yaml, as a log line or a traceback names it
        self.assertFalse(exists("nowhere.yaml", 1))
        self.assertFalse(exists("production.yaml", 999))
        self.assertEqual(unverified_code_refs("The pool is set in production.yaml:9.", exists), [])

    def test_code_reference_guard(self):
        text = "The pool is set at config/production.yaml:9; see also src/db.js:400 and src/nowhere.py:3. Version 2026.10.02:4"
        self.assertEqual(unverified_code_refs(text, self.box.code_line_exists), ["src/db.js:400", "src/nowhere.py:3"])


class KeepCodeLocalTests(unittest.TestCase):
    def test_code_is_removed_but_references_stay(self):
        text = "Found it at `config/production.yaml:9`:\n```yaml\n    max_size: 2\n```\nand `return pool.get(timeout=5000) # waits`"
        out = without_code(text)
        self.assertIn("config/production.yaml:9", out)
        self.assertNotIn("max_size", out)
        self.assertNotIn("pool.get", out)

    def test_code_level_is_local_unless_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(agent_provider="auto", gemini_api_key="g", gemini_model="gemini-x", groq_api_key="q",
                                       groq_model="gpt-oss", agent_model="qwen3:8b", code_model="qwen2.5-coder:7b",
                                       ollama_url="", data_dir=Path(tmp), logs_dir=LOGS_DIR)
            models = lambda: [m for _, m in llm.chain_for(settings, "code")]  # noqa: E731
            self.assertEqual(models(), ["qwen2.5-coder:7b", "qwen3:8b"])
            llm.model_store(settings).update(lambda d: d.__setitem__("routes", {"code": ["groq", "ollama"]}))
            self.assertEqual(models(), ["qwen2.5-coder:7b", "qwen3:8b"])  # a saved chain cannot send code to the cloud
            from backend.app import sources

            sources.source_store(settings).save_kind("code", "demo", {"cloud": "on"})
            self.assertEqual(models(), ["gpt-oss", "qwen2.5-coder:7b", "qwen3:8b"])
            settings.code_model = "qwen3:8b"  # the default: the same model as the other agents, one time only
            self.assertEqual(models(), ["gpt-oss", "qwen3:8b"])


class RescueTests(unittest.TestCase):
    def test_json_text_becomes_a_tool_call_only_for_bound_tools(self):
        from langchain_core.messages import AIMessage

        from backend.app.agent.llm import rescue_tool_calls

        text = AIMessage(content='{"name": "search_code", "arguments": {"service": "payments-api", "query": "ssl"}}')
        out = rescue_tool_calls(text, {"search_code"})
        self.assertEqual(out.tool_calls[0]["name"], "search_code")
        self.assertEqual(out.tool_calls[0]["args"], {"service": "payments-api", "query": "ssl"})
        fenced = AIMessage(content='```json\n{"name": "read_code", "arguments": "{\\"service\\": \\"x\\", \\"path\\": \\"a.py\\"}"}\n```')
        self.assertEqual(rescue_tool_calls(fenced, {"read_code"}).tool_calls[0]["args"]["path"], "a.py")
        prose = ('First I search.\n```json\n{"name": "search_code", "arguments": {"service": "p", "query": "tls"}}\n```\n'
                 'Then I read.\n```json\n{"name": "read_code", "arguments": {"service": "p", "path": "a.py"}}\n```')
        self.assertEqual([c["name"] for c in rescue_tool_calls(AIMessage(content=prose), {"search_code", "read_code"}).tool_calls],
                         ["search_code", "read_code"])
        for content, names in (('{"name": "drop_tables", "arguments": {}}', {"search_code"}),  # not a bound tool
                               ('The answer is {"name": "search_code"}', {"search_code"}),  # no block, not the whole reply
                               ("plain text", {"search_code"})):
            with self.subTest(content=content):
                self.assertEqual(rescue_tool_calls(AIMessage(content=content), names).tool_calls, [])


class FilePathGuardTests(unittest.TestCase):
    def test_invented_files_without_line_numbers_are_found(self):
        source = FolderCodeSource(DEMO)

        def exists(path, line):
            try:
                return 1 <= line <= len(source.read_lines("payments-api", path))
            except CodeError:
                return False
        text = ("The CA path is set in `config/config.yaml` and loaded in internal/tls/tls.go. The real client is "
                "src/gateway/client.py, built from the Dockerfile. See https://example.com/a/b.py for docs.")
        self.assertEqual(unverified_code_refs(text, exists), ["config/config.yaml", "internal/tls/tls.go"])


class CodeQuoteGuardTests(unittest.TestCase):
    def test_made_up_quotes_are_found_and_real_ones_pass(self):
        from backend.app.agent.guards import misquoted_code

        source = FolderCodeSource(DEMO)

        def lines_of(path):
            try:
                return source.read_lines("payments-api", path)
            except CodeError:
                return None
        # The words of a live run : the model cited real lines but invented the code on them. 4 October 2026.
        made_up = ("**Evidence:** `src/gateway/client.py:10` - `logger.error(\"TLS protected HTTP call failed: %s\", e)`\n"
                   "**Path:** `src/gateway/client.py`\n**Old Code:**\n```python\n"
                   "response = requests.post(url, headers=headers, json=data, verify=True)\n```\n"
                   "**New Code:**\n```python\nresponse = requests.post(url, verify=tls_config)\n```")
        self.assertEqual(misquoted_code(made_up, lines_of),
                         ["src/gateway/client.py:10 (the quoted code differs from the file)",
                          "src/gateway/client.py (the quoted code is not in the file)"])
        real = ("The context trusts only CA_BUNDLE: `src/gateway/client.py:27` - `return ssl.create_default_context(cafile=CA_BUNDLE)`\n"
                "In src/gateway/client.py:\n```python\nCA_BUNDLE = os.environ.get(\"CA_BUNDLE\", \"/etc/ssl/certs/ca-certificates.crt\")\n```")
        self.assertEqual(misquoted_code(real, lines_of), [])
        # words of a live run: a real quote before the reference, and prose after it, are not code quotes
        prose = ('The log error `[gateway-client] TLS handshake failed host=%s err="%s"` in `src/gateway/client.py:38` '
                 "suggests that the TLS handshake failed due to an unset `GATEWAY_HOST`.")
        self.assertEqual(misquoted_code(prose, lines_of), [])
        self.assertEqual(misquoted_code("`nowhere.py:3` - `some made up call(x)`", lines_of), [])  # unverified_code_refs reports it

    def test_listing_files_is_not_reading_code(self):
        from backend.app.agent.team import CODE_READ_TOOLS

        self.assertNotIn("list_code_files", CODE_READ_TOOLS)


class PatchBriefTests(unittest.TestCase):
    def test_the_brief_has_the_code_read_and_stays_small(self):
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        from backend.app.agent.team import PATCH_PROMPT, patch_brief

        messages = [HumanMessage("task"),
                    ToolMessage("old " + "x" * 9000, tool_call_id="1", name="read_code"),
                    ToolMessage("logs that are not code", tool_call_id="2", name="read_service_logs"),
                    ToolMessage("  42  return a / b", tool_call_id="3", name="read_code"),
                    AIMessage("ratio() divides by b without a check.")]
        brief = patch_brief("Fix the crash", messages, limit=5000)
        self.assertIn("  42  return a / b", brief)
        self.assertIn("ratio() divides by b", brief)
        self.assertNotIn("logs that are not code", brief)
        self.assertNotIn("x" * 9000, brief)  # over the limit: left out
        self.assertTrue(brief.endswith(PATCH_PROMPT))


if __name__ == "__main__":
    unittest.main()
