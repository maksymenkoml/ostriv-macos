import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ostriv_macos import __version__ as PROJECT_VERSION


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPOSITORY_ROOT / "scripts/release_version.py"


class ReleaseVersionTests(unittest.TestCase):
    def _run(self, source):
        with tempfile.TemporaryDirectory() as directory:
            version_file = Path(directory) / "version.py"
            version_file.write_text(source, encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--version-file",
                    str(version_file),
                ],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_valid_project_version_is_emitted_as_a_release_tag(self):
        result = self._run('__version__ = "2.4.0"\n')

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("v2.4.0\n", result.stdout)
        self.assertEqual("", result.stderr)

    def test_default_version_file_is_the_project_package(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("v{}\n".format(PROJECT_VERSION), result.stdout)
        self.assertEqual("", result.stderr)

    def test_invalid_or_ambiguous_versions_fail_with_one_clean_error(self):
        cases = {
            "missing": "PROJECT_VERSION = '1.2.3'\n",
            "dynamic": "__version__ = current_version()\n",
            "duplicate": '__version__ = "1.2.3"\n__version__ = "1.2.4"\n',
            "prefix": '__version__ = "v1.2.3"\n',
            "prerelease": '__version__ = "1.2.3-rc.1"\n',
            "short": '__version__ = "1.2"\n',
            "leading zero": '__version__ = "01.2.3"\n',
        }
        for label, source in cases.items():
            with self.subTest(label=label):
                result = self._run(source)

                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertTrue(
                    result.stderr.startswith("release-version: "), result.stderr
                )
                self.assertEqual(1, len(result.stderr.splitlines()))


if __name__ == "__main__":
    unittest.main()
