import base64
import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path

from ostriv_macos.diagnostics import CommandResult, PatchError
from ostriv_macos.discovery import Bottle, CrossOverInstall, GameInstallation
from ostriv_macos.installer import InstallJournal, Installer, Transaction
from ostriv_macos.launcher import LauncherInstaller


def digest(data):
    return hashlib.sha256(data).hexdigest()


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.cxmenu_returncode = 0

    def run(self, argv, timeout=None):
        argv = list(argv)
        self.calls.append((argv, timeout))
        if Path(argv[0]).name == "cxmenu":
            return CommandResult(self.cxmenu_returncode, "", "cxmenu failed")
        raise AssertionError(argv)


class FakeExtractor:
    def __init__(self):
        self.fail = False

    def __call__(self, template, destination):
        if self.fail:
            raise OSError("fixture extraction failure")
        executable = destination / "Contents/MacOS/Menu Helper"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"menu-helper-executable")
        executable.chmod(0o755)
        resources = destination / "Contents/Resources"
        resources.mkdir(parents=True)
        with (destination / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump(
                {
                    "CFBundleExecutable": "Menu Helper",
                    "CFBundleIconFile": "CrossOverHelper.icns",
                },
                stream,
            )


class LauncherFixture:
    def __init__(self, scope="private"):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.release = self.root / "Release Source"
        self.release.mkdir()
        source_runtime = Path(__file__).parents[1] / "ostriv_macos/launcher_runtime.py"
        self.runtime_source = self.release / "launcher_runtime.py"
        shutil.copyfile(source_runtime, self.runtime_source)

        self.crossover_app = self.root / "CrossOver 26.app"
        self.shared_support = self.crossover_app / "Contents/SharedSupport/CrossOver"
        self.bin_dir = self.shared_support / "bin"
        self.bin_dir.mkdir(parents=True)
        for name in ("wine", "cxmenu"):
            tool = self.bin_dir / name
            tool.write_bytes(b"#!/bin/sh\n")
            tool.chmod(0o755)
        resources = self.crossover_app / "Contents/Resources"
        resources.mkdir(parents=True)
        self.template = resources / "Menu Helper.cpbz2"
        self.template.write_bytes(b"fixture archive")

        self.bottle_root = self.root / "External Bottles/Bottle With Spaces"
        self.game_dir = self.bottle_root / "drive_c/Program Files/Ostriv"
        self.game_dir.mkdir(parents=True)
        (self.game_dir / "ostriv.exe").write_bytes(b"game")
        (self.bottle_root / "cxbottle.conf").write_text(
            '"BottleID" = "fixture-bottle-id"\n', encoding="utf-8"
        )
        crossover = CrossOverInstall(self.crossover_app, self.shared_support, "26.2")
        self.bottle = Bottle("Bottle With Spaces", self.bottle_root, scope, crossover)
        self.installation = GameInstallation(self.bottle, self.game_dir, "0.5.9.58")

        self.destination = self.root / "Applications With Spaces/CrossOver"
        self.destination.mkdir(parents=True)
        self.game_launcher = self.destination / "Games/Ostriv.app"
        icon = self.game_launcher / "Contents/Resources/CrossOverHelper.icns"
        icon.parent.mkdir(parents=True)
        icon.write_bytes(b"ostriv-icon")
        with (self.game_launcher / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump(
                {
                    "CXHelperAppBottleName": self.bottle.name,
                    "CrossOverHelperCommand": '"C:/Program Files/Ostriv/Ostriv.lnk"',
                },
                stream,
            )

        self.runner = FakeRunner()
        self.extractor = FakeExtractor()
        self.installer = LauncherInstaller(
            package_root=self.release,
            launcher_destination=self.destination,
            runner=self.runner,
            runtime_source=self.runtime_source,
            extractor=self.extractor,
        )
        self.journal = InstallJournal(self.bottle_root / "launcher-test-journal.json")
        self.transaction = Transaction(
            self.journal, {"restore_launcher": self._restore_snapshots}
        )
        self.transaction.start("launcher-test")

    def cleanup(self):
        self.temp.cleanup()

    @property
    def app(self):
        return self.destination / "Ostriv (patched).app"

    @property
    def runtime(self):
        return self.bottle_root / "play-ostriv-patched.py"

    @property
    def config(self):
        return self.bottle_root / "launcher-config.json"

    def create_legacy_launcher(self):
        executable = self.app / "Contents/MacOS/Menu Helper"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"legacy executable")
        executable.chmod(0o755)
        with (self.app / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump({"legacy": True}, stream)
        self.runtime.write_text(
            '#!/usr/bin/env python3\n"""Generated by ostriv-macos patch.py"""\n',
            encoding="utf-8",
        )

    @staticmethod
    def _restore_snapshots(record):
        for snapshot in record.data.get("snapshots", []):
            path = Path(snapshot["path"])
            if snapshot.get("present"):
                data = base64.b64decode(snapshot["content"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(snapshot.get("mode", 0o644))
            elif path.is_file() and snapshot.get("remove_sha256"):
                if digest(path.read_bytes()) == snapshot["remove_sha256"]:
                    path.unlink()


class LauncherInstallerTests(unittest.TestCase):
    def test_private_external_bottle_paths_stay_absolute_and_shell_safe(self):
        """Reconstructing an external bottle under the managed root breaks private installs."""
        fixture = LauncherFixture(scope="private")
        self.addCleanup(fixture.cleanup)

        state = fixture.installer.install(fixture.transaction, fixture.installation)

        config = json.loads(fixture.config.read_text(encoding="utf-8"))
        self.assertEqual(str(fixture.bottle_root.resolve()), config["bottle_argument"])
        self.assertEqual("private", config["scope"])
        self.assertEqual(
            [
                str(fixture.bin_dir / "wine"),
                "--bottle",
                str(fixture.bottle_root.resolve()),
                "--check",
                "--wait-children",
                "--start",
                "C:/Program Files/Ostriv/ostriv.exe",
            ],
            config["game_command"],
        )
        plist = plistlib.loads((fixture.app / "Contents/Info.plist").read_bytes())
        expected_command = (
            "exec /usr/bin/env python3 "
            "'{}' '{}'".format(fixture.runtime.resolve(), fixture.config.resolve())
        )
        self.assertEqual(expected_command, plist["CrossOverHelperCommand"])
        cxmenu = fixture.runner.calls[-1][0]
        self.assertEqual(str(fixture.bottle_root.resolve()), cxmenu[cxmenu.index("--bottle") + 1])
        self.assertNotIn("--scope", cxmenu)
        self.assertEqual(expected_command, state["command"])

    def test_managed_bottle_keeps_name_and_managed_scope(self):
        """Dropping managed scope can bind Wine and cxmenu to a same-named private bottle."""
        fixture = LauncherFixture(scope="managed")
        self.addCleanup(fixture.cleanup)

        fixture.installer.install(fixture.transaction, fixture.installation)

        config = json.loads(fixture.config.read_text(encoding="utf-8"))
        self.assertEqual(fixture.bottle.name, config["bottle_argument"])
        self.assertEqual("managed", config["scope"])
        self.assertEqual(
            ["--bottle", fixture.bottle.name, "--scope", "managed"],
            fixture.runner.calls[-1][0][1:5],
        )
        self.assertEqual(
            ["--scope", "managed"],
            config["game_command"][3:5],
        )

    def test_missing_menu_helper_is_typed_and_mutates_nothing(self):
        """Starting without the CrossOver template would leave a partial launcher install."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.template.unlink()

        with self.assertRaises(PatchError) as caught:
            fixture.installer.install(fixture.transaction, fixture.installation)

        self.assertEqual("install.launcher_template", caught.exception.code)
        self.assertFalse(fixture.runtime.exists())
        self.assertFalse(fixture.config.exists())
        self.assertFalse(fixture.app.exists())
        self.assertEqual([], fixture.runner.calls)

    def test_cxmenu_failure_is_typed_and_rolls_back_legacy_launcher(self):
        """Ignoring cxmenu failure reports success with no registered CrossOver menu entry."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        old_app = {
            path.relative_to(fixture.app).as_posix(): path.read_bytes()
            for path in fixture.app.rglob("*")
            if path.is_file()
        }
        old_runtime = fixture.runtime.read_bytes()
        fixture.runner.cxmenu_returncode = 7

        with self.assertRaises(PatchError) as caught:
            fixture.installer.install(fixture.transaction, fixture.installation)

        self.assertEqual("install.launcher_menu", caught.exception.code)
        self.assertEqual(old_runtime, fixture.runtime.read_bytes())
        self.assertEqual(
            old_app,
            {
                path.relative_to(fixture.app).as_posix(): path.read_bytes()
                for path in fixture.app.rglob("*")
                if path.is_file()
            },
        )

    def test_task5_undo_handler_removes_only_empty_owned_staging_directories(self):
        """Leaving empty pending/backup bundles makes a recovered install permanently conflict."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        fixture.runner.cxmenu_returncode = 7
        core = Installer(
            fixture.release,
            fixture.installer,
            launcher_destination=fixture.destination,
        )
        transaction = core.transaction_for(fixture.installation)
        transaction.start("launcher-core-undo-test")

        with self.assertRaises(PatchError):
            fixture.installer.install(transaction, fixture.installation)

        self.assertFalse(fixture.app.with_name(fixture.app.name + ".pending").exists())
        self.assertFalse(
            fixture.app.with_name("." + fixture.app.name + ".ostriv-macos.previous").exists()
        )

    def test_materializes_verified_plist_runtime_config_and_icon(self):
        """A launcher with drifted identity, runtime, config, or icon is deleted or fails later."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)

        state = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.installer.verify(fixture.installation, state)

        self.assertEqual(fixture.runtime_source.read_bytes(), fixture.runtime.read_bytes())
        self.assertEqual(b"ostriv-icon", (fixture.app / "Contents/Resources/CrossOverHelper.icns").read_bytes())
        info = plistlib.loads((fixture.app / "Contents/Info.plist").read_bytes())
        bundle_id = "com.codeweavers.CrossOverHelper.{}.{}".format(
            hashlib.md5(fixture.bottle.name.encode("utf-8")).hexdigest().upper(),
            hashlib.md5("Ostriv (patched)".encode("utf-8")).hexdigest().upper(),
        )
        self.assertEqual(
            {
                "CFBundleName": "Ostriv (patched)",
                "CFBundleDisplayName": "Ostriv (patched)",
                "CFBundleIdentifier": bundle_id,
                "CrossOverHelperCommand": state["command"],
                "CXHelperAppBottleName": fixture.bottle.name,
                "CXHelperAppBottleTag": "CrossOver-fixture-bottle-id/",
            },
            {key: info[key] for key in state["plist_fields"]},
        )
        config = json.loads(fixture.config.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "schema",
                "bottle_name",
                "bottle_argument",
                "scope",
                "wine",
                "game_command",
                "steam_apps_root",
                "steam_links",
                "game_log",
                "launcher_log",
                "lock_path",
                "recovery_marker",
                "messages",
            },
            set(config),
        )
        self.assertEqual(digest(fixture.runtime.read_bytes()), state["runtime_sha256"])
        self.assertEqual(digest(fixture.config.read_bytes()), state["config_sha256"])

    def test_verify_rejects_plist_tampering(self):
        """Checking only that an app exists misses a foreign or corrupted helper bundle."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = fixture.installer.install(fixture.transaction, fixture.installation)
        plist_path = fixture.app / "Contents/Info.plist"
        info = plistlib.loads(plist_path.read_bytes())
        info["CXHelperAppBottleName"] = "Another Bottle"
        plist_path.write_bytes(plistlib.dumps(info))

        with self.assertRaises(PatchError) as caught:
            fixture.installer.verify(fixture.installation, state)

        self.assertEqual("install.launcher_verify", caught.exception.code)
        self.assertIn("CXHelperAppBottleName", caught.exception.detail)

    def test_new_launcher_verifies_before_legacy_is_removed(self):
        """A failed pending app must not destroy the working legacy launcher."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        fixture.extractor.fail = True

        with self.assertRaises(PatchError):
            fixture.installer.install(fixture.transaction, fixture.installation)

        self.assertTrue(fixture.app.exists())
        self.assertEqual(b"legacy executable", (fixture.app / "Contents/MacOS/Menu Helper").read_bytes())
        self.assertIn(b"Generated by ostriv-macos patch.py", fixture.runtime.read_bytes())

    def test_success_replaces_legacy_only_after_pending_verification_and_restore_recovers_it(self):
        """Deleting the old app instead of recording it makes Restore destructive."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()

        state = fixture.installer.install(fixture.transaction, fixture.installation)

        self.assertEqual(b"menu-helper-executable", (fixture.app / "Contents/MacOS/Menu Helper").read_bytes())
        self.assertEqual(fixture.runtime_source.read_bytes(), fixture.runtime.read_bytes())
        self.assertTrue(Path(state["previous_app"]).exists())
        fixture.installer.restore(fixture.installation, state)
        self.assertEqual(b"legacy executable", (fixture.app / "Contents/MacOS/Menu Helper").read_bytes())
        self.assertIn(b"Generated by ostriv-macos patch.py", fixture.runtime.read_bytes())
        purge = fixture.runner.calls[-1][0]
        self.assertEqual(["--purge", "--filter", "StartMenu/Ostriv (patched)"], purge[-3:])

    def test_reinstall_preserves_the_original_legacy_restore_target(self):
        """Backing up the first hardened app on reinstall loses the genuine legacy launcher."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        first = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.transaction.journal.commit()
        (fixture.bottle_root / "ostriv-macos-state.json").write_text(
            json.dumps({"launcher_artifacts": dict(first)}), encoding="utf-8"
        )
        second_journal = InstallJournal(fixture.bottle_root / "launcher-reinstall.json")
        second_transaction = Transaction(
            second_journal, {"restore_launcher": fixture._restore_snapshots}
        )
        second_transaction.start("launcher-reinstall")

        second = fixture.installer.install(second_transaction, fixture.installation)
        fixture.installer.restore(fixture.installation, second)

        self.assertEqual(b"legacy executable", (fixture.app / "Contents/MacOS/Menu Helper").read_bytes())
        self.assertIn(b"Generated by ostriv-macos patch.py", fixture.runtime.read_bytes())

    def test_restore_removes_only_owned_inventory_and_leaves_unknown_files(self):
        """Recursive launcher cleanup can erase user files that were never installed by us."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = fixture.installer.install(fixture.transaction, fixture.installation)
        unknown = fixture.app / "Contents/Resources/user-note.txt"
        unknown.write_text("keep", encoding="utf-8")

        fixture.installer.restore(fixture.installation, state)

        self.assertEqual("keep", unknown.read_text(encoding="utf-8"))
        self.assertFalse(fixture.runtime.exists())
        self.assertFalse(fixture.config.exists())
        self.assertFalse((fixture.app / "Contents/MacOS/Menu Helper").exists())

    def test_restore_rejects_outside_launcher_paths_without_touching_them(self):
        """Trusting duplicated paths in state lets a corrupt record claim unrelated files."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = dict(fixture.installer.install(fixture.transaction, fixture.installation))
        victim = fixture.root / "unrelated.py"
        victim.write_bytes(fixture.runtime.read_bytes())
        state["runtime"] = str(victim)

        with self.assertRaises(PatchError) as caught:
            fixture.installer.restore(fixture.installation, state)

        self.assertEqual("restore.launcher_ownership", caught.exception.code)
        self.assertTrue(victim.exists())

    def test_legacy_restore_purges_only_the_patched_menu_entry(self):
        """Skipping purge leaves the obsolete launcher in CrossOver after legacy Restore."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        state = {
            "legacy": True,
            "artifacts": [
                {"path": str(fixture.app)},
                {"path": str(fixture.runtime)},
            ],
        }

        fixture.installer.restore(fixture.installation, state)

        purge = fixture.runner.calls[-1][0]
        self.assertEqual(
            ["--purge", "--filter", "StartMenu/Ostriv (patched)"], purge[-3:]
        )
        self.assertFalse(fixture.app.exists())
        self.assertFalse(fixture.runtime.exists())

    def test_installed_runtime_has_no_project_import_and_survives_source_move(self):
        """Importing package code from the release directory makes the installed app non-standalone."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.installer.install(fixture.transaction, fixture.installation)
        installed_source = fixture.runtime.read_text(encoding="utf-8")
        self.assertNotRegex(installed_source, r"(?m)^\s*(?:from|import)\s+ostriv_macos\b")
        moved_release = fixture.release.with_name("Release Source moved away")
        fixture.release.rename(moved_release)

        spec = importlib.util.spec_from_file_location("installed_ostriv_launcher", fixture.runtime)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = module.LauncherConfig.load(fixture.config)
        events = []

        class Lock:
            def acquire(self):
                events.append("lock")
                return True

            def close(self):
                events.append("unlock")

        class Log:
            def info(self, *args):
                events.append("log")

            def exception(self, *args):
                raise AssertionError(args)

        class Steam:
            def ensure_ready(self, retry=False):
                events.append(("steam", retry))

        class Profile:
            def recover(self):
                events.append("recover")

            def switch(self):
                events.append("switch")

            def restore_once(self):
                events.append("restore")

        class Runner:
            def run(self, argv):
                events.append(("game", list(argv)))
                return object()

        result = module.run_launcher(
            config,
            lock=Lock(),
            log_factory=lambda _path: Log(),
            runner=Runner(),
            steam=Steam(),
            profile=Profile(),
            dialog=lambda message: events.append(("dialog", message)),
            install_handlers=lambda profile: events.append("handlers"),
        )
        self.assertEqual(0, result)
        self.assertIn(("game", config.game_command), events)
        self.assertEqual("unlock", events[-1])


if __name__ == "__main__":
    unittest.main()
