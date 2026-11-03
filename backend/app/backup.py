"""Backups of the RootIQ data, and a check that a backup can be read.

    .venv/bin/python -m backend.app.backup                   # make a backup now
    .venv/bin/python -m backend.app.backup --list            # the backups, newest first
    .venv/bin/python -m backend.app.backup --verify FILE     # open a backup and count its cases
    .venv/bin/python -m backend.app.backup --restore FILE --yes   # SQLite only: put a backup back

A backup is one file named rootiq-<UTC time>.tar.gz, in backend/data/backups or in ROOTIQ_BACKUP_DIR. It has:
- SQLite: a consistent copy of rootiq.db and agent_checkpoints.db. The SQLite online backup makes it, so the app
  can continue to run. PostgreSQL: a pg_dump file of the database. pg_dump must be installed.
- data_sources.json and model_settings.json. Their secrets stay encrypted.

The encryption key, backend/data/secret.key or ROOTIQ_SECRET_KEY, is NOT in the backup. Keep a copy of it in a
safe place, apart from the backups: a person with both can read the saved secrets.

The app keeps the newest ROOTIQ_BACKUP_KEEP backups, 14 by default, and deletes the older ones. The API makes a
backup every ROOTIQ_BACKUP_HOURS hours: 24 by default, and 0 means off.
"""

import argparse
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from . import db

PREFIX = "rootiq-"
CONFIG_FILES = ("data_sources.json", "model_settings.json")


def backup_dir(settings) -> Path:
    return Path(getattr(settings, "backup_dir", "") or Path(settings.data_dir) / "backups")


def make(settings) -> Path:
    """Make one backup file. Returns its path."""
    folder = backup_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = folder / f"{PREFIX}{stamp}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        target = settings.database_target
        if db.is_postgres(target):
            if not shutil.which("pg_dump"):
                raise RuntimeError("pg_dump is not installed. Install the PostgreSQL client tools.")
            subprocess.run(["pg_dump", "--format=custom", "--no-owner", f"--file={work / 'rootiq.pgdump'}", str(target)],
                           check=True, capture_output=True)
        else:
            for name in ("rootiq.db", "agent_checkpoints.db"):
                source = Path(settings.data_dir) / name
                if source.exists():
                    with sqlite3.connect(source) as src, sqlite3.connect(work / name) as dst:
                        src.backup(dst)
        for name in CONFIG_FILES:
            source = Path(settings.data_dir) / name
            if source.exists():
                shutil.copy2(source, work / name)
        partial = out.with_suffix(".partial")
        with tarfile.open(partial, "w:gz") as archive:
            for item in sorted(work.iterdir()):
                archive.add(item, arcname=item.name)
        partial.replace(out)
    out.chmod(0o600)
    prune(settings)
    return out


def backups(settings) -> list[Path]:
    return sorted(backup_dir(settings).glob(f"{PREFIX}*.tar.gz"), reverse=True)


def prune(settings) -> list[Path]:
    keep = max(1, int(getattr(settings, "backup_keep", 14)))
    old = backups(settings)[keep:]
    for path in old:
        path.unlink()
    return old


def verify(path: Path) -> dict:
    """Open a backup and read it. Returns what it has; raises an error if it is damaged."""
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(path, "r:gz") as archive:
        archive.extractall(tmp, filter="data")
        work = Path(tmp)
        found = {"files": sorted(p.name for p in work.iterdir())}
        if (work / "rootiq.db").exists():
            with sqlite3.connect(work / "rootiq.db") as connection:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("The database in the backup is damaged.")
                found["cases"] = connection.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0]
                found["audit_events"] = connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        if (work / "rootiq.pgdump").exists():
            found["pgdump_bytes"] = (work / "rootiq.pgdump").stat().st_size
            if shutil.which("pg_restore"):
                listing = subprocess.run(["pg_restore", "--list", str(work / "rootiq.pgdump")], check=True,
                                         capture_output=True, text=True).stdout
                found["tables"] = sum(1 for line in listing.splitlines() if " TABLE DATA " in line)
        return found


def restore(settings, path: Path) -> None:
    """SQLite only. The current files are kept next to the restored ones as *.before-restore."""
    if db.is_postgres(settings.database_target):
        raise RuntimeError("For PostgreSQL, extract rootiq.pgdump from the backup and use: "
                           "pg_restore --clean --if-exists --no-owner -d <database URL> rootiq.pgdump")
    verify(path)
    data = Path(settings.data_dir)
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(path, "r:gz") as archive:
        archive.extractall(tmp, filter="data")
        for item in Path(tmp).iterdir():
            current = data / item.name
            if current.exists():
                shutil.copy2(current, current.with_name(current.name + ".before-restore"))
            shutil.copy2(item, current)


def main() -> int:
    from .config import Settings

    parser = argparse.ArgumentParser(description="Back up RootIQ data.")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--verify", metavar="FILE")
    parser.add_argument("--restore", metavar="FILE")
    parser.add_argument("--yes", action="store_true", help="confirm --restore (stop the app first)")
    args = parser.parse_args()
    settings = Settings()
    if args.list:
        for path in backups(settings):
            print(f"{path.name}  {path.stat().st_size // 1024} KB")
        return 0
    if args.verify:
        print(verify(Path(args.verify)))
        return 0
    if args.restore:
        if not args.yes:
            print("This replaces the current data with the backup. Stop the app first, then add --yes.")
            return 1
        restore(settings, Path(args.restore))
        print("Restored. The earlier files are kept as *.before-restore. Start the app again.")
        return 0
    path = make(settings)
    print(f"Backup: {path} ({path.stat().st_size // 1024} KB). Keep a copy of the encryption key apart from it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
