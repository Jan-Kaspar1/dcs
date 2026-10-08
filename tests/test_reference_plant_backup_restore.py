"""The configuration-backup and restore leg's declaration and
audit seams, unit-tested against faked backups: backup_restore.LEG
registers the leg with its two missing-artifact cases,
backup_restore.audit_backup holds the whole set — the manifest pin,
every named document, each declared controller's durability files,
and every file's recorded sha256 — and
backup_restore.check_journal_file holds the run boundaries and the
continuing `seq` order. The backup record
docs/releases/backup-record.md names the same set."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_CI_DIR = Path(__file__).resolve().parents[1] / "reference-plant" / "ci"
sys.path.insert(0, str(_CI_DIR))
_PATH = _CI_DIR / "legs" / "backup_restore.py"
_spec = importlib.util.spec_from_file_location("backup_restore", _PATH)
backup_restore = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backup_restore)

_DOC = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "releases"
    / "backup-record.md"
)


def manifest():
    """A minimal deployment manifest shape: one pair, durability
    files declared on both members."""
    controllers = []
    for name, standby in (("ctrl-a", False), ("ctrl-b", True)):
        entry = {
            "name": name,
            "listen": "0.0.0.0:8080",
            "state_file": "/var/tmp/state.json",
            "journal_file": "/var/tmp/journal.jsonl",
            "history_file": "/var/tmp/history.jsonl",
        }
        if standby:
            entry["standby"] = "ctrl-a:8080"
        controllers.append(entry)
    return {
        "dcs_release": "v0.10.0",
        "model": {"path": "model/plant.json", "fingerprint": "ed0f55"},
        "dynamics": {"path": "model/dynamics.json"},
        "controllers": controllers,
    }


def backup_tree(root, manifest_doc=None):
    """A complete backup directory under `root`: the three named
    documents, a `backup.json` record pinning the manifest, and both
    controllers' durability files hashed into it."""
    manifest_doc = manifest_doc if manifest_doc is not None else manifest()
    backup = Path(root) / "backup"
    backup.mkdir()
    for name in (
        "model-plant.json",
        "model-dynamics.json",
        "manifest.json",
    ):
        (backup / name).write_text("{}\n")
    record = {
        "dcs_release": manifest_doc["dcs_release"],
        "model": {"fingerprint": "ed0f55", "sha256": "0" * 64},
        "controllers": {},
        "files": {},
    }
    for controller in manifest_doc["controllers"]:
        name = controller["name"]
        entry = {}
        for field in ("state_file", "journal_file", "history_file"):
            relative = f"{name}/{field}.bak"
            target = backup / relative
            target.parent.mkdir(exist_ok=True)
            target.write_text(f"{name} {field}\n")
            entry[field] = relative
            record["files"][relative] = backup_restore.sha256(str(target))
        record["controllers"][name] = entry
    (backup / "backup.json").write_text(json.dumps(record))
    return backup


class LegRegistrationTests(unittest.TestCase):
    """The leg's stage registration."""

    def test_leg_registers_after_the_revision_roll(self):
        self.assertEqual(backup_restore.LEG["order"], 893)
        self.assertEqual(
            backup_restore.LEG["title"],
            "the configuration-backup and restore leg",
        )
        self.assertEqual(backup_restore.LEG["passes"], "backup-restore-leg")

    def test_tampers_name_their_missing_artifact(self):
        tampers = {entry["name"]: entry for entry in backup_restore.LEG["tampers"]}
        self.assertEqual(
            set(tampers), {"missing-state-file", "missing-journal-file"}
        )
        self.assertIn(
            "the backup carries no state file",
            tampers["missing-state-file"]["evidence"],
        )
        self.assertIn(
            "the backup carries no journal file",
            tampers["missing-journal-file"]["evidence"],
        )


