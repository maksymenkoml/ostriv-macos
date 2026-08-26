import hashlib
import io
import json
import os
import plistlib
import stat
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import ostriv_macos.installer as installer_module
from ostriv_macos.diagnostics import CommandResult, PatchError
from ostriv_macos.discovery import Bottle, CrossOverInstall, GameInstallation
from ostriv_macos.installer import (
    BOTTLE_ENV,
    InstallJournal,
    Installer,
    Transaction,
    UndoRecord,
    WineRegistry,
)
from ostriv_macos.payload import PayloadEntry


REGISTRY_KEY = r"HKCU\Software\Wine\AppDefaults\ostriv.exe\DllOverrides"
REGISTRY_VALUE = "opengl32"
DRIVERS = ("opengl32.dll", "libgallium_wgl.dll", "dxil.dll", "libwinpthread-1.dll")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def settings_bytes(multisampling=1, marker=b"user-settings-tail"):
    key = b"bMultisampling"
    return b"\x05\x00fixture-prefix" + struct.pack("<i", len(key)) + key + bytes(
        [multisampling]
    ) + marker


class FakeRunner:
    def __init__(self, registry):
        self.registry = registry
        self.calls = []
        self.add_failures = 0
        self.delete_failures = 0
        self.query_failures = 0
        self.query_failures_after_delete = 0
        self.query_results = []
        self.status_result = CommandResult(0, "running\n", "")

    def run(self, argv, timeout=None):
        argv = list(argv)
        self.calls.append((argv, timeout))
        if argv[-1:] == ["--status"]:
            return self.status_result
        if "reg" not in argv:
            return CommandResult(0, "", "")
        operation = argv[argv.index("reg") + 1]
        key = argv[argv.index("reg") + 2]
        value = argv[argv.index("/v") + 1]
        identity = (key, value)
        if operation == "query":
            if self.query_results:
                return self.query_results.pop(0)
            if self.query_failures:
                self.query_failures -= 1
                return CommandResult(2, "", "\ufffd registry query failed")
            if identity not in self.registry:
                return CommandResult(
                    1,
                    "",
                    "reg: Unable to find the specified registry key or value\n",
                )
            return CommandResult(
                0,
                "\ufffd ignored\n{}    REG_SZ    {}\n".format(
                    value, self.registry[identity]
                ),
                "",
            )
        if operation == "add":
            if self.add_failures:
                self.add_failures -= 1
                return CommandResult(1, "", "\ufffd transient add failure")
            self.registry[identity] = argv[argv.index("/d") + 1]
            return CommandResult(0, "", "")
        if operation == "delete":
            if self.delete_failures:
                self.delete_failures -= 1
                return CommandResult(1, "", "\ufffd transient delete failure")
            self.registry.pop(identity, None)
            if self.query_failures_after_delete:
                self.query_failures = self.query_failures_after_delete
                self.query_failures_after_delete = 0
            return CommandResult(0, "", "")
        raise AssertionError(argv)


class FakeLauncherPort:
    def __init__(self, artifact):
        self.artifact = artifact
        self.calls = []
        self.payload = b"standalone-launcher"

    def install(self, transaction, installation):
        self.calls.append(("install", installation.game_dir))
        undo = UndoRecord(
            "remove_path",
            {
                "path": str(self.artifact),
                "owned_path": str(self.artifact),
                "expected_sha256": digest(self.payload),
            },
        )
        transaction.step(
            "install launcher fixture",
            undo,
            lambda: self.artifact.write_bytes(self.payload),
        )
        return {
            "artifacts": [
                {"path": str(self.artifact), "sha256": digest(self.payload)}
            ],
            "runtime_sha256": digest(self.payload),
            "config_sha256": digest(b"launcher-config"),
            "plist_verified": True,
            "icon_verified": True,
        }

    def verify(self, installation, launcher_state):
        self.calls.append(("verify", installation.game_dir))
        if (
            not self.artifact.is_file()
            or digest(self.artifact.read_bytes()) != launcher_state["runtime_sha256"]
            or not launcher_state["plist_verified"]
            or not launcher_state["icon_verified"]
        ):
            raise PatchError("install.launcher_verify", "Installation failed.", "bad launcher")

    def restore(self, installation, launcher_state):
        self.calls.append(("restore", installation.game_dir))
        if launcher_state.get("legacy"):
            if self.artifact.is_file() and self.artifact.read_bytes() == self.payload:
                self.artifact.unlink()
            return
        expected = launcher_state["artifacts"][0]["sha256"]
        if self.artifact.is_file() and digest(self.artifact.read_bytes()) == expected:
            self.artifact.unlink()


class FailureTransaction(Transaction):
    def __init__(self, journal, handlers, fail_after=None):
        super().__init__(journal, handlers)
        self.fail_after = fail_after
        self.step_count = 0

    def step(self, name, undo, action):
        self.step_count += 1
        if self.step_count != self.fail_after:
            return super().step(name, undo, action)

        def fail_after_action():
            action()
            raise PatchError(
                "test.injected_failure",
                "Installation failed.",
                "failure after journaled mutation {}".format(self.step_count),
            )

        return super().step(name, undo, fail_after_action)


