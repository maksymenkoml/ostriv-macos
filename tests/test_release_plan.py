import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPOSITORY_ROOT / "scripts/release_plan.py"


class ReleasePlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "Release Test")
        self._git("config", "user.email", "release@example.invalid")
        self._write_version("0.1.3")
        self._commit("Initial version")

        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        gh = bin_directory / "gh"
        gh.write_text(
            "#!/bin/sh\n"
            'if [ "$FAKE_GH_ERROR" = true ]; then\n'
            '  echo "gh: server error (HTTP 500)" >&2\n'
            "  exit 1\n"
            "fi\n"
            'if [ "$1" = api ]; then\n'
            '  case "$*" in\n'
            '    *"/releases/tags/$FAKE_RELEASE_TAG"*) '
            '[ -n "$FAKE_RELEASE_TAG" ] && echo "{}" && exit 0 ;;\n'
            "  esac\n"
            '  echo "gh: Not Found (HTTP 404)" >&2\n'
            "  exit 1\n"
            "fi\n"
            'if [ "$1" = release ] && [ "$2" = view ] && '
            '[ "$3" = "$FAKE_RELEASE_TAG" ]; then\n'
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.environment = os.environ.copy()
        self.environment["PATH"] = "{}{}{}".format(
            bin_directory, os.pathsep, self.environment["PATH"]
        )
        self.environment["FAKE_RELEASE_TAG"] = ""
        self.environment["FAKE_GH_ERROR"] = "false"
        self.environment["GITHUB_REPOSITORY"] = "owner/repository"

    def tearDown(self):
        self.temporary.cleanup()

    def _git(self, *arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def _write_version(self, version):
        package = self.repository / "ostriv_macos"
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_text(
            '__version__ = "{}"\n'.format(version), encoding="utf-8"
        )

    def _commit(self, message, filename=None):
        if filename is not None:
            (self.repository / filename).write_text(message + "\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD")

    def _run(self, release_sha, released_tag="", api_error=False):
        environment = self.environment.copy()
        environment["FAKE_RELEASE_TAG"] = released_tag
        environment["FAKE_GH_ERROR"] = str(api_error).lower()
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--release-sha", release_sha],
            cwd=self.repository,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def _outputs(self, result):
        self.assertEqual(0, result.returncode, result.stderr)
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def test_new_version_is_published_from_the_verified_commit(self):
        self._write_version("0.1.4")
        release_sha = self._commit("Release 0.1.4")

        outputs = self._outputs(self._run(release_sha))

        self.assertEqual(
            {
                "tag": "v0.1.4",
                "release_sha": release_sha,
                "tag_exists": "false",
                "publish": "true",
            },
            outputs,
        )

    def test_completed_release_for_the_same_commit_is_skipped_on_retry(self):
        self._write_version("0.1.4")
        release_sha = self._commit("Release 0.1.4")
        self._git("tag", "v0.1.4", release_sha)

        outputs = self._outputs(self._run(release_sha, released_tag="v0.1.4"))

        self.assertEqual("false", outputs["publish"])
        self.assertEqual(release_sha, outputs["release_sha"])

    def test_existing_tag_without_release_recovers_the_exact_tagged_commit(self):
        self._write_version("0.1.4")
        tagged_sha = self._commit("Release 0.1.4")
        self._git("tag", "v0.1.4", tagged_sha)
        verified_sha = self._commit("Later documentation", filename="README.md")

        outputs = self._outputs(self._run(verified_sha))

        self.assertEqual("true", outputs["publish"])
        self.assertEqual("true", outputs["tag_exists"])
        self.assertEqual(tagged_sha, outputs["release_sha"])

    def test_changed_version_cannot_reuse_an_older_tag(self):
        tagged_sha = self._git("rev-parse", "HEAD")
        self._git("tag", "v0.1.3", tagged_sha)
        self._write_version("0.1.4")
        self._commit("Temporary version")
        self._write_version("0.1.3")
        release_sha = self._commit("Reuse old version")

        result = self._run(release_sha)

        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("choose a new version", result.stderr)

    def test_tagged_commit_must_declare_the_matching_version(self):
        initial_sha = self._git("rev-parse", "HEAD")
        self._git("tag", "v0.1.4", initial_sha)
        self._write_version("0.1.4")
        self._commit("Release 0.1.4")
        release_sha = self._commit("Later documentation", filename="README.md")

        result = self._run(release_sha)

        self.assertEqual(2, result.returncode)
        self.assertIn("does not declare 0.1.4", result.stderr)

    def test_version_history_cannot_change_after_a_recovery_tag(self):
        tagged_sha = self._git("rev-parse", "HEAD")
        self._git("tag", "v0.1.3", tagged_sha)
        self._write_version("0.1.4")
        self._commit("Temporary version")
        self._write_version("0.1.3")
        self._commit("Reuse old version")
        release_sha = self._commit("Later documentation", filename="README.md")

        result = self._run(release_sha)

        self.assertEqual(2, result.returncode)
        self.assertIn("changed after v0.1.3", result.stderr)

    def test_github_api_failure_does_not_look_like_an_absent_release(self):
        release_sha = self._git("rev-parse", "HEAD")

        result = self._run(release_sha, api_error=True)

        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("GitHub API request failed", result.stderr)


if __name__ == "__main__":
    unittest.main()
