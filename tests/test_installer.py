import base64
import ctypes
import errno
import hashlib
import io
import json
import os
import plistlib
import stat
import struct
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import ostriv_macos.installer as installer_module
from ostriv_macos.cli import _player_action
from ostriv_macos.diagnostics import CommandResult, PatchError, configure_logger
from ostriv_macos.discovery import Bottle, CrossOverInstall, GameInstallation
from ostriv_macos.installer import (
    BOTTLE_ENV,
    InstallJournal,
    Installer,
    Transaction,
    UndoRecord,
    WineRegistry,
)
from ostriv_macos.launcher import LauncherInstaller
from ostriv_macos.launcher_runtime import ProcessLock
from ostriv_macos.payload import PayloadEntry


REGISTRY_KEY = r"HKCU\Software\Wine\AppDefaults\ostriv.exe\DllOverrides"
REGISTRY_VALUE = "opengl32"
DRIVERS = ("opengl32.dll", "libgallium_wgl.dll", "dxil.dll", "libwinpthread-1.dll")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def lock_identity_integrity(owner_token, lock_digest, device, inode):
    identity = json.dumps(
        [
            "ostriv-restore-lock-identity-v1",
            owner_token,
            lock_digest,
            device,
            inode,
            0o600,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return digest(identity)


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


class FakeRestoreProfiles:
    def __init__(self, current="/Profiles/sRGB.icc"):
        self.current = current
        self.calls = []

    def get(self):
        return self.current

    def set(self, value):
        self.calls.append(value)
        self.current = value
        return True


def real_launcher_for_restore(fixture, profiles):
    destination = fixture.root / "Real Applications/CrossOver"
    destination.mkdir(parents=True)
    game_app = destination / "Games/Ostriv.app"
    icon = game_app / "Contents/Resources/CrossOverHelper.icns"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"real-ostriv-icon")
    with (game_app / "Contents/Info.plist").open("wb") as stream:
        plistlib.dump(
            {
                "CXHelperAppBottleName": fixture.bottle.name,
                "CrossOverHelperCommand": '"C:/Program Files/Ostriv/Ostriv.lnk"',
            },
            stream,
        )

    def extract(_template, pending):
        executable = pending / "Contents/MacOS/Menu Helper"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"menu-helper")
        executable.chmod(0o755)
        resources = pending / "Contents/Resources"
        resources.mkdir(parents=True)
        with (pending / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump(
                {
                    "CFBundleExecutable": "Menu Helper",
                    "CFBundleIconFile": "CrossOverHelper.icns",
                },
                stream,
            )

    launcher = LauncherInstaller(
        fixture.package_root,
        launcher_destination=destination,
        runner=fixture.runner,
        runtime_source=Path(__file__).parents[1]
        / "ostriv_macos/launcher_runtime.py",
        extractor=extract,
        profile_backend_factory=lambda: profiles,
    )
    installer = TrackingInstaller(
        fixture.package_root,
        launcher,
        runner=fixture.runner,
        launcher_destination=destination,
    )
    return launcher, installer


class FinalLockUnlinkInterruption(BaseException):
    pass


def interrupt_restore_after_final_lock_unlink(installer, installation):
    """Leave the exact journal produced after Restore unlinks its owned lock."""
    armed = [True]

    class InterruptingTransaction(Transaction):
        def __init__(self, journal, handlers):
            super().__init__(journal, handlers)
            self.interrupted = False

        def step(self, name, undo, action):
            if name != "remove launcher recovery lock" or not armed:
                return super().step(name, undo, action)
            self.journal.begin(name, undo)
            action()
            armed.pop()
            self.interrupted = True
            raise FinalLockUnlinkInterruption()

        def rollback(self):
            if self.interrupted:
                raise FinalLockUnlinkInterruption()
            return super().rollback()

    def transaction_for(selected):
        return InterruptingTransaction(
            InstallJournal(installer.journal_path(selected)),
            installer.undo_handlers(selected),
        )

    with patch.object(installer, "transaction_for", side_effect=transaction_for):
        try:
            installer.restore(installation)
        except FinalLockUnlinkInterruption:
            return
    raise AssertionError("Restore reached completion instead of final-unlink interruption")


class LauncherRestoreInterruption(BaseException):
    pass


def interrupt_restore_after_launcher(installer, installation):
    """Leave the exact journal produced after Restore changes the launcher."""
    class InterruptingTransaction(Transaction):
        def __init__(self, journal, handlers):
            super().__init__(journal, handlers)
            self.interrupted = False

        def step(self, name, undo, action):
            result = super().step(name, undo, action)
            if name == "restore launcher":
                self.interrupted = True
                raise LauncherRestoreInterruption()
            return result

        def rollback(self):
            if self.interrupted:
                raise LauncherRestoreInterruption()
            return super().rollback()

    def transaction_for(selected):
        return InterruptingTransaction(
            InstallJournal(installer.journal_path(selected)),
            installer.undo_handlers(selected),
        )

    with patch.object(installer, "transaction_for", side_effect=transaction_for):
        try:
            installer.restore(installation)
        except LauncherRestoreInterruption:
            return
    raise AssertionError("Restore reached completion instead of launcher interruption")


class StateUnlinkInterruption(BaseException):
    pass


def interrupt_restore_after_state_unlink(installer, installation):
    """Leave the exact journal produced after Restore unlinks ownership state."""
    armed = [True]

    class InterruptingTransaction(Transaction):
        def __init__(self, journal, handlers):
            super().__init__(journal, handlers)
            self.interrupted = False

        def step(self, name, undo, action):
            if name != "remove ownership state" or not armed:
                return super().step(name, undo, action)
            self.journal.begin(name, undo)
            action()
            armed.pop()
            self.interrupted = True
            raise StateUnlinkInterruption()

        def rollback(self):
            if self.interrupted:
                raise StateUnlinkInterruption()
            return super().rollback()

    def transaction_for(selected):
        return InterruptingTransaction(
            InstallJournal(installer.journal_path(selected)),
            installer.undo_handlers(selected),
        )

    with patch.object(installer, "transaction_for", side_effect=transaction_for):
        try:
            installer.restore(installation)
        except StateUnlinkInterruption:
            return
    raise AssertionError("Restore reached completion instead of state-unlink interruption")


def active_lock_snapshot_references(journal, lock):
    """Return exact lock snapshots in rollback-active supported list fields."""
    references = []
    for index, item in enumerate(journal["records"]):
        if item["status"] not in ("pending", "applied"):
            continue
        undo = item["undo"]
        for field in ("snapshots", "restore_files"):
            snapshots = undo["data"].get(field, [])
            if not isinstance(snapshots, list):
                continue
            for snapshot in snapshots:
                if isinstance(snapshot, dict) and snapshot.get("path") == str(lock):
                    references.append((index, item, field, snapshot))
    return references


def active_record_snapshot(journal, record_name, path):
    """Return one exact snapshot from the named rollback-active record."""
    matches = []
    for item in journal["records"]:
        if item["status"] not in ("pending", "applied") or item["name"] != record_name:
            continue
        snapshots = item["undo"]["data"].get("snapshots", [])
        if not isinstance(snapshots, list):
            continue
        matches.extend(
            snapshot
            for snapshot in snapshots
            if isinstance(snapshot, dict) and snapshot.get("path") == str(path)
        )
    if len(matches) != 1:
        raise AssertionError(
            "expected one active {!r} snapshot for {}, found {}".format(
                record_name, path, len(matches)
            )
        )
    return matches[0]


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
            status = path.lstat()
            mode = stat.S_IMODE(status.st_mode)
            if stat.S_ISLNK(status.st_mode):
                paths[relative] = ("symlink", os.readlink(path), mode)
            elif stat.S_ISDIR(status.st_mode):
                paths[relative] = ("dir", b"", mode)
            else:
                paths[relative] = ("file", path.read_bytes(), mode)
        registry = tuple(sorted((key, value, data) for (key, value), data in self.registry.items()))
        return paths, registry


class RenameAt2Fixture:
    """Exercise the libc boundary while keeping Linux calls portable to macOS."""

    AT_FDCWD = -100
    RENAME_NOREPLACE = 1

    def __call__(
        self,
        source_directory,
        source_name,
        destination_directory,
        destination_name,
        flags,
    ):
        if flags != self.RENAME_NOREPLACE:
            ctypes.set_errno(errno.EINVAL)
            return -1
        source_name = os.fsdecode(source_name)
        destination_name = os.fsdecode(destination_name)
        source_options = (
            {}
            if source_directory == self.AT_FDCWD
            else {"src_dir_fd": source_directory}
        )
        destination_options = (
            {}
            if destination_directory == self.AT_FDCWD
            else {"dst_dir_fd": destination_directory}
        )
        try:
            os.stat(
                destination_name,
                dir_fd=None
                if destination_directory == self.AT_FDCWD
                else destination_directory,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        except OSError as error:
            ctypes.set_errno(error.errno)
            return -1
        else:
            ctypes.set_errno(errno.EEXIST)
            return -1
        try:
            os.rename(
                source_name,
                destination_name,
                **source_options,
                **destination_options,
            )
        except OSError as error:
            ctypes.set_errno(error.errno)
            return -1
        return 0


class ExclusiveRenameTests(unittest.TestCase):
    def test_renameat2_fallback_moves_to_an_unoccupied_path(self):
        # Catches losing the Linux equivalent of macOS RENAME_EXCL.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.write_bytes(b"owned")

            with patch.object(
                installer_module, "_RENAME_EXCLUSIVE", None
            ), patch.object(
                installer_module,
                "_RENAMEAT2",
                RenameAt2Fixture(),
                create=True,
            ):
                installer_module._rename_exclusive(source, destination)

            self.assertFalse(source.exists())
            self.assertEqual(b"owned", destination.read_bytes())

    def test_renameat2_fallback_never_replaces_an_occupied_path(self):
        # Catches using ordinary rename, which would destroy unrelated content.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            source.write_bytes(b"owned")
            destination.write_bytes(b"unrelated")

            with patch.object(
                installer_module, "_RENAME_EXCLUSIVE", None
            ), patch.object(
                installer_module,
                "_RENAMEAT2",
                RenameAt2Fixture(),
                create=True,
            ):
                with self.assertRaises(OSError) as caught:
                    installer_module._rename_exclusive(source, destination)

            self.assertEqual(errno.EEXIST, caught.exception.errno)
            self.assertEqual(b"owned", source.read_bytes())
            self.assertEqual(b"unrelated", destination.read_bytes())

    def test_descriptor_relative_rename_uses_the_renameat2_fallback(self):
        # Catches Linux recovery failing after it opens the trusted directory.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source").write_bytes(b"owned")
            descriptor = os.open(str(root), os.O_RDONLY)
            self.addCleanup(os.close, descriptor)

            with patch.object(
                installer_module, "_RENAMEAT_EXCLUSIVE", None
            ), patch.object(
                installer_module,
                "_RENAMEAT2",
                RenameAt2Fixture(),
                create=True,
            ):
                installer_module._renameat_exclusive(
                    descriptor,
                    "source",
                    descriptor,
                    "destination",
                )

            self.assertFalse((root / "source").exists())
            self.assertEqual(b"owned", (root / "destination").read_bytes())


class InstallerTests(unittest.TestCase):
    def assert_recovery_rejected_before_mutation(
        self,
        fixture,
        installer,
        profiles,
    ):
        journal_path = installer.journal_path(fixture.installation)
        journal_before = journal_path.read_bytes()
        filesystem_before = fixture.snapshot()
        profile_before = (profiles.current, list(profiles.calls))
        calls_before = list(fixture.runner.calls)

        with self.assertRaises(PatchError) as caught:
            installer.restore(fixture.installation)

        self.assertEqual("restore.launcher_recovery", caught.exception.code)
        self.assertEqual(journal_before, journal_path.read_bytes())
        self.assertEqual(filesystem_before, fixture.snapshot())
        self.assertEqual(profile_before, (profiles.current, profiles.calls))
        self.assertEqual(calls_before, fixture.runner.calls)
        return caught.exception

    def test_successful_install_logs_stages_verification_and_completion(self):
        fixture = FakeBottleFixture()
        self.addCleanup(fixture.cleanup)
        log_path = fixture.root / "install.log"
        configure_logger(log_path)

        fixture.installer().install(fixture.installation, fixture.payload)

        text = log_path.read_text(encoding="utf-8")
        self.assertGreater(len(text), 0)
        self.assertIn("install start bottle=Bottle With Spaces", text)
        self.assertIn("install stage=payload_validation status=OK", text)
        self.assertIn("install stage=preflight status=OK", text)
        self.assertIn("install verification start", text)
        self.assertIn("install verification status=OK", text)
        self.assertIn("install complete bottle=Bottle With Spaces", text)

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

    @unittest.skipIf(os.name == "nt", "directory fsync is POSIX-only")
    def test_restore_directory_sync_failure_keeps_installed_state_recoverable(self):
        """A backup move is not complete until its containing directory is synced."""
        fixture = FakeBottleFixture()
        try:
            installer = fixture.installer()
            installer.install(fixture.installation, fixture.payload)
            before = fixture.snapshot()
            real_sync = os.fsync
            settings_directory = os.stat(fixture.settings.parent)
            failures = [OSError("injected restore directory sync failure")]

            def fail_settings_sync(descriptor):
                observed = os.fstat(descriptor)
                if (
                    stat.S_ISDIR(observed.st_mode)
                    and observed.st_dev == settings_directory.st_dev
                    and observed.st_ino == settings_directory.st_ino
                    and failures
                ):
                    raise failures.pop()
                return real_sync(descriptor)

            with patch.object(
                installer_module.os, "fsync", side_effect=fail_settings_sync
            ):
                with self.assertRaisesRegex(OSError, "restore directory sync failure"):
                    installer.restore(fixture.installation)

            self.assertEqual(before, fixture.snapshot())
            self.assertTrue(installer.state_path(fixture.installation).is_file())
            self.assertFalse(installer.journal_path(fixture.installation).exists())
        finally:
            fixture.cleanup()

    @unittest.skipIf(os.name == "nt", "directory fsync is POSIX-only")
    def test_legacy_unlink_directory_sync_failure_restores_removed_app_id(self):
        """Legacy unlink completion includes the containing-directory sync boundary."""
        fixture = FakeBottleFixture()
        try:
            app_id = fixture.game_dir / "steam_appid.txt"
            app_id.write_bytes(b"773790")
            installer = fixture.installer()
            real_sync = installer_module._fsync_directory
            failures = [OSError("injected legacy unlink sync failure")]

            def fail_game_directory(path):
                if Path(path).resolve() == fixture.game_dir.resolve() and failures:
                    raise failures.pop()
                return real_sync(path)

            with patch.object(
                installer_module, "_fsync_directory", side_effect=fail_game_directory
            ):
                with self.assertRaisesRegex(OSError, "legacy unlink sync failure"):
                    installer.restore(fixture.installation)

            self.assertEqual(b"773790", app_id.read_bytes())
            self.assertFalse(installer.journal_path(fixture.installation).exists())
        finally:
            fixture.cleanup()

    def test_restore_recovers_exact_profile_and_removes_owned_marker_and_lock(self):
        """Restore consumes only install-reserved launcher recovery artifacts under lock."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            launcher, installer = real_launcher_for_restore(fixture, profiles)
            state = installer.install(fixture.installation, fixture.payload)
            launcher_state = state.launcher_artifacts
            marker = Path(str(launcher_state["recovery_marker"]))
            lock = Path(str(launcher_state["lock_path"]))
            original = "/Profiles/Player Custom.icc"
            marker.write_text(
                json.dumps(
                    {
                        "owner": launcher_state["profile_owner_token"],
                        "original": original,
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(lock.is_file())

            installer.restore(fixture.installation)

            self.assertEqual([original], profiles.calls)
            self.assertEqual(original, profiles.current)
            self.assertFalse(marker.exists())
            self.assertFalse(lock.exists())
            self.assertFalse(installer.state_path(fixture.installation).exists())
            self.assertFalse(launcher._app_path().exists())
        finally:
            fixture.cleanup()

    def test_restore_restart_holds_lock_through_incomplete_journal_rollback(self):
        """No contender may enter while recovery restores the lock snapshot."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            _launcher, installer = real_launcher_for_restore(fixture, profiles)
            before = fixture.snapshot()
            state = installer.install(fixture.installation, fixture.payload)
            lock = Path(str(state.launcher_artifacts["lock_path"]))
            marker = Path(str(state.launcher_artifacts["recovery_marker"]))
            original_profile = "/Profiles/Player Custom.icc"
            marker.write_text(
                json.dumps(
                    {
                        "owner": state.launcher_artifacts["profile_owner_token"],
                        "original": original_profile,
                    }
                ),
                encoding="utf-8",
            )
            interrupt_restore_after_final_lock_unlink(
                installer, fixture.installation
            )

            self.assertFalse(lock.exists())
            self.assertTrue(installer.state_path(fixture.installation).is_file())
            journal = json.loads(
                installer.journal_path(fixture.installation).read_text(encoding="utf-8")
            )
            self.assertFalse(journal["complete"])
            references = active_lock_snapshot_references(journal, lock)
            self.assertEqual(
                [
                    ("restore launcher", "restore_launcher", "restore_files"),
                    (
                        "remove launcher recovery lock",
                        "restore_launcher",
                        "snapshots",
                    ),
                ],
                [
                    (item["name"], item["undo"]["kind"], field)
                    for _index, item, field, _snapshot in references
                ],
            )
            self.assertEqual([0o600, 0o600], [item[3]["mode"] for item in references])

            restart_transactions = []
            contender_acquired = []

            def restart_transaction_for(installation):
                transaction = Transaction(
                    InstallJournal(installer.journal_path(installation)),
                    installer.undo_handlers(installation),
                )
                restart_transactions.append(transaction)
                if len(restart_transactions) == 1:
                    original = transaction.handlers["restore_launcher"]

                    def probe_inside_rollback(record):
                        original(record)
                        if contender_acquired:
                            return
                        contender = ProcessLock(lock)
                        try:
                            contender_acquired.append(contender.acquire())
                        finally:
                            contender.close()

                    handlers = dict(transaction.handlers)
                    handlers["restore_launcher"] = probe_inside_rollback
                    transaction.handlers = handlers
                return transaction

            with patch.object(
                installer, "transaction_for", side_effect=restart_transaction_for
            ):
                installer.restore(fixture.installation)

            self.assertEqual([False], contender_acquired)
            self.assertEqual([original_profile], profiles.calls)
            self.assertEqual(original_profile, profiles.current)
            self.assertFalse(lock.exists())
            self.assertFalse(installer.state_path(fixture.installation).exists())
            self.assertFalse(installer.journal_path(fixture.installation).exists())
            self.assertEqual(before, fixture.snapshot())
        finally:
            fixture.cleanup()

    def test_reinstall_requires_protected_restore_recovery_before_any_work(self):
        """Reinstall must never replay a Restore journal through generic recovery."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            launcher, installer = real_launcher_for_restore(fixture, profiles)
            state = installer.install(fixture.installation, fixture.payload)
            marker = Path(str(state.launcher_artifacts["recovery_marker"]))
            marker.write_text(
                json.dumps(
                    {
                        "owner": state.launcher_artifacts["profile_owner_token"],
                        "original": "/Profiles/Player Custom.icc",
                    }
                ),
                encoding="utf-8",
            )
            interrupt_restore_after_launcher(installer, fixture.installation)

            journal_path = installer.journal_path(fixture.installation)
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual("restore", journal["operation"])
            self.assertFalse(journal["complete"])
            self.assertEqual(
                [("restore launcher", "applied")],
                [(item["name"], item["status"]) for item in journal["records"]],
            )
            journal_before = journal_path.read_bytes()
            filesystem_before = fixture.snapshot()
            runner_before = list(fixture.runner.calls)
            profile_before = (profiles.current, list(profiles.calls))
            undo_calls = []
            payload_validation_calls = []
            preflight_calls = []
            journal_save_calls = []
            launcher_calls = []

            class ObservedLauncher:
                def __getattr__(self, name):
                    value = getattr(launcher, name)
                    if not callable(value):
                        return value

                    def call(*args, **kwargs):
                        launcher_calls.append(name)
                        return value(*args, **kwargs)

                    return call

            original_undo = Transaction._undo

            def observe_undo(selected, record_data, index=None):
                undo_calls.append((record_data["kind"], index))
                return original_undo(selected, record_data, index)

            original_validate_payload = installer_module.validate_payload

            def observe_payload_validation(*args, **kwargs):
                payload_validation_calls.append(None)
                return original_validate_payload(*args, **kwargs)

            original_preflight = installer.preflight

            def observe_preflight(*args, **kwargs):
                preflight_calls.append(None)
                return original_preflight(*args, **kwargs)

            original_save = InstallJournal._save

            def observe_save(selected, candidate):
                journal_save_calls.append(None)
                return original_save(selected, candidate)

            installer.launcher = ObservedLauncher()
            caught = None
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(Transaction, "_undo", observe_undo)
                )
                stack.enter_context(
                    patch.object(
                        installer_module,
                        "validate_payload",
                        observe_payload_validation,
                    )
                )
                stack.enter_context(
                    patch.object(installer, "preflight", observe_preflight)
                )
                stack.enter_context(
                    patch.object(InstallJournal, "_save", observe_save)
                )
                try:
                    installer.install(fixture.installation, fixture.payload)
                except PatchError as error:
                    caught = error

            self.assertEqual([], undo_calls)
            self.assertEqual([], payload_validation_calls)
            self.assertEqual([], preflight_calls)
            self.assertEqual([], journal_save_calls)
            self.assertEqual([], launcher_calls)
            self.assertEqual(runner_before, fixture.runner.calls)
            self.assertEqual(profile_before, (profiles.current, profiles.calls))
            self.assertEqual(journal_before, journal_path.read_bytes())
            self.assertEqual(filesystem_before, fixture.snapshot())
            self.assertIsNotNone(caught)
            self.assertEqual("install.recovery_required", caught.code)
            self.assertEqual(
                "A previous installation needs recovery.",
                caught.player_message,
            )
            self.assertEqual(
                "A previous installation needs recovery. Run Restore, then try again.",
                _player_action(caught),
            )
        finally:
            fixture.cleanup()

    def test_restore_restart_after_state_unlink_holds_lock_before_first_undo(self):
        """Journal-derived state must protect recovery before it is written back."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            _launcher, installer = real_launcher_for_restore(fixture, profiles)
            before = fixture.snapshot()
            state = installer.install(fixture.installation, fixture.payload)
            lock = Path(str(state.launcher_artifacts["lock_path"]))
            marker = Path(str(state.launcher_artifacts["recovery_marker"]))
            state_path = installer.state_path(fixture.installation)
            original_profile = "/Profiles/Player Custom.icc"
            marker.write_text(
                json.dumps(
                    {
                        "owner": state.launcher_artifacts["profile_owner_token"],
                        "original": original_profile,
                    }
                ),
                encoding="utf-8",
            )

            interrupt_restore_after_state_unlink(installer, fixture.installation)

            self.assertFalse(os.path.lexists(lock))
            self.assertFalse(os.path.lexists(state_path))
            journal = json.loads(
                installer.journal_path(fixture.installation).read_text(encoding="utf-8")
            )
            state_snapshot = active_record_snapshot(
                journal, "remove ownership state", state_path
            )
            self.assertTrue(state_snapshot["present"])
            contender_acquired = []
            restart_transactions = []

            def restart_transaction_for(installation):
                transaction = Transaction(
                    InstallJournal(installer.journal_path(installation)),
                    installer.undo_handlers(installation),
                )
                restart_transactions.append(transaction)
                if len(restart_transactions) == 1:
                    original = transaction.handlers["restore_file"]

                    def probe_inside_first_undo(record):
                        original(record)
                        if contender_acquired:
                            return
                        contender = ProcessLock(lock)
                        try:
                            contender_acquired.append(contender.acquire())
                        finally:
                            contender.close()

                    handlers = dict(transaction.handlers)
                    handlers["restore_file"] = probe_inside_first_undo
                    transaction.handlers = handlers
                return transaction

            with patch.object(
                installer, "transaction_for", side_effect=restart_transaction_for
            ):
                installer.restore(fixture.installation)

            self.assertEqual([False], contender_acquired)
            self.assertEqual([original_profile], profiles.calls)
            self.assertEqual(original_profile, profiles.current)
            self.assertFalse(os.path.lexists(lock))
            self.assertFalse(os.path.lexists(state_path))
            self.assertFalse(installer.journal_path(fixture.installation).exists())
            self.assertEqual(before, fixture.snapshot())
        finally:
            fixture.cleanup()

    def test_restore_restart_accepts_rolled_back_state_suffix(self):
        """A retry may resume after the newest undo was durably marked rolled back."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            _launcher, installer = real_launcher_for_restore(fixture, profiles)
            before = fixture.snapshot()
            installer.install(fixture.installation, fixture.payload)
            state_path = installer.state_path(fixture.installation)

            interrupt_restore_after_state_unlink(
                installer, fixture.installation
            )

            transaction = installer.transaction_for(fixture.installation)
            index = len(transaction.journal.data["records"]) - 1
            item = transaction.journal.data["records"][index]
            self.assertEqual("remove ownership state", item["name"])
            undo = item["undo"]
            transaction.handlers[undo["kind"]](
                UndoRecord(undo["kind"], undo["data"])
            )
            transaction.journal.mark_rolled_back(index)
            self.assertTrue(state_path.is_file())

            installer.restore(fixture.installation)

            self.assertEqual(before, fixture.snapshot())
            self.assertFalse(installer.journal_path(fixture.installation).exists())
            self.assertEqual([], profiles.calls)
        finally:
            fixture.cleanup()

    def test_restore_restart_rejects_unauthenticated_state_snapshot_before_mutation(self):
        """Every state-snapshot boundary is authenticated before protected recovery."""
        mutation_names = (
            "path",
            "type",
            "content",
            "digest",
            "mode",
            "ownership",
        )
        for mutation_name in mutation_names:
            with self.subTest(mutation=mutation_name):
                fixture = FakeBottleFixture()
                try:
                    profiles = FakeRestoreProfiles()
                    _launcher, installer = real_launcher_for_restore(fixture, profiles)
                    state = installer.install(fixture.installation, fixture.payload)
                    lock = Path(str(state.launcher_artifacts["lock_path"]))
                    state_path = installer.state_path(fixture.installation)
                    journal_path = installer.journal_path(fixture.installation)

                    interrupt_restore_after_state_unlink(
                        installer, fixture.installation
                    )

                    journal = json.loads(journal_path.read_text(encoding="utf-8"))
                    snapshot = active_record_snapshot(
                        journal, "remove ownership state", state_path
                    )
                    snapshot["type"] = "file"
                    if mutation_name == "path":
                        snapshot["path"] = "{}/./{}".format(
                            state_path.parent, state_path.name
                        )
                    elif mutation_name == "type":
                        snapshot["type"] = "symlink"
                    elif mutation_name == "content":
                        snapshot["content"] = "e30="
                    elif mutation_name == "digest":
                        snapshot["sha256"] = "0" * 64
                    elif mutation_name == "mode":
                        snapshot["mode"] = 0o644
                    elif mutation_name == "ownership":
                        captured = json.loads(base64.b64decode(snapshot["content"]))
                        captured["launcher_artifacts"]["profile_owner_token"] = (
                            "not-an-owner-token"
                        )
                        encoded = json.dumps(
                            captured,
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        ).encode("utf-8")
                        snapshot["content"] = base64.b64encode(encoded).decode("ascii")
                        snapshot["sha256"] = digest(encoded)
                    journal_path.write_text(
                        json.dumps(
                            journal,
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    journal_before = journal_path.read_bytes()
                    filesystem_before = fixture.snapshot()
                    profile_before = (profiles.current, list(profiles.calls))

                    with self.assertRaises(PatchError) as caught:
                        installer.restore(fixture.installation)

                    self.assertEqual(
                        "restore.launcher_recovery", caught.exception.code
                    )
                    self.assertEqual(journal_before, journal_path.read_bytes())
                    self.assertEqual(filesystem_before, fixture.snapshot())
                    self.assertEqual(
                        profile_before, (profiles.current, profiles.calls)
                    )
                    self.assertFalse(os.path.lexists(lock))
                    self.assertFalse(os.path.lexists(state_path))
                finally:
                    fixture.cleanup()

    def test_restore_restart_accepts_interruption_before_first_restore_record(self):
        """An empty incomplete Restore journal has no lock snapshot to sanitize."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            _launcher, installer = real_launcher_for_restore(fixture, profiles)
            before = fixture.snapshot()
            installer.install(fixture.installation, fixture.payload)
            transaction = installer.transaction_for(fixture.installation)
            transaction.start("restore")

            installer.restore(fixture.installation)

            self.assertEqual(before, fixture.snapshot())
            self.assertEqual([], profiles.calls)
        finally:
            fixture.cleanup()

    def test_restore_restart_preflights_every_lock_snapshot_before_mutation(self):
        """An earlier bad lock snapshot must fail before replaying a later exact one."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            launcher, installer = real_launcher_for_restore(fixture, profiles)
            state = installer.install(fixture.installation, fixture.payload)
            state_path = installer.state_path(fixture.installation)
            journal_path = installer.journal_path(fixture.installation)
            lock = Path(str(state.launcher_artifacts["lock_path"]))
            unowned = fixture.bottle_root / "player-owned-note.txt"
            unowned.write_bytes(b"leave this unrelated file byte-for-byte")
            unowned.chmod(0o640)

            interrupt_restore_after_final_lock_unlink(
                installer, fixture.installation
            )

            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            references = active_lock_snapshot_references(journal, lock)
            self.assertEqual(2, len(references))
            earlier = references[0]
            latest = references[1]
            self.assertEqual("restore_files", earlier[2])
            self.assertEqual("snapshots", latest[2])
            self.assertEqual(0o600, latest[3]["mode"])
            earlier[3]["mode"] = 0o644
            journal_path.write_text(
                json.dumps(journal, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )

            journal_before = journal_path.read_bytes()
            statuses_before = [item["status"] for item in journal["records"]]
            state_before = state_path.read_bytes()
            filesystem_before = fixture.snapshot()
            profile_before = (profiles.current, list(profiles.calls))
            launcher_present_before = os.path.lexists(launcher._app_path())

            with self.assertRaises(PatchError) as caught:
                installer.restore(fixture.installation)

            self.assertEqual(journal_before, journal_path.read_bytes())
            self.assertEqual(
                statuses_before,
                [
                    item["status"]
                    for item in json.loads(journal_path.read_text(encoding="utf-8"))[
                        "records"
                    ]
                ],
            )
            self.assertEqual(state_before, state_path.read_bytes())
            self.assertEqual(filesystem_before, fixture.snapshot())
            self.assertEqual(profile_before, (profiles.current, profiles.calls))
            self.assertEqual(b"leave this unrelated file byte-for-byte", unowned.read_bytes())
            self.assertEqual(0o640, stat.S_IMODE(unowned.stat().st_mode))
            self.assertFalse(os.path.lexists(lock))
            self.assertEqual(
                launcher_present_before, os.path.lexists(launcher._app_path())
            )
            self.assertEqual("restore.launcher_recovery", caught.exception.code)

            earlier[3]["mode"] = 0o600
            journal_path.write_text(
                json.dumps(journal, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            installer.restore(fixture.installation)

            self.assertFalse(journal_path.exists())
            self.assertFalse(state_path.exists())
            self.assertFalse(os.path.lexists(lock))
            self.assertEqual(b"leave this unrelated file byte-for-byte", unowned.read_bytes())
            self.assertEqual(0o640, stat.S_IMODE(unowned.stat().st_mode))
            self.assertEqual([], profiles.calls)
        finally:
            fixture.cleanup()

    def test_restore_restart_rejects_nonlexical_lock_snapshot_before_mutation(self):
        """A normalized spelling of the lock path is an alias, never an exact path."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            _launcher, installer = real_launcher_for_restore(fixture, profiles)
            state = installer.install(fixture.installation, fixture.payload)
            state_path = installer.state_path(fixture.installation)
            journal_path = installer.journal_path(fixture.installation)
            lock = Path(str(state.launcher_artifacts["lock_path"]))

            interrupt_restore_after_final_lock_unlink(
                installer, fixture.installation
            )

            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            references = active_lock_snapshot_references(journal, lock)
            self.assertEqual(2, len(references))
            references[0][3]["path"] = "{}/./{}".format(lock.parent, lock.name)
            self.assertNotEqual(str(lock), references[0][3]["path"])
            self.assertEqual(lock, Path(str(references[0][3]["path"])).resolve())
            journal_path.write_text(
                json.dumps(journal, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )

            journal_before = journal_path.read_bytes()
            state_before = state_path.read_bytes()
            filesystem_before = fixture.snapshot()
            profile_before = (profiles.current, list(profiles.calls))

            with self.assertRaises(PatchError) as caught:
                installer.restore(fixture.installation)

            self.assertEqual("restore.launcher_recovery", caught.exception.code)
            self.assertEqual(journal_before, journal_path.read_bytes())
            self.assertEqual(state_before, state_path.read_bytes())
            self.assertEqual(filesystem_before, fixture.snapshot())
            self.assertEqual(profile_before, (profiles.current, profiles.calls))
            self.assertFalse(os.path.lexists(lock))
        finally:
            fixture.cleanup()

    def test_restore_restart_rejects_lock_alias_in_unexpected_handler_before_mutation(self):
        """A lock alias outside its two supported undo locations is never replayed."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            launcher, installer = real_launcher_for_restore(fixture, profiles)
            state = installer.install(fixture.installation, fixture.payload)
            state_path = installer.state_path(fixture.installation)
            journal_path = installer.journal_path(fixture.installation)
            lock = Path(str(state.launcher_artifacts["lock_path"]))

            interrupt_restore_after_final_lock_unlink(
                installer, fixture.installation
            )

            alias = fixture.bottle_root / "unowned-launcher-lock-alias"
            alias.symlink_to(lock.name)
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            references = active_lock_snapshot_references(journal, lock)
            self.assertEqual(2, len(references))
            alias_snapshot = dict(references[-1][3])
            alias_snapshot["path"] = str(alias)
            journal["records"].append(
                {
                    "name": "unexpected launcher lock alias",
                    "status": "pending",
                    "undo": {
                        "kind": "restore_file",
                        "data": {"snapshots": [alias_snapshot]},
                    },
                }
            )
            journal_path.write_text(
                json.dumps(journal, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )

            journal_before = journal_path.read_bytes()
            statuses_before = [item["status"] for item in journal["records"]]
            state_before = state_path.read_bytes()
            filesystem_before = fixture.snapshot()
            profile_before = (profiles.current, list(profiles.calls))
            alias_target_before = os.readlink(alias)
            launcher_present_before = os.path.lexists(launcher._app_path())

            with self.assertRaises(PatchError) as caught:
                installer.restore(fixture.installation)

            self.assertEqual(journal_before, journal_path.read_bytes())
            self.assertEqual(
                statuses_before,
                [
                    item["status"]
                    for item in json.loads(journal_path.read_text(encoding="utf-8"))[
                        "records"
                    ]
                ],
            )
            self.assertEqual(state_before, state_path.read_bytes())
            self.assertEqual(filesystem_before, fixture.snapshot())
            self.assertEqual(profile_before, (profiles.current, profiles.calls))
            self.assertTrue(alias.is_symlink())
            self.assertEqual(alias_target_before, os.readlink(alias))
            self.assertFalse(os.path.lexists(lock))
            self.assertEqual(
                launcher_present_before, os.path.lexists(launcher._app_path())
            )
            self.assertEqual("restore.launcher_recovery", caught.exception.code)
        finally:
            fixture.cleanup()

    def test_restore_restart_rejects_nested_lock_alias_before_mutation(self):
        """Nested launcher inventories cannot smuggle a lock alias into replay."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            launcher, installer = real_launcher_for_restore(fixture, profiles)
            state = installer.install(fixture.installation, fixture.payload)
            state_path = installer.state_path(fixture.installation)
            journal_path = installer.journal_path(fixture.installation)
            lock = Path(str(state.launcher_artifacts["lock_path"]))

            interrupt_restore_after_final_lock_unlink(
                installer, fixture.installation
            )

            alias = fixture.bottle_root / "nested-launcher-lock-alias"
            alias.symlink_to(lock.name)
            app = launcher._app_path()
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["records"].append(
                {
                    "name": "unexpected nested launcher lock alias",
                    "status": "pending",
                    "undo": {
                        "kind": "restore_launcher",
                        "data": {
                            "snapshots": [],
                            "moved_tree": {
                                "source": str(
                                    app.with_name(
                                        "." + app.name + ".ostriv-macos.previous"
                                    )
                                ),
                                "destination": str(app),
                                "source_inventory": [],
                                "replacement_inventory": [
                                    {
                                        "relative_path": str(alias),
                                        "type": "symlink",
                                        "target": lock.name,
                                    }
                                ],
                            },
                        },
                    },
                }
            )
            journal_path.write_text(
                json.dumps(journal, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )

            journal_before = journal_path.read_bytes()
            state_before = state_path.read_bytes()
            filesystem_before = fixture.snapshot()
            profile_before = (profiles.current, list(profiles.calls))

            with self.assertRaises(PatchError) as caught:
                installer.restore(fixture.installation)

            self.assertEqual("restore.launcher_recovery", caught.exception.code)
            self.assertEqual(journal_before, journal_path.read_bytes())
            self.assertEqual(state_before, state_path.read_bytes())
            self.assertEqual(filesystem_before, fixture.snapshot())
            self.assertEqual(profile_before, (profiles.current, profiles.calls))
            self.assertTrue(alias.is_symlink())
            self.assertEqual(lock.name, os.readlink(alias))
            self.assertFalse(os.path.lexists(lock))
        finally:
            fixture.cleanup()

    def test_restore_final_unlink_rejects_hardlink_added_at_mutation_boundary(self):
        """The held lock must still have one link at the final unlink boundary."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            launcher, installer = real_launcher_for_restore(fixture, profiles)
            state = installer.install(fixture.installation, fixture.payload)
            state_path = installer.state_path(fixture.installation)
            journal_path = installer.journal_path(fixture.installation)
            lock = Path(str(state.launcher_artifacts["lock_path"]))
            alias = fixture.bottle_root / "unowned-held-lock-hardlink"
            expected = lock.read_bytes()
            original_finalize = launcher.finalize_restore
            boundary = {}

            def hardlink_then_finalize(*args, **kwargs):
                os.link(lock, alias)
                lock_status = lock.stat()
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                final_snapshot = active_record_snapshot(
                    journal, "remove launcher recovery lock", lock
                )
                boundary.update(
                    {
                        "journal": journal_path.read_bytes(),
                        "state": state_path.read_bytes(),
                        "filesystem": fixture.snapshot(),
                        "profile": (profiles.current, list(profiles.calls)),
                        "identity": (lock_status.st_dev, lock_status.st_ino),
                        "recorded_identity": (
                            final_snapshot.get("device"),
                            final_snapshot.get("inode"),
                        ),
                    }
                )
                return original_finalize(*args, **kwargs)

            with patch.object(
                launcher, "finalize_restore", side_effect=hardlink_then_finalize
            ):
                with self.assertRaises(PatchError) as caught:
                    installer.restore(fixture.installation)

            self.assertEqual("install.rollback_failed", caught.exception.code)
            self.assertEqual(boundary["identity"], boundary["recorded_identity"])
            self.assertEqual(boundary["journal"], journal_path.read_bytes())
            self.assertEqual(boundary["state"], state_path.read_bytes())
            self.assertEqual(boundary["filesystem"], fixture.snapshot())
            self.assertEqual(boundary["profile"], (profiles.current, profiles.calls))
            self.assertEqual(expected, lock.read_bytes())
            self.assertEqual(expected, alias.read_bytes())
            lock_status = lock.stat()
            alias_status = alias.stat()
            self.assertEqual(
                (lock_status.st_dev, lock_status.st_ino),
                (alias_status.st_dev, alias_status.st_ino),
            )
            self.assertEqual(2, lock_status.st_nlink)
            self.assertEqual(0o600, stat.S_IMODE(lock_status.st_mode))
        finally:
            fixture.cleanup()

    def test_restore_restart_rejects_referenced_surviving_lock_inode_before_mutation(self):
        """A final-unlinked lock inode cannot hide below another bottle-local name."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            _launcher, installer = real_launcher_for_restore(fixture, profiles)
            state = installer.install(fixture.installation, fixture.payload)
            state_path = installer.state_path(fixture.installation)
            journal_path = installer.journal_path(fixture.installation)
            lock = Path(str(state.launcher_artifacts["lock_path"]))
            alias = fixture.bottle_root / "surviving-pre-unlink-lock-inode"

            interrupt_restore_after_final_lock_unlink(
                installer, fixture.installation
            )

            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            references = active_lock_snapshot_references(journal, lock)
            expected = base64.b64decode(references[-1][3]["content"], validate=True)
            lock.write_bytes(expected)
            lock.chmod(0o600)
            os.link(lock, alias)
            old_status = lock.stat()
            lock.unlink()
            for _index, _item, _field, snapshot in references:
                snapshot["device"] = old_status.st_dev
                snapshot["inode"] = old_status.st_ino
            journal["records"].append(
                {
                    "name": "remove synthetic owned alias",
                    "status": "pending",
                    "undo": {
                        "kind": "restore_file",
                        "data": {
                            "snapshots": [
                                {
                                    "path": str(alias),
                                    "present": False,
                                    "type": "absent",
                                    "remove_sha256": digest(expected),
                                }
                            ]
                        },
                    },
                }
            )
            journal_path.write_text(
                json.dumps(journal, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            journal_before = journal_path.read_bytes()
            state_before = state_path.read_bytes()
            filesystem_before = fixture.snapshot()
            profile_before = (profiles.current, list(profiles.calls))
            alias_status = alias.stat()

            with self.assertRaises(PatchError) as caught:
                installer.restore(fixture.installation)

            self.assertEqual("restore.launcher_recovery", caught.exception.code)
            self.assertEqual(journal_before, journal_path.read_bytes())
            self.assertEqual(state_before, state_path.read_bytes())
            self.assertEqual(filesystem_before, fixture.snapshot())
            self.assertEqual(profile_before, (profiles.current, profiles.calls))
            self.assertFalse(os.path.lexists(lock))
            self.assertEqual(expected, alias.read_bytes())
            current_alias = alias.stat()
            self.assertEqual(
                (alias_status.st_dev, alias_status.st_ino),
                (current_alias.st_dev, current_alias.st_ino),
            )
            self.assertEqual(0o600, stat.S_IMODE(current_alias.st_mode))
        finally:
            fixture.cleanup()

    def _assert_owned_lock_at_allowed_path_rejected(
        self,
        *,
        identity_integrity,
        multiply_linked=False,
        classification_error=None,
    ):
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            launcher, installer = real_launcher_for_restore(fixture, profiles)
            state = installer.install(fixture.installation, fixture.payload)
            journal_path = installer.journal_path(fixture.installation)
            state_path = installer.state_path(fixture.installation)
            lock = Path(str(state.launcher_artifacts["lock_path"]))
            allowed_path = fixture.game_dir.resolve() / "dxil.dll"
            second_link = fixture.bottle_root / "second-lock-inode-name"
            expected = lock.read_bytes()

            def unlink_with_allowed_survivor(*_args, **_kwargs):
                os.link(lock, allowed_path)
                if multiply_linked:
                    os.link(lock, second_link)
                lock.unlink()

            with patch.object(
                launcher,
                "finalize_restore",
                side_effect=unlink_with_allowed_survivor,
            ):
                interrupt_restore_after_final_lock_unlink(
                    installer, fixture.installation
                )

            self.assertFalse(os.path.lexists(lock))
            survivor = allowed_path.lstat()
            self.assertTrue(stat.S_ISREG(survivor.st_mode))
            self.assertEqual(2 if multiply_linked else 1, survivor.st_nlink)
            self.assertEqual(0o600, stat.S_IMODE(survivor.st_mode))
            self.assertEqual(expected, allowed_path.read_bytes())
            if multiply_linked:
                linked = second_link.lstat()
                self.assertEqual(
                    (survivor.st_dev, survivor.st_ino),
                    (linked.st_dev, linked.st_ino),
                )

            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            removal = active_record_snapshot(
                journal, "remove dxil.dll", allowed_path
            )
            removal.clear()
            removal.update(
                {
                    "path": str(allowed_path),
                    "present": True,
                    "type": "file",
                    "content": base64.b64encode(expected).decode("ascii"),
                    "sha256": digest(expected),
                    "mode": 0o600,
                }
            )
            final_snapshot = active_record_snapshot(
                journal, "remove launcher recovery lock", lock
            )
            self.assertEqual(
                (survivor.st_dev, survivor.st_ino),
                (final_snapshot["device"], final_snapshot["inode"]),
            )
            final_snapshot["inode"] += 1
            if identity_integrity == "missing":
                final_snapshot.pop("identity_integrity")
            elif identity_integrity == "rebound":
                final_snapshot["identity_integrity"] = lock_identity_integrity(
                    state.launcher_artifacts["profile_owner_token"],
                    state.launcher_artifacts["lock_sha256"],
                    final_snapshot["device"],
                    final_snapshot["inode"],
                )
            recovered_state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
            next(
                item
                for item in recovered_state["owned_files"]
                if item["path"] == str(allowed_path)
            )["sha256"] = digest(expected)
            state_path.write_text(
                json.dumps(
                    recovered_state,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                encoding="utf-8",
            )
            journal_path.write_text(
                json.dumps(
                    journal,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                ),
                encoding="utf-8",
            )

            state_before = state_path.read_bytes()
            protected_paths = [allowed_path]
            if multiply_linked:
                protected_paths.append(second_link)
            protected_before = {
                path: (
                    stat.S_IFMT(path.lstat().st_mode),
                    stat.S_IMODE(path.lstat().st_mode),
                    path.lstat().st_dev,
                    path.lstat().st_ino,
                    path.lstat().st_nlink,
                    path.read_bytes(),
                )
                for path in protected_paths
            }

            if classification_error is None:
                error = self.assert_recovery_rejected_before_mutation(
                    fixture, installer, profiles
                )
            else:
                relation_active = [False]
                injected_failures = [0]
                real_lstat = os.lstat
                real_relation = type(installer)._recovery_path_relation.__func__

                def classify_relation(selected_class, *args, **kwargs):
                    relation_active[0] = True
                    try:
                        return real_relation(selected_class, *args, **kwargs)
                    finally:
                        relation_active[0] = False

                def fail_preliminary_candidate_lstat(path):
                    if (
                        relation_active[0]
                        and Path(path) == allowed_path
                    ):
                        injected_failures[0] += 1
                        raise classification_error
                    return real_lstat(path)

                with patch.object(
                    type(installer),
                    "_recovery_path_relation",
                    classmethod(classify_relation),
                ), patch.object(
                    installer_module.os,
                    "lstat",
                    side_effect=fail_preliminary_candidate_lstat,
                ):
                    error = self.assert_recovery_rejected_before_mutation(
                        fixture, installer, profiles
                    )
                self.assertGreater(injected_failures[0], 0)
                self.assertIn(
                    "Launcher lock content could not be classified in",
                    error.detail,
                )
                self.assertNotIn(
                    "private preliminary metadata detail",
                    "\n".join(
                        (error.player_message, error.detail, str(error))
                    ),
                )
            if identity_integrity == "rebound" and classification_error is None:
                self.assertIn("Launcher lock alias appears in", error.detail)
            self.assertFalse(os.path.lexists(lock))
            self.assertTrue(state_path.is_file())
            self.assertEqual(state_before, state_path.read_bytes())
            current = allowed_path.lstat()
            self.assertEqual(
                (survivor.st_dev, survivor.st_ino),
                (current.st_dev, current.st_ino),
            )
            self.assertEqual(expected, allowed_path.read_bytes())
            self.assertEqual(
                protected_before,
                {
                    path: (
                        stat.S_IFMT(path.lstat().st_mode),
                        stat.S_IMODE(path.lstat().st_mode),
                        path.lstat().st_dev,
                        path.lstat().st_ino,
                        path.lstat().st_nlink,
                        path.read_bytes(),
                    )
                    for path in protected_paths
                },
            )
        finally:
            fixture.cleanup()

    def test_restore_restart_rejects_owned_lock_at_allowed_path_with_inconsistent_identity(self):
        """Stale identity integrity cannot authorize deleting owned lock bytes elsewhere."""
        self._assert_owned_lock_at_allowed_path_rejected(
            identity_integrity="stale"
        )

    def test_restore_restart_rejects_bare_final_lock_identity(self):
        """A device/inode pair without its state-bound integrity is never trusted."""
        self._assert_owned_lock_at_allowed_path_rejected(
            identity_integrity="missing"
        )

    def test_restore_restart_rejects_owned_lock_content_with_rebound_identity(self):
        """Owned lock content remains protected even under self-consistent metadata."""
        self._assert_owned_lock_at_allowed_path_rejected(
            identity_integrity="rebound"
        )

    def test_restore_restart_rejects_multiply_linked_owned_lock_content(self):
        """Every name for exact protected lock content survives failed recovery."""
        self._assert_owned_lock_at_allowed_path_rejected(
            identity_integrity="rebound",
            multiply_linked=True,
        )

    def test_restore_restart_fails_closed_on_preliminary_path_inspection_errors(self):
        """Unknown referenced-leaf metadata never authorizes recovery replay."""
        failures = (
            PermissionError(
                errno.EACCES,
                "private preliminary metadata detail",
            ),
            OSError(
                errno.EIO,
                "private preliminary metadata detail",
            ),
        )
        for failure in failures:
            with self.subTest(error=type(failure).__name__):
                self._assert_owned_lock_at_allowed_path_rejected(
                    identity_integrity="rebound",
                    classification_error=failure,
                )

    def test_recovery_path_relation_fails_closed_on_other_preliminary_errors(self):
        """Every failed preliminary identity check remains an unsafe relation."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            candidate = root / "referenced-leaf"
            lock = root / ".ostriv-launcher.lock"
            expected = b"protected owner token\n"
            candidate.write_bytes(expected)
            candidate.chmod(0o600)

            real_resolve = Path.resolve

            def fail_candidate_resolve(path, strict=False):
                if path == candidate:
                    raise PermissionError(
                        errno.EACCES,
                        "private relation resolution detail",
                    )
                return real_resolve(path, strict=strict)

            with self.subTest(stage="resolve"), patch.object(
                Path,
                "resolve",
                new=fail_candidate_resolve,
            ):
                self.assertEqual(
                    "unsafe",
                    Installer._recovery_path_relation(
                        str(candidate),
                        lock,
                        None,
                        expected_data=expected,
                        expected_digest=digest(expected),
                    ),
                )

            real_lstat = os.lstat

            def fail_lock_lstat(path):
                if Path(path) == lock:
                    raise OSError(
                        errno.EIO,
                        "private lock metadata detail",
                    )
                return real_lstat(path)

            with self.subTest(stage="lock_lstat"), patch.object(
                installer_module.os,
                "lstat",
                side_effect=fail_lock_lstat,
            ):
                self.assertEqual(
                    "unsafe",
                    Installer._recovery_path_relation(
                        str(candidate),
                        lock,
                        None,
                        expected_data=expected,
                        expected_digest=digest(expected),
                    ),
                )

    def test_recovery_path_relation_fails_closed_on_content_inspection_errors(self):
        """A present leaf must remain stable through bounded no-follow inspection."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            candidate = root / "referenced-leaf"
            lock = root / ".ostriv-launcher.lock"
            expected = b"protected owner token\n"
            candidate.write_bytes(expected)
            candidate.chmod(0o600)

            def relation():
                return Installer._recovery_path_relation(
                    str(candidate),
                    lock,
                    None,
                    expected_data=expected,
                    expected_digest=digest(expected),
                )

            failures = (
                ("open", installer_module.os, "open", PermissionError(errno.EACCES)),
                ("fstat", installer_module.os, "fstat", OSError(errno.EIO)),
                ("read", installer_module.os, "read", OSError(errno.EIO)),
            )
            for stage, target, name, failure in failures:
                with self.subTest(stage=stage), patch.object(
                    target,
                    name,
                    side_effect=failure,
                ):
                    self.assertEqual("unsafe", relation())

            real_lstat = os.lstat
            candidate_lookups = [0]

            def disappear_on_restat(path):
                if Path(path) == candidate:
                    candidate_lookups[0] += 1
                    if candidate_lookups[0] == 3:
                        raise FileNotFoundError(errno.ENOENT, "disappeared")
                return real_lstat(path)

            with patch.object(
                installer_module.os,
                "lstat",
                side_effect=disappear_on_restat,
            ):
                self.assertEqual("unsafe", relation())
            self.assertEqual(3, candidate_lookups[0])

    def test_restore_restart_accepts_absent_referenced_owned_leaf(self):
        """Initial absence remains valid for a state-consistent owned snapshot."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            _launcher, installer = real_launcher_for_restore(fixture, profiles)
            before = fixture.snapshot()
            installer.install(fixture.installation, fixture.payload)
            leaf = fixture.game_dir.resolve() / "dxil.dll"

            interrupt_restore_after_final_lock_unlink(
                installer, fixture.installation
            )

            journal = json.loads(
                installer.journal_path(fixture.installation).read_text(
                    encoding="utf-8"
                )
            )
            snapshot = active_record_snapshot(
                journal, "remove dxil.dll", leaf
            )
            self.assertEqual("file", snapshot["type"])
            with self.assertRaises(FileNotFoundError):
                os.lstat(leaf)

            installer.restore(fixture.installation)

            self.assertEqual(before, fixture.snapshot())
            self.assertEqual([], profiles.calls)
            self.assertFalse(
                installer.journal_path(fixture.installation).exists()
            )
        finally:
            fixture.cleanup()

    def test_restore_restart_rejects_invalid_final_lock_identity_components(self):
        """Only concrete bounded stat identities may authenticate final unlink."""
        mutations = (
            ("null_pair", "both", None),
            ("null_device", "device", None),
            ("null_inode", "inode", None),
            ("bool_device", "device", True),
            ("bool_inode", "inode", True),
            ("negative_device", "device", -1),
            ("negative_inode", "inode", -1),
            ("string_device", "device", "1"),
            ("string_inode", "inode", "1"),
            ("overlarge_device", "device", 1 << 64),
            ("overlarge_inode", "inode", 1 << 64),
            ("zero_inode", "inode", 0),
        )
        for mutation_name, component, value in mutations:
            with self.subTest(mutation=mutation_name):
                fixture = FakeBottleFixture()
                try:
                    profiles = FakeRestoreProfiles()
                    _launcher, installer = real_launcher_for_restore(
                        fixture, profiles
                    )
                    state = installer.install(
                        fixture.installation, fixture.payload
                    )
                    state_path = installer.state_path(fixture.installation)
                    journal_path = installer.journal_path(fixture.installation)
                    lock = Path(str(state.launcher_artifacts["lock_path"]))

                    interrupt_restore_after_final_lock_unlink(
                        installer, fixture.installation
                    )

                    journal = json.loads(
                        journal_path.read_text(encoding="utf-8")
                    )
                    final_snapshot = active_record_snapshot(
                        journal, "remove launcher recovery lock", lock
                    )
                    if component == "both":
                        final_snapshot["device"] = value
                        final_snapshot["inode"] = value
                    else:
                        final_snapshot[component] = value
                    final_snapshot["identity_integrity"] = (
                        lock_identity_integrity(
                            state.launcher_artifacts["profile_owner_token"],
                            state.launcher_artifacts["lock_sha256"],
                            final_snapshot["device"],
                            final_snapshot["inode"],
                        )
                    )
                    journal_path.write_text(
                        json.dumps(
                            journal,
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                    state_before = state_path.read_bytes()

                    self.assert_recovery_rejected_before_mutation(
                        fixture, installer, profiles
                    )
                    self.assertEqual(state_before, state_path.read_bytes())
                    self.assertFalse(os.path.lexists(lock))
                finally:
                    fixture.cleanup()

    def test_restore_restart_rejects_registry_semantic_and_topology_drift(self):
        """Authenticated state fixes the one exact registry rollback transition."""
        mutation_names = (
            "key",
            "value",
            "before",
            "after",
            "original_absence",
            "missing",
            "duplicate",
            "order",
        )
        for mutation_name in mutation_names:
            with self.subTest(mutation=mutation_name):
                fixture = FakeBottleFixture(
                    prior_registry=None
                    if mutation_name == "original_absence"
                    else "builtin"
                )
                try:
                    profiles = FakeRestoreProfiles()
                    _launcher, installer = real_launcher_for_restore(
                        fixture, profiles
                    )
                    installer.install(fixture.installation, fixture.payload)
                    interrupt_restore_after_state_unlink(
                        installer, fixture.installation
                    )
                    journal_path = installer.journal_path(fixture.installation)
                    journal = json.loads(
                        journal_path.read_text(encoding="utf-8")
                    )
                    registry_index = next(
                        index
                        for index, item in enumerate(journal["records"])
                        if item["name"] == "restore registry override"
                    )
                    registry_record = journal["records"][registry_index]
                    registry_data = registry_record["undo"]["data"]
                    if mutation_name == "key":
                        registry_data["key"] = r"HKCU\Software\Wrong"
                    elif mutation_name == "value":
                        registry_data["value"] = "wrong-value"
                    elif mutation_name == "before":
                        registry_data["before"] = "self-consistent-wrong-installed"
                    elif mutation_name == "after":
                        registry_data["after"] = "self-consistent-wrong-original"
                    elif mutation_name == "original_absence":
                        self.assertIsNone(registry_data["after"])
                        registry_data["after"] = "fabricated-original"
                    elif mutation_name == "missing":
                        journal["records"].pop(registry_index)
                    elif mutation_name == "duplicate":
                        journal["records"].insert(
                            registry_index + 1,
                            json.loads(json.dumps(registry_record)),
                        )
                    else:
                        following = registry_index + 1
                        journal["records"][registry_index], journal["records"][following] = (
                            journal["records"][following],
                            journal["records"][registry_index],
                        )
                    journal_path.write_text(
                        json.dumps(
                            journal,
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

                    self.assert_recovery_rejected_before_mutation(
                        fixture, installer, profiles
                    )
                finally:
                    fixture.cleanup()

    def test_restore_restart_rejects_state_inconsistent_undo_semantics(self):
        """Parsed file and launcher undo data must describe authenticated state."""
        mutation_names = (
            "owned_file",
            "app_id",
            "backup_file",
            "backup_original",
            "config",
            "settings",
            "launcher_runtime",
            "launcher_config",
            "launcher_tree",
            "launcher_menu",
        )
        for mutation_name in mutation_names:
            with self.subTest(mutation=mutation_name):
                fixture = FakeBottleFixture()
                try:
                    profiles = FakeRestoreProfiles()
                    _launcher, installer = real_launcher_for_restore(
                        fixture, profiles
                    )
                    installer.install(fixture.installation, fixture.payload)
                    interrupt_restore_after_state_unlink(
                        installer, fixture.installation
                    )
                    journal_path = installer.journal_path(fixture.installation)
                    journal = json.loads(
                        journal_path.read_text(encoding="utf-8")
                    )

                    def record(name):
                        return next(
                            item
                            for item in journal["records"]
                            if item["name"] == name
                        )

                    injected = b"state-inconsistent-journal-bytes"
                    if mutation_name == "owned_file":
                        snapshot = record("remove dxil.dll")["undo"]["data"][
                            "snapshots"
                        ][0]
                    elif mutation_name == "app_id":
                        snapshot = record("remove steam_appid.txt")["undo"][
                            "data"
                        ]["snapshots"][0]
                    elif mutation_name == "backup_file":
                        snapshot = record("restore opengl32.dll")["undo"]["data"][
                            "snapshots"
                        ][0]
                    elif mutation_name == "backup_original":
                        snapshot = record("restore opengl32.dll")["undo"]["data"][
                            "snapshots"
                        ][1]
                    elif mutation_name == "config":
                        snapshot = record("restore cxbottle.conf")["undo"]["data"][
                            "snapshots"
                        ][0]
                    elif mutation_name == "settings":
                        snapshot = record("restore settings.data")["undo"]["data"][
                            "snapshots"
                        ][0]
                    elif mutation_name == "launcher_runtime":
                        snapshot = record("restore launcher")["undo"]["data"][
                            "restore_files"
                        ][0]
                    elif mutation_name == "launcher_config":
                        snapshot = record("restore launcher")["undo"]["data"][
                            "restore_files"
                        ][1]
                    elif mutation_name == "launcher_tree":
                        snapshot = next(
                            item
                            for item in record("restore launcher")["undo"]["data"][
                                "restore_trees"
                            ][0]["entries"]
                            if item.get("type") == "file"
                        )
                    else:
                        record("restore launcher")["undo"]["data"][
                            "recreate_menu"
                        ] = False
                        snapshot = None
                    if snapshot is not None:
                        snapshot["content"] = base64.b64encode(injected).decode(
                            "ascii"
                        )
                        snapshot["sha256"] = digest(injected)
                    journal_path.write_text(
                        json.dumps(
                            journal,
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )

                    self.assert_recovery_rejected_before_mutation(
                        fixture, installer, profiles
                    )
                finally:
                    fixture.cleanup()

    def test_restore_restart_rejects_dict_restore_trees_before_lock_recreation(self):
        """Malformed tree containers are rejected before any recovery mutation."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            launcher, installer = real_launcher_for_restore(fixture, profiles)
            state = installer.install(fixture.installation, fixture.payload)
            state_path = installer.state_path(fixture.installation)
            journal_path = installer.journal_path(fixture.installation)
            lock = Path(str(state.launcher_artifacts["lock_path"]))

            interrupt_restore_after_final_lock_unlink(
                installer, fixture.installation
            )

            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            launcher_record = next(
                item
                for item in journal["records"]
                if item["status"] in ("pending", "applied")
                and item["name"] == "restore launcher"
            )
            launcher_record["undo"]["data"]["restore_trees"] = {
                "root": str(launcher._app_path()),
                "present": True,
                "entries": [
                    {
                        "relative_path": str(lock),
                        "type": "file",
                        "content": "",
                        "sha256": digest(b""),
                        "mode": 0o600,
                    }
                ],
            }
            journal_path.write_text(
                json.dumps(journal, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            journal_before = journal_path.read_bytes()
            state_before = state_path.read_bytes()
            filesystem_before = fixture.snapshot()
            profile_before = (profiles.current, list(profiles.calls))

            with self.assertRaises(PatchError) as caught:
                installer.restore(fixture.installation)

            self.assertEqual("restore.launcher_recovery", caught.exception.code)
            self.assertEqual(journal_before, journal_path.read_bytes())
            self.assertEqual(state_before, state_path.read_bytes())
            self.assertEqual(filesystem_before, fixture.snapshot())
            self.assertEqual(profile_before, (profiles.current, profiles.calls))
            self.assertFalse(os.path.lexists(lock))
        finally:
            fixture.cleanup()

    def test_restore_restart_rejects_unknown_nested_carrier_before_lock_recreation(self):
        """Unknown handlers cannot smuggle path data to late rollback dispatch."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            _launcher, installer = real_launcher_for_restore(fixture, profiles)
            state = installer.install(fixture.installation, fixture.payload)
            state_path = installer.state_path(fixture.installation)
            journal_path = installer.journal_path(fixture.installation)
            lock = Path(str(state.launcher_artifacts["lock_path"]))

            interrupt_restore_after_final_lock_unlink(
                installer, fixture.installation
            )

            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal["records"].append(
                {
                    "name": "future nested restore carrier",
                    "status": "pending",
                    "undo": {
                        "kind": "future_restore_handler",
                        "data": {
                            "payload": {
                                "entries": [
                                    {
                                        "path": str(lock),
                                        "target": lock.name,
                                    }
                                ]
                            }
                        },
                    },
                }
            )
            journal_path.write_text(
                json.dumps(journal, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            journal_before = journal_path.read_bytes()
            state_before = state_path.read_bytes()
            filesystem_before = fixture.snapshot()
            profile_before = (profiles.current, list(profiles.calls))

            with self.assertRaises(PatchError) as caught:
                installer.restore(fixture.installation)

            self.assertEqual("restore.launcher_recovery", caught.exception.code)
            self.assertEqual(journal_before, journal_path.read_bytes())
            self.assertEqual(state_before, state_path.read_bytes())
            self.assertEqual(filesystem_before, fixture.snapshot())
            self.assertEqual(profile_before, (profiles.current, profiles.calls))
            self.assertFalse(os.path.lexists(lock))
        finally:
            fixture.cleanup()

    def test_restore_restart_preserves_lock_substituted_after_lease_acquisition(self):
        """Recovery must not replace a lookalike inode that displaced the held lock."""
        fixture = FakeBottleFixture()
        try:
            profiles = FakeRestoreProfiles()
            _launcher, installer = real_launcher_for_restore(fixture, profiles)
            state = installer.install(fixture.installation, fixture.payload)
            lock = Path(str(state.launcher_artifacts["lock_path"]))
            expected = lock.read_bytes()
            interrupt_restore_after_final_lock_unlink(
                installer, fixture.installation
            )

            self.assertFalse(lock.exists())
            replacement_identity = []
            recovery_boundary = []

            def restart_transaction_for(installation):
                transaction = Transaction(
                    InstallJournal(installer.journal_path(installation)),
                    installer.undo_handlers(installation),
                )
                original_recover = transaction.recover_incomplete

                def substitute_then_recover():
                    lock.unlink()
                    lock.write_bytes(expected)
                    lock.chmod(0o600)
                    status = lock.stat()
                    replacement_identity.append((status.st_dev, status.st_ino))
                    journal_path = installer.journal_path(fixture.installation)
                    recovery_boundary.append(
                        {
                            "journal": journal_path.read_bytes(),
                            "statuses": [
                                record["status"]
                                for record in json.loads(
                                    journal_path.read_text(encoding="utf-8")
                                )["records"]
                            ],
                            "state": installer.state_path(
                                fixture.installation
                            ).read_bytes(),
                            "filesystem": fixture.snapshot(),
                            "profile": (profiles.current, list(profiles.calls)),
                        }
                    )
                    original_recover()

                transaction.recover_incomplete = substitute_then_recover
                return transaction

            with patch.object(
                installer, "transaction_for", side_effect=restart_transaction_for
            ):
                with self.assertRaises(PatchError) as caught:
                    installer.restore(fixture.installation)

            self.assertEqual("install.rollback_failed", caught.exception.code)
            self.assertEqual(1, len(recovery_boundary))
            boundary = recovery_boundary[0]
            journal_path = installer.journal_path(fixture.installation)
            self.assertEqual(boundary["journal"], journal_path.read_bytes())
            self.assertEqual(
                boundary["statuses"],
                [
                    record["status"]
                    for record in json.loads(
                        journal_path.read_text(encoding="utf-8")
                    )["records"]
                ],
            )
            self.assertEqual(
                boundary["state"],
                installer.state_path(fixture.installation).read_bytes(),
            )
            self.assertEqual(boundary["filesystem"], fixture.snapshot())
            self.assertEqual(boundary["profile"], (profiles.current, profiles.calls))
            status = lock.stat()
            self.assertEqual(replacement_identity, [(status.st_dev, status.st_ino)])
            self.assertEqual(expected, lock.read_bytes())
            self.assertEqual([], profiles.calls)
            self.assertTrue(installer.state_path(fixture.installation).is_file())
            journal = json.loads(
                installer.journal_path(fixture.installation).read_text(encoding="utf-8")
            )
            self.assertFalse(journal["complete"])
        finally:
            fixture.cleanup()

    def test_restore_preserves_invalid_or_unowned_launcher_recovery_artifacts(self):
        """Invalid marker/lock ownership fails before profile or launcher mutation."""
        for conflict in ("marker", "lock"):
            with self.subTest(conflict=conflict):
                fixture = FakeBottleFixture()
                try:
                    profiles = FakeRestoreProfiles()
                    launcher, installer = real_launcher_for_restore(fixture, profiles)
                    state = installer.install(fixture.installation, fixture.payload)
                    launcher_state = state.launcher_artifacts
                    marker = Path(str(launcher_state["recovery_marker"]))
                    lock = Path(str(launcher_state["lock_path"]))
                    marker.write_text(
                        json.dumps(
                            {
                                "owner": launcher_state["profile_owner_token"],
                                "original": "/Profiles/Player Custom.icc",
                            }
                        ),
                        encoding="utf-8",
                    )
                    if conflict == "marker":
                        marker.write_text(
                            json.dumps(
                                {
                                    "owner": "another-install",
                                    "original": "/Profiles/Player Custom.icc",
                                }
                            ),
                            encoding="utf-8",
                        )
                    else:
                        lock.write_text("another-install\n", encoding="utf-8")
                    marker_before = marker.read_bytes()
                    lock_before = lock.read_bytes()

                    with self.assertRaises(PatchError) as caught:
                        installer.restore(fixture.installation)

                    self.assertEqual("restore.launcher_recovery", caught.exception.code)
                    self.assertEqual([], profiles.calls)
                    self.assertEqual(marker_before, marker.read_bytes())
                    self.assertEqual(lock_before, lock.read_bytes())
                    self.assertTrue(installer.state_path(fixture.installation).is_file())
                    self.assertTrue(launcher._app_path().is_dir())
                    self.assertTrue(Path(str(launcher_state["runtime"])).is_file())
                    self.assertFalse(installer.journal_path(fixture.installation).exists())
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

    def test_preflight_rejects_arbitrary_in_bottle_directory_without_mutation(self):
        fixture = FakeBottleFixture()
        try:
            arbitrary = fixture.bottle_root / "drive_c/users/crossover/Documents"
            arbitrary.mkdir(parents=True)
            installation = GameInstallation(
                fixture.bottle, arbitrary, fixture.installation.version
            )
            before = fixture.snapshot()
            installer = fixture.installer()
            with self.assertRaises(PatchError) as caught:
                installer.install(installation, fixture.payload)
            self.assertEqual("install.preflight", caught.exception.code)
            self.assertIn("ostriv.exe", caught.exception.detail)
            self.assertEqual([], installer.transactions)
            self.assertEqual(before, fixture.snapshot())
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

    def test_install_rejects_missing_settings_below_symlinked_ancestor_before_mutation(self):
        """A Saved Games symlink must not redirect template creation outside the bottle."""
        fixture = FakeBottleFixture()
        try:
            saved_games = fixture.settings.parents[1]
            fixture.settings.unlink()
            fixture.settings.parent.rmdir()
            saved_games.rmdir()
            outside = fixture.root / "outside-player-data"
            outside.mkdir()
            saved_games.symlink_to(outside, target_is_directory=True)
            installer = fixture.installer()

            with self.assertRaises(PatchError) as caught:
                installer.install(fixture.installation, fixture.payload)

            self.assertEqual("install.preflight", caught.exception.code)
            self.assertEqual([], list(outside.iterdir()))
            self.assertTrue(saved_games.is_symlink())
            self.assertEqual([], installer.transactions)
            self.assertFalse(installer.state_path(fixture.installation).exists())
            self.assertFalse(installer.journal_path(fixture.installation).exists())
        finally:
            fixture.cleanup()

    def test_restore_rejects_symlinked_settings_ancestor_before_legacy_mutation(self):
        """Restore validates settings ancestry before changing any legacy artifact."""
        fixture = FakeBottleFixture()
        try:
            saved_games = fixture.settings.parents[1]
            fixture.settings.unlink()
            fixture.settings.parent.rmdir()
            saved_games.rmdir()
            outside = fixture.root / "outside-restore-data"
            outside.mkdir()
            saved_games.symlink_to(outside, target_is_directory=True)
            legacy_driver = fixture.game_dir / "libgallium_wgl.dll"
            payload_driver = fixture.package_root / "prebuilt/libgallium_wgl.dll"
            legacy_driver.write_bytes(payload_driver.read_bytes())
            before = legacy_driver.read_bytes()
            installer = fixture.installer()

            with self.assertRaises(PatchError) as caught:
                installer.restore(fixture.installation)

            self.assertEqual("restore.settings_path", caught.exception.code)
            self.assertEqual(before, legacy_driver.read_bytes())
            self.assertEqual([], list(outside.iterdir()))
            self.assertTrue(saved_games.is_symlink())
            self.assertFalse(installer.state_path(fixture.installation).exists())
            self.assertFalse(installer.journal_path(fixture.installation).exists())
        finally:
            fixture.cleanup()

    def test_owned_restore_rejects_missing_settings_behind_symlinked_ancestor(self):
        """Owned Restore leaves its state and patch intact when settings ancestry escapes."""
        fixture = FakeBottleFixture()
        try:
            installer = fixture.installer()
            installer.install(fixture.installation, fixture.payload)
            state_path = installer.state_path(fixture.installation)
            state_before = state_path.read_bytes()
            driver = fixture.game_dir / "libgallium_wgl.dll"
            driver_before = driver.read_bytes()
            saved_games = fixture.settings.parents[1]
            parked = saved_games.with_name("Saved Games parked")
            saved_games.rename(parked)
            outside = fixture.root / "outside-owned-restore"
            outside.mkdir()
            saved_games.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(PatchError) as caught:
                installer.restore(fixture.installation)

            self.assertEqual("restore.settings_path", caught.exception.code)
            self.assertEqual([], list(outside.iterdir()))
            self.assertEqual(state_before, state_path.read_bytes())
            self.assertEqual(driver_before, driver.read_bytes())
            self.assertFalse(installer.journal_path(fixture.installation).exists())
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