class TrackingInstaller(Installer):
    def __init__(self, *args, fail_after=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_after = fail_after
        self.transactions = []

    def transaction_for(self, installation):
        transaction = FailureTransaction(
            InstallJournal(self.journal_path(installation)),
            self.undo_handlers(installation),
            self.fail_after,
        )
        self.transactions.append(transaction)
        return transaction


class FakeBottleFixture:
    def __init__(self, scope="private", prior_registry="builtin"):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package_root = self.root / "release"
        self.prebuilt = self.package_root / "prebuilt"
        self.prebuilt.mkdir(parents=True)
        self.assets = self.package_root / "assets"
        self.assets.mkdir()
        self.payload = []
        self.payload_bytes = {}
        for index, name in enumerate(DRIVERS):
            data = b"MZfixture-driver-" + bytes([index])
            path = self.prebuilt / name
            path.write_bytes(data)
            self.payload_bytes[name] = data
            self.payload.append(
                PayloadEntry("prebuilt/" + name, len(data), digest(data), True)
            )
        template = settings_bytes(0, b"template-tail")
        (self.assets / "settings.data").write_bytes(template)
        self.payload.append(
            PayloadEntry(
                "assets/settings.data", len(template), digest(template), False
            )
        )

        self.app = self.root / "CrossOver.app"
        self.shared_support = self.app / "Contents/SharedSupport/CrossOver"
        self.bin_dir = self.shared_support / "bin"
        self.bin_dir.mkdir(parents=True)
        for tool in ("wine", "cxbottle", "cxmenu"):
            path = self.bin_dir / tool
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(0o755)
        resources = self.app / "Contents/Resources"
        resources.mkdir(parents=True)
        (resources / "Menu Helper.cpbz2").write_bytes(b"fixture-template")

        bottle_parent = self.root / ("Managed Bottles" if scope == "managed" else "External")
        self.bottle_root = bottle_parent / "Bottle With Spaces"
        self.game_dir = self.bottle_root / "drive_c/Program Files/Ostriv"
        self.game_dir.mkdir(parents=True)
        (self.game_dir / "ostriv.exe").write_bytes(b"genuine game")
        (self.game_dir / "opengl32.dll").write_bytes(b"genuine opengl")
        self.config = self.bottle_root / "cxbottle.conf"
        self.config.write_bytes(
            b'"BottleID" = "fixture-id"\n[EnvironmentVariables]\n'
            b'"USER_SETTING" = "leave this byte-for-byte"\n'
        )
        (self.bottle_root / "system.reg").write_bytes(b"REGEDIT4\r\nuser registry\r\n")
        self.settings = (
            self.bottle_root
            / "drive_c/users/crossover/Saved Games/Ostriv/settings.data"
        )
        self.settings.parent.mkdir(parents=True)
        self.settings.write_bytes(settings_bytes(1))
        self.settings.chmod(0o640)

        crossover = CrossOverInstall(self.app, self.shared_support, "26.2")
        self.bottle = Bottle("Bottle With Spaces", self.bottle_root, scope, crossover)
        self.installation = GameInstallation(self.bottle, self.game_dir, "0.5.9.58")
        self.registry = {}
        if prior_registry is not None:
            self.registry[(REGISTRY_KEY, REGISTRY_VALUE)] = prior_registry
        self.runner = FakeRunner(self.registry)
        self.launcher_artifact = self.root / "Applications/Ostriv (patched).app/launcher"
        self.launcher_artifact.parent.mkdir(parents=True)
        self.launcher = FakeLauncherPort(self.launcher_artifact)

    def cleanup(self):
        self.temp.cleanup()

    def installer(self, fail_after=None):
        return TrackingInstaller(
            self.package_root,
            self.launcher,
            runner=self.runner,
            launcher_destination=self.launcher_artifact.parent,
            fail_after=fail_after,
        )

    def snapshot(self):
        paths = {}
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            mode = stat.S_IMODE(path.lstat().st_mode)
            paths[relative] = (
                "dir" if path.is_dir() else "file",
                b"" if path.is_dir() else path.read_bytes(),
                mode,
            )
        registry = tuple(sorted((key, value, data) for (key, value), data in self.registry.items()))
        return paths, registry


class InstallerTests(unittest.TestCase):
    def test_staging_cleanup_preserves_payload_substituted_after_validation(self):
        fixture = FakeBottleFixture()
        try:
            destination = (fixture.game_dir / "opengl32.dll.bak").resolve()
            genuine = (fixture.game_dir / "opengl32.dll").read_bytes()
            unknown = b"unknown payload replacement after successful validation"
            installer = fixture.installer()
            installer.preflight(fixture.installation, fixture.payload)
            installer._start_ownership()
            transaction = installer.transaction_for(fixture.installation)
            transaction.start("install")
            staging = installer._prepare_copy_staging(
                transaction, fixture.installation, destination
            )

            real_owned_file_status = installer_module._owned_file_status
            substituted = []

            def substitute_after_validation(descriptor, owned_staging):
                status = real_owned_file_status(descriptor, owned_staging)
                if not substituted:
                    payload = (
                        owned_staging.cleanup_directory / owned_staging.path.name
                    )
                    try:
                        payload.unlink()
                    except FileNotFoundError:
                        pass
                    payload.write_bytes(unknown)
                    substituted.append(payload)
                return status

            with patch.object(
                installer_module,
                "_owned_file_status",
                side_effect=substitute_after_validation,
            ):
                recovered = fixture.installer().transaction_for(
                    fixture.installation
                )
                recovered.recover_incomplete()

            self.assertEqual(1, len(substituted))
            preserved = [
                path
                for path in fixture.root.rglob("*")
                if path.is_file() and path.read_bytes() == unknown
            ]
            self.assertEqual(1, len(preserved))
            self.assertTrue(recovered.journal.data["complete"])
            self.assertEqual(
                genuine, (fixture.game_dir / "opengl32.dll").read_bytes()
            )
        finally:
            fixture.cleanup()

    def test_occupied_staging_handoff_keeps_recoverable_transaction_state(self):
        fixture = FakeBottleFixture()
        try:
            installer = fixture.installer()
            genuine = (fixture.game_dir / "opengl32.dll").read_bytes()
            unknown = b"unknown cleanup handoff occupant"
            real_rename_exclusive = installer_module._rename_exclusive
            occupied = []

            def occupy_cleanup_handoff(source, destination):
                if not occupied:
                    cleanup_directory = Path(destination)
                    cleanup_directory.mkdir(mode=0o700)
                    unknown_path = cleanup_directory / "unknown-user-file"
                    unknown_path.write_bytes(unknown)
                    occupied.append((Path(source), cleanup_directory, unknown_path))
                return real_rename_exclusive(source, destination)

            with patch.object(
                installer_module,
                "_rename_exclusive",
                side_effect=occupy_cleanup_handoff,
            ):
                with self.assertRaises(PatchError) as caught:
                    installer.install(fixture.installation, fixture.payload)

            self.assertEqual("install.rollback_failed", caught.exception.code)
            self.assertEqual(1, len(occupied))
            staging_directory, cleanup_directory, unknown_path = occupied[0]
            self.assertTrue(staging_directory.is_dir())
            self.assertEqual(unknown, unknown_path.read_bytes())
            self.assertFalse(installer.state_path(fixture.installation).exists())
            persisted = json.loads(
                installer.journal_path(fixture.installation).read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(persisted["complete"])
            self.assertEqual(
                genuine, (fixture.game_dir / "opengl32.dll").read_bytes()
            )

            unknown_path.unlink()
            cleanup_directory.rmdir()
            recovered = fixture.installer().transaction_for(fixture.installation)
            recovered.recover_incomplete()

            self.assertTrue(recovered.journal.data["complete"])
            self.assertFalse(staging_directory.exists())
            self.assertFalse(cleanup_directory.exists())
            self.assertEqual(
                genuine, (fixture.game_dir / "opengl32.dll").read_bytes()
            )
        finally:
            fixture.cleanup()

    def test_hard_termination_after_staging_fsync_recovers_the_owned_stage(self):
        fixture = FakeBottleFixture()
        try:
            destination = (fixture.game_dir / "opengl32.dll.bak").resolve()
            genuine = (fixture.game_dir / "opengl32.dll").read_bytes()
            installer = fixture.installer()
            installer.preflight(fixture.installation, fixture.payload)
            installer._start_ownership()

            class TerminateBeforeApplied(Transaction):
                def step(inner_self, name, undo, action):
                    index = inner_self.journal.begin(name, undo)
                    inner_self._active_step = index
                    try:
                        action()
                    finally:
                        inner_self._active_step = None
                    if (
                        undo.kind == "remove_staging"
                        and undo.data.get("destination") == str(destination)
                    ):
                        raise SystemExit("simulated hard termination after staging fsync")
                    inner_self.journal.mark_applied(index)

            interrupted = TerminateBeforeApplied(
                InstallJournal(installer.journal_path(fixture.installation)),
                installer.undo_handlers(fixture.installation),
            )
            interrupted.start("install")
            with self.assertRaisesRegex(SystemExit, "after staging fsync"):
                installer.stage_driver_files(
                    interrupted, fixture.installation, fixture.payload
                )

            persisted = json.loads(
                installer.journal_path(fixture.installation).read_text(
                    encoding="utf-8"
                )
            )
            cleanup = next(
                item
                for item in persisted["records"]
                if item["undo"]["kind"] == "remove_staging"
            )
            staging = Path(cleanup["undo"]["data"]["path"])
            staging_directory = Path(
                cleanup["undo"]["data"].get("directory", staging.parent)
            )
            self.assertTrue(staging.is_file())

            fixture.installer().transaction_for(
                fixture.installation
            ).recover_incomplete()

            self.assertFalse(staging.exists())
            self.assertFalse(staging_directory.exists())
            self.assertEqual(
                genuine, (fixture.game_dir / "opengl32.dll").read_bytes()
            )
        finally:
            fixture.cleanup()

    def test_staging_cleanup_preserves_directory_substituted_at_atomic_handoff(self):
        fixture = FakeBottleFixture()
        try:
            destination = (fixture.game_dir / "opengl32.dll.bak").resolve()
            genuine = (fixture.game_dir / "opengl32.dll").read_bytes()
            unknown = b"unknown replacement at the cleanup boundary"
            installer = fixture.installer()
            installer.preflight(fixture.installation, fixture.payload)
            installer._start_ownership()
            transaction = installer.transaction_for(fixture.installation)
            transaction.start("install")
            staging = installer._prepare_copy_staging(
                transaction, fixture.installation, destination
            )

            real_rename_exclusive = getattr(
                installer_module, "_rename_exclusive", None
            )
            substituted = []

            def substitute_at_handoff(source, destination_path):
                if Path(source) == staging.directory and not substituted:
                    staging.path.unlink()
                    staging.directory.rmdir()
                    staging.directory.mkdir(mode=0o700)
                    replacement = staging.directory / "unknown-user-file"
                    replacement.write_bytes(unknown)
                    substituted.append(replacement)
                if real_rename_exclusive is None:
                    raise AssertionError("atomic staging handoff was not implemented")
                return real_rename_exclusive(source, destination_path)

            with patch.object(
                installer_module,
                "_rename_exclusive",
                side_effect=substitute_at_handoff,
                create=True,
            ):
                fixture.installer().transaction_for(
                    fixture.installation
                ).recover_incomplete()

            self.assertEqual(1, len(substituted))
            preserved = [
                path
                for path in fixture.root.rglob("*")
                if path.is_file() and path.read_bytes() == unknown
            ]
            self.assertEqual(1, len(preserved))
            self.assertEqual(
                genuine, (fixture.game_dir / "opengl32.dll").read_bytes()
            )
        finally:
            fixture.cleanup()

    def test_incomplete_copy_journal_removes_owned_staging_file(self):
        fixture = FakeBottleFixture()
        try:
            destination = (fixture.game_dir / "opengl32.dll").resolve()
            genuine = destination.read_bytes()
            installer = fixture.installer()
            installer.preflight(fixture.installation, fixture.payload)
            installer._start_ownership()

            class TerminateBeforeReplace(Transaction):
                def step(inner_self, name, undo, action):
                    if name != "stage opengl32.dll":
                        return super(TerminateBeforeReplace, inner_self).step(
                            name, undo, action
                        )
                    cleanup_records = [
                        item
                        for item in inner_self.journal.data["records"]
                        if item["undo"]["kind"] == "remove_staging"
                        and item["undo"]["data"].get("destination")
                        == str(destination)
                    ]
                    self.assertEqual(1, len(cleanup_records))
                    cleanup_data = cleanup_records[0]["undo"]["data"]
                    staging = Path(cleanup_data["path"])
                    staging_directory = Path(
                        cleanup_data.get("directory", staging.parent)
                    )
                    self.assertEqual(destination.parent, staging_directory.parent)
                    self.assertEqual(staging_directory, staging.parent)
                    staging_status = staging.lstat()
                    self.assertEqual(staging_status.st_dev, cleanup_data["device"])
                    self.assertEqual(staging_status.st_ino, cleanup_data["inode"])
                    inner_self.journal.begin(name, undo)
                    staging.write_bytes(b"partial driver bytes")
                    raise SystemExit("simulated hard termination")

            journal = InstallJournal(installer.journal_path(fixture.installation))
            interrupted = TerminateBeforeReplace(
                journal, installer.undo_handlers(fixture.installation)
            )
            interrupted.start("install")
            with self.assertRaisesRegex(SystemExit, "hard termination"):
                installer.stage_driver_files(
                    interrupted, fixture.installation, fixture.payload
                )

            cleanup = next(
                item
                for item in journal.data["records"]
                if item["undo"]["kind"] == "remove_staging"
            )
            staging = Path(cleanup["undo"]["data"]["path"])
            self.assertTrue(staging.is_file())
            self.assertEqual(genuine, destination.read_bytes())

            recovered = fixture.installer().transaction_for(fixture.installation)
            recovered.recover_incomplete()

            self.assertFalse(staging.exists())
            self.assertEqual(genuine, destination.read_bytes())
        finally:
            fixture.cleanup()

    def test_symlink_driver_destination_is_rejected_before_journaling(self):
        fixture = FakeBottleFixture()
        try:
            destination = fixture.game_dir / "opengl32.dll"
            destination.unlink()
            victim = fixture.root / "outside-bottle-driver.dll"
            original = b"outside victim bytes"
            victim.write_bytes(original)
            destination.symlink_to(victim)
            installer = fixture.installer()

            with self.assertRaises(PatchError) as caught:
                installer.install(fixture.installation, fixture.payload)

            self.assertEqual("install.preflight", caught.exception.code)
            self.assertEqual([], installer.transactions)
            self.assertEqual(original, victim.read_bytes())
            self.assertTrue(destination.is_symlink())
            self.assertFalse(installer.journal_path(fixture.installation).exists())
            self.assertEqual([], list(victim.parent.glob(".outside-bottle-driver.dll.*")))
            self.assertEqual([], list(destination.parent.glob(".opengl32.dll.*")))
        finally:
            fixture.cleanup()

    def test_staging_symlink_substitution_never_follows_or_deletes_symlink(self):
        fixture = FakeBottleFixture()
        try:
            destination = (fixture.game_dir / "opengl32.dll").resolve()
            genuine = destination.read_bytes()
            victim = fixture.root / "outside-bottle-victim"
            victim_original = b"do not truncate or overwrite"
            victim.write_bytes(victim_original)
            installer = fixture.installer()
            installer.preflight(fixture.installation, fixture.payload)
            installer._start_ownership()
            substituted = []

            class SubstituteBeforeCopy(Transaction):
                def step(inner_self, name, undo, action):
                    if name == "stage opengl32.dll":
                        cleanup = next(
                            item
                            for item in inner_self.journal.data["records"]
                            if item["undo"]["kind"] == "remove_staging"
                            and item["undo"]["data"].get("destination")
                            == str(destination)
                        )
                        staging = Path(cleanup["undo"]["data"]["path"])
                        staging.unlink()
                        staging.symlink_to(victim)
                        substituted.append(staging)
                    return super(SubstituteBeforeCopy, inner_self).step(
                        name, undo, action
                    )

            transaction = SubstituteBeforeCopy(
                InstallJournal(installer.journal_path(fixture.installation)),
                installer.undo_handlers(fixture.installation),
            )
            transaction.start("install")
            try:
                with self.assertRaises(OSError):
                    installer.stage_driver_files(
                        transaction, fixture.installation, fixture.payload
                    )
            finally:
                transaction.rollback()

            self.assertEqual(1, len(substituted))
            self.assertEqual(victim_original, victim.read_bytes())
            self.assertEqual(genuine, destination.read_bytes())
            preserved_symlinks = [
                path
                for path in fixture.root.rglob("*")
                if path.is_symlink() and path.resolve() == victim.resolve()
            ]
            self.assertEqual(1, len(preserved_symlinks))
            self.assertEqual(
                [],
                [
                    path
                    for path in destination.parent.glob(".opengl32.dll.*")
                    if not path.is_symlink()
                ],
            )
        finally:
            fixture.cleanup()

    def test_successful_driver_copy_has_ordered_staging_cleanup_and_no_artifact(self):
        fixture = FakeBottleFixture()
        try:
            installer = fixture.installer()
            installer.install(fixture.installation, fixture.payload)
            records = installer.transactions[-1].journal.data["records"]

            for driver in DRIVERS:
                destination = (fixture.game_dir / driver).resolve()
                mutation_index = next(
                    index
                    for index, item in enumerate(records)
                    if item["name"] == "stage {}".format(driver)
                )
                cleanup_indexes = [
                    index
                    for index, item in enumerate(records)
                    if item["undo"]["kind"] == "remove_staging"
                    and item["undo"]["data"].get("destination")
                    == str(destination)
                ]
                self.assertEqual(1, len(cleanup_indexes))
                self.assertLess(cleanup_indexes[0], mutation_index)
                staging = Path(
                    records[cleanup_indexes[0]]["undo"]["data"]["path"]
                )
                self.assertFalse(staging.exists())
                self.assertFalse(staging.parent.exists())
        finally:
            fixture.cleanup()

    def test_driver_copy_failure_before_replace_preserves_genuine_destination(self):
        fixture = FakeBottleFixture()
        try:
            before = fixture.snapshot()

            real_replace = os.replace

            def fail_before_driver_replace(source, destination, *args, **kwargs):
                if (
                    Path(destination).name == "opengl32.dll"
                    and Path(source).name == "payload"
                ):
                    self.assertEqual(
                        b"genuine opengl",
                        (fixture.game_dir / "opengl32.dll").read_bytes(),
                    )
                    raise OSError("injected staging copy failure")
                return real_replace(source, destination, *args, **kwargs)

            with patch.object(
                installer_module.os,
                "replace",
                side_effect=fail_before_driver_replace,
            ):
                with self.assertRaisesRegex(OSError, "staging copy failure"):
                    fixture.installer().install(
                        fixture.installation, fixture.payload
                    )

            self.assertEqual(before, fixture.snapshot())
            self.assertEqual(
                b"genuine opengl",
                (fixture.game_dir / "opengl32.dll").read_bytes(),
            )
            self.assertEqual([], list(fixture.game_dir.glob(".opengl32.dll.*")))
        finally:
            fixture.cleanup()

    def test_failure_after_each_actual_journaled_mutation_restores_original_tree(self):
        counter = FakeBottleFixture()
        try:
            installer = counter.installer()
            installer.install(counter.installation, counter.payload)
            mutation_count = installer.transactions[-1].step_count
        finally:
            counter.cleanup()

        self.assertGreater(mutation_count, 7)
        for fail_after in range(1, mutation_count + 1):
            with self.subTest(fail_after=fail_after):
                fixture = FakeBottleFixture()
                try:
                    before = fixture.snapshot()
                    installer = fixture.installer(fail_after=fail_after)
                    with self.assertRaises(PatchError) as caught:
                        installer.install(fixture.installation, fixture.payload)
                    self.assertEqual("test.injected_failure", caught.exception.code)
                    self.assertEqual(before, fixture.snapshot())
                finally:
                    fixture.cleanup()

    def test_install_reinstall_restore_are_byte_for_byte_idempotent(self):
        fixture = FakeBottleFixture()
        try:
            installer = fixture.installer()
            state = installer.install(fixture.installation, fixture.payload)
            installed_once = fixture.snapshot()
            second_state = installer.install(fixture.installation, fixture.payload)
            self.assertEqual(state, second_state)
            self.assertEqual(installed_once, fixture.snapshot())

            installer.restore(fixture.installation)
            restored_once = fixture.snapshot()
            installer.restore(fixture.installation)
            self.assertEqual(restored_once, fixture.snapshot())
        finally:
            fixture.cleanup()

    def test_reinstall_with_changed_payload_keeps_genuine_restore_backup(self):
        fixture = FakeBottleFixture()
        try:
            installer = fixture.installer()
            installer.install(fixture.installation, fixture.payload)
            replacement = b"MZreplacement-driver"
            source = fixture.prebuilt / "opengl32.dll"
            source.write_bytes(replacement)
            fixture.payload = [
                PayloadEntry(
                    entry.relative_path,
                    len(replacement),
                    digest(replacement),
                    entry.pe,
                )
                if entry.relative_path == "prebuilt/opengl32.dll"
                else entry
                for entry in fixture.payload
            ]

            installer.install(fixture.installation, fixture.payload)
            self.assertEqual(replacement, (fixture.game_dir / "opengl32.dll").read_bytes())
            self.assertEqual(
                b"genuine opengl",
                (fixture.game_dir / "opengl32.dll.bak").read_bytes(),
            )

            installer.restore(fixture.installation)

            self.assertEqual(
                b"genuine opengl",
                (fixture.game_dir / "opengl32.dll").read_bytes(),
            )
        finally:
            fixture.cleanup()

    def test_failure_after_each_restore_mutation_returns_to_installed_tree(self):
        counter = FakeBottleFixture()
        try:
            counter.installer().install(counter.installation, counter.payload)
            restore_installer = counter.installer()
            restore_installer.restore(counter.installation)
            mutation_count = restore_installer.transactions[-1].step_count
        finally:
            counter.cleanup()

        self.assertGreater(mutation_count, 7)
        for fail_after in range(1, mutation_count + 1):
            with self.subTest(fail_after=fail_after):
                fixture = FakeBottleFixture()
                try:
                    fixture.installer().install(fixture.installation, fixture.payload)
                    installed = fixture.snapshot()
                    with self.assertRaises(PatchError) as caught:
                        fixture.installer(fail_after=fail_after).restore(
                            fixture.installation
                        )
                    self.assertEqual("test.injected_failure", caught.exception.code)
                    self.assertEqual(installed, fixture.snapshot())
                finally:
                    fixture.cleanup()

    def test_restart_recovers_install_interrupted_after_state_write(self):
        fixture = FakeBottleFixture()
        try:
            before = fixture.snapshot()
            interrupted = fixture.installer()
            interrupted.preflight(fixture.installation, fixture.payload)
            transaction = interrupted.transaction_for(fixture.installation)
            transaction.start("install")
            interrupted._start_ownership()
            interrupted.stage_driver_files(
                transaction, fixture.installation, fixture.payload
            )
            interrupted.write_app_id(transaction, fixture.installation, "773790")
            interrupted.set_native_override(transaction, fixture.installation)
            interrupted.set_bottle_environment(transaction, fixture.installation)
            interrupted.set_safe_graphics(transaction, fixture.installation)
            launcher_state = fixture.launcher.install(
                transaction, fixture.installation
            )
            state = interrupted.verify(
                fixture.installation, fixture.payload, launcher_state
            )
            interrupted.write_install_state(
                transaction, fixture.installation, state
            )
            self.assertFalse(transaction.journal.data["complete"])

            restarted = fixture.installer()
            restarted.install(fixture.installation, fixture.payload)
            restarted.restore(fixture.installation)

            self.assertEqual(before, fixture.snapshot())
        finally:
            fixture.cleanup()

    def test_install_preserves_genuine_dll_and_unrelated_config_and_settings(self):
        fixture = FakeBottleFixture()
        try:
            original_config = fixture.config.read_bytes()
            original_settings = fixture.settings.read_bytes()
            original_mode = stat.S_IMODE(fixture.settings.stat().st_mode)

            fixture.installer().install(fixture.installation, fixture.payload)

            self.assertEqual(b"genuine opengl", (fixture.game_dir / "opengl32.dll.bak").read_bytes())
            installed_config = fixture.config.read_bytes()
            self.assertIn(b'"USER_SETTING" = "leave this byte-for-byte"\n', installed_config)
            installed_settings = fixture.settings.read_bytes()
            marker = struct.pack("<i", len(b"bMultisampling")) + b"bMultisampling"
            value_position = original_settings.index(marker) + len(marker)
            self.assertEqual(
                original_settings[:value_position], installed_settings[:value_position]
            )
            self.assertEqual(
                original_settings[value_position + 1 :],
                installed_settings[value_position + 1 :],
            )
            self.assertTrue(installed_settings.endswith(b"user-settings-tail"))
            self.assertEqual(0, installed_settings[-len(b"user-settings-tail") - 1])
            self.assertEqual(original_mode, stat.S_IMODE(fixture.settings.stat().st_mode))

            fixture.installer().restore(fixture.installation)
            self.assertEqual(original_config, fixture.config.read_bytes())
            self.assertEqual(original_settings, fixture.settings.read_bytes())
            self.assertEqual(b"genuine opengl", (fixture.game_dir / "opengl32.dll").read_bytes())
        finally:
            fixture.cleanup()

    def test_install_state_records_complete_ownership_schema(self):
        fixture = FakeBottleFixture()
        try:
            state = fixture.installer().install(fixture.installation, fixture.payload)
            stored = json.loads((fixture.bottle_root / "ostriv-macos-state.json").read_text())
            self.assertEqual(1, stored["schema"])
            self.assertEqual("0.1.0", stored["project_version"])
            self.assertEqual(str(fixture.bottle_root.resolve()), stored["bottle_realpath"])
            self.assertEqual(str(fixture.game_dir.resolve()), stored["game_realpath"])
            self.assertTrue(stored["owned_files"])
            self.assertTrue(stored["backup_files"])
            self.assertEqual("builtin", stored["prior_registry_value"])
            self.assertEqual(digest(fixture.config.read_bytes()), stored["installed_config_digest"])
            self.assertEqual(digest(fixture.settings.read_bytes()), stored["installed_settings_digest"])
            self.assertTrue(stored["original_config_backup"])
            self.assertTrue(stored["original_config_digest"])
            self.assertTrue(stored["original_settings_backup"])
            self.assertTrue(stored["original_settings_digest"])
            self.assertEqual(state.launcher_artifacts, stored["launcher_artifacts"])
            self.assertTrue(stored["completed_verification_time"].endswith("Z"))
        finally:
            fixture.cleanup()

    def test_registry_retries_once_and_verifies_the_written_value(self):
        fixture = FakeBottleFixture()
        try:
            fixture.runner.add_failures = 1
            fixture.installer().install(fixture.installation, fixture.payload)
            add_calls = [call for call, _ in fixture.runner.calls if "add" in call]
            self.assertEqual(2, len(add_calls))
            self.assertEqual("native", fixture.registry[(REGISTRY_KEY, REGISTRY_VALUE)])
        finally:
            fixture.cleanup()

    def test_registry_second_failure_is_typed_and_rolls_back_exactly(self):
        fixture = FakeBottleFixture()
        try:
            before = fixture.snapshot()
            fixture.runner.add_failures = 2
            with self.assertRaises(PatchError) as caught:
                fixture.installer().install(fixture.installation, fixture.payload)
            self.assertEqual("install.registry", caught.exception.code)
            self.assertEqual(before, fixture.snapshot())
            add_calls = [call for call, _ in fixture.runner.calls if "add" in call]
            self.assertEqual(2, len(add_calls))
        finally:
            fixture.cleanup()

    def test_registry_query_tolerates_non_utf8_replacement_text(self):
        fixture = FakeBottleFixture()
        try:
            registry = WineRegistry(
                fixture.bin_dir / "wine", fixture.bottle, fixture.runner
            )
            self.assertEqual("builtin", registry.query(REGISTRY_KEY, REGISTRY_VALUE))
        finally:
            fixture.cleanup()

    def test_registry_missing_requires_exact_wine_registry_diagnostic(self):
        exact_missing = (
            "reg: Unable to find the specified registry key",
            "reg: Unable to find the specified registry value",
            "reg: The system was unable to find the specified registry key or value.",
        )
        for diagnostic in exact_missing:
            with self.subTest(exact_missing=diagnostic):
                fixture = FakeBottleFixture()
                try:
                    fixture.runner.query_results = [
                        CommandResult(1, "", diagnostic + "\n")
                    ]
                    registry = WineRegistry(
                        fixture.bin_dir / "wine", fixture.bottle, fixture.runner
                    )
                    self.assertIsNone(
                        registry.query(REGISTRY_KEY, REGISTRY_VALUE)
                    )
                finally:
                    fixture.cleanup()

        ambiguous_failures = (
            "wine: selected bottle not found",
            "wine: unable to find ntdll.dll dependency",
            "reg: backend not found while querying registry",
            "The system was unable to find the specified registry key or value.",
        )
        for diagnostic in ambiguous_failures:
            with self.subTest(ambiguous_failure=diagnostic):
                fixture = FakeBottleFixture()
                try:
                    failure = CommandResult(2, "", diagnostic + "\n")
                    fixture.runner.query_results = [failure, failure]
                    registry = WineRegistry(
                        fixture.bin_dir / "wine", fixture.bottle, fixture.runner
                    )
                    with self.assertRaises(PatchError) as caught:
                        registry.query(REGISTRY_KEY, REGISTRY_VALUE)
                    self.assertEqual("install.registry", caught.exception.code)
                    query_calls = [
                        call for call, _timeout in fixture.runner.calls if "query" in call
                    ]
                    self.assertEqual(2, len(query_calls))
                finally:
                    fixture.cleanup()

    def test_registry_delete_rejects_ambiguous_not_found_output(self):
        fixture = FakeBottleFixture()
        try:
            ambiguous = CommandResult(
                2, "", "wine: selected bottle not found while running reg\n"
            )
            fixture.runner.query_results = [ambiguous] * 4
            registry = WineRegistry(
                fixture.bin_dir / "wine", fixture.bottle, fixture.runner
            )

            with self.assertRaises(PatchError) as caught:
                registry.delete(REGISTRY_KEY, REGISTRY_VALUE)

            self.assertEqual("restore.registry", caught.exception.code)
            delete_calls = [
                call for call, _timeout in fixture.runner.calls if "delete" in call
            ]
            query_calls = [
                call for call, _timeout in fixture.runner.calls if "query" in call
            ]
            self.assertEqual(2, len(delete_calls))
            self.assertEqual(4, len(query_calls))
        finally:
            fixture.cleanup()

    def test_registry_preserves_prior_value_containing_spaces(self):
        fixture = FakeBottleFixture(prior_registry="user override with spaces")
        try:
            installer = fixture.installer()
            state = installer.install(fixture.installation, fixture.payload)
            self.assertEqual("user override with spaces", state.prior_registry_value)

            installer.restore(fixture.installation)

            self.assertEqual(
                "user override with spaces",
                fixture.registry[(REGISTRY_KEY, REGISTRY_VALUE)],
            )
        finally:
            fixture.cleanup()

    def test_registry_query_failure_is_typed_and_rolls_back(self):
        fixture = FakeBottleFixture()
        try:
            before = fixture.snapshot()
            fixture.runner.query_failures = 2

            with self.assertRaises(PatchError) as caught:
                fixture.installer().install(fixture.installation, fixture.payload)

            self.assertEqual("install.registry", caught.exception.code)
            self.assertEqual(before, fixture.snapshot())
        finally:
            fixture.cleanup()

    def test_delete_verification_query_failure_does_not_pass_as_absent(self):
        fixture = FakeBottleFixture(prior_registry=None)
        try:
            installer = fixture.installer()
            installer.install(fixture.installation, fixture.payload)
            installed = fixture.snapshot()
            fixture.runner.query_failures_after_delete = 4

            with self.assertRaises(PatchError) as caught:
                installer.restore(fixture.installation)

            self.assertEqual("restore.registry", caught.exception.code)
            self.assertEqual(installed, fixture.snapshot())
        finally:
            fixture.cleanup()

    def test_restore_retries_registry_delete_once_and_verifies_absence(self):
        fixture = FakeBottleFixture(prior_registry=None)
        try:
            installer = fixture.installer()
            installer.install(fixture.installation, fixture.payload)
            fixture.runner.delete_failures = 1
            installer.restore(fixture.installation)
            delete_calls = [call for call, _ in fixture.runner.calls if "delete" in call]
            self.assertEqual(2, len(delete_calls))
            self.assertNotIn((REGISTRY_KEY, REGISTRY_VALUE), fixture.registry)
        finally:
            fixture.cleanup()

    def test_restore_registry_second_failure_is_typed_and_keeps_installed_tree(self):
        fixture = FakeBottleFixture(prior_registry=None)
        try:
            installer = fixture.installer()
            installer.install(fixture.installation, fixture.payload)
            installed = fixture.snapshot()
            fixture.runner.delete_failures = 2
            with self.assertRaises(PatchError) as caught:
                installer.restore(fixture.installation)
            self.assertEqual("restore.registry", caught.exception.code)
            self.assertEqual(installed, fixture.snapshot())
        finally:
            fixture.cleanup()

    def test_restore_registry_set_failure_uses_restore_error_code(self):
        fixture = FakeBottleFixture(prior_registry="builtin")
        try:
            installer = fixture.installer()
            installer.install(fixture.installation, fixture.payload)
            installed = fixture.snapshot()
            fixture.runner.add_failures = 2
            with self.assertRaises(PatchError) as caught:
                installer.restore(fixture.installation)
            self.assertEqual("restore.registry", caught.exception.code)
            self.assertEqual(installed, fixture.snapshot())
        finally:
            fixture.cleanup()

    def test_bottle_environment_values_are_exact_and_no_steam_variables_are_added(self):
        fixture = FakeBottleFixture()
        try:
            fixture.installer().install(fixture.installation, fixture.payload)
            config = fixture.config.read_text(encoding="utf-8")
            expected = {
                "GALLIUM_DRIVER": "d3d12",
                "wgl_require_gdi_compat": "true",
                "MESA_D3D12_ASYNC_PRESENT": "1",
                "MESA_OSTRIV_TREE_SHADER_HACK": "1",
                "MESA_OSTRIV_FLAT_VARYING_HACK": "1",
                "MESA_GLSL_DISABLE_IO_OPT": "true",
                "MESA_GL_VERSION_OVERRIDE": "4.3",
                "MESA_GLSL_VERSION_OVERRIDE": "430",
            }
            self.assertEqual(expected, BOTTLE_ENV)
            for key, value in expected.items():
                self.assertIn('"{}" = "{}"'.format(key, value), config)
            self.assertFalse(any(key.lower().startswith("steam") for key in BOTTLE_ENV))
        finally:
            fixture.cleanup()

    def test_preflight_checks_all_tools_and_files_before_journaling(self):
        fixture = FakeBottleFixture()
        try:
            (fixture.bin_dir / "wine").unlink()
            (fixture.bin_dir / "cxmenu").unlink()
            (fixture.app / "Contents/Resources/Menu Helper.cpbz2").unlink()
            (fixture.bottle_root / "system.reg").unlink()
            with self.assertRaises(PatchError) as caught:
                fixture.installer().install(fixture.installation, fixture.payload)
            self.assertEqual("install.preflight", caught.exception.code)
            self.assertIn("wine", caught.exception.detail)
            self.assertIn("cxmenu", caught.exception.detail)
            self.assertIn("Menu Helper.cpbz2", caught.exception.detail)
            self.assertIn("system.reg", caught.exception.detail)
            self.assertFalse((fixture.bottle_root / ".ostriv-macos-journal.json").exists())
        finally:
            fixture.cleanup()

    def test_missing_required_payload_inventory_fails_before_transaction(self):
        for missing in ("prebuilt/dxil.dll", "assets/settings.data"):
            with self.subTest(missing=missing):
                fixture = FakeBottleFixture()
                try:
                    payload = [
                        entry
                        for entry in fixture.payload
                        if entry.relative_path != missing
                    ]
                    before = fixture.snapshot()
                    installer = fixture.installer()

                    with self.assertRaises(PatchError) as caught:
                        installer.install(fixture.installation, payload)

                    self.assertEqual("install.payload_inventory", caught.exception.code)
                    self.assertEqual([], installer.transactions)
                    self.assertEqual(before, fixture.snapshot())
                    self.assertFalse(
                        (fixture.bottle_root / ".ostriv-macos-journal.json").exists()
                    )
                finally:
                    fixture.cleanup()

    def test_missing_settings_directory_is_removed_on_install_rollback(self):
        fixture = FakeBottleFixture()
        try:
            fixture.settings.unlink()
            fixture.settings.parent.rmdir()
            before = fixture.snapshot()
            with patch.object(
                fixture.launcher,
                "install",
                side_effect=PatchError(
                    "test.launcher_failure", "Installation failed.", "injected"
                ),
            ):
                with self.assertRaises(PatchError) as caught:
                    fixture.installer().install(
                        fixture.installation, fixture.payload
                    )

            self.assertEqual("test.launcher_failure", caught.exception.code)
            self.assertEqual(before, fixture.snapshot())
            self.assertFalse(fixture.settings.parent.exists())
        finally:
            fixture.cleanup()

    def test_external_private_bottle_status_uses_absolute_root_without_managed_scope(self):
        fixture = FakeBottleFixture(scope="private")
        try:
            fixture.installer().install(fixture.installation, fixture.payload)
            status_call = next(call for call, _ in fixture.runner.calls if call[-1] == "--status")
            self.assertEqual(
                [
                    str(fixture.bin_dir / "cxbottle"),
                    "--bottle",
                    str(fixture.bottle.root),
                    "--status",
                ],
                status_call,
            )
            self.assertNotIn("--scope", status_call)
        finally:
            fixture.cleanup()

    def test_managed_bottle_status_and_registry_keep_managed_scope(self):
        fixture = FakeBottleFixture(scope="managed")
        try:
            fixture.installer().install(fixture.installation, fixture.payload)
            status_call = next(call for call, _ in fixture.runner.calls if call[-1] == "--status")
            self.assertEqual(
                [
                    str(fixture.bin_dir / "cxbottle"),
                    "--bottle",
                    fixture.bottle.name,
                    "--scope",
                    "managed",
                    "--status",
                ],
                status_call,
            )
            registry_call = next(call for call, _ in fixture.runner.calls if "query" in call)
            self.assertEqual(fixture.bottle.name, registry_call[registry_call.index("--bottle") + 1])
            self.assertEqual(["--scope", "managed"], registry_call[3:5])
        finally:
            fixture.cleanup()

    def test_corrupt_state_is_typed_and_restore_does_not_mutate(self):
        fixture = FakeBottleFixture()
        try:
            state_path = fixture.bottle_root / "ostriv-macos-state.json"
            state_path.write_bytes(b"{\xff")
            before = fixture.snapshot()
            with self.assertRaises(PatchError) as caught:
                fixture.installer().restore(fixture.installation)
            self.assertEqual("restore.state_corrupt", caught.exception.code)
            self.assertEqual(before, fixture.snapshot())
        finally:
            fixture.cleanup()

    def test_state_cannot_claim_paths_outside_the_selected_installation(self):
        fixture = FakeBottleFixture()
        try:
            installer = fixture.installer()
            installer.install(fixture.installation, fixture.payload)
            victim = fixture.root / "unowned-victim"
            victim.write_bytes(b"must survive")
            state_path = fixture.bottle_root / "ostriv-macos-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["owned_files"].append(
                {"path": str(victim), "sha256": digest(victim.read_bytes())}
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")
            before = fixture.snapshot()

            with self.assertRaises(PatchError) as caught:
                installer.restore(fixture.installation)

            self.assertEqual("restore.state_corrupt", caught.exception.code)
            self.assertEqual(before, fixture.snapshot())
            self.assertEqual(b"must survive", victim.read_bytes())
        finally:
            fixture.cleanup()

    def test_state_cannot_claim_unrelated_file_inside_game_directory(self):
        fixture = FakeBottleFixture()
        try:
            installer = fixture.installer()
            installer.install(fixture.installation, fixture.payload)
            executable = fixture.game_dir / "ostriv.exe"
            state_path = fixture.bottle_root / "ostriv-macos-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["owned_files"].append(
                {"path": str(executable.resolve()), "sha256": digest(executable.read_bytes())}
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")
            before = fixture.snapshot()

            with self.assertRaises(PatchError) as caught:
                installer.restore(fixture.installation)

            self.assertEqual("restore.state_corrupt", caught.exception.code)
            self.assertEqual(before, fixture.snapshot())
            self.assertEqual(b"genuine game", executable.read_bytes())
        finally:
            fixture.cleanup()

    def test_restore_never_deletes_an_owned_path_replaced_with_unknown_content(self):
        fixture = FakeBottleFixture()
        try:
            installer = fixture.installer()
            installer.install(fixture.installation, fixture.payload)
            path = fixture.game_dir / "dxil.dll"
            path.write_bytes(b"unknown user replacement")
            installer.restore(fixture.installation)
            self.assertEqual(b"unknown user replacement", path.read_bytes())
        finally:
            fixture.cleanup()

    def test_legacy_restore_migrates_only_recognizable_owned_artifacts(self):
        fixture = FakeBottleFixture(prior_registry=None)
        try:
            driver = fixture.game_dir / "opengl32.dll"
            backup = fixture.game_dir / "opengl32.dll.bak"
            backup.write_bytes(b"genuine legacy opengl")
            driver.write_bytes(fixture.payload_bytes["opengl32.dll"])
            (fixture.game_dir / "dxil.dll").write_bytes(fixture.payload_bytes["dxil.dll"])
            (fixture.game_dir / "steam_appid.txt").write_text("773790", encoding="ascii")
            fixture.registry[(REGISTRY_KEY, REGISTRY_VALUE)] = "native"
            fixture.config.write_text(
                fixture.config.read_text()
                + '\n"GALLIUM_DRIVER" = "d3d12"\n'
                + '"UNKNOWN_ENV" = "preserve"\n',
                encoding="utf-8",
            )
            original_settings = settings_bytes(1, b"legacy-settings")
            fixture.settings.with_name("settings.data.bak").write_bytes(original_settings)
            fixture.settings.write_bytes(settings_bytes(0, b"legacy-settings"))
            unknown = fixture.game_dir / "unowned.dll"
            unknown.write_bytes(b"leave me")
            fixture.launcher_artifact.write_bytes(fixture.launcher.payload)

            fixture.installer().restore(fixture.installation)

            self.assertEqual(b"genuine legacy opengl", driver.read_bytes())
            self.assertFalse(backup.exists())
            self.assertFalse((fixture.game_dir / "dxil.dll").exists())
            self.assertFalse((fixture.game_dir / "steam_appid.txt").exists())
            self.assertEqual("preserve", fixture.config.read_text().split('"UNKNOWN_ENV" = "')[1].split('"')[0])
            self.assertNotIn('"GALLIUM_DRIVER" = "d3d12"', fixture.config.read_text())
            self.assertEqual(original_settings, fixture.settings.read_bytes())
            self.assertEqual(b"leave me", unknown.read_bytes())
            self.assertFalse(fixture.launcher_artifact.exists())
            self.assertIn(("restore", fixture.game_dir), fixture.launcher.calls)
        finally:
            fixture.cleanup()

    def test_legacy_environment_cleanup_is_limited_to_environment_section(self):
        fixture = FakeBottleFixture()
        try:
            fixture.config.write_text(
                fixture.config.read_text(encoding="utf-8")
                + '"GALLIUM_DRIVER" = "d3d12"\n'
                + "[UnrelatedSection]\n"
                + '"GALLIUM_DRIVER" = "d3d12"\n',
                encoding="utf-8",
            )

            fixture.installer().restore(fixture.installation)

            config = fixture.config.read_text(encoding="utf-8")
            environment, unrelated = config.split("[UnrelatedSection]", 1)
            self.assertNotIn('"GALLIUM_DRIVER" = "d3d12"', environment)
            self.assertIn('"GALLIUM_DRIVER" = "d3d12"', unrelated)
        finally:
            fixture.cleanup()

    def test_verify_detects_payload_tampering_and_launcher_is_verified(self):
        fixture = FakeBottleFixture()
        try:
            installer = fixture.installer()
            state = installer.install(fixture.installation, fixture.payload)
            (fixture.game_dir / "dxil.dll").write_bytes(b"tampered")
            with self.assertRaises(PatchError) as caught:
                installer.verify(
                    fixture.installation, fixture.payload, state.launcher_artifacts
                )
            self.assertEqual("install.verify", caught.exception.code)
            self.assertIn(("verify", fixture.game_dir), fixture.launcher.calls)
        finally:
            fixture.cleanup()

    def test_verify_detects_collateral_config_and_settings_changes(self):
        fixture = FakeBottleFixture()
        try:
            installer = fixture.installer()
            state = installer.install(fixture.installation, fixture.payload)
            original_config = fixture.config.read_bytes()
            original_settings = fixture.settings.read_bytes()
            mutations = (
                (fixture.config, original_config.replace(b"leave this", b"lost this")),
                (fixture.settings, original_settings + b"unexpected-tail"),
            )
            for path, changed in mutations:
                with self.subTest(path=path.name):
                    path.write_bytes(changed)
                    with self.assertRaises(PatchError) as caught:
                        installer.verify(
                            fixture.installation,
                            fixture.payload,
                            state.launcher_artifacts,
                        )
                    self.assertEqual("install.verify", caught.exception.code)
                    fixture.config.write_bytes(original_config)
                    fixture.settings.write_bytes(original_settings)
        finally:
            fixture.cleanup()

    def test_lower_layers_never_print(self):
        fixture = FakeBottleFixture()
        try:
            with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                installer = fixture.installer()
                installer.install(fixture.installation, fixture.payload)
                installer.restore(fixture.installation)
            self.assertEqual("", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())
        finally:
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
