import io
import hashlib
import json
import logging
import plistlib
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
from tests.test_installer import FakeBottleFixture


class FakeServices:
    def __init__(
        self,
        package_error=None,
        games=None,
        installed=False,
        operation_error=None,
        discovery_error=None,
        diagnostic_error=None,
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
        self.discovery_error = discovery_error
        self.diagnostic_error = diagnostic_error
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
        if self.discovery_error is not None:
            raise self.discovery_error
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
        if self.diagnostic_error is not None:
            raise self.diagnostic_error
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

    def test_restore_has_dedicated_stage_and_normal_crossover_next_step(self):
        services = FakeServices(installed=True)
        stream = io.StringIO()
        code = main(
            [], services=services, stdin=io.StringIO("2\n"), stdout=stream
        )
        self.assertEqual(0, code)
        self.assertEqual([("restore", services.game)], services.mutations)
        self.assertEqual(
            "Ostriv for macOS\n"
            "Found: CrossOver 26.2 · Ostriv 0.5.9.60 · My Bottle\n"
            "Package: OK\n"
            "Reinstall reapplies the patch; Restore removes it.\n"
            "Choose an action:\n"
            "  [1] Reinstall\n"
            "  [2] Restore\n"
            "Select (number, or Q to quit): \n"
            "Restoration: OK\n"
            "\n"
            "Restored. Open Ostriv normally in CrossOver.\n"
            "Log: ~/Library/Logs/ostriv-macos/install.log\n",
            stream.getvalue(),
        )

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
            "install.launcher_menu",
            "Installation failed.",
            "cxmenu returned 9: raw stderr",
        )
        stream = io.StringIO()
        code = main([], services=FakeServices(operation_error=error), stdout=stream)
        self.assertEqual(2, code)
        self.assertEqual(1, stream.getvalue().count("Reinstall once."))
        self.assertEqual(1, stream.getvalue().count("Installation: FAILED"))
        self.assertNotIn("cxmenu", stream.getvalue())
        self.assertNotIn("raw stderr", stream.getvalue())

    def test_production_error_codes_map_to_one_action_at_cli_boundary(self):
        cases = (
            (
                "discovery",
                FakeServices(
                    discovery_error=PatchError(
                        "discovery.no_crossover",
                        "CrossOver could not be found.",
                        "/raw/CrossOver.app missing",
                    )
                ),
                "",
                "CrossOver could not be found. Install CrossOver, then try again.",
                "Discovery: FAILED",
            ),
            (
                "preflight",
                FakeServices(
                    operation_error=PatchError(
                        "install.preflight",
                        "Installation cannot start.",
                        "cxbottle returned raw output",
                    )
                ),
                "",
                "Installation cannot start. Check CrossOver and the selected Ostriv folder, then try again.",
                "Installation: FAILED",
            ),
            (
                "registry",
                FakeServices(
                    operation_error=PatchError(
                        "install.registry",
                        "Installation failed.",
                        "wine reg returned raw output",
                    )
                ),
                "",
                "Installation failed. Try Reinstall once.",
                "Installation: FAILED",
            ),
            (
                "launcher",
                FakeServices(
                    operation_error=PatchError(
                        "install.launcher_menu",
                        "Installation failed.",
                        "cxmenu returned raw output",
                    )
                ),
                "",
                "Installation failed. Try Reinstall once.",
                "Installation: FAILED",
            ),
            (
                "restore",
                FakeServices(
                    installed=True,
                    operation_error=PatchError(
                        "restore.registry",
                        "Restore failed.",
                        "wine reg delete returned raw output",
                    ),
                ),
                "2\n",
                "Restore failed. Run Restore once more.",
                "Restoration: FAILED",
            ),
            (
                "corrupt state",
                FakeServices(
                    operation_error=PatchError(
                        "install.state_corrupt",
                        "The installation ownership record is unreadable.",
                        "JSONDecodeError /raw/state.json",
                    )
                ),
                "",
                "The installation record is unreadable. Reinstall Ostriv in this bottle, then try again.",
                "Installation: FAILED",
            ),
        )
        for name, services, entered, action, failed_stage in cases:
            with self.subTest(name=name):
                stream = io.StringIO()
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    code = main(
                        [],
                        services=services,
                        stdin=io.StringIO(entered),
                        stdout=stream,
                    )
                text = stream.getvalue()
                self.assertEqual(2, code)
                self.assertEqual("", stderr.getvalue())
                self.assertEqual(1, text.count(action))
                self.assertEqual(1, text.count(failed_stage))
                self.assertEqual(1, text.count("Log:"))
                self.assertNotIn("raw output", text)
                self.assertNotIn("JSONDecodeError", text)
                self.assertNotIn("Traceback", text)

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

    def test_invalid_arguments_are_one_clean_action_without_usage_or_stderr(self):
        stream = io.StringIO()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(["--not-an-option"], stdout=stream)
        self.assertEqual(2, code)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(
            "Ostriv for macOS\n"
            "\n"
            "The options were not recognized. Run patch.py --help and try again.\n",
            stream.getvalue(),
        )

    def test_logger_setup_failure_has_safe_fallback_and_no_unavailable_log_line(self):
        stream = io.StringIO()
        stderr = io.StringIO()
        with patch(
            "ostriv_macos.cli.configure_logger",
            side_effect=PermissionError("raw log path"),
        ), redirect_stderr(stderr):
            code = main([], stdout=stream)
        self.assertEqual(3, code)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(
            "Ostriv for macOS\n"
            "\n"
            "The installer could not start. Check permissions for ~/Library/Logs, then try again.\n",
            stream.getvalue(),
        )
        self.assertNotIn("Log:", stream.getvalue())
        self.assertNotIn("raw log path", stream.getvalue())

    def test_diagnose_keyboard_interrupt_is_inside_clean_boundary(self):
        stream = io.StringIO()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                ["--diagnose"],
                services=FakeServices(diagnostic_error=KeyboardInterrupt()),
                stdout=stream,
            )
        self.assertEqual(130, code)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(
            "Ostriv for macOS\n\nDiagnosis cancelled.\n",
            stream.getvalue(),
        )
        self.assertNotIn("Log:", stream.getvalue())

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


