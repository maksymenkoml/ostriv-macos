import io
import logging
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ostriv_macos.diagnostics import (
    CommandRunner,
    PatchError,
    PlayerOutput,
    configure_logger,
    decode_output,
)


class DiagnosticsTests(unittest.TestCase):
    def test_decode_output_replaces_invalid_utf8(self):
        self.assertEqual("OK\ufffd", decode_output(b"OK\x8e"))

    def test_patch_error_keeps_player_copy_separate_from_detail(self):
        error = PatchError(
            "payload.invalid",
            "The download is incomplete. Download the release ZIP again.",
            "opengl32.dll is a Git LFS pointer",
        )
        self.assertEqual("payload.invalid", error.code)
        self.assertNotIn("Git LFS", error.player_message)
        self.assertIn("Git LFS", error.detail)

    def test_stage_is_printed_once(self):
        stream = io.StringIO()
        output = PlayerOutput(stream, color=False)
        output.title()
        output.stage("Package", "OK")
        output.stage("Package", "OK")
        self.assertEqual("Ostriv for macOS\nPackage: OK\n", stream.getvalue())

    def test_tty_progress_replaces_one_temporary_line_before_final_stage(self):
        """Slow work must stay visibly active without leaving repeated status lines."""

        class TtyStream(io.StringIO):
            def isatty(self):
                return True

        stream = TtyStream()
        output = PlayerOutput(stream)
        output.progress("Installing", "configuring CrossOver (may take a minute)")
        output.progress("Installing", "creating launcher")
        output.stage("Installation", "OK")

        self.assertEqual(
            "\r\x1b[KInstalling… configuring CrossOver (may take a minute)"
            "\r\x1b[KInstalling… creating launcher"
            "\r\x1b[KInstallation: OK\n",
            stream.getvalue(),
        )

    def test_non_tty_progress_does_not_add_noisy_log_lines(self):
        """Redirected output keeps the existing concise, stable transcript."""
        stream = io.StringIO()
        output = PlayerOutput(stream, color=False)
        output.progress("Installing", "configuring CrossOver")
        output.stage("Installation", "OK")
        self.assertEqual("Installation: OK\n", stream.getvalue())

    def test_success_owns_one_blank_line_outcome_and_short_log_path(self):
        stream = io.StringIO()
        output = PlayerOutput(stream, color=False)
        output.success(Path.home() / "Library/Logs/ostriv-macos/install.log")
        self.assertEqual(
            "\n"
            "Ready. Quit and reopen CrossOver once, then open Ostriv (patched).\n"
            "Log: ~/Library/Logs/ostriv-macos/install.log\n",
            stream.getvalue(),
        )

    def test_failure_omits_log_line_when_path_is_not_supplied(self):
        stream = io.StringIO()
        output = PlayerOutput(stream, color=False)
        output.failure("The download is incomplete. Download the release ZIP again.")
        self.assertEqual(
            "\nThe download is incomplete. Download the release ZIP again.\n",
            stream.getvalue(),
        )

    @patch("ostriv_macos.diagnostics.subprocess.run")
    def test_command_runner_captures_stderr_and_decodes_replacement(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["wine", "--check"], 3, b"OK\x8e", b"bad\xff"
        )
        result = CommandRunner().run(["wine", "--check"], timeout=2)
        self.assertEqual(3, result.returncode)
        self.assertEqual("OK\ufffd", result.stdout)
        self.assertEqual("bad\ufffd", result.stderr)
        run.assert_called_once_with(
            ["wine", "--check"], capture_output=True, check=False, timeout=2
        )

    @patch("ostriv_macos.diagnostics.subprocess.run")
    def test_command_runner_applies_only_allowed_environment_overrides(self, run):
        """Dropping CX_BOTTLE_PATH makes a named external bottle impossible to select."""
        run.return_value = subprocess.CompletedProcess(["cxmenu"], 0, b"", b"")

        CommandRunner().run(
            ["cxmenu", "--bottle", "Steam", "--scope", "private"],
            timeout=2,
            environment={"CX_BOTTLE_PATH": "/Volumes/Games/Bottles"},
        )

        command_environment = run.call_args.kwargs.get("env", {})
        self.assertEqual(
            "/Volumes/Games/Bottles", command_environment.get("CX_BOTTLE_PATH")
        )
        self.assertEqual(os.environ.get("PATH"), command_environment.get("PATH"))

    @patch("ostriv_macos.diagnostics.subprocess.run")
    def test_command_runner_rejects_unlisted_environment_overrides(self, run):
        """A generic environment escape hatch would bypass the command allowlist."""
        run.return_value = subprocess.CompletedProcess(["cxmenu"], 0, b"", b"")
        with self.assertRaisesRegex(ValueError, "environment variable is not allowed"):
            CommandRunner().run(
                ["cxmenu", "--bottle", "Steam"],
                environment={"DYLD_INSERT_LIBRARIES": "/tmp/injected.dylib"},
            )

    @patch("ostriv_macos.diagnostics.subprocess.run")
    def test_command_timeout_is_typed_and_keeps_partial_output_private(self, run):
        """A stalled CrossOver command needs one actionable error, not a raw traceback."""
        run.side_effect = subprocess.TimeoutExpired(
            ["wine", "reg", "query"], 90, output=b"partial output", stderr=b"stalled"
        )

        with self.assertRaises(PatchError) as caught:
            CommandRunner().run(["wine", "reg", "query"], timeout=90)

        self.assertEqual("command.timeout", caught.exception.code)
        self.assertEqual("CrossOver took too long to respond.", caught.exception.player_message)
        self.assertIn("timeout=90", caught.exception.detail)
        self.assertIn("partial output", caught.exception.detail)

    @patch("ostriv_macos.diagnostics.subprocess.run")
    def test_command_runner_logs_bounded_decoded_result_and_redacts_sensitive_data(
        self, run
    ):
        run.return_value = subprocess.CompletedProcess(
            ["wine", "reg", "add"],
            7,
            b"decoded\x8e output private-value\n" + b"x" * 5000,
            b"failure\xff detail\n",
        )
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "diagnostics.log"
            logger = configure_logger(log_path)

            runner = CommandRunner()
            runner.logger = logger
            result = runner.run(
                ["wine", "reg", "add", "/d", "private-value", "/f"],
                timeout=2,
            )

            text = log_path.read_text(encoding="utf-8")
        self.assertEqual(7, result.returncode)
        self.assertIn('command start argv=["wine", "reg", "add", "/d", "<redacted>", "/f"]', text)
        self.assertIn("command result returncode=7", text)
        self.assertIn("decoded� output", text)
        self.assertIn("failure� detail", text)
        self.assertIn("<truncated", text)
        self.assertNotIn("private-value", text)
        self.assertLess(len(text), 6000)

    def test_logger_keeps_detail_out_of_player_output(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "diagnostics.log"
            logger = configure_logger(log_path)
            stream = io.StringIO()
            output = PlayerOutput(stream, color=False)
            error = PatchError("payload.invalid", "Download the release ZIP again.", "Git LFS pointer")
            logger.error("%s: %s", error.code, error.detail)
            output.stage("Payload", "FAILED", error.player_message)
            self.assertEqual("Payload: FAILED · Download the release ZIP again.\n", stream.getvalue())
            self.assertIn("Git LFS pointer", log_path.read_text(encoding="utf-8"))
            self.assertNotIn("Git LFS", stream.getvalue())

    def test_logger_does_not_propagate_technical_detail_to_root(self):
        root = logging.getLogger()
        root_stream = io.StringIO()
        root_handler = logging.StreamHandler(root_stream)
        root.addHandler(root_handler)
        try:
            with tempfile.TemporaryDirectory() as directory:
                logger = configure_logger(Path(directory) / "diagnostics.log")
                logger.error("payload.invalid: Git LFS pointer")
            self.assertEqual("", root_stream.getvalue())
        finally:
            root.removeHandler(root_handler)


if __name__ == "__main__":
    unittest.main()