class AuditTests(unittest.TestCase):
    """The completeness audit's findings."""

    def audit(self, backup, manifest_doc=None):
        failures = []
        try:
            record = backup_restore.audit_backup(
                str(backup), manifest_doc if manifest_doc is not None else manifest(), failures
            )
        except Exception as error:
            failures.extend(str(arg) for arg in error.args)
            return None, failures
        return record, failures

    def test_whole_backup_passes(self):
        with tempfile.TemporaryDirectory() as root:
            backup = backup_tree(root)
            record, failures = self.audit(backup)
        self.assertEqual(failures, [])
        self.assertEqual(record["dcs_release"], "v0.10.0")

    def test_repointed_pin_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            backup = backup_tree(root)
            manifest_doc = manifest()
            manifest_doc["dcs_release"] = "v0.11.0"
            _record, failures = self.audit(backup, manifest_doc)
        self.assertEqual(len(failures), 1)
        self.assertIn("dcs_release", failures[0])

    def test_missing_state_file_names_its_controller(self):
        with tempfile.TemporaryDirectory() as root:
            backup = backup_tree(root)
            (backup / "ctrl-a" / "state_file.bak").unlink()
            _record, failures = self.audit(backup)
        self.assertEqual(len(failures), 1)
        self.assertIn("the backup carries no state file for ctrl-a", failures[0])

    def test_missing_journal_file_names_its_controller(self):
        with tempfile.TemporaryDirectory() as root:
            backup = backup_tree(root)
            (backup / "ctrl-b" / "journal_file.bak").unlink()
            _record, failures = self.audit(backup)
        self.assertEqual(len(failures), 1)
        self.assertIn(
            "the backup carries no journal file for ctrl-b", failures[0]
        )

    def test_changed_bytes_fail_the_recorded_hash(self):
        with tempfile.TemporaryDirectory() as root:
            backup = backup_tree(root)
            with open(backup / "ctrl-a" / "state_file.bak", "a") as handle:
                handle.write("changed under its record\n")
            _record, failures = self.audit(backup)
        self.assertEqual(len(failures), 1)
        self.assertIn("sha256", failures[0])


class JournalFileTests(unittest.TestCase):
    """The durable file's contract across the restore."""

    def write(self, path, boundaries, seqs):
        with open(path, "w") as handle:
            for run in boundaries:
                handle.write(json.dumps({"run_boundary": run}) + "\n")
            for seq in seqs:
                handle.write(
                    json.dumps({"entry": {"seq": seq, "event": {}}}) + "\n"
                )

    def check(self, path, persisted):
        failures = []
        backup_restore.check_journal_file(path, persisted, "ctrl-a", failures)
        return failures

    def test_two_boundaries_and_continuing_seqs_hold(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "journal.jsonl")
            self.write(
                path,
                [{"run": 1, "tick": 0}, {"run": 2, "tick": 7}],
                [1, 2, 3],
            )
            self.assertEqual(self.check(path, 7), [])

    def test_cold_start_alone_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "journal.jsonl")
            self.write(path, [{"run": 1, "tick": 0}], [1, 2])
            failures = self.check(path, 7)
        self.assertEqual(len(failures), 1)
        self.assertIn("journal boundaries", failures[0])

    def test_broken_seq_order_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "journal.jsonl")
            self.write(
                path,
                [{"run": 1, "tick": 0}, {"run": 2, "tick": 7}],
                [1, 3],
            )
            failures = self.check(path, 7)
        self.assertEqual(len(failures), 1)
        self.assertIn("seqs", failures[0])

    def test_resume_tick_names_the_backup(self):
        failures = []
        with self.assertRaises(Exception):
            backup_restore.resume_tick(
                ["listening on 127.0.0.1:8080"], 7, failures
            )
        self.assertIn("never reported a resume", failures[0])

    def test_resume_at_another_tick_is_refused(self):
        failures = []
        with self.assertRaises(Exception):
            backup_restore.resume_tick(
                ["resumed from state file at tick 5"], 7, failures
            )
        self.assertIn("resumed at tick 5", failures[0])

    def test_resume_at_the_persisted_tick_holds(self):
        failures = []
        resumed = backup_restore.resume_tick(
            ["resumed from state file at tick 7"], 7, failures
        )
        self.assertEqual(resumed, 7)
        self.assertEqual(failures, [])


class RecordDocTests(unittest.TestCase):
    """The backup record names the drilled set."""

    def test_record_doc_names_every_artifact(self):
        doc = _DOC.read_text()
        for term in (
            "`model`",
            "`dynamics`",
            "`state`",
            "`journal`",
            "`history`",
            "`manifest pin`",
            "backup-restore-failed",
            "backup-restore-nondeterministic",
            "backup-restore-unchecked",
            "Compatible repin rollback",
            "Revised-model rollback",
        ):
            self.assertIn(term, doc)


if __name__ == "__main__":
    unittest.main()
