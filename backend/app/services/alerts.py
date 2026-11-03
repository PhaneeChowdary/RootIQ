"""Grafana alerts: read the webhook message and connect each alert to a case.

Grafana sends a JSON message when an alert starts, with the status "firing", and when it stops: "resolved".
Each alert has a fingerprint. The same alert always has the same fingerprint.

The rules:
- A new firing alert opens a case. The agent team starts on it.
- The same alert again, while its case is open, does not open a second case.
- An alert for the same rule and service as an open alert case joins that case.
- A resolved alert adds a note to its case. The engineer still resolves the case.
- A firing alert whose earlier case is resolved opens a new case: the problem came back.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .. import db, migrations
from ..agent.tools import ALERT_CUSTOMER
from ..models import CaseCreate

SEVERITIES = {"critical", "high", "medium", "low"}


@dataclass
class Alert:
    fingerprint: str
    status: str  # "firing" or "resolved"
    name: str
    service: str
    severity: str
    summary: str
    description: str
    starts_at: str
    ends_at: str
    url: str
    values: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)


def parse_grafana(payload: dict) -> list[Alert]:
    """The alerts in a Grafana webhook message. Unknown fields are ignored."""
    alerts = []
    for item in payload.get("alerts") or []:
        labels = {str(k): str(v) for k, v in (item.get("labels") or {}).items()}
        notes = item.get("annotations") or {}
        severity = labels.get("severity", "").lower()
        name = labels.get("alertname", "Grafana alert")
        alerts.append(Alert(
            fingerprint=str(item.get("fingerprint") or f"{name}:{sorted(labels.items())}"),
            status=str(item.get("status") or payload.get("status") or "firing").lower(),
            name=name,
            service=labels.get("service", "unknown").lower(),
            severity=severity if severity in SEVERITIES else "high",
            summary=str(notes.get("summary") or name),
            description=str(notes.get("description") or ""),
            starts_at=str(item.get("startsAt") or ""),
            ends_at=str(item.get("endsAt") or ""),
            url=str(item.get("generatorURL") or item.get("panelURL") or ""),
            values=item.get("values") or {},
            labels=labels,
        ))
    return alerts


def case_for(alert: Alert, recurred_from: str = "") -> CaseCreate:
    """The case that a firing alert opens."""
    issue = alert.description or f"Grafana alert {alert.name} is firing for {alert.service}."
    if recurred_from:
        issue += f" The same alert fired before. Case {recurred_from} was resolved, so the problem came back."
    details = [f"Grafana alert: {alert.name} ({alert.status}) since {alert.starts_at or 'unknown'}",
               "Labels: " + ", ".join(f"{k}={v}" for k, v in sorted(alert.labels.items()))]
    if alert.values:
        details.append("Values: " + ", ".join(f"{k}={v}" for k, v in alert.values.items()))
    if alert.url:
        details.append(f"Rule: {alert.url}")
    return CaseCreate(title=alert.summary[:200], customer=ALERT_CUSTOMER, service=alert.service,
                      environment="production", severity=alert.severity, reported_issue=issue, logs="\n".join(details))


class AlertLinks:
    """Which case each alert opened. One small table in the main database."""

    def __init__(self, path: Path | str) -> None:
        self.path = path
        self._db = db.get(path)

    def _connect(self):
        return self._db.connect()

    def initialize(self) -> None:
        migrations.upgrade(self._db)

    def case_for_fingerprint(self, fingerprint: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT case_id FROM alert_cases WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return row["case_id"] if row else None

    def cases_for_rule(self, alert_name: str, service: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute("SELECT case_id FROM alert_cases WHERE alert_name = ? AND service = ? ORDER BY updated_at DESC",
                                      (alert_name, service)).fetchall()
        return [row["case_id"] for row in rows]

    def link(self, alert: Alert, case_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO alert_cases (fingerprint, case_id, alert_name, service, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(fingerprint) DO UPDATE SET
                   case_id = excluded.case_id, status = excluded.status, updated_at = excluded.updated_at""",
                (alert.fingerprint, case_id, alert.name, alert.service, alert.status, datetime.now(UTC).isoformat()))
