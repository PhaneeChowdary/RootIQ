"""The postmortem agent: writes the postmortem of a resolved case.

The facts come from the records, not from the model:
- the timeline: the case events, the alert, the agent runs, the approvals, the remediations and the verification
  checks, each with its recorded time
- the durations: from the start to the first analysis, to the fix, and to the resolution
A model of the standard level writes the summary, the impact, the cause, what went well and badly, and the action
items. The guard of the investigation checks its text: an id or a log time that is not in the facts is not allowed.
"""

from datetime import UTC, datetime

from langchain_core.messages import HumanMessage, SystemMessage

from ..models import Postmortem, PostmortemDraft, PostmortemEvent
from .guards import unverified_ids, unverified_log_times

PROMPT = """You write the postmortem of a resolved production incident, for engineers and managers. Use only the
facts below. Blameless: describe systems and decisions, never blame a person. Plain text, no Markdown. Quote evidence
with its exact time. Use the durations as given. Action items: 3 to 6, concrete, each one prevents a repeat or finds
the problem sooner; P1 only for what must happen this week; the owner is a team or role."""


def _minutes(start: datetime | None, end: datetime | None) -> float | None:
    if start and end and end >= start:
        return round((end - start).total_seconds() / 60, 1)
    return None


def facts(runner, case_id: str) -> tuple[list[PostmortemEvent], dict[str, float], str]:
    case = runner.database.get_case(case_id)
    events = [PostmortemEvent(at=case.created_at, event=f"Case opened: {case.title}", source="case")]
    events += [PostmortemEvent(at=e.at, event=e.event, source="case") for e in case.timeline]
    runs = sorted(runner.store.list_for_case(case_id, limit=50), key=lambda r: r.created_at)
    first_analysis = fix_at = None
    lines = []
    for run in runs:
        events.append(PostmortemEvent(at=run.created_at, event=f"Agent team run started ({run.trigger.replace('_', ' ')})", source="agent"))
        finished = next((s for s in run.spans if s.agent == "finalize"), None)
        if finished:
            first_analysis = first_analysis or finished.started_at
            events.append(PostmortemEvent(at=finished.started_at, event="Agent team analysis ready", source="agent"))
        for vote in run.approvals:
            what = run.proposals[vote.index].summary if vote.index < len(run.proposals) else "a proposal"
            verb = {"approve": "approved", "edit": "edited and approved", "reject": "rejected"}[vote.action]
            events.append(PostmortemEvent(at=vote.at, event=f"{vote.by} {verb}: {what}", source="approval"))
        for outcome in run.outcomes:
            if outcome.kind == "remediation":
                events.append(PostmortemEvent(at=run.updated_at, event=f"Remediation {outcome.status}: {outcome.detail}", source="remediation"))
        for check in run.verifications:
            events.append(PostmortemEvent(at=check.at, event=f"Verification check {check.check}: {check.status.replace('_', ' ')}. {check.summary}", source="verification"))
            if check.status == "fixed":
                fix_at = fix_at or check.at
        if run.analysis:
            lines.append(f"Agent analysis (run {run.id[:8]}): cause: {run.analysis.likely_cause}")
        lines += [f"Proposed: {p.summary}" for p in run.proposals]
        lines += [f"Finding ({name}): {text}" for name, text in run.findings.items()]
    resolved_at = next((e.at for e in reversed(case.timeline) if "resolv" in e.event.lower()), None) if case.status == "resolved" else None
    events.sort(key=lambda e: e.at)
    durations = {k: v for k, v in {"to_analysis": _minutes(case.created_at, first_analysis),
                                   "to_fix": _minutes(case.created_at, fix_at),
                                   "to_resolve": _minutes(case.created_at, resolved_at)}.items() if v is not None}
    text = "\n".join([
        f"Title: {case.title}", f"Service: {case.service}", f"Customer: {case.customer}", f"Severity: {case.severity}",
        f"Reported issue: {case.reported_issue}", f"Logs from the report: {case.logs or 'none'}",
        f"Resolution recorded: {case.resolution or 'none'}",
        f"Preventive actions recorded: {'; '.join(case.preventive_actions) or 'none'}",
        "Durations in minutes from the case opening: " + (", ".join(f"{k} {v}" for k, v in durations.items()) or "unknown"),
        "Timeline:", *[f"{e.at.strftime('%Y-%m-%dT%H:%M:%SZ')} [{e.source}] {e.event}" for e in events],
        "Investigation:", *lines,
    ])
    return events, durations, text


