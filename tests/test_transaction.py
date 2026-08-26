import copy
import importlib
import io
import json
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import ostriv_macos.installer as installer_module
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

    @unittest.skipIf(os.name == "nt", "Windows does not support opening directories this way")
    def test_atomic_write_syncs_containing_directory_after_replacement(self):
        opened_directories = []
        real_open = os.open

        def record_directory_open(*args):
            descriptor = real_open(*args)
            if args[0] == str(self.path.parent):
                opened_directories.append(descriptor)
            return descriptor

        with patch("ostriv_macos.installer.os.open", side_effect=record_directory_open), patch(
            "ostriv_macos.installer.os.fsync", wraps=os.fsync
        ) as sync:
            atomic_write_json(self.path, {"records": []})

        self.assertEqual(1, len(opened_directories))
        self.assertIn(
            ((opened_directories[0],), {}),
            [(call.args, call.kwargs) for call in sync.call_args_list],
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

    def test_save_failure_does_not_change_any_in_memory_journal_state(self):
        mutations = {
            "start": lambda journal: journal.start("install"),
            "begin": lambda journal: journal.begin(
                "copy", UndoRecord("event", {"undo": "restore"})
            ),
            "mark_applied": lambda journal: journal.mark_applied(0),
            "mark_rolled_back": lambda journal: journal.mark_rolled_back(0),
            "commit": lambda journal: journal.commit(),
        }

        for name, mutation in mutations.items():
            with self.subTest(mutation=name):
                path = Path(self.temp.name) / name / "journal.json"
                journal = InstallJournal(path)
                if name != "start":
                    journal.start("install")
                if name in ("mark_applied", "mark_rolled_back", "commit"):
                    journal.begin("copy", UndoRecord("event", {"undo": "restore"}))
                before_object = journal.data
                before_data = copy.deepcopy(journal.data)
                before_disk = path.read_bytes() if path.exists() else None

                with patch(
                    "ostriv_macos.installer.atomic_write_json",
                    side_effect=OSError("disk full"),
                ):
                    with self.assertRaises(OSError):
                        mutation(journal)

                self.assertIs(before_object, journal.data)
                self.assertEqual(before_data, journal.data)
                actual_disk = path.read_bytes() if path.exists() else None
                self.assertEqual(before_disk, actual_disk)

    def test_commit_save_failure_leaves_operation_recoverable(self):
        journal = InstallJournal(self.path)
        journal.start("install")
        journal.begin("copy", UndoRecord("event", {"undo": "restore"}))
        before_disk = self.path.read_bytes()
        before_object = journal.data

        with patch(
            "ostriv_macos.installer.atomic_write_json", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                journal.commit()

        self.assertIs(before_object, journal.data)
        self.assertFalse(journal.data["complete"])
        with self.assertRaises(PatchError) as caught:
            journal.start("reinstall")
        self.assertEqual("install.recovery_required", caught.exception.code)
        self.assertEqual(before_disk, self.path.read_bytes())

    @unittest.skipIf(os.name == "nt", "Windows does not support opening directories this way")
    def test_directory_sync_failure_reconciles_visible_commit(self):
        journal = InstallJournal(self.path)
        journal.start("install")
        journal.begin("copy", UndoRecord("event", {"undo": "restore"}))
        real_sync = os.fsync
        sync_calls = 0

        def fail_directory_sync(descriptor):
            nonlocal sync_calls
            sync_calls += 1
            if sync_calls == 2:
                raise OSError("directory sync failed")
            return real_sync(descriptor)

        with patch("ostriv_macos.installer.os.fsync", side_effect=fail_directory_sync):
            with self.assertRaisesRegex(OSError, "directory sync failed"):
                journal.commit()

        self.assertTrue(journal.data["complete"])
        self.assertTrue(json.loads(self.path.read_text(encoding="utf-8"))["complete"])
        journal.start("reinstall")
        self.assertEqual("reinstall", journal.data["operation"])

    def test_rollback_failure_is_silent_before_logger_configuration(self):
        package_logger = logging.getLogger("ostriv_macos")
        original_handlers = package_logger.handlers[:]
        original_level = package_logger.level
        original_propagate = package_logger.propagate
        for handler in original_handlers:
            package_logger.removeHandler(handler)
        package_logger.setLevel(logging.NOTSET)
        package_logger.propagate = True
        try:
            module = importlib.reload(installer_module)
            transaction = module.Transaction(
                module.InstallJournal(self.path),
                {"event": lambda record: (_ for _ in ()).throw(RuntimeError("broken"))},
            )
            transaction.start("install")
            transaction.step(
                "copy", module.UndoRecord("event", {"undo": "restore"}), lambda: None
            )

            with redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(PatchError):
                    transaction.rollback()

            self.assertEqual("", stderr.getvalue())
        finally:
            for handler in package_logger.handlers[:]:
                package_logger.removeHandler(handler)
            for handler in original_handlers:
                package_logger.addHandler(handler)
            package_logger.setLevel(original_level)
            package_logger.propagate = original_propagate

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

    def test_successful_rollback_logs_each_reversed_journal_boundary(self):
        log_path = Path(self.temp.name) / "rollback.log"
        configure_logger(log_path)
        transaction = Transaction(InstallJournal(self.path), self.handlers)
        transaction.start("install")
        transaction.step(
            "copy driver", UndoRecord("event", {"undo": "restore driver"}), lambda: None
        )

        transaction.rollback()

        text = log_path.read_text(encoding="utf-8")
        self.assertIn("journal start operation=install", text)
        self.assertIn("journal step begin name=copy driver", text)
        self.assertIn("journal step applied name=copy driver", text)
        self.assertIn("journal rollback start operation=install", text)
        self.assertIn("journal rollback record name=copy driver", text)
        self.assertIn("journal rollback complete operation=install", text)

    def test_incomplete_journal_recovery_logs_start_and_completion(self):
        log_path = Path(self.temp.name) / "recovery.log"
        configure_logger(log_path)
        journal = InstallJournal(self.path)
        journal.start("install")
        journal.begin("copy driver", UndoRecord("event", {"undo": "restore driver"}))

        Transaction(journal, self.handlers).recover_incomplete()

        text = log_path.read_text(encoding="utf-8")
        self.assertIn("journal recovery start operation=install", text)
        self.assertIn("journal rollback record name=copy driver", text)
        self.assertIn("journal recovery complete operation=install", text)


if __name__ == "__main__":
    unittest.main()
