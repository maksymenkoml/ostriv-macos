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
            if identity not in self.registry:
                return CommandResult(1, "\ufffd missing\n", "\ufffd not found")
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
