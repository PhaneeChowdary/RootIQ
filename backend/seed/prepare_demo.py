"""Put the running app in a clean state for a live demo. Run it shortly before the demo, with ./run.sh running:

    python -m backend.seed.prepare_demo                # back up, clear old open cases, reset logs, fresh cases
    python -m backend.seed.prepare_demo --no-runs      # the same, but the agents do not start on the fresh cases

1. A backup of the database. Settings > Security lists it.
2. Open cases older than --older-than hours go to the recycle bin. Restore any of them from the bin. The resolved
   cases, agent runs, ratings and users stay, so Insights keeps its history.
3. The demo service logs go back to their state before `simulate_incident`.
4. The code source: the demo code, plus the real projects of seed/projects.json, read-only. "cloud_code" in that file
 sets the "Cloud models" switch of the code source: on, cloud models read the code and write the
 patches; off, or with --local-code, only the local model reads it. Secrets masked.
5. The incidents of those projects: simulated logs and one open case for each, without Jira
 tickets. The first response is recorded, so the SLA counts from the resolution target. Then the agent team
 investigates them, one at a time. When a project folder is missing, the demo open cases come instead. Simulate_incident.py.
6. The Jira tickets of "jira_keep": their old cases are deleted and a Jira sync imports them again, with a fresh SLA
   and a new investigation after the projects. "jira_scenarios" writes the logs that those tickets need.

Open SLA targets are short, so run this on the day of the demo. High: 8 h to the fix."""
import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from ..app.config import Settings
from ..app.database import CaseDatabase
from ..app.services.memory import LocalEmbedder
from ..app.simulate_incident import SCENARIOS, reset, write
from .seed_demo import SEED_FILE, seed

PROJECTS_FILE = Path(__file__).with_name("projects.json")
DEMO_CODE = Path(__file__).with_name("code")


def project_folders(projects: dict) -> dict[str, Path]:
    """The project folders that exist, by service. Paths in projects.json are relative to the folder of the repo."""
    base = Path(__file__).resolve().parents[2]
    folders = {service: (base / path).resolve() for service, path in projects["code"].items()}
    return {service: folder for service, folder in folders.items() if folder.is_dir()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Put the running app in a clean state for a live demo.")
    parser.add_argument("--older-than", type=float, default=6, help="bin open cases older than these hours (default 6)")
    parser.add_argument("--no-runs", action="store_true", help="do not start the agent team on the fresh cases")
    parser.add_argument("--local-code", action="store_true", help="only the local model reads the code of the projects")
    args = parser.parse_args()
    settings = Settings()

    api = httpx.Client(base_url=settings.api_url, timeout=60)
    try:
        login = api.post("/api/auth/login", json={"username": settings.auth_username, "password": settings.auth_password})
    except httpx.TransportError:
        print(f"The app does not answer at {settings.api_url}. Start it with ./run.sh first.")
        return 1
    if login.status_code != 200:
        print("Sign-in failed: check ROOTIQ_AUTH_USERNAME and ROOTIQ_AUTH_PASSWORD in backend/.env.")
        return 1
    api.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

    api.post("/api/security/backup").raise_for_status()
    print("1. Backup made. Settings > Security lists it; `python -m backend.app.backup --list` shows the files.")

    projects = json.loads(PROJECTS_FILE.read_text())
    keep = set(projects.get("jira_keep", []))
    cutoff = datetime.now(UTC) - timedelta(hours=args.older_than)
    old = [c for c in api.get("/api/cases").json() if c["status"] != "resolved"
           and datetime.fromisoformat(c["created_at"]) < cutoff and c.get("jira_source_key") not in keep]
    for case in old:
        api.delete(f"/api/cases/{case['id']}").raise_for_status()
    print(f"2. Moved {len(old)} old open case(s) to the recycle bin.")

    restored = reset(settings)
    print(f"3. Demo logs reset{': ' + ', '.join(restored) if restored else ' (they were already clean)'}.")

    folders = project_folders(projects)
    repos = "\n".join(f"{service} = {folder}" for service, folder in folders.items())
    cloud = bool(projects.get("cloud_code")) and not args.local_code
    saved = api.put("/api/sources/code", json={"type": "local", "config": {"root": str(DEMO_CODE.resolve()), "repos": repos,
                                                                           "cloud": "on" if cloud else "off"}})
    saved.raise_for_status()
    where = "cloud models may read it, with secrets masked" if cloud else "local models only"
    print(f"4. Code source: the demo code and {len(folders)} project(s): {', '.join(folders) or 'none'}. Read-only; {where}.")

    database = CaseDatabase(settings.database_target)
    embedder = LocalEmbedder(settings.ollama_url, settings.embedding_model)
    if folders:
        for name in projects["scenarios"]:
            if SCENARIOS[name]["service"] in folders:
                write(name, settings)
        data = {"resolved": [i for i in projects["resolved"] if i["service"] in folders],
                "open": [i for i in projects["open"] if i["service"] in folders]}
    else:
        data = {"resolved": [], "open": json.loads(SEED_FILE.read_text())["open"]}
    seed(database, embedder, data)
    titles = {item["title"] for item in data["open"]}
    fresh = [c for c in api.get("/api/cases").json() if c["title"] in titles and c["status"] != "resolved"]
    for case in fresh:
        api.put(f"/api/cases/{case['id']}/acknowledge", json={"note": "First response sent to the customer."})
        if not args.no_runs and case["status"] == "new":
            api.post(f"/api/cases/{case['id']}/agent-runs")
    started = "" if args.no_runs else " The agent team investigates them now: about 3 to 5 minutes each."
    print(f"5. {len(fresh)} open case(s) ready.{started}")

    if keep:
        for case in api.get("/api/cases").json():  # the old cases of the kept tickets: deleted, then imported fresh
            if case.get("jira_source_key") in keep:
                api.delete(f"/api/cases/{case['id']}").raise_for_status()
                api.delete(f"/api/bin/{case['id']}").raise_for_status()
        database.unignore_jira(keep)
        for name in projects.get("jira_scenarios", []):
            write(name, settings)
        synced = api.post("/api/jira/sync")
        if synced.status_code != 200:
            print(f"6. Jira sync failed ({synced.status_code}): {synced.text[:120]}")
            return 1
        imported = synced.json()["cases"]
        for case in imported:
            api.put(f"/api/cases/{case['id']}/acknowledge", json={"note": "First response sent to the customer."})
        others = [c["title"] for c in imported if c["jira_source_key"] not in keep]
        print(f"6. Jira: {len(imported)} ticket(s) imported fresh." + (f" Not in jira_keep, close them in Jira: {others}" if others else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
