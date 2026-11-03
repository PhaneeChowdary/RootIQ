"""The correlation agent: does another open case have the same root cause as this one?

It runs after each investigation, and when a person asks. The app picks the candidates without a model: the open
cases of AGENT_WINDOW_HOURS around the case. Facts put them in order: the same service, shared names in the WARN and
ERROR lines, and shared words. The model gets the cases, their likely causes and their log lines. It names the cases
with the same cause.

The code checks each answer. Each id must be a candidate, and each cited log line must be a real line. The reason must
not quote a time or an id that is not in the evidence. The app drops a link without a real log line. Each link is a
suggestion for a person.
"""

import re
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..correlation import AGENT_WINDOW_HOURS, MAX_CANDIDATES, IncidentGroups, log_terms, signals
from ..sources import SourceError
from .guards import unverified_ids, unverified_log_times

# A reason that says the opposite of a link. Models sometimes list a case only to say that it is unrelated.
_NOT_RELATED = re.compile(r"\b(unrelated|not related|no (direct |clear )?(connection|link|relation)|different (root )?cause|"
                          r"not (the )?same (root )?cause|independent)\b", re.I)
LOG_LINES = 12  # the number of WARN and ERROR lines for each service

PROMPT = """You are the correlation agent of an incident team. You get THE CASE and other open cases from the same
day, with their log lines. For each other case, decide: does one failure cause both cases? For example, one provider
outage can cause failed text messages in one service and a growing email queue in another.
Not enough: the same time, the same service, or a similar symptom. You need a line in the logs that connects them.
For each case with the same cause, give 1 to 3 log lines by their number (for example "L4"): a link without
log lines is not used. Say in one or two
sentences how one cause explains both. List only the cases with the same cause: never list a case to say that it
is unrelated. If you are not sure, leave the case out. An empty list is a good answer.
Use the case ids as given."""


class CorrelationLink(BaseModel):
    case_id: str = Field(description="The id of the other case, as given")
    same_cause: bool = Field(default=True, description="True only if one failure causes both cases")
    reason: str = Field(description="How one cause explains both cases, in one or two sentences")
    evidence: list[str] = Field(description='Required: 1 to 3 log line numbers that show the link, for example ["L4", "L9"]')
    confidence: Literal["high", "medium", "low"] = "medium"


class CorrelationVerdict(BaseModel):
    shared_cause: str = Field(default="", description="The common root cause in one sentence, if any case has it")
    links: list[CorrelationLink] = Field(default_factory=list)


# Models often write a non-breaking hyphen or a dash where the log has "-". Such a quote is still a real quote.
_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2212"} | {"\u00a0": " ", "\u202f": " "})


def _flat(text: str) -> str:
    return " ".join(text.translate(_DASHES).split())


_LINE_REF = re.compile(r"\s*\(?\b(?:lines?\s*)?L\d{1,4}(?:\s*(?:[-\u2010-\u2014,]|and|to)\s*L?\d{1,4})*\b\)?", re.I)


def _clean_reason(text: str) -> str:
    """The reason without line numbers such as "": a person reads the real lines below it. L8-L9."""
    return re.sub(r"\s+([,.;])", r"\1", _LINE_REF.sub("", text)).strip()


def _raw_lines(logs, service: str) -> list[str]:
    try:
        return logs.lines(service.lower())[-300:]
    except (SourceError, OSError):
        return []


def _tokens(line: str) -> set[str]:
    return set(re.split(r"[^a-z0-9]+", line.lower()))


def _pick(raw: list[str], bridge_terms: set[str]) -> list[str]:
    """The last WARN and ERROR lines of a service, and its last lines of any level that name a bridge term.
    A bridge term is a name from the errors of the other service, for example "email" in the line where the login
    service offers the email fallback."""
    picked = [l for l in raw if " ERROR " in l or " WARN " in l][-LOG_LINES:]
    picked += [l for l in raw if l not in picked and _tokens(l) & bridge_terms][-4:]
    return sorted(picked)


def _describe(case, label: str) -> str:
    cause = case.analysis.likely_cause.strip()
    return (f"{label} id={case.id[:8]} service={case.service} opened={case.created_at:%Y-%m-%dT%H:%M}Z status={case.status}\n"
            f"Title: {case.title}\nReport: {case.reported_issue[:400]}" + (f"\nLikely cause (investigation): {cause[:400]}" if cause else ""))


