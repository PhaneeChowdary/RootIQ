"""Tools the investigation agent can call.

Read tools look up incident memory, the case store, service logs and Jira.
Propose tools never act on anything: they record an action for a person to approve.
Every call is timed and recorded as an AgentStep, including calls that fail.
"""
import json
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from ..models import AgentStep, ProposedAction, SupportCase
from ..services.memory import rank_memory
from ..sources import FileLogSource, SourceError
from .plaintext import plain_text

ALERT_CUSTOMER = "Grafana alert"  # the customer name of the cases that Grafana alerts open, see services/alerts.py

MAX_LOG_LINES = 40
LOG_CONTEXT_LINES = 2
RESULT_PREVIEW_CHARS = 2000
EXTERNAL_TOOLS = {"search_jira", "list_metrics", "query_metrics", "recent_changes", "db_activity", "db_schema", "db_query"}  # remote integrations get a circuit breaker
EXTERNAL_FAILURE_LIMIT = 2
# Log tags that every service writes; they do not lead to the code of the failure
LEAD_SKIP = {"deploy", "config", "ops", "flags", "alert", "info", "warn", "warning", "error", "debug", "main", "app"}
_LEAD_TAG = re.compile(r"\[([A-Za-z][\w.-]{2,40})\]")
_LEAD_KEY = re.compile(r"\b([a-z]+_[a-z_]+|[a-z]+[A-Z][A-Za-z]+|[a-z][\w-]*\.[a-z][\w.-]+)=")  # max_size=, averageCalories=, db.pool.max_size=
_NUMBERED = re.compile(r"^ *\d+  (.*)$")  # a line as read_code shows it: "  255  export function ..."


def _without_line_numbers(code: str) -> list[str]:
    """The lines of code from a model. Small models often copy the line numbers of read_code; the app removes them
    when every line has one. Blank lines at the start and the end are dropped."""
    lines = code.strip("\n").splitlines()
    filled = [line for line in lines if line.strip()]
    if filled and all(_NUMBERED.match(line) for line in filled):
        lines = [m.group(1) if (m := _NUMBERED.match(line)) else "" for line in lines]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _locate(lines: list[str], block: list[str], near: int, span: int = 40) -> int | None:
    """The first line of `block` in the file: at `near` if it is there, else the one place within `span` lines of
    it, else the one place in the file. The code must still match exactly; only the line number may be off."""
    size = len(block)
    starts = [i + 1 for i in range(len(lines) - size + 1) if [x.rstrip() for x in lines[i:i + size]] == block]
    if near in starts:
        return near
    close = [s for s in starts if abs(s - near) <= span]
    if len(close) == 1:
        return close[0]
    return starts[0] if len(starts) == 1 else None


_EXCEPTION = re.compile(r"\b[A-Z]\w*(?:Error|Exception)\b: .+$")  # TypeError: '<' not supported between ...
# Stack frames: Python 'File "app/engine.py", line 303, in _lab' and JavaScript 'at f '. Src/lib/a.ts:12:5.
_PY_FRAME = re.compile(r'File "([^"]+)", line (\d+)(?:, in ([\w<>.]+))?')
_JS_FRAME = re.compile(r"([\w./@-]+\.(?:ts|tsx|js|jsx|mjs|py|go|java|rb|rs|kt)):(\d+)(?::\d+)?")
_LEAD_HOST = re.compile(r"\bhost=([a-z][\w-]*(?:\.[\w-]+)+)")


class ToolFailure(RuntimeError):
    pass


