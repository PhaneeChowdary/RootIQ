"""After the approval: carry out remediations, check that they worked, and act on the result.

Remediation: _remediator. Just before a fix runs, the app checks the rules again: the kill switch, the rate limits,
the catalog, the service and the parameters. An engineer can edit the parameters. Each approved fix runs one time only.

Verification: verify. A fix that ran gets its first check after "first_check_seconds" of the automation settings,
90 by default. It gets at most "checks" checks, each one later than the one before. A check counts the ERROR lines of
the service before the fix, from 10 minutes before the case opened, and after the fix:
- fixed:        no errors after the fix for at least one minute, and errors before it
- not fixed:    the errors continue at half or more of the rate before the fix, or 3 or more errors after it
- inconclusive: too early, or too little data; the next check decides
The verification agent, a model of the complex level, reads these numbers and the last log lines. It writes the
summary. It can make a "fixed" result weaker, never stronger: the numbers decide.

Results:
- fixed:     a note on the case. With "auto_resolve" on, the app resolves the case and writes the postmortem.
- not fixed: a note, and a follow-up investigation that knows what the team tried. At most "follow_up_runs" a day.
"""

import json
from datetime import UTC, datetime, timedelta

from langchain_core.messages import HumanMessage, SystemMessage

from ..models import MessageCreate, ResolutionUpdate, VerificationResult, VerificationVerdict
from ..sources import SourceError, active_sources

MIN_QUIET_MINUTES = 1.0
VERIFY_PROMPT = """You are the verification agent. A fix was applied to a production service. Read the numbers and
the log lines after the fix. Write two sentences for the engineer: what the evidence shows, with the numbers. If the
status is fixed but the lines after the fix show a new problem (for example new warnings), set status to
inconclusive and say why. Never set fixed when the given status is not fixed."""


def _line_time(line: str) -> datetime | None:
    try:
        return datetime.fromisoformat(line.split(" ", 1)[0].replace("Z", "+00:00"))
    except ValueError:
        return None


def measure(lines: list[str], action_at: datetime, since: datetime, now: datetime) -> dict:
    """ERROR lines before the fix, from `since`, and after it, and the minutes of each window."""
    before = after = 0
    tail = []
    for line in lines:
        at = _line_time(line)
        if at is None or at < since:
            continue
        is_error = " ERROR " in f" {line} "
        if at < action_at:
            before += is_error
        else:
            after += is_error
            tail.append(line)
    minutes_before = max((action_at - since).total_seconds() / 60, 0.1)
    minutes_after = max((now - action_at).total_seconds() / 60, 0.0)
    return {"errors_before": before, "errors_after": after, "minutes_before": round(minutes_before, 1),
            "minutes_after": round(minutes_after, 1), "tail": tail[-15:]}


def decide(numbers: dict) -> str:
    before, after = numbers["errors_before"], numbers["errors_after"]
    m_before, m_after = numbers["minutes_before"], numbers["minutes_after"]
    if after == 0:
        return "fixed" if before > 0 and m_after >= MIN_QUIET_MINUTES else "inconclusive"
    rate_before = before / m_before if m_before else 0
    rate_after = after / m_after if m_after else float("inf")
    if after >= 3 or rate_after >= 0.5 * rate_before:
        return "not_fixed"
    return "inconclusive"