def correlate(runner, case_id: str, logs=None) -> list[CorrelationLink]:
    """Suggest links for one case. Returns the links that passed the checks. The app saved these links."""
    from datetime import timedelta

    from .. import sources

    database = runner.database
    case = database.get_case(case_id)
    if case is None or case.status == "resolved":
        return []
    groups = IncidentGroups(database.path)
    logs = logs or sources.active_sources(runner.settings).logs
    window = timedelta(hours=AGENT_WINDOW_HOURS)
    own_group = groups.group_for(case.id)
    others = [c for c in database.list_cases()
              if c.id != case.id and c.status != "resolved" and abs(c.created_at - case.created_at) <= window
              and not groups.unrelated(c.id, case.id)
              and not (own_group and any(l.case_id == c.id for l in own_group.links))
              and not (own_group and groups.group_for(c.id))]
    if not others:
        return []
    raw = {service: _raw_lines(logs, service) for service in {case.service, *(o.service for o in others)}}
    scored = []
    for other in others:
        facts = signals(case, other, raw[case.service], raw[other.service])
        if facts["same_service"] or facts["shared_log_terms"] or len(facts["shared_words"]) >= 2:
            scored.append((facts["score"], other, facts))
    scored = sorted(scored, key=lambda x: -x[0])[:MAX_CANDIDATES]
    shared = {other.id: set(facts["shared_log_terms"]) for _, other, facts in scored}
    if not scored:
        database.record_audit("rootiq-agent", "agent.correlation", "case", case.id, "No open case shares facts with this case.")
        return []
    terms_of = {service: log_terms(lines) for service, lines in raw.items()}
    bridge: dict[str, set[str]] = {case.service: set()}
    for _, other, _ in scored:
        if other.service != case.service:
            bridge[case.service] |= terms_of[other.service] - terms_of[case.service]
            bridge.setdefault(other.service, set()).update(terms_of[case.service] - terms_of[other.service])
    lines = {service: _pick(raw[service], bridge.get(service, set())) for service in {case.service, *(o.service for _, o, _ in scored)}}
    parts = [_describe(case, "THE CASE")]
    for _, other, facts in scored:
        parts.append(_describe(other, "OTHER CASE") + f"\nFacts: {facts['minutes_apart']} min apart; same service: "
                     f"{'yes' if facts['same_service'] else 'no'}; shared names in the logs: {', '.join(facts['shared_log_terms']) or 'none'}")
    services = sorted({case.service, *(o.service for _, o, _ in scored)})
    numbered = [(s, l) for s in services for l in lines[s]]  # the model cites each line by its number, so it cannot copy a line wrongly
    log_text = "\n\n".join(f"LOG LINES of {s}:\n" + ("\n".join(f"L{i} {l}" for i, (t, l) in enumerate(numbered, 1) if t == s) or "(none)")
                           for s in services)
    text = "\n\n".join(parts) + "\n\n" + log_text

    llm = runner.llm_factory()
    llm = llm.for_agent("correlation") if hasattr(llm, "for_agent") else llm
    verdict = llm.with_structured_output(CorrelationVerdict).invoke(
        [SystemMessage(PROMPT), HumanMessage(text)], config={"metadata": {"agent": "correlation"}})

    real_lines = [_flat(l) for s in services for l in lines[s]]
    service_of = {_flat(l): s for s in services for l in lines[s]}
    by_id = {o.id[:8]: o for _, o, _ in scored}
    kept: list[CorrelationLink] = []
    dropped: list[str] = []
    for link in verdict.links:
        other = by_id.get(link.case_id.strip()[:8])
        # a line number gives that line, and a quote gives the real line that it quotes. The app drops all other evidence.
        numbers = re.findall(r"\bL(\d{1,4})\b", link.reason) + [n for x in link.evidence for n in (re.findall(r"\bL(\d{1,4})\b", x)
                   or re.findall(r"^\s*(?:line\s*#?\s*)?(\d{1,4})\s*$", x, re.I))]  # "L4", "4" or "line 4"
        cited = [_flat(numbered[int(n) - 1][1]) for n in numbers if 0 < int(n) <= len(numbered)]
        quoted = [r for e in (_flat(x) for x in link.evidence if not re.fullmatch(r"\s*(?:L|line\s*#?\s*)?\d{1,4}\s*", x, re.I))
                  if len(e) >= 20 for r in real_lines if e in r or r in e]
        evidence = list(dict.fromkeys(cited + quoted))
        bad_words = unverified_ids(link.reason, text) + unverified_log_times(link.reason, text)
        # two services: the quotes must show both sides, or a name that the two logs share
        connected = other is None or other.service == case.service or (
            {service_of[e] for e in evidence} >= {case.service, other.service}
            or any(t in e.lower() for e in evidence for t in shared.get(other.id, ())))
        words = re.findall(r"[A-Za-z]{2,}", re.sub(r"\bL\d{1,4}\b", " ", link.reason))
        why = ("not a candidate" if other is None else "not the same cause" if not link.same_cause or _NOT_RELATED.search(link.reason)
               else "no reason" if len(words) < 6
               else "low confidence" if link.confidence == "low"
               else f"no real log line in {[x[:40] for x in link.evidence][:3]}" if not evidence
               else "no log line connects the two services" if not connected
               else f"not in the evidence: {', '.join(bad_words)}" if bad_words else "")
        if why:
            dropped.append(f"{link.case_id[:8]} ({why})")
            continue
        if groups.link(case, other, status="suggested", source="agent", by="rootiq-agent", reason=_clean_reason(link.reason),
                       evidence=evidence[:3], confidence=link.confidence, cause=verdict.shared_cause):
            kept.append(link.model_copy(update={"case_id": other.id, "evidence": evidence[:3]}))
    detail = (f"Suggested {len(kept)} related case(s): {', '.join(k.case_id[:8] for k in kept)}." if kept else "No related case.")
    if dropped:
        detail += f" Dropped {len(dropped)} link(s): {'; '.join(dropped)}."
    database.record_audit("rootiq-agent", "agent.correlation", "case", case.id, detail)
    return kept
