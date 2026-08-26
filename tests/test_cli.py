import io
import hashlib
import json
import logging
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from ostriv_macos.cli import (
    DiagnosticContext,
    ProductionServices,
    build_services,
    diagnose,
    main,
    preflight,
)
from ostriv_macos.diagnostics import PatchError, PlayerOutput
from ostriv_macos.discovery import Bottle, CrossOverInstall, GameInstallation
from ostriv_macos.installer import Installer
from ostriv_macos.launcher import LauncherInstaller


class FakeServices:
    def __init__(
        self,
        package_error=None,
        games=None,
        installed=False,
        operation_error=None,
    ):
        crossover = CrossOverInstall(
            Path("/Applications/CrossOver.app"),
            Path("/Applications/CrossOver.app/Contents/SharedSupport/CrossOver"),
            "26.2",
        )
        bottle = Bottle(
            "My Bottle",
            Path("/Users/player/Library/Application Support/CrossOver/Bottles/My Bottle"),
            "private",
            crossover,
        )
        self.game = GameInstallation(
            bottle,
            bottle.root / "drive_c/Program Files/Ostriv",
            "0.5.9.60",
        )
        self.games = [self.game] if games is None else games
        self.package_error = package_error
        self.installed = installed
        self.operation_error = operation_error
        self.mutations = []
        self.requested_path = None

    @classmethod
    def success(cls):
        return cls()

    @classmethod
    def payload_failure(cls):
        return cls(
            PatchError(
                "payload.lfs_pointer",
                "The download is incomplete. Download the release ZIP again.",
                "opengl32.dll is a Git LFS pointer",
            )
        )

    def validate_package(self):
        if self.package_error is not None:
            raise self.package_error
        return ()

    def find_games(self, game_path=None):
        self.requested_path = game_path
        return self.games

    def is_installed(self, installation):
        return self.installed

    def install(self, installation, payload):
        if self.operation_error is not None:
            raise self.operation_error
        self.mutations.append(("install", installation))

    def restore(self, installation):
        if self.operation_error is not None:
            raise self.operation_error
        self.mutations.append(("restore", installation))

    def print_diagnosis(self, game_path=None):
        return None