class ProductionCliIntegrationTests(unittest.TestCase):
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

    def setUp(self):
        self.fixture = FakeBottleFixture()
        with (self.fixture.app / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleShortVersionString": "26.2"}, stream)
        version_log = (
            self.fixture.bottle_root
            / "drive_c/users/crossover/Saved Games/Ostriv/log.txt"
        )
        version_log.parent.mkdir(parents=True, exist_ok=True)
        version_log.write_text(
            "Alpha (0.5.9.60 Aug 25 2026)\n", encoding="utf-8"
        )
        manifest = {
            "schema": 1,
            "files": [
                {
                    "path": entry.relative_path,
                    "size": entry.size,
                    "sha256": entry.sha256,
                    "pe": entry.pe,
                }
                for entry in self.fixture.payload
            ],
        }
        (self.fixture.package_root / "payload-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        for relative in self.REQUIRED_FILES:
            path = self.fixture.package_root / relative
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")
        self.home = self.fixture.root / "home"
        self.home.mkdir()

    def tearDown(self):
        self.fixture.cleanup()

    def services(self, stream):
        installer = self.fixture.installer()
        return ProductionServices(
            self.fixture.package_root,
            installer,
            self.fixture.runner,
            io.StringIO(),
            PlayerOutput(stream, color=False),
            home=self.home,
            env={
                "OSTRIV_CROSSOVER_APP": str(self.fixture.app),
                "CX_BOTTLE_PATH": str(self.fixture.bottle_root.parent),
            },
        )

    def test_real_explicit_install_then_restore_has_exact_snapshots_and_exact_recovery(self):
        before = self.fixture.snapshot()
        install_stream = io.StringIO()
        install_code = main(
            [str(self.fixture.game_dir)],
            services=self.services(install_stream),
            stdout=install_stream,
        )
        self.assertEqual(0, install_code)
        self.assertEqual(
            "Ostriv for macOS\n"
            "Found: CrossOver 26.2 · Ostriv 0.5.9.60 · Bottle With Spaces\n"
            "Package: OK\n"
            "Installation: OK\n"
            "\n"
            "Ready. Quit and reopen CrossOver once, then open Ostriv (patched).\n"
            "Log: ~/Library/Logs/ostriv-macos/install.log\n",
            install_stream.getvalue(),
        )

        restore_stream = io.StringIO()
        restore_code = main(
            [str(self.fixture.game_dir)],
            services=self.services(restore_stream),
            stdin=io.StringIO("2\n"),
            stdout=restore_stream,
        )
        self.assertEqual(0, restore_code)
        self.assertEqual(
            "Ostriv for macOS\n"
            "Found: CrossOver 26.2 · Ostriv 0.5.9.60 · Bottle With Spaces\n"
            "Package: OK\n"
            "Reinstall reapplies the patch; Restore removes it.\n"
            "Choose an action:\n"
            "  [1] Reinstall\n"
            "  [2] Restore\n"
            "Select (number, or Q to quit): \n"
            "Restoration: OK\n"
            "\n"
            "Restored. Open Ostriv normally in CrossOver.\n"
            "Log: ~/Library/Logs/ostriv-macos/install.log\n",
            restore_stream.getvalue(),
        )
        self.assertEqual(before, self.fixture.snapshot())

    def test_real_explicit_invalid_path_is_actionable_and_mutates_nothing(self):
        arbitrary = self.fixture.bottle_root / "drive_c/users/crossover/Documents"
        arbitrary.mkdir(parents=True)
        before = self.fixture.snapshot()
        stream = io.StringIO()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = main(
                [str(arbitrary)], services=self.services(stream), stdout=stream
            )
        self.assertEqual(2, code)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(
            "Ostriv for macOS\n"
            "Discovery: FAILED\n"
            "\n"
            "The selected folder is not a supported Ostriv installation. "
            "Choose the Ostriv folder in CrossOver, then try again.\n"
            "Log: ~/Library/Logs/ostriv-macos/install.log\n",
            stream.getvalue(),
        )
        self.assertEqual(before, self.fixture.snapshot())
        self.assertFalse(
            (self.fixture.bottle_root / ".ostriv-macos-journal.json").exists()
        )

    def test_real_zero_and_multiple_game_discovery_are_clean_and_non_mutating(self):
        self.fixture.game_dir.joinpath("ostriv.exe").unlink()
        zero_before = self.fixture.snapshot()
        zero_stream = io.StringIO()
        zero_code = main([], services=self.services(zero_stream), stdout=zero_stream)
        self.assertEqual(2, zero_code)
        self.assertEqual(
            "Ostriv for macOS\n"
            "Discovery: FAILED\n"
            "\n"
            "Ostriv could not be found. Choose its folder and try again.\n"
            "Log: ~/Library/Logs/ostriv-macos/install.log\n",
            zero_stream.getvalue(),
        )
        self.assertEqual(zero_before, self.fixture.snapshot())

        self.fixture.game_dir.joinpath("ostriv.exe").write_bytes(b"genuine game")
        second_root = self.fixture.bottle_root.parent / "Second Bottle"
        shutil.copytree(self.fixture.bottle_root, second_root)
        (second_root / "ostriv-macos-state.json").write_text("{}\n", encoding="utf-8")
        multiple_before = self.fixture.snapshot()
        stream = io.StringIO()
        code = main(
            [],
            services=self.services(stream),
            stdin=io.StringIO("2\nq\n"),
            stdout=stream,
        )
        self.assertEqual(0, code)
        first_game = str(self.fixture.game_dir.resolve())
        second_game = str(
            (second_root / "drive_c/Program Files/Ostriv").resolve()
        )
        self.assertEqual(
            "Ostriv for macOS\n"
            "Select an Ostriv installation:\n"
            "  [1] Bottle With Spaces · Ostriv 0.5.9.60 · {}\n"
            "  [2] Second Bottle · Ostriv 0.5.9.60 · {}\n"
            "Select (number, or Q to quit): \n"
            "Found: CrossOver 26.2 · Ostriv 0.5.9.60 · Second Bottle\n"
            "Package: OK\n"
            "Reinstall reapplies the patch; Restore removes it.\n"
            "Choose an action:\n"
            "  [1] Reinstall\n"
            "  [2] Restore\n"
            "Select (number, or Q to quit): \n"
            "\n"
            "Cancelled.\n"
            "Log: ~/Library/Logs/ostriv-macos/install.log\n".format(
                first_game, second_game
            ),
            stream.getvalue(),
        )
        self.assertEqual(multiple_before, self.fixture.snapshot())

    def test_real_corrupt_journal_is_actionable_and_mutates_nothing(self):
        journal = self.fixture.bottle_root / ".ostriv-macos-journal.json"
        journal.write_text("{broken", encoding="utf-8")
        before = self.fixture.snapshot()
        stream = io.StringIO()
        code = main(
            [str(self.fixture.game_dir)],
            services=self.services(stream),
            stdout=stream,
        )
        text = stream.getvalue()
        self.assertEqual(2, code)
        self.assertEqual(
            "Ostriv for macOS\n"
            "Found: CrossOver 26.2 · Ostriv 0.5.9.60 · Bottle With Spaces\n"
            "Package: OK\n"
            "Installation: FAILED\n"
            "\n"
            "The installation journal is unreadable. Restore before trying again.\n"
            "Log: ~/Library/Logs/ostriv-macos/install.log\n",
            text,
        )
        self.assertNotIn("JSONDecodeError", text)
        self.assertNotIn("Traceback", text)
        self.assertEqual(before, self.fixture.snapshot())

    def test_real_registry_and_launcher_failures_rollback_without_raw_output(self):
        failures = (
            (
                "registry",
                lambda: setattr(self.fixture.runner, "add_failures", 2),
                "transient add failure",
            ),
            (
                "launcher",
                lambda: patch.object(
                    self.fixture.launcher,
                    "install",
                    side_effect=PatchError(
                        "install.launcher_menu",
                        "Installation failed.",
                        "cxmenu raw command output",
                    ),
                ),
                "raw command output",
            ),
        )
        for name, arrange, hidden in failures:
            with self.subTest(name=name):
                before = self.fixture.snapshot()
                arranged = arrange()
                context = arranged if hasattr(arranged, "__enter__") else None
                if context is not None:
                    context.__enter__()
                try:
                    stream = io.StringIO()
                    code = main(
                        [str(self.fixture.game_dir)],
                        services=self.services(stream),
                        stdout=stream,
                    )
                finally:
                    if context is not None:
                        context.__exit__(None, None, None)
                text = stream.getvalue()
                self.assertEqual(2, code)
                self.assertEqual(
                    "Ostriv for macOS\n"
                    "Found: CrossOver 26.2 · Ostriv 0.5.9.60 · Bottle With Spaces\n"
                    "Package: OK\n"
                    "Installation: FAILED\n"
                    "\n"
                    "Installation failed. Try Reinstall once.\n"
                    "Log: ~/Library/Logs/ostriv-macos/install.log\n",
                    text,
                )
                self.assertNotIn(hidden, text)
                self.assertNotIn("Traceback", text)
                self.assertEqual(before, self.fixture.snapshot())


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

    def test_diagnose_reads_populated_bottle_state_without_crossover_app(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            self.write_package(root)
            bottles_root = root / "External Bottles"
            bottle = bottles_root / "Offline Bottle"
            game = bottle / "drive_c/Games/Ostriv"
            game.mkdir(parents=True)
            (game / "ostriv.exe").write_bytes(b"MZ")
            (bottle / "cxbottle.conf").write_text("[Bottle]\n", encoding="utf-8")
            (bottle / "system.reg").write_text("REGEDIT4\n", encoding="utf-8")
            version_log = bottle / "drive_c/users/crossover/Saved Games/Ostriv/log.txt"
            version_log.parent.mkdir(parents=True)
            version_log.write_text("Alpha (0.5.9.60 Aug 25 2026)\n", encoding="utf-8")
            (bottle / "ostriv-macos-state.json").write_text(
                '{"schema": 1}\n', encoding="utf-8"
            )
            (bottle / "play-ostriv-patched.py").write_text("pass\n", encoding="utf-8")
            (bottle / "launcher-config.json").write_text("{}\n", encoding="utf-8")
            launcher = home / "Applications/CrossOver/Ostriv (patched).app"
            launcher.mkdir(parents=True)
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            context = DiagnosticContext(
                root,
                home,
                {"CX_BOTTLE_PATH": str(bottles_root)},
                None,
                root / "missing/CrossOver.app",
            )
            with patch("subprocess.run", side_effect=AssertionError("subprocess")):
                summary = diagnose(context)
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual("not found", summary.crossover)
            self.assertEqual(
                (
                    "Offline Bottle · Ostriv 0.5.9.60 · "
                    + str(game.resolve()),
                ),
                summary.games,
            )
            self.assertEqual(("installed",), summary.installation)
            self.assertEqual(("installed",), summary.launcher)
            self.assertEqual(before, after)

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

    def test_patch_entrypoint_delegates_to_silent_preflight_without_subprocess(self):
        root = Path(__file__).resolve().parent.parent
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(
            sys, "argv", [str(root / "patch.py"), "--preflight"]
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(root / "patch.py"), run_name="__main__")
        self.assertEqual(0, stopped.exception.code)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
