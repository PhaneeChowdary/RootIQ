"""Load demo incidents into the RootIQ database: resolved past cases, indexed into memory, and open cases.

    python -m backend.seed.seed_demo

You can run it again: the command skips a case whose title already exists.
--retime gives realistic times to past incidents that an older version seeded as opened and resolved in the same second.
"""
import json
import sys
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..app.config import Settings
from ..app.database import CaseDatabase
from ..app.models import CaseCreate, ResolutionUpdate, SupportAnalysis
from ..app.services.memory import EmbeddingUnavailable, LocalEmbedder

SEED_FILE = Path(__file__).with_name("incidents.json")
PROJECTS_FILE = Path(__file__).with_name("projects.json")
# Typical times of a past incident, by severity: first response in minutes, fix in hours. All within the SLA targets.
ACK_MINUTES = {"critical": 6, "high": 18, "medium": 40, "low": 120}
FIX_HOURS = {"critical": 2.0, "high": 4.5, "medium": 11.0, "low": 30.0}


def history_times(item: dict, now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    """When a past incident opened, got its first response and was resolved. "days_ago", "ack_minutes" and "fix_hours"
    in the item set them; else typical values for its severity, varied by its title so that the history looks real."""
    now = now or datetime.now(UTC)
    vary = zlib.crc32(item["title"].encode())
    severity = item.get("severity", "medium")
    created = now - timedelta(days=item.get("days_ago", 2 + vary % 20), hours=vary % 9)
    ack = timedelta(minutes=item.get("ack_minutes", ACK_MINUTES.get(severity, 40) * (0.6 + (vary >> 4) % 5 / 10)))
    fix = timedelta(hours=item.get("fix_hours", FIX_HOURS.get(severity, 11.0) * (0.6 + (vary >> 8) % 7 / 10)))
    return created, created + ack, created + fix


def seed(database: CaseDatabase, embedder, data: dict | None = None) -> tuple[int, int, int]:
    """data: {"resolved": [...], "open": [...]}; by default the demo incidents of incidents.json."""
    data = data or json.loads(SEED_FILE.read_text())
    existing = {case.title for case in database.list_cases()}
    created_resolved = created_open = indexed = 0

    for item in data.get("resolved", []):
        if item["title"] in existing:
            continue
        case = database.create_case(CaseCreate(**{k: item[k] for k in CaseCreate.model_fields if k in item}))
        analysis = SupportAnalysis(
            incident_summary=item["reported_issue"],
            likely_cause=item["cause"],
            suggested_severity=item["severity"],
            workaround=item["workaround"],
        )
        database.save_analysis(case.id, analysis)
        database.save_review(case.id, analysis, approve=True)
        database.resolve_case(
            case.id, ResolutionUpdate(resolution=item["resolution"], preventive_actions=item["preventive_actions"])
        )
        case = database.set_history_times(case.id, *history_times(item))
        created_resolved += 1
        try:
            database.save_memory(case.id, embedder.embed(database.memory_content(case)))
            indexed += 1
        except EmbeddingUnavailable as exc:
            print(f"Not indexed ({exc}): {case.title}")

    for item in data.get("open", []):
        if item["title"] in existing:
            continue
        database.create_case(CaseCreate(**item))
        created_open += 1
    return created_resolved, indexed, created_open


def retime(database: CaseDatabase, data: dict | None = None) -> int:
    """Realistic times for the seeded past incidents that opened and were resolved within one minute. Returns the count."""
    items = {item["title"]: item for item in (data or {}).get("resolved", []) or [
        *json.loads(SEED_FILE.read_text()).get("resolved", []), *json.loads(PROJECTS_FILE.read_text()).get("resolved", [])]}
    count = 0
    for case in database.list_cases():
        item = items.get(case.title)
        if item is None or case.status != "resolved" or not case.timeline:
            continue
        if (case.timeline[-1].at - case.created_at) > timedelta(minutes=1):
            continue  # a real case, or one with real times already
        database.set_history_times(case.id, *history_times(item))
        count += 1
    return count


def main() -> None:
    settings = Settings()
    database = CaseDatabase(settings.database_target)
    database.initialize()
    if "--retime" in sys.argv:
        print(f"Gave realistic times to {retime(database)} seeded past incident(s).")
        return
    resolved, indexed, opened = seed(database, LocalEmbedder(settings.ollama_url, settings.embedding_model))
    print(f"Seeded {resolved} resolved cases ({indexed} indexed into memory) and {opened} open cases "
          f"into {str(settings.database_target).split('@')[-1]}.")


if __name__ == "__main__":
    main()