class CliTests(unittest.TestCase):
    @staticmethod
    def game(name, version="0.5.9.60"):
        home = Path.home()
        crossover = CrossOverInstall(
            home / "Applications/CrossOver.app",
            home / "Applications/CrossOver.app/Contents/SharedSupport/CrossOver",
            "26.2",
        )
        bottle = Bottle(
            name,
            home / "Library/Application Support/CrossOver/Bottles" / name,
            "private",
            crossover,
        )
        return GameInstallation(
            bottle,
            bottle.root / "drive_c/Program Files/Ostriv",
            version,
        )

    def test_success_is_brief_and_non_repetitive(self):
        stream = io.StringIO()
        code = main(
            [],
            services=FakeServices.success(),
            stdin=io.StringIO("\n"),
            stdout=stream,
        )
        self.assertEqual(0, code)
        self.assertEqual(
            "Ostriv for macOS\n"
            "Found: CrossOver 26.2 · Ostriv 0.5.9.60 · My Bottle\n"
            "Package: OK\n"
            "Installation: OK\n"
            "\n"
            "Ready. Quit and reopen CrossOver once, then open Ostriv (patched).\n"
            "Log: ~/Library/Logs/ostriv-macos/install.log\n",
            stream.getvalue(),
        )

    def test_expected_failure_has_one_action_and_no_traceback(self):
        stream = io.StringIO()
        code = main(
            [],
            services=FakeServices.payload_failure(),
            stdin=io.StringIO(),
            stdout=stream,
        )
        text = stream.getvalue()
        self.assertEqual(2, code)
        self.assertEqual(1, text.count("Download the release ZIP again."))
        self.assertEqual(1, text.count("Log:"))
        self.assertNotIn("Traceback", text)
        self.assertNotIn("Git LFS", text)

    def test_diagnose_is_read_only(self):
        services = FakeServices.success()
        code = main(["--diagnose"], services=services, stdout=io.StringIO())
        self.assertEqual(0, code)
        self.assertEqual([], services.mutations)

    def test_zero_games_has_one_stage_one_action_and_one_log_line(self):
        stream = io.StringIO()
        code = main([], services=FakeServices(games=[]), stdout=stream)
        self.assertEqual(2, code)
        self.assertEqual(
            "Ostriv for macOS\n"
            "Discovery: FAILED\n"
            "\n"
            "Ostriv could not be found. Choose its folder and try again.\n"
            "Log: ~/Library/Logs/ostriv-macos/install.log\n",
            stream.getvalue(),
        )

    def test_multiple_games_uses_short_labels_and_selects_once(self):
        first = self.game("First Bottle", "0.5.9.59")
        second = self.game("Second Bottle")
        services = FakeServices(games=[first, second])
        stream = io.StringIO()
        code = main(
            [], services=services, stdin=io.StringIO("2\n"), stdout=stream
        )
        self.assertEqual(0, code)
        self.assertEqual([("install", second)], services.mutations)
        self.assertEqual(
            "Ostriv for macOS\n"
            "Select an Ostriv installation:\n"
            "  [1] First Bottle · Ostriv 0.5.9.59 · "
            "~/Library/Application Support/CrossOver/Bottles/First Bottle/drive_c/Program Files/Ostriv\n"
            "  [2] Second Bottle · Ostriv 0.5.9.60 · "
            "~/Library/Application Support/CrossOver/Bottles/Second Bottle/drive_c/Program Files/Ostriv\n"
            "Select (number, or Q to quit): \n"
            "Found: CrossOver 26.2 · Ostriv 0.5.9.60 · Second Bottle\n"
            "Package: OK\n"
            "Installation: OK\n"
            "\n"
            "Ready. Quit and reopen CrossOver once, then open Ostriv (patched).\n"
            "Log: ~/Library/Logs/ostriv-macos/install.log\n",
            stream.getvalue(),
        )

    def test_explicit_external_game_path_is_used_without_extra_copy(self):
        game = self.game("External Bottle")
        services = FakeServices(games=[game])
        stream = io.StringIO()
        code = main(
            [str(game.game_dir)], services=services, stdout=stream
        )
        self.assertEqual(0, code)
        self.assertEqual(str(game.game_dir), services.requested_path)
        self.assertEqual(1, stream.getvalue().count("Found:"))
        self.assertNotIn(str(Path.home()), stream.getvalue())

    def test_reinstall_explains_choices_once_and_uses_short_labels(self):
        services = FakeServices(installed=True)
        stream = io.StringIO()
        code = main(
            [], services=services, stdin=io.StringIO("1\n"), stdout=stream
        )
        self.assertEqual(0, code)
        self.assertEqual([("install", services.game)], services.mutations)
        text = stream.getvalue()
        self.assertEqual(1, text.count("Reinstall reapplies the patch; Restore removes it."))
        self.assertEqual(1, text.count("[1] Reinstall"))
        self.assertEqual(1, text.count("[2] Restore"))
        self.assertNotIn("—", text)
        self.assertEqual(1, text.count("Installation: OK"))
        self.assertEqual(1, text.count("Ready."))

    def test_restore_is_selected_without_installing(self):
        services = FakeServices(installed=True)
        stream = io.StringIO()
        code = main(
            [], services=services, stdin=io.StringIO("2\n"), stdout=stream
        )
        self.assertEqual(0, code)
        self.assertEqual([("restore", services.game)], services.mutations)
        self.assertEqual(1, stream.getvalue().count("Installation: OK"))
        self.assertEqual(1, stream.getvalue().count("Log:"))

    def test_corrupt_journal_failure_is_one_clean_action(self):
        error = PatchError(
            "install.journal_corrupt",
            "The installation journal is unreadable. Restore before trying again.",
            "JSONDecodeError at /Users/player/raw-state.json",
        )
        stream = io.StringIO()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [], services=FakeServices(operation_error=error), stdout=stream
            )
        self.assertEqual(2, code)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(1, stream.getvalue().count("Restore before trying again."))
        self.assertEqual(1, stream.getvalue().count("Installation: FAILED"))
        self.assertEqual(1, stream.getvalue().count("Log:"))
        self.assertNotIn("JSONDecodeError", stream.getvalue())

    def test_launcher_failure_does_not_leak_command_output(self):
        error = PatchError(
            "launcher.cxmenu",
            "Installation failed. Reinstall once.",
            "cxmenu returned 9: raw stderr",
        )
        stream = io.StringIO()
        code = main([], services=FakeServices(operation_error=error), stdout=stream)
        self.assertEqual(2, code)
        self.assertEqual(1, stream.getvalue().count("Reinstall once."))
        self.assertEqual(1, stream.getvalue().count("Installation: FAILED"))
        self.assertNotIn("cxmenu", stream.getvalue())
        self.assertNotIn("raw stderr", stream.getvalue())

    def test_unexpected_failure_has_no_traceback_or_stderr(self):
        stream = io.StringIO()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [],
                services=FakeServices(operation_error=RuntimeError("secret")),
                stdout=stream,
            )
        self.assertEqual(3, code)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(
            "Ostriv for macOS\n"
            "Found: CrossOver 26.2 · Ostriv 0.5.9.60 · My Bottle\n"
            "Package: OK\n"
            "Installation: FAILED\n"
            "\n"
            "Something went wrong. Try Reinstall once.\n"
            "Log: ~/Library/Logs/ostriv-macos/install.log\n",
            stream.getvalue(),
        )

    def test_keyboard_interrupt_is_clean_and_has_no_traceback(self):
        class InterruptedInput(io.StringIO):
            def readline(self, *args, **kwargs):
                raise KeyboardInterrupt

        stream = io.StringIO()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [],
                services=FakeServices(installed=True),
                stdin=InterruptedInput(),
                stdout=stream,
            )
        self.assertEqual(130, code)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(1, stream.getvalue().count("Cancelled."))
        self.assertEqual(1, stream.getvalue().count("Log:"))
        self.assertNotIn("Traceback", stream.getvalue())

    def test_production_services_compose_existing_installers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            services = build_services(
                root,
                logging.getLogger("test.cli.composition"),
                io.StringIO(),
                PlayerOutput(io.StringIO(), color=False),
            )
        self.assertIsInstance(services, ProductionServices)
        self.assertIsInstance(services.installer, Installer)
        self.assertIsInstance(services.installer.launcher, LauncherInstaller)


