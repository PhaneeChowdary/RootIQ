"""Incident correlation: one root cause, many cases.

Cases join a group in two ways:
1. The duplicate rule, without a model call. A case that a Grafana alert opens joins an open case of the same service
   from the last RULE_WINDOW_HOURS. Its own investigation does not start: the team investigates the first case. This
   cuts the alert noise. A person can ungroup the case. Its investigation then starts.
2. The correlation agent of agent/correlation_agent.py. After an investigation, it compares the case with the other
   open cases of AGENT_WINDOW_HOURS around it. Each link that it proposes must cite real log lines. A link is a
   suggestion until a person confirms it.

A person confirms or dismisses a suggestion, makes a case the primary, or ungroups a case. The app never links again
a pair that a person separated. A group has at least two cases. The team works on the primary case.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from . import db, migrations
from .models import IncidentGroup, IncidentLink, SupportCase

RULE_WINDOW_HOURS = 2
AGENT_WINDOW_HOURS = 24
MAX_CANDIDATES = 6
ALERT_CUSTOMER = "Grafana alert"  # the customer of the cases that Grafana alerts open, see services/alerts.py
LINKED_PREFIX = "Linked case: "  # a note for the lead agent about the confirmed cases of the same group

_WORD = re.compile(r"[a-z][a-z0-9]{3,}")
_KEY_VALUE = re.compile(r"\b[a-z_]+=([^\s,]+)")
_TAG = re.compile(r"\[([A-Za-z][\w.-]+)\]")
# Words that most incidents share; they do not show a common cause
COMMON = set("""
error errors warn warning info failed failing fail fails failure status code request requests response user users
customer customers service services some since after before from with that this have when they their there what
which while about into only more most also does dont cannot cant still again today yesterday morning evening hour
hours minute minutes since issue issues problem problems report reported case cases help please team down slow
time times true false none null http https path paths value values level region note ok api get post true
""".split())


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in COMMON}


def log_terms(lines: list[str]) -> set[str]:
    """The names in the WARN and ERROR lines that can show a shared dependency: the values of key=value pairs, for
    example a host, a provider, a queue or a template, and the log tags. The function splits them into words."""
    terms: set[str] = set()
    for line in lines:
        if " ERROR " not in line and " WARN " not in line:
            continue
        for value in _KEY_VALUE.findall(line) + _TAG.findall(line):
            terms |= {t for t in re.split(r"[^a-z0-9]+", value.lower()) if len(t) >= 3 and not t.isdigit() and t not in COMMON}
    return terms


def case_words(case: SupportCase) -> set[str]:
    return _words(" ".join([case.title, case.reported_issue, case.analysis.likely_cause]))


def signals(case: SupportCase, other: SupportCase, case_lines: list[str], other_lines: list[str]) -> dict:
    """Facts that a link can rest on. The score only orders the candidates; the agent and a person decide."""
    shared_log = sorted(log_terms(case_lines) & log_terms(other_lines)) if case.service != other.service else []
    shared_words = sorted(case_words(case) & case_words(other))
    minutes = abs(round((case.created_at - other.created_at).total_seconds() / 60))
    score = (0.4 if case.service == other.service else 0) + 0.15 * min(len(shared_log), 3) + 0.08 * min(len(shared_words), 4)
    return {"minutes_apart": minutes, "same_service": case.service == other.service, "shared_log_terms": shared_log[:6],
            "shared_words": shared_words[:6], "score": round(score, 2)}


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


class IncidentGroups:
    """Incident groups in the main database. See migration 8."""

    def __init__(self, path: Path | str) -> None:
        self.path = path
        self._db = db.get(path)

    def _connect(self):
        return self._db.connect()

    def initialize(self) -> None:
        migrations.upgrade(self._db)

    # Reading

    def group_for(self, case_id: str) -> IncidentGroup | None:
        with self._connect() as connection:
            row = connection.execute("SELECT group_id FROM incident_links WHERE case_id = ?", (case_id,)).fetchone()
        return self.get(row["group_id"]) if row else None

    def get(self, group_id: str) -> IncidentGroup | None:
        with self._connect() as connection:
            group = connection.execute("SELECT * FROM incident_groups WHERE id = ?", (group_id,)).fetchone()
            links = connection.execute("SELECT * FROM incident_links WHERE group_id = ? ORDER BY linked_at", (group_id,)).fetchall()
        if group is None:
            return None
        return IncidentGroup(id=group["id"], primary_case_id=group["primary_case_id"], cause=group["cause"],
                             created_at=group["created_at"], updated_at=group["updated_at"],
                             links=[IncidentLink(case_id=r["case_id"], status=r["status"], source=r["source"], reason=r["reason"],
                                                 evidence=json.loads(r["evidence"] or "[]"), confidence=r["confidence"],
                                                 linked_at=r["linked_at"], linked_by=r["linked_by"]) for r in links])

    def groups(self) -> list[IncidentGroup]:
        with self._connect() as connection:
            ids = [r["id"] for r in connection.execute("SELECT id FROM incident_groups ORDER BY updated_at DESC").fetchall()]
        return [g for g in (self.get(i) for i in ids) if g]

    def unrelated(self, a: str, b: str) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT 1 FROM incident_unrelated WHERE case_a = ? AND case_b = ?", _pair(a, b)).fetchone() is not None

    # Changes

    def link(self, anchor: SupportCase, case: SupportCase, *, status: str, source: str, by: str, reason: str = "",
             evidence: list[str] | None = None, confidence: str = "medium", cause: str = "") -> IncidentGroup | None:
        """Put `case` in the group of `anchor`, or in a new group if `anchor` has none. Returns None when the link is not
        allowed: a person separated the two cases, or the two cases are in different groups already."""
        if anchor.id == case.id or self.unrelated(anchor.id, case.id):
            return None
        group_a, group_b = self.group_for(anchor.id), self.group_for(case.id)
        if group_a and group_b:
            return group_a if group_a.id == group_b.id else None
        if group_b and not group_a:  # the other case is in a group: the anchor joins that group
            anchor, case, group_a = case, anchor, group_b
        now = datetime.now(UTC).isoformat()
        row = (status, source, reason.strip(), json.dumps(evidence or []), confidence, now, by)
        with self._connect() as connection:
            if group_a is None:
                group_id = str(uuid4())
                primary = min((anchor, case), key=lambda c: c.created_at)  # the first case is the primary
                connection.execute("INSERT INTO incident_groups (id, primary_case_id, cause, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                                   (group_id, primary.id, cause.strip(), now, now))
                connection.execute("INSERT INTO incident_links (case_id, group_id, status, source, reason, evidence, confidence, linked_at, linked_by) "
                                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (anchor.id, group_id, *row))
            else:
                group_id = group_a.id
                connection.execute("UPDATE incident_groups SET updated_at = ?, cause = CASE WHEN cause = '' THEN ? ELSE cause END WHERE id = ?",
                                   (now, cause.strip(), group_id))
            connection.execute("INSERT INTO incident_links (case_id, group_id, status, source, reason, evidence, confidence, linked_at, linked_by) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (case_id) DO NOTHING", (case.id, group_id, *row))
        return self.get(group_id)

    def confirm(self, case_id: str, by: str) -> IncidentGroup:
        """A person confirms a suggested link. The primary of a new group is confirmed with it."""
        group = self._required(case_id)
        with self._connect() as connection:
            connection.execute("UPDATE incident_links SET status = 'confirmed', linked_by = ? WHERE case_id IN (?, ?) AND status = 'suggested'",
                               (by, case_id, group.primary_case_id))
            connection.execute("UPDATE incident_groups SET updated_at = ? WHERE id = ?", (datetime.now(UTC).isoformat(), group.id))
        return self.get(group.id)

    def separate(self, case_id: str, by: str) -> IncidentGroup | None:
        """A person dismisses a suggestion or ungroups a case. The case is never linked to these cases again.
        Returns the group that is left, if any."""
        group = self._required(case_id)
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            for link in group.links:
                if link.case_id != case_id:
                    connection.execute("INSERT INTO incident_unrelated (case_a, case_b, decided_by, decided_at) VALUES (?, ?, ?, ?) "
                                       "ON CONFLICT (case_a, case_b) DO NOTHING", (*_pair(case_id, link.case_id), by, now))
            connection.execute("DELETE FROM incident_links WHERE case_id = ?", (case_id,))
        return self._tidy(group.id)

    def set_primary(self, case_id: str) -> IncidentGroup:
        group = self._required(case_id)
        with self._connect() as connection:
            connection.execute("UPDATE incident_groups SET primary_case_id = ?, updated_at = ? WHERE id = ?",
                               (case_id, datetime.now(UTC).isoformat(), group.id))
        return self.get(group.id)

    def forget_case(self, case_id: str) -> None:
        """For delete forever: the case leaves its group and its decisions."""
        group = self.group_for(case_id)
        with self._connect() as connection:
            connection.execute("DELETE FROM incident_links WHERE case_id = ?", (case_id,))
            connection.execute("DELETE FROM incident_unrelated WHERE case_a = ? OR case_b = ?", (case_id, case_id))
        if group:
            self._tidy(group.id)

    def _required(self, case_id: str) -> IncidentGroup:
        group = self.group_for(case_id)
        if group is None:
            raise KeyError(case_id)
        return group

    def _tidy(self, group_id: str) -> IncidentGroup | None:
        """A group of one case is no group. A group without its primary gets its oldest case as the primary."""
        group = self.get(group_id)
        if group is None:
            return None
        with self._connect() as connection:
            if len(group.links) < 2:
                connection.execute("DELETE FROM incident_links WHERE group_id = ?", (group_id,))
                connection.execute("DELETE FROM incident_groups WHERE id = ?", (group_id,))
                return None
            if group.primary_case_id not in {link.case_id for link in group.links}:
                connection.execute("UPDATE incident_groups SET primary_case_id = ? WHERE id = ?", (group.links[0].case_id, group_id))
        return self.get(group_id)


def duplicate_of(cases: list[SupportCase], case: SupportCase, groups: IncidentGroups) -> SupportCase | None:
    """The duplicate rule: for a case that a Grafana alert opened, the open case of the same service from the last
    RULE_WINDOW_HOURS. If that case is in a group, the primary of the group. None for a case that a person or Jira opened."""
    if case.customer != ALERT_CUSTOMER:
        return None
    since = case.created_at - timedelta(hours=RULE_WINDOW_HOURS)
    matches = sorted((c for c in cases if c.id != case.id and c.service == case.service and c.status != "resolved"
                      and since <= c.created_at <= case.created_at and not groups.unrelated(c.id, case.id)),
                     key=lambda c: c.created_at)
    if not matches:
        return None
    group = groups.group_for(matches[0].id)
    primary = next((c for c in matches if group and c.id == group.primary_case_id), None)
    return primary or matches[0]


def linked_notes(groups: IncidentGroups, cases_by_id: dict[str, SupportCase], case_id: str) -> list[str]:
    """For the lead agent: the confirmed cases of the case's group, with the cause that links them."""
    group = groups.group_for(case_id)
    if group is None or not any(l.case_id == case_id and l.status == "confirmed" for l in group.links):
        return []
    notes = []
    for link in group.links:
        other = cases_by_id.get(link.case_id)
        if link.case_id != case_id and link.status == "confirmed" and other:
            notes.append(f"{LINKED_PREFIX}case {other.id[:8]} ({other.service}, {other.status}) \"{other.title}\" has the same root cause"
                         + (f": {group.cause}" if group.cause else "."))
    return notes
