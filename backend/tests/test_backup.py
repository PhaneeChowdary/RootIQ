import json
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.app import backup
from backend.app.database import CaseDatabase
from backend.app.models import CaseCreate


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.settings = SimpleNamespace(data_dir=self.data, database_target=self.data / "rootiq.db", backup_dir="", backup_keep=2)
        self.cases = CaseDatabase(self.data / "rootiq.db")
        self.cases.initialize()
        self.cases.create_case(CaseCreate(title="Checkout fails", customer="Acme", service="payments-api", reported_issue="502"))
        (self.data / "data_sources.json").write_text(json.dumps({"logs": {"type": "demo"}}))
        (self.data / "secret.key").write_text("never-in-a-backup")

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_has_the_data_but_not_the_key(self):
        path = backup.make(self.settings)
        with tarfile.open(path) as archive:
            self.assertEqual(sorted(archive.getnames()), ["data_sources.json", "rootiq.db"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.verify(path)["cases"], 1)

    def test_restore_puts_the_data_back_and_keeps_the_old_files(self):
        path = backup.make(self.settings)
        self.cases.create_case(CaseCreate(title="Second case", customer="Acme", service="web", reported_issue="slow"))
        self.assertEqual(len(self.cases.list_cases()), 2)
        backup.restore(self.settings, path)
        self.assertEqual(len(CaseDatabase(self.data / "rootiq.db").list_cases()), 1)
        self.assertTrue((self.data / "rootiq.db.before-restore").exists())

    def test_damaged_backup_is_found(self):
        path = backup.make(self.settings)
        path.write_bytes(path.read_bytes()[:200])
        with self.assertRaises(Exception):
            backup.verify(path)

    def test_only_the_newest_backups_are_kept(self):
        made = []
        for _ in range(3):
            made.append(backup.make(self.settings))
            time.sleep(1.05)  # the file name has the time in seconds
        self.assertEqual(backup.backups(self.settings), list(reversed(made[1:])))


if __name__ == "__main__":
    unittest.main()