class ReadOnlyCliTests(unittest.TestCase):
    REQUIRED_FILES = (
        "patch.py",
        "ostriv_macos/__init__.py",
        "ostriv_macos/cli.py",
        "ostriv_macos/diagnostics.py",
        "ostriv_macos/discovery.py",
        "ostriv_macos/installer.py",
        "ostriv_macos/launcher.py",
        "ostriv_macos/launcher_runtime.py",
        "ostriv_macos/payload.py",
        "assets/settings.data",
        "README.md",
        "LICENSE",
    )

    @classmethod
    def write_package(cls, root):
        payload = b"MZtest-payload"
        payload_path = root / "prebuilt/driver.dll"
        payload_path.parent.mkdir(parents=True)
        payload_path.write_bytes(payload)
        manifest = {
            "schema": 1,
            "files": [
                {
                    "path": "prebuilt/driver.dll",
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "pe": True,
                }
            ],
        }
        (root / "payload-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        for relative in cls.REQUIRED_FILES:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("placeholder\n", encoding="utf-8")

    def test_preflight_validates_payload_and_exact_release_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_package(root)
            self.assertEqual(0, preflight(root))
            for relative in self.REQUIRED_FILES:
                target = root / relative
                contents = target.read_bytes()
                target.unlink()
                with self.assertRaises(PatchError, msg=relative):
                    preflight(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(contents)

    def test_preflight_is_silent_process_free_and_does_not_import_macos_frameworks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_package(root)
            stream = io.StringIO()
            imported = []
            real_import = __import__

            def guarded_import(name, *args, **kwargs):
                if name.split(".")[0] in {"AppKit", "Quartz", "ColorSync"}:
                    imported.append(name)
                    raise AssertionError(name)
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=guarded_import), patch(
                "subprocess.run", side_effect=AssertionError("subprocess")
            ), patch("ostriv_macos.cli.configure_logger", side_effect=AssertionError("log")):
                with redirect_stderr(stream):
                    code = preflight(root)
            self.assertEqual(0, code)
            self.assertEqual([], imported)
            self.assertEqual("", stream.getvalue())

    def test_diagnose_is_process_free_read_only_and_succeeds_without_crossover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            self.write_package(root)
            log_path = home / "Library/Logs/ostriv-macos/install.log"
            log_path.parent.mkdir(parents=True)
            log_path.write_text("existing\n", encoding="utf-8")
            before = log_path.read_bytes()
            context = DiagnosticContext(
                root, home, {}, None, root / "missing/CrossOver.app"
            )
            with patch("subprocess.run", side_effect=AssertionError("subprocess")):
                summary = diagnose(context)
            self.assertEqual("not found", summary.crossover)
            self.assertEqual("OK", summary.payload)
            self.assertEqual(before, log_path.read_bytes())
            self.assertFalse((home / "Applications").exists())

    def test_main_diagnose_prints_concise_findings_without_creating_log(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            stream = io.StringIO()
            with patch("pathlib.Path.home", return_value=home), patch(
                "ostriv_macos.cli.configure_logger", side_effect=AssertionError("log")
            ), patch("subprocess.run", side_effect=AssertionError("subprocess")):
                code = main(["--diagnose"], stdout=stream)
            self.assertEqual(0, code)
            self.assertFalse((home / "Library/Logs/ostriv-macos/install.log").exists())
            text = stream.getvalue()
            self.assertEqual(1, text.count("Ostriv for macOS"))
            self.assertEqual(1, text.count("Python:"))
            self.assertEqual(1, text.count("CrossOver:"))
            self.assertEqual(1, text.count("Bottle roots:"))
            self.assertEqual(1, text.count("Ostriv:"))
            self.assertEqual(1, text.count("Package:"))
            self.assertEqual(1, text.count("Installation:"))
            self.assertEqual(1, text.count("Launcher:"))
            self.assertEqual(1, text.count("Logs:"))
            self.assertNotIn(str(home), text)

    def test_patch_entrypoint_delegates_to_silent_preflight(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, str(root / "patch.py"), "--preflight"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)
        self.assertEqual("", result.stderr)


if __name__ == "__main__":
    unittest.main()
