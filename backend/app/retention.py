"""Data retention: the app deletes old data by the rules in backend/.env.

- ROOTIQ_RETENTION_BIN_DAYS, 30 by default: the app deletes forever a case that is in the recycle bin for longer.
- ROOTIQ_RETENTION_RESOLVED_DAYS, 0 by default, which means keep: the app deletes forever a resolved case that did
  not change for longer. The case also leaves the knowledge base, so the agents no longer find it as a similar
  incident.
- ROOTIQ_RETENTION_AUDIT_DAYS, 0 by default, which means keep: the app deletes older audit events.

"Delete forever" removes the case, its messages, its knowledge-base entry, its agent runs and checkpoints, its lesson
and its alert links. The app checks the rules at start and then every 6 hours. An admin can also start the check on
Settings > Security. Each cleanup that deletes data writes one audit event with the counts, never the content.
"""

from datetime import UTC, datetime, timedelta

INTERVAL_SECONDS = 6 * 3600


def due(database, settings, now: datetime | None = None) -> dict:
    """What the next cleanup deletes: case ids for each rule, and the number of old audit events."""
    now = now or datetime.now(UTC)
    bin_ids, resolved_ids, audit = [], [], 0
    with database._connect() as connection:
        if settings.retention_bin_days > 0:
            cutoff = (now - timedelta(days=settings.retention_bin_days)).isoformat()
            bin_ids = [r[0] for r in connection.execute(
                "SELECT id FROM support_cases WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,))]
        if settings.retention_resolved_days > 0:
            cutoff = (now - timedelta(days=settings.retention_resolved_days)).isoformat()
            resolved_ids = [r[0] for r in connection.execute(
                "SELECT id FROM support_cases WHERE deleted_at IS NULL AND status = 'resolved' AND updated_at < ?", (cutoff,))]
        if settings.retention_audit_days > 0:
            cutoff = (now - timedelta(days=settings.retention_audit_days)).isoformat()
            audit = connection.execute("SELECT COUNT(*) FROM audit_events WHERE at < ?", (cutoff,)).fetchone()[0]
    return {"bin": bin_ids, "resolved": resolved_ids, "audit": audit}


def run(database, runner, settings, actor: str = "retention", now: datetime | None = None) -> dict:
    """Delete what the rules say. Returns the counts."""
    now = now or datetime.now(UTC)
    todo = due(database, settings, now)
    for case_id in todo["bin"] + todo["resolved"]:
        runner.forget_case(case_id)
        database.delete_case(case_id)
    if todo["audit"]:
        cutoff = (now - timedelta(days=settings.retention_audit_days)).isoformat()
        with database._connect() as connection:
            connection.execute("DELETE FROM audit_events WHERE at < ?", (cutoff,))
    counts = {"bin": len(todo["bin"]), "resolved": len(todo["resolved"]), "audit": todo["audit"]}
    if any(counts.values()):
        database.record_audit(actor, "retention.cleanup", "data", "", (
            f"Deleted {counts['bin']} case(s) from the recycle bin, {counts['resolved']} old resolved case(s) "
            f"and {counts['audit']} old audit event(s)."))
    return counts
