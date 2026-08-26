import io
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

    @patch("ostriv_macos.diagnostics.subprocess.run")
    def test_command_runner_captures_stderr_and_decodes_replacement(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["tool", "--check"], 3, b"OK\x8e", b"bad\xff"
        )
        result = CommandRunner().run(["tool", "--check"], timeout=2)
        self.assertEqual(3, result.returncode)
        self.assertEqual("OK\ufffd", result.stdout)
        self.assertEqual("bad\ufffd", result.stderr)
        run.assert_called_once_with(
            ["tool", "--check"], capture_output=True, check=False, timeout=2
        )

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


if __name__ == "__main__":
    unittest.main()