def write(runner, case_id: str) -> Postmortem | None:
    case = runner.database.get_case(case_id)
    if case is None or case.status != "resolved":
        return None
    events, durations, text = facts(runner, case_id)
    llm = runner.llm_factory()
    llm = llm.for_agent("postmortem") if hasattr(llm, "for_agent") else llm
    writer = llm.with_structured_output(PostmortemDraft)
    prompt = [SystemMessage(PROMPT), HumanMessage(text)]
    config = {"metadata": {"agent": "postmortem"}}
    draft = writer.invoke(prompt, config=config)

    def invented(d: PostmortemDraft) -> list[str]:
        body = " ".join([d.summary, d.impact, d.root_cause, d.trigger, d.detection, d.resolution, *d.went_well,
                         *d.went_poorly, *(a.item for a in d.action_items)])
        return unverified_ids(body, text) + [f"a log line at {t}" for t in unverified_log_times(body, text)]

    warnings = []
    if bad := invented(draft):
        draft = writer.invoke([*prompt, HumanMessage(f"{', '.join(bad)} is not in the facts. Rewrite without it.")], config=config)
        if bad := invented(draft):
            warnings.append(f"Not in the records: {', '.join(bad)}. Check these parts.")
    label = getattr(llm, "label", "") or ""
    postmortem = Postmortem(**draft.model_dump(), case_id=case_id, timeline=events, durations=durations,
                            warnings=warnings, model=label, created_at=datetime.now(UTC))
    runner.database.save_postmortem(postmortem)
    runner.database.record_audit("rootiq-agent", "agent.postmortem_written", "case", case_id,
                                 f"Postmortem written with {len(postmortem.action_items)} action item(s).")
    return postmortem


def to_markdown(p: Postmortem, tz=None) -> str:
    """The postmortem as Markdown. Times in `tz`, the zone of the people who read it. ROOTIQ_TIME_ZONE."""
    local = (lambda t: t.astimezone(tz)) if tz else (lambda t: t)
    zone_name = local(p.created_at).tzname() or "UTC"
    d = p.durations
    lines = [f"# {p.title}", "", p.summary, "",
             "| Measure | Minutes |", "|---|---|"]
    names = {"to_analysis": "From the start to the agents' analysis", "to_fix": "From the start to the verified fix",
             "to_resolve": "From the start to the resolution"}
    lines += [f"| {names[k]} | {v} |" for k, v in d.items()] or ["| Unknown | |"]
    lines += ["", "## Impact", p.impact, "", "## Root cause", p.root_cause, "", "## Trigger", p.trigger, "",
              "## Detection", p.detection, "", "## Resolution", p.resolution, "", f"## Timeline ({zone_name})"]
    lines += [f"- {local(e.at).strftime('%Y-%m-%d %H:%M:%S')} ({e.source}) {e.event}" for e in p.timeline]
    lines += ["", "## What went well", *[f"- {x}" for x in p.went_well], "", "## What went badly", *[f"- {x}" for x in p.went_poorly],
              "", "## Action items", "| Priority | Action | Owner |", "|---|---|---|",
              *[f"| {a.priority} | {a.item} | {a.owner} |" for a in p.action_items]]
    if p.warnings:
        lines += ["", "## Warnings", *[f"- {w}" for w in p.warnings]]
    lines += ["", f"Written by the RootIQ postmortem agent on {local(p.created_at).strftime('%Y-%m-%d %H:%M')} {zone_name}."]
    return "\n".join(lines) + "\n"