class InvestigationToolbox:
    def __init__(
        self,
        case: SupportCase | None,
        database,
        embedder,
        jira,
        logs,
        on_step: Callable[[AgentStep], None] | None = None,
        metrics=None,
        changes=None,
    ) -> None:
        """logs: a log source of sources.py, or a folder of demo log files. metrics and changes are optional sources."""
        self.case = case
        self.database = database
        self.embedder = embedder
        self.jira = jira
        self.logs = FileLogSource(logs) if isinstance(logs, (str, Path)) else logs
        self.metrics = metrics
        self.changes = changes
        self.on_step = on_step
        self.steps: list[AgentStep] = []
        self.evidence: list[str] = []  # the full results of the successful calls; the steps keep a short preview
        self.proposals: list[ProposedAction] = []
        self._lock = threading.Lock()
        self._external_failures: dict[str, int] = {}
        self.remediation = None  # the policy and the RemediationLog; the runner sets them when remediation is on
        self.code = None  # the code source, see code_sources.py; the runner sets it when a code source is on
        self.code_cloud = False  # True: code may go to cloud models; False: code stays with the local model
        self.service_db = None  # the read-only database of the service, see db_sources.py; the runner sets it when one is on

    # Read tools

    def search_similar_incidents(self, query: str) -> list[dict]:
        matches = rank_memory(self.database, self.embedder, query, exclude_case_id=self.case.id if self.case else None)
        return [
            {
                "incident_id": m.case.id[:8],
                "title": m.case.title,
                "service": m.case.service,
                "similarity": m.score,
                "cause": m.case.analysis.likely_cause,
                "resolution": m.case.resolution,
            }
            for m in matches
        ]

    def get_incident(self, incident_id: str) -> dict:
        case = self._find(incident_id)
        return {
            "incident_id": case.id[:8],
            "title": case.title,
            "service": case.service,
            "status": case.status,
            "severity": case.severity,
            "reported_issue": case.reported_issue,
            "cause": case.analysis.likely_cause,
            "workaround": case.analysis.workaround,
            "resolution": case.resolution,
            "preventive_actions": case.preventive_actions,
        }

    def list_service_incidents(self, service: str) -> list[dict]:
        cases = [c for c in self.database.list_cases() if c.service.lower() == service.strip().lower()]
        return [
            {
                "incident_id": c.id[:8],
                "title": c.title,
                "status": c.status,
                "created_at": c.created_at.date().isoformat(),
                "cause": c.analysis.likely_cause,
            }
            for c in cases
            if self.case is None or c.id != self.case.id
        ][:10]

    def read_service_logs(self, service: str, contains: str = "", level: str = "") -> dict:
        try:
            all_lines = self.logs.lines(service.strip().lower())
        except SourceError as exc:
            raise ToolFailure(str(exc)) from exc
        needle = contains.strip().lower()
        tag = f" {level.strip().upper()} " if level else ""
        hits = [i for i, line in enumerate(all_lines) if tag in line and needle in line.lower()]
        if contains or level:
            # as grep -C does: show the lines around each match, so that the events before it are visible
            keep = sorted({j for i in hits for j in range(i - LOG_CONTEXT_LINES, i + LOG_CONTEXT_LINES + 1)
                           if 0 <= j < len(all_lines)})
            lines = [(">> " if j in hits else "   ") + all_lines[j] for j in keep]
        else:
            lines = all_lines
        result: dict = {"service": service, "matched_lines": len(hits), "lines": lines[-MAX_LOG_LINES:]}
        if contains or level:
            result["note"] = "Lines starting with >> match the filter; the others are surrounding context."
        if not hits and (contains or level):
            # Give real lines, not an empty result: with nothing to read, a small model can invent log lines.
            result["hint"] = ("No lines matched the filter. Filters are plain substrings: retry with a short term "
                              "(an error code, a component). The most recent lines of the log, with no filter, follow.")
            result["recent_lines"] = all_lines[-MAX_LOG_LINES:]
        return result

    def list_metrics(self, contains: str = "") -> list[str]:
        try:
            names = self.metrics.list_metrics(contains)
        except SourceError as exc:
            raise ToolFailure(str(exc)) from exc
        return names[:60] if names else [f"No metric names contain '{contains}'. Try a shorter word, or no word."]

    def query_metrics(self, query: str, minutes: int = 60) -> list[dict] | str:
        try:
            series = self.metrics.query(query, minutes)
        except SourceError as exc:
            raise ToolFailure(str(exc)) from exc
        return series or "The query returned no data in that time. Check the metric name with list_metrics."

    def recent_changes(self, service: str, hours: int = 48) -> list[str] | str:
        try:
            changes = self.changes.recent_changes(service.strip().lower(), hours)
        except SourceError as exc:
            raise ToolFailure(str(exc)) from exc
        return changes or f"No commits or releases for {service} in the last {hours} hours."

    def search_jira(self, text: str) -> list[dict]:
        if not self.jira.configured:
            raise ToolFailure("Jira is not configured for this deployment.")
        safe = text.replace('"', " ").replace("\\", " ").strip()
        issues = self.jira.search_issues(f'text ~ "{safe}" ORDER BY created DESC', max_results=5)
        return [
            {"key": i["key"], "summary": i["summary"], "status": i["status"], "created": i["created"]}
            for i in issues
        ]

    # Propose tools: recorded for human approval, never executed here

    def _replace_proposal(self, kind: str) -> bool:
        """Keep one proposal of each kind: a second call replaces the first, so the engineer sees no copies."""
        before = len(self.proposals)
        self.proposals[:] = [p for p in self.proposals if p.kind != kind]
        return len(self.proposals) < before

    def propose_customer_update(self, subject: str, body: str) -> str:
        subject, body = plain_text(subject), plain_text(body)  # emails do not show Markdown
        replaced = self._replace_proposal("customer_update")
        self.proposals.append(
            ProposedAction(
                kind="customer_update",
                summary=f"Email {self.case.customer}: {subject}",
                payload={"subject": subject, "body": body},
            )
        )
        return ("Recorded" + (" (it replaces your earlier update)" if replaced else "") +
                ". A support engineer must approve this update before it is sent. Do not call this tool again.")

    def propose_jira_comment(self, comment: str) -> str:
        issue_key = self.case.jira_source_key or self.case.jira_issue_key or ""
        comment = plain_text(comment)  # Jira comments do not show Markdown
        replaced = self._replace_proposal("jira_comment")
        self.proposals.append(
            ProposedAction(
                kind="jira_comment",
                summary=f"Comment on {issue_key or 'the tracking ticket'}",
                payload={"issue_key": issue_key, "comment": comment},
            )
        )
        return ("Recorded" + (" (it replaces your earlier comment)" if replaced else "") +
                ". A support engineer must approve this comment before it is posted. Do not call this tool again.")

    # Database tools: read-only, see db_sources.py

    def _db(self, service: str):
        if self.service_db is None:
            raise ToolFailure("No database source is on. Do not call database tools.")
        return self.service_db

    def db_activity(self, service: str) -> dict:
        from ..db_sources import DatabaseError

        try:
            return self._db(service).activity(service.strip())
        except DatabaseError as exc:
            raise ToolFailure(str(exc)) from exc

    def db_schema(self, service: str, table: str = "") -> dict:
        from ..db_sources import DatabaseError

        try:
            return self._db(service).schema(service.strip(), table.strip())
        except DatabaseError as exc:
            raise ToolFailure(str(exc)) from exc

    def db_query(self, service: str, sql: str) -> dict:
        from ..db_sources import DatabaseError

        try:
            return self._db(service).query(service.strip(), sql)
        except DatabaseError as exc:
            raise ToolFailure(str(exc)) from exc

    # Code tools: read-only, see code_sources.py

    def _code(self):
        if self.code is None:
            raise ToolFailure("No code source is on. Do not call code tools.")
        return self.code

    def list_code_files(self, service: str, prefix: str = "") -> list[str] | str:
        from ..code_sources import CodeError

        try:
            files = self._code().list_files(service.strip(), prefix)
        except CodeError as exc:
            raise ToolFailure(str(exc)) from exc
        return files or f"No files under '{prefix}'."

    def search_code(self, service: str, query: str, path_glob: str = "") -> list[dict] | str:
        from ..code_sources import CodeError

        try:
            hits = self._code().search(service.strip(), query, path_glob)
        except CodeError as exc:
            raise ToolFailure(str(exc)) from exc
        return hits or f"Nothing matches '{query}'. Try a shorter word, for example a log tag or a function name."

    def read_code(self, service: str, path: str, start: int = 1, end: int = 0) -> dict:
        from ..code_sources import CodeError, read_range

        try:
            return read_range(self._code(), service.strip(), path, start, end)
        except CodeError as exc:
            raise ToolFailure(str(exc)) from exc

    def find_symbol(self, service: str, name: str) -> list[dict] | str:
        from ..code_sources import CodeError, find_symbol

        try:
            hits = find_symbol(self._code(), service.strip(), name.strip())
        except CodeError as exc:
            raise ToolFailure(str(exc)) from exc
        return hits or f"No definition of {name}."

    def code_leads(self, max_terms: int = 5, max_hits: int = 4) -> str:
        """Search hits for the log tags and keys of the ERROR lines of the service. The app finds them before the Code
        agent starts. A small model often searches for the wrong words; real hits start it at the right files.
        Each search is a normal step of the run, with the agent "code", so the trace shows it."""
        from collections import Counter

        from ..code_sources import SYMBOL, CodeError

        if self.code is None or self.case is None:
            return ""
        if (cached := self.__dict__.get("_code_leads")) is not None:
            return cached
        service = self.case.service.strip()
        try:
            errors = [line for line in self.logs.lines(service.lower()) if " ERROR " in line or " WARN " in line][-200:]
        except SourceError:
            errors = []
        terms: Counter[str] = Counter()
        for line in errors:
            weight = 2 if " ERROR " in line else 1  # tags such as [gateway-client] and keys such as num_predict= count the same
            terms.update({t: 2 * weight for t in _LEAD_TAG.findall(line) if t.lower() not in LEAD_SKIP})
            terms.update({t: 2 * weight for t in _LEAD_KEY.findall(line) + _LEAD_HOST.findall(line)})
        parts = [self._code_layout(service), self._stack_frames(service, errors)]
        found: list[str] = []
        searched = 0
        for term, _ in terms.most_common(max_terms * 2):
            if searched >= max_terms:
                break
            started = time.perf_counter()
            try:
                hits = self.code.search(service, term)
            except CodeError:
                continue
            code_hits = [h for h in hits if not h["path"].lower().endswith((".md", ".txt", ".rst")) and "makefile" not in h["path"].lower()]
            defines = re.compile(SYMBOL.format(name=re.escape(term)) + r"|^\s*(?:export\s+)?(?:async\s+)?(?:function|def|class|const|let|var)\s+" + re.escape(term) + r"\b")
            hits = sorted(code_hits or hits, key=lambda h: not defines.search(h["text"]))[:max_hits]  # definitions first
            if hits:
                searched += 1
                result = json.dumps(hits)
                with self._lock:
                    self.evidence.append(result)
                self._record("code", "search_code", {"service": service, "query": term, "by": "app"}, result, True, started)
                found += [f"- {term}: {h['path']}:{h['line']}  {h['text']}" for h in hits]
        if found:
            parts.append("Search hits that the app found for the log tags and keys of this service (real lines; read the "
                         "files around them with read_code before you cite them):\n" + "\n".join(found))
        self.__dict__["_code_leads"] = "\n\n".join(p for p in parts if p)
        return self.__dict__["_code_leads"]

    def _code_layout(self, service: str) -> str:
        """The top folders of the service's code, so the agent does not guess a layout such as src/main/java."""
        from collections import Counter

        from ..code_sources import CodeError

        try:
            files = self.code.list_files(service)
        except CodeError:
            return ""
        tops = Counter(f.split("/")[0] + ("/" if "/" in f else "") for f in files)
        return f"The code of {service} has {len(files)} readable files. Top folders and files: " + ", ".join(
            f"{name} ({n})" if n > 1 else name for name, n in tops.most_common(12)) + "."

    def _stack_frames(self, service: str, lines: list[str], max_frames: int = 6) -> str:
        """The code at each stack frame of the error lines: the app reads it for the agent, as a normal step.
        A frame path from the server, such as /app/backend/app/engine.py, matches the file that ends with it."""
        from ..code_sources import CodeError, read_range

        frames: list[tuple[str, int, str]] = []
        for line in lines:
            for path, number, func in _PY_FRAME.findall(line):
                frames.append((path, int(number), func))
            if not _PY_FRAME.search(line):
                frames += [(path, int(number), "") for path, number in _JS_FRAME.findall(line)]
        if not frames:
            return ""
        try:
            files = self.code.list_files(service)
        except CodeError:
            return ""
        out, seen = [], set()
        for path, number, func in reversed(frames):  # the deepest frame first: there the error happened
            clean = path.replace("\\", "/").lstrip("./")
            match = max((f for f in files if clean == f or clean.endswith("/" + f) or f.endswith("/" + clean)), key=len, default=None)
            if not match or (match, number) in seen:
                continue
            seen.add((match, number))
            started = time.perf_counter()
            try:
                code = read_range(self.code, service, match, max(1, number - 6), number + 6)
            except CodeError:
                continue
            result = json.dumps(code)
            with self._lock:
                self.evidence.append(result)
            self._record("code", "read_code", {"service": service, "path": match, "start": max(1, number - 6),
                                               "end": number + 6, "by": "app"}, result, True, started)
            out.append(f"{match}:{number}" + (f" in {func}" if func else "") + f"\n{code['code']}")
            if len(out) >= max_frames:
                break
        if not out:
            return ""
        raised = list(dict.fromkeys(m.group(0).strip() for line in lines if (m := _EXCEPTION.search(line))))[-2:]
        error = ("The error: " + " | ".join(raised) + "\n") if raised else ""
        return (f"{error}The error lines have a stack trace. The app read the code at each frame, the deepest frame first "
                "(real lines with their numbers). The error happens in the first frame; its cause is often in a caller, "
                "where the bad value comes from:\n\n" + "\n\n".join(out))

    def code_line_exists(self, path: str, line: int) -> bool:
        """For the guard: does `path` exist in the case's service code, with at least `line` lines?"""
        from ..code_sources import CodeError

        if self.code is None:
            return True
        for match in self._code_paths(path):
            try:
                if 1 <= line <= len(self.code.read_lines(self.case.service, match)):
                    return True
            except CodeError:
                continue
        return False

    def _code_paths(self, path: str) -> list[str]:
        """The files that a reference names: the path itself, or the files that end with it. Agents often write
        engine.py for backend/app/engine.py, as a traceback or a log line does. A name that no file has stays unknown."""
        from ..code_sources import CodeError

        path = path.strip().strip("`'\"").lstrip("./")
        cache = self.__dict__.setdefault("_code_files", {})
        if None not in cache:
            try:
                cache[None] = self.code.list_files(self.case.service)
            except CodeError:
                cache[None] = []
        files = cache[None]
        if path in files:
            return [path]
        return [f for f in files if f.endswith("/" + path) or path.endswith("/" + f)] or [path]

    def code_lines(self, path: str) -> list[str] | None:
        """For the guard: the lines of `path` in the case's service code, or None if there is no such file."""
        from ..code_sources import CodeError

        if self.code is None:
            return None
        cache = self.__dict__.setdefault("_code_lines", {})  # the guards ask for the same files many times
        if path not in cache:
            matches = self._code_paths(path)
            try:
                cache[path] = self.code.read_lines(self.case.service, matches[0]) if len(matches) == 1 else None
            except CodeError:
                cache[path] = None
        return cache[path]

    def propose_code_change(self, service: str, path: str, start_line: int, end_line: int, old_code: str,
                            new_code: str, reason: str) -> str:
        """A patch for a person to review and apply. The app never applies it. Checked in code: the lines exist,
        old_code is exactly what is there now, the change is small, and the reason cites real evidence."""
        import difflib

        from ..code_sources import CodeError
        from .guards import misquoted_lines, unverified_log_times

        if service.strip().lower() != self.case.service.strip().lower():
            raise ToolFailure(f"A code change can be only for the service of this case ({self.case.service}).")
        try:
            lines = self._code().read_lines(self.case.service, path)
        except CodeError as exc:
            raise ToolFailure(str(exc)) from exc
        given = [line.rstrip() for line in _without_line_numbers(old_code)]
        if not given:
            raise ToolFailure("old_code is empty. Copy the lines to replace from read_code, without the line numbers.")
        near = int(start_line)
        start_line = _locate(lines, given, near)
        if start_line is None:
            raise ToolFailure(f"old_code is not in {path} near line {near}. Read the file again "
                              "with read_code and copy the lines exactly, without the line numbers.")
        end_line = start_line + len(given) - 1
        current = [line.rstrip() for line in lines[start_line - 1:end_line]]
        replacement = _without_line_numbers(new_code)
        if len(current) + len(replacement) > 80:
            raise ToolFailure("The change is too large. Propose the smallest change that fixes the cause.")
        if current == [line.rstrip() for line in replacement]:
            raise ToolFailure("new_code is the same as old_code. Write the changed lines in new_code.")
        known = "\n".join([*self.evidence, self.case.reported_issue, self.case.logs or ""])
        if not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}|[\w./-]+:\d+", reason):
            raise ToolFailure("The reason must cite evidence: a log line with its time, or a file:line.")
        if bad := unverified_log_times(reason, known) + misquoted_lines(reason, known):
            raise ToolFailure(f"The reason cites {', '.join(bad)}, which no tool returned.")
        if sum(p.kind == "code_change" for p in self.proposals) >= 2:
            raise ToolFailure("You already proposed two code changes. Stop.")
        after = lines[:start_line - 1] + replacement + lines[end_line:]
        diff = "\n".join(difflib.unified_diff(lines, after, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="", n=3))
        self.proposals.append(ProposedAction(
            kind="code_change", summary=f"Change {path} (lines {start_line}-{end_line})",
            payload={"service": self.case.service, "path": path, "start_line": str(start_line), "end_line": str(end_line),
                     "diff": diff, "reason": plain_text(reason)},
            risk="medium",
        ))
        return "Recorded as a patch for a person to review. The app never applies it. Stop unless a second file must change."

    def propose_remediation(self, action: str, service: str, reason: str, verify: str, rollback_plan: str,
                            params: dict) -> str:
        """A fix from the catalog of automation.py. The code checks the catalog, the service of the case, the
        parameters, the evidence for versions and flags, a cited reason, the kill switch and the rate limits."""
        from .. import automation
        from .guards import misquoted_lines, unverified_ids, unverified_log_times

        if self.remediation is None:
            raise ToolFailure("Remediation is off. Do not call this tool.")
        policy, log = self.remediation
        evidence = "\n".join(self.evidence)
        try:
            automation.check_limits(policy, log, self.case.id)
            clean = automation.validate(action, service, params, self.case.service, evidence)
        except automation.AutomationError as exc:
            raise ToolFailure(str(exc)) from exc
        reason = plain_text(reason)
        known = "\n".join([evidence, self.case.title, self.case.reported_issue, self.case.logs or ""])
        cited = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", reason)
        if not cited:
            raise ToolFailure("The reason must quote the evidence: a log line or a change with its exact time.")
        if bad := unverified_log_times(reason, known) + unverified_ids(reason, known) + misquoted_lines(reason, known):
            raise ToolFailure(f"The reason cites {', '.join(bad)}, which no tool returned. Cite only real evidence.")
        if sum(p.kind == "remediation" for p in self.proposals) >= 2:
            raise ToolFailure("You already proposed two remediations. Stop.")
        if any(p.kind == "remediation" and p.payload.get("action") == action for p in self.proposals):
            raise ToolFailure(f"You already proposed {action}. Stop.")
        self.proposals.append(ProposedAction(
            kind="remediation",
            summary=automation.describe(action, self.case.service, clean),
            payload={"action": action, "service": self.case.service, "params": json.dumps(clean, sort_keys=True),
                     "reason": reason, "verify": plain_text(verify), "rollback_plan": plain_text(rollback_plan)},
            risk=automation.RISK[action],
        ))
        return ("Recorded. A person must approve this change before it runs, and the verification agent checks the "
                "result after it runs. Propose a second remediation only if it is needed with the first one.")

    @property
    def has_customer(self) -> bool:
        """A case from a monitoring alert, or a Jira ticket that names no customer, has nobody to write to. It gets only
        the internal Jira comment."""
        from ..services.jira import NO_CUSTOMER

        return self.case.customer not in (ALERT_CUSTOMER, NO_CUSTOMER)

    def missing_required_actions(self) -> list[str]:
        kinds = {p.kind for p in self.proposals}
        missing = [] if "customer_update" in kinds or not self.has_customer else ["propose_customer_update"]
        if (self.case.jira_source_key or self.case.jira_issue_key) and "jira_comment" not in kinds:
            missing.append("propose_jira_comment")
        return missing

    # Internals

    def _find(self, incident_id: str) -> SupportCase:
        prefix = incident_id.strip().lower()
        matches = [c for c in self.database.list_cases() if c.id.lower().startswith(prefix)] if prefix else []
        if len(matches) != 1:
            raise ToolFailure(f"No single incident matches id '{incident_id}'. Use an incident_id from a search result.")
        return matches[0]

    def run(self, name: str, fn: Callable[..., object], agent: str = "agent", **arguments: object) -> str:
        started = time.perf_counter()
        if self._external_failures.get(name, 0) >= EXTERNAL_FAILURE_LIMIT:
            message = f"{name} is unavailable for the rest of this investigation. Do not call it again."
            self._record(agent, name, arguments, f"Error: {message}", False, started)
            raise ToolFailure(message)
        try:
            value = fn(**arguments)
        except Exception as exc:
            if name in EXTERNAL_TOOLS:
                with self._lock:
                    self._external_failures[name] = self._external_failures.get(name, 0) + 1
            self._record(agent, name, arguments, f"Error: {exc}", False, started)
            raise
        result = value if isinstance(value, str) else json.dumps(value, default=str)
        with self._lock:
            self.evidence.append(result)
        self._record(agent, name, arguments, result, True, started)
        return result

    def _record(self, agent: str, name: str, arguments: dict, result: str, ok: bool, started: float) -> None:
        with self._lock:  # specialists run in parallel threads
            step = AgentStep(
                index=len(self.steps) + 1,
                agent=agent,
                tool=name,
                arguments=arguments,
                result=result[:RESULT_PREVIEW_CHARS],
                ok=ok,
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
            self.steps.append(step)
        if self.on_step:
            self.on_step(step)


def build_tools(box: InvestigationToolbox, agent: str = "agent", only: set[str] | None = None) -> list[BaseTool]:
    @tool
    def search_similar_incidents(query: str) -> str:
        """Semantic search over resolved past incidents. Returns the closest matches with their cause and resolution.
        Use a short description of the symptom, for example 'SMS verification codes delayed'."""
        return box.run("search_similar_incidents", box.search_similar_incidents, agent, query=query)

    @tool
    def get_incident(incident_id: str) -> str:
        """Full details of one past incident: reported issue, cause, workaround, resolution and preventive actions.
        incident_id comes from a search or list result."""
        return box.run("get_incident", box.get_incident, agent, incident_id=incident_id)

    @tool
    def list_service_incidents(service: str) -> str:
        """Other incidents recorded for the same service, newest first. Use it to spot a recurring problem."""
        return box.run("list_service_incidents", box.list_service_incidents, agent, service=service)

    @tool
    def read_service_logs(service: str, contains: str = "", level: str = "") -> str:
        """Read recent server-side log lines for a service. Returns at most the last 40 matching lines.
        Start with no filters, or level='ERROR', to see what is failing. 'contains' is a plain substring match on
        server log text, so use short terms such as an error code, a region or a component name, never a sentence
        a customer saw in the UI."""
        return box.run("read_service_logs", box.read_service_logs, agent, service=service, contains=contains, level=level)

    @tool
    def list_metrics(contains: str = "") -> str:
        """Names of the metrics in Prometheus. 'contains' is a short word to filter them, for example 'error' or 'latency'."""
        return box.run("list_metrics", box.list_metrics, agent, contains=contains)

    @tool
    def query_metrics(query: str, minutes: int = 60) -> str:
        """Run a PromQL query over the last `minutes` and get, for each series, the first, last, lowest and highest
        value and when the highest value occurred. Use rate(...[5m]) only for counters (names that end in _total);
        use a gauge as it is. Example: rate(http_requests_total{status="500"}[5m]) or queue_length{service="x"}."""
        return box.run("query_metrics", box.query_metrics, agent, query=query, minutes=minutes)

    @tool
    def recent_changes(service: str, hours: int = 48) -> str:
        """Commits and releases of the service's code in the last `hours`, oldest first. A change just before the
        incident started is often the cause."""
        return box.run("recent_changes", box.recent_changes, agent, service=service, hours=hours)

    @tool
    def search_jira(text: str) -> str:
        """Search Jira tickets by free text, for example an error code or a symptom. Read-only."""
        return box.run("search_jira", box.search_jira, agent, text=text)

    @tool
    def db_activity(service: str) -> str:
        """The service database's connections now: by state and wait event, the longest running statements, and
        the connection limit. Use it for timeouts, pool exhaustion, locks and slow queries."""
        return box.run("db_activity", box.db_activity, agent, service=service)

    @tool
    def db_schema(service: str, table: str = "") -> str:
        """The tables of the service database, or the columns of one table."""
        return box.run("db_schema", box.db_schema, agent, service=service, table=table)

    @tool
    def db_query(service: str, sql: str) -> str:
        """Run one read-only SELECT on the service database. Writes are refused. At most 100 rows."""
        return box.run("db_query", box.db_query, agent, service=service, sql=sql)

    @tool
    def list_code_files(service: str, prefix: str = "") -> str:
        """List the files of the service's code. prefix narrows it to a folder, for example "src/"."""
        return box.run("list_code_files", box.list_code_files, agent, service=service, prefix=prefix)

    @tool
    def search_code(service: str, query: str, path_glob: str = "") -> str:
        """Search the service's code for a word or pattern: a log tag, an error text,. For example "gateway-client".
        a config key, a function name. Returns path, line and text. path_glob limits the files, for example "*.yaml"."""
        return box.run("search_code", box.search_code, agent, service=service, query=query, path_glob=path_glob)

    @tool
    def read_code(service: str, path: str, start: int = 1, end: int = 0) -> str:
        """Read a file of the service's code with line numbers. Read before you cite a line. At most 300 lines."""
        return box.run("read_code", box.read_code, agent, service=service, path=path, start=start, end=end)

    @tool
    def find_symbol(service: str, name: str) -> str:
        """Find where a function, class, constant or variable is defined in the service's code."""
        return box.run("find_symbol", box.find_symbol, agent, service=service, name=name)

    @tool
    def propose_code_change(service: str, path: str, start_line: int, end_line: int, old_code: str, new_code: str,
                            reason: str) -> str:
        """Propose a patch for a person to review. It is NEVER applied by the app. old_code must be exactly the
        current lines start_line to end_line (copy them from read_code, without the line numbers). new_code replaces
        them. reason: why this fixes the cause; cite log lines with times or file:line."""
        return box.run("propose_code_change", box.propose_code_change, agent, service=service, path=path,
                       start_line=start_line, end_line=end_line, old_code=old_code, new_code=new_code, reason=reason)

    @tool
    def propose_remediation(action: str, service: str, reason: str, verify: str, rollback_plan: str,
                            to_version: str = "", replicas: int = 0, current_replicas: int = 0,
                            path_prefix: str = "", flag: str = "", change_id: str = "") -> str:
        """Propose ONE fix from the catalog. It does NOT run: a person approves it first.
        action: rollback_release (needs to_version: an earlier release that a tool returned),
        revert_config_change (needs change_id: a config change id that recent_changes returned), restart_service,
        scale_service (needs replicas, and current_replicas if known), purge_cdn (needs path_prefix, for example /),
        disable_feature_flag (needs flag: a flag name that a tool returned).
        service: the service of this case. reason: why this fixes the cause; quote the log lines or changes, with
        their times. verify: what shows that it worked (for example: no TLS handshake errors for 5 minutes).
        rollback_plan: how to undo the fix if it makes things worse."""
        params = {"to_version": to_version, "replicas": replicas, "current_replicas": current_replicas,
                  "path_prefix": path_prefix, "flag": flag, "change_id": change_id}
        return box.run("propose_remediation", box.propose_remediation, agent, action=action, service=service, reason=reason,
                       verify=verify, rollback_plan=rollback_plan, params=params)

    @tool
    def propose_customer_update(subject: str, body: str) -> str:
        """Propose a status update email to the customer. It is NOT sent: an engineer approves it first.
        Only state what the evidence supports; never claim the issue is fixed."""
        return box.run("propose_customer_update", box.propose_customer_update, agent, subject=subject, body=body)

    @tool
    def propose_jira_comment(comment: str) -> str:
        """Propose an internal comment on the case's Jira ticket summarizing findings. It is NOT posted
        until an engineer approves it."""
        return box.run("propose_jira_comment", box.propose_jira_comment, agent, comment=comment)

    tools = [search_similar_incidents, get_incident, list_service_incidents, read_service_logs]
    if box.metrics is not None:
        tools += [list_metrics, query_metrics]
    if box.changes is not None:
        tools.append(recent_changes)
    if box.jira.configured:
        tools.append(search_jira)
    tools.append(propose_customer_update)
    if box.remediation is not None:
        tools.append(propose_remediation)
    if box.code is not None:
        tools += [list_code_files, search_code, read_code, find_symbol, propose_code_change]
    if box.service_db is not None and box.case is not None and box.service_db.covers(box.case.service):
        tools += [db_activity, db_schema, db_query]
    if box.case.jira_source_key or box.case.jira_issue_key:
        tools.append(propose_jira_comment)
    return [t for t in tools if only is None or t.name in only]