class FollowUpMixin:
    """Methods of AgentRunner for the work after an approval. See runner.py."""

    # ------------------------------------------------------------ remediation

    def _remediator(self, run, case, sources):
        from .. import automation

        def remediate(index: int, proposal, payload: dict) -> tuple[str, str]:
            policy = automation.store(self.settings).load()
            log = automation.RemediationLog(self.settings.database_target)
            action, service = payload.get("action", ""), payload.get("service", "")
            try:
                params = json.loads(payload.get("params") or "{}")
            except ValueError:
                return "failed", "The parameters are not valid JSON."
            evidence = []
            try:  # check the versions and flags against the current change history and logs, not against the words of the model
                if sources.changes is not None:
                    evidence += sources.changes.recent_changes(service, 24 * 14)
                evidence += sources.logs.lines(service)
            except (SourceError, Exception):  # noqa: BLE001 - a source problem must not run an unchecked action
                pass
            try:
                automation.check_limits(policy, log, case.id)
                clean = automation.validate(action, service, params, case.service, "\n".join(evidence))
            except automation.AutomationError as exc:
                return "skipped", f"Not run: {exc}"
            key = f"{run.id}:{index}"
            if not log.start(key, case.id, run.id, action, service, clean, run.decided_by):
                return "skipped", "This remediation already ran. It never runs twice."
            try:
                runner = automation.make_runner(self.settings, policy)
            except automation.AutomationError as exc:
                log.finish(key, "failed", str(exc))
                return "failed", str(exc)
            if run.dry_run and runner.external:
                detail = f"Dry run: would ask {runner.name} to {automation.describe(action, service, clean).lower()}."
                log.finish(key, "dry_run", detail)
                return "dry_run", detail
            status, detail = runner.run(action, service, clean, key, extra={
                "case_id": case.id, "run_id": run.id, "approved_by": run.decided_by, "reason": payload.get("reason", "")})
            log.finish(key, status, detail)
            return status, detail

        return remediate

    # ------------------------------------------------------------ verification

    def schedule_verification(self, run, index: int, check: int) -> None:
        """The first check after a fix ran. verify queues the later checks itself."""
        from .. import automation

        policy = automation.store(self.settings).load()["verify"]
        delay = int(policy["first_check_seconds"])
        payload = {"run_id": run.id, "index": index, "check": check, "action_at": datetime.now(UTC).isoformat()}
        self.store.enqueue_job("verify", f"verify:{run.id}:{index}:{check}", json.dumps(payload),
                               run_after=datetime.now(UTC) + timedelta(seconds=delay))
        self._wake.set()

    def verify(self, job: dict) -> VerificationResult | None:
        from .. import automation

        run = self.store.get(job["run_id"])
        if run is None:
            return None
        case = self.database.get_case(run.case_id, include_deleted=False)
        if case is None:
            return None
        proposal = run.proposals[job["index"]]
        action_at = datetime.fromisoformat(job["action_at"])
        now = datetime.now(UTC)
        policy = automation.store(self.settings).load()["verify"]
        try:
            lines = active_sources(self.settings).logs.lines(case.service)
        except SourceError as exc:
            lines = []
            self.database.record_audit("rootiq-agent", "agent.verify_failed", "case", case.id, str(exc))
        since = max(case.created_at - timedelta(minutes=10), action_at - timedelta(hours=6))
        numbers = measure(lines, action_at, since, now)
        status = decide(numbers)
        summary = (f"{numbers['errors_before']} errors in {numbers['minutes_before']} min before the fix, "
                   f"{numbers['errors_after']} in {numbers['minutes_after']} min after it.")
        try:
            llm = self.llm_factory()
            llm = llm.for_agent("verifier") if hasattr(llm, "for_agent") else llm
            facts = (f"Fix: {proposal.summary}\nStatus from the numbers: {status}\n{summary}\n"
                     f"Log lines after the fix:\n" + ("\n".join(numbers["tail"]) or "none"))
            verdict = llm.with_structured_output(VerificationVerdict).invoke(
                [SystemMessage(VERIFY_PROMPT), HumanMessage(facts)], config={"metadata": {"agent": "verifier"}})
            if status == "fixed" and verdict.status == "inconclusive":
                status = "inconclusive"  # the agent can make the result weaker, never stronger
            summary = f"{summary} {verdict.summary}".strip()
        except Exception:  # noqa: BLE001 - the numbers alone are enough for a result
            pass
        result = VerificationResult(check=job["check"], at=now, action=proposal.summary, status=status,
                                    errors_before=numbers["errors_before"], errors_after=numbers["errors_after"],
                                    minutes_before=numbers["minutes_before"], minutes_after=numbers["minutes_after"],
                                    summary=summary[:900])
        run = self.store.get(run.id)
        self.store.save(run.model_copy(update={"verifications": [*run.verifications, result], "updated_at": now}))
        last_check = job["check"] >= int(policy["checks"])
        if status == "inconclusive" and not last_check:
            payload = {**job, "check": job["check"] + 1}
            delay = int(policy["first_check_seconds"]) * (2 ** job["check"])
            self.store.enqueue_job("verify", f"verify:{run.id}:{job['index']}:{job['check'] + 1}", json.dumps(payload),
                                   run_after=now + timedelta(seconds=delay))
            self._note(case.id, f"Verification check {job['check']}: not clear yet. {summary} Next check in "
                                f"{delay // 60 or 1} min.", "agent.verify_pending")
            return result
        if status == "fixed":
            self._note(case.id, f"Fix verified: {proposal.summary}. {summary}", "agent.fix_verified")
            if policy["auto_resolve"] and case.status != "resolved":
                self.auto_resolve(case.id, f"{proposal.summary}. Verified by the verification agent: {summary}")
        elif status == "not_fixed":
            self._note(case.id, f"The fix did not work: {proposal.summary}. {summary}", "agent.fix_failed")
            self._follow_up(run, proposal, summary, int(policy["follow_up_runs"]))
        else:
            self._note(case.id, f"Could not confirm the fix after {job['check']} checks: {proposal.summary}. {summary} "
                                "Check the service before you resolve the case.", "agent.verify_unclear")
        return result

    def _note(self, case_id: str, text: str, action: str) -> None:
        self.database.create_message(case_id, "Verification agent", MessageCreate(content=text[:1990], message_type="internal"))
        self.database.add_event(case_id, text[:300])
        self.database.record_audit("rootiq-agent", action, "case", case_id, text[:500])

    def _follow_up(self, run, proposal, summary: str, allowed: int) -> None:
        from .runner import FOLLOW_UP_PREFIX, RunConflict

        day = datetime.now(UTC) - timedelta(hours=24)
        used = sum(1 for r in self.store.list_for_case(run.case_id, limit=50)
                   if r.trigger == "verification_failed" and r.created_at >= day)
        if used >= allowed:
            self._note(run.case_id, "No new investigation starts on its own: the limit of follow-up runs for one day "
                                    "is reached. An engineer must look at the case.", "agent.follow_up_limit")
            return
        note = (f"{FOLLOW_UP_PREFIX}the approved fix \"{proposal.summary}\" ran but did not fix the incident "
                f"({summary}). Find what else causes it; do not propose the same fix again.")
        try:
            self.start(run.case_id, "rootiq-agent", "verification_failed", notes=[note])
        except RunConflict:
            pass

    def auto_resolve(self, case_id: str, resolution: str) -> None:
        case = self.database.resolve_case(case_id, ResolutionUpdate(resolution=resolution[:1900]))
        self.database.record_audit("rootiq-agent", "case.resolved", "case", case_id, "Resolved after a verified fix.")
        try:
            self.database.save_memory(case_id, self.embedder.embed(self.database.memory_content(case)))
        except Exception:  # noqa: BLE001 - the knowledge base can be indexed later
            pass
        key = case.jira_source_key or case.jira_issue_key
        if key and getattr(self.jira, "configured", False):
            try:
                self.jira.transition_to_done(key)
            except Exception:  # noqa: BLE001
                pass
        self.schedule_reflection(case_id)
        self.schedule_postmortem(case_id)

    # ------------------------------------------------------------ postmortem

    def schedule_postmortem(self, case_id: str) -> None:
        self._enqueue("postmortem", f"postmortem:{case_id}")

    def write_postmortem(self, case_id: str):
        from .postmortem import write

        return write(self, case_id)
