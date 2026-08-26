import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ostriv_macos.diagnostics import PatchError, configure_logger
from ostriv_macos.installer import InstallJournal, Transaction, UndoRecord, atomic_write_json


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.json"
        self.events = []
        self.handlers = {
            "event": lambda record: self.events.append(record.data["undo"]),
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_failure_rolls_back_applied_steps_in_reverse_order(self):
        transaction = Transaction(InstallJournal(self.path), self.handlers)
        transaction.start("install")
        transaction.step(
            "first",
            UndoRecord("event", {"undo": "undo-first"}),
            lambda: self.events.append("first"),
        )
        transaction.step(
            "second",
            UndoRecord("event", {"undo": "undo-second"}),
            lambda: self.events.append("second"),
        )
        with self.assertRaises(RuntimeError):
            transaction.step(
                "third",
                UndoRecord("event", {"undo": "undo-third"}),
                lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            )
        transaction.rollback()
        self.assertEqual(
            ["first", "second", "undo-third", "undo-second", "undo-first"],
            self.events,
        )

    def test_pending_record_is_recovered_idempotently(self):
        journal = InstallJournal(self.path)
        journal.start("install")
        journal.begin("copy", UndoRecord("event", {"undo": "restore"}))
        Transaction(journal, self.handlers).recover_incomplete()
        Transaction(journal, self.handlers).recover_incomplete()
        self.assertEqual(["restore"], self.events)

    def test_completed_operation_is_not_replayed_by_the_next_operation(self):
        first = Transaction(InstallJournal(self.path), self.handlers)
        first.start("install")
        first.step("copy", UndoRecord("event", {"undo": "old"}), lambda: None)
        first.journal.commit()
        second = Transaction(InstallJournal(self.path), self.handlers)
        second.recover_incomplete()
        second.start("reinstall")
        self.assertEqual([], self.events)
        self.assertEqual([], second.journal.data["records"])

    def test_atomic_write_replaces_existing_json(self):
        self.path.write_text('{"previous": true}', encoding="utf-8")

        atomic_write_json(self.path, {"operation": "install", "records": []})

        self.assertEqual(
            {"operation": "install", "records": []},
            json.loads(self.path.read_text(encoding="utf-8")),
        )

    def test_atomic_write_cleans_temporary_file_when_replacement_fails(self):
        with patch("ostriv_macos.installer.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                atomic_write_json(self.path, {"records": []})

        self.assertEqual([], list(self.path.parent.iterdir()))

    def test_corrupt_journal_is_not_replaced_by_a_new_operation(self):
        self.path.write_text("{", encoding="utf-8")

        with self.assertRaises(PatchError) as caught:
            InstallJournal(self.path)

        self.assertEqual("install.journal_corrupt", caught.exception.code)
        self.assertEqual("{", self.path.read_text(encoding="utf-8"))

    def test_unsupported_journal_schema_is_not_replaced_by_a_new_operation(self):
        self.path.write_text(
            json.dumps({"schema": 2, "complete": True, "records": []}),
            encoding="utf-8",
        )

        with self.assertRaises(PatchError) as caught:
            InstallJournal(self.path)

        self.assertEqual("install.journal_corrupt", caught.exception.code)
        self.assertEqual(2, json.loads(self.path.read_text(encoding="utf-8"))["schema"])

    def test_start_replaces_only_a_completed_operation(self):
        journal = InstallJournal(self.path)
        journal.start("install")
        journal.begin("copy", UndoRecord("event", {"undo": "old"}))
        journal.commit()

        journal.start("reinstall")

        self.assertEqual("reinstall", journal.data["operation"])
        self.assertEqual([], journal.data["records"])
        self.assertFalse(journal.data["complete"])

    def test_start_rejects_an_incomplete_operation_without_overwriting_it(self):
        journal = InstallJournal(self.path)
        journal.start("install")
        journal.begin("copy", UndoRecord("event", {"undo": "restore"}))

        with self.assertRaises(PatchError) as caught:
            journal.start("reinstall")

        self.assertEqual("install.recovery_required", caught.exception.code)
        self.assertEqual("install", journal.data["operation"])
        self.assertEqual(1, len(journal.data["records"]))

    def test_repeated_rollback_does_not_execute_an_undo_handler_twice(self):
        transaction = Transaction(InstallJournal(self.path), self.handlers)
        transaction.start("install")
        transaction.step("copy", UndoRecord("event", {"undo": "restore"}), lambda: None)

        transaction.rollback()
        transaction.rollback()

        self.assertEqual(["restore"], self.events)

    def test_undo_failure_keeps_record_recoverable_and_logs_detail(self):
        def broken_handler(record):
            raise RuntimeError("cannot restore " + record.data["undo"])

        log_path = Path(self.temp.name) / "diagnostics.log"
        configure_logger(log_path)
        transaction = Transaction(InstallJournal(self.path), {"event": broken_handler})
        transaction.start("install")
        transaction.step("copy", UndoRecord("event", {"undo": "backup"}), lambda: None)

        with self.assertRaises(PatchError) as caught:
            transaction.rollback()

        self.assertEqual("install.rollback_failed", caught.exception.code)
        self.assertFalse(transaction.journal.data["complete"])
        self.assertEqual("applied", transaction.journal.data["records"][0]["status"])
        self.assertIn("cannot restore backup", caught.exception.detail)
        self.assertIn("cannot restore backup", log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
