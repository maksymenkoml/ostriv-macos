import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = REPOSITORY_ROOT / "scripts/verify_ci.py"
PUBLISH_SCRIPT = REPOSITORY_ROOT / "scripts/publish_release.py"


FAKE_GH = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from urllib.parse import unquote

state_path = Path(os.environ["FAKE_GH_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
arguments = sys.argv[1:]

def save():
    state_path.write_text(json.dumps(state), encoding="utf-8")

def fail(status, message="request failed"):
    print("gh: {} (HTTP {})".format(message, status), file=sys.stderr)
    raise SystemExit(1)

if state.get("operational_error"):
    fail(state["operational_error"])

if arguments[:2] == ["run", "list"]:
    print(json.dumps(state.get("runs", [])))
    raise SystemExit(0)

if arguments[:1] == ["api"]:
    method = arguments[arguments.index("--method") + 1]
    endpoint = next(value for value in arguments[1:] if value.startswith("repos/"))
    if "/releases/tags/" in endpoint and method == "GET":
        tag = unquote(endpoint.rsplit("/", 1)[1])
        release = state.get("releases", {}).get(tag)
        if release is None:
            fail(404, "Not Found")
        print(json.dumps(release))
        raise SystemExit(0)
    if "/git/ref/tags/" in endpoint and method == "GET":
        tag = unquote(endpoint.rsplit("/", 1)[1])
        sha = state.get("tags", {}).get(tag)
        if sha is None:
            fail(404, "Not Found")
        print(json.dumps({"object": {"sha": sha, "type": "commit"}}))
        raise SystemExit(0)
    if endpoint.endswith("/git/refs") and method == "POST":
        fields = {
            value.split("=", 1)[0]: value.split("=", 1)[1]
            for index, value in enumerate(arguments)
            if index and arguments[index - 1] == "-f"
        }
        tag = fields["ref"].removeprefix("refs/tags/")
        race_sha = state.pop("race_tag_sha", None)
        if race_sha is not None:
            state.setdefault("tags", {})[tag] = race_sha
            save()
            fail(422, "Reference already exists")
        if tag in state.get("tags", {}):
            fail(422, "Reference already exists")
        state.setdefault("tags", {})[tag] = fields["sha"]
        save()
        print(json.dumps({"ref": fields["ref"], "object": {"sha": fields["sha"]}}))
        raise SystemExit(0)
    if "/commits/" in endpoint and method == "GET":
        tag = unquote(endpoint.rsplit("/", 1)[1])
        sha = state.get("tags", {}).get(tag)
        if sha is None:
            fail(404, "Not Found")
        print(json.dumps({"sha": sha}))
        raise SystemExit(0)
    fail(500, "unexpected fake endpoint")

if arguments[:2] == ["release", "create"]:
    tag = arguments[2]
    if tag in state.get("releases", {}):
        fail(422, "Release already exists")
    sha = state.get("tags", {}).get(tag)
    if sha is None or "--verify-tag" not in arguments:
        fail(422, "Tag is unavailable")
    state.setdefault("releases", {})[tag] = {"sha": sha, "asset": arguments[3]}
    save()
    raise SystemExit(0)

fail(500, "unexpected fake command")
'''


class GitHubReleaseBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        gh = bin_directory / "gh"
        gh.write_text(FAKE_GH, encoding="utf-8")
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.state_path = self.root / "state.json"
        self.environment = os.environ.copy()
        self.environment["PATH"] = "{}{}{}".format(
            bin_directory, os.pathsep, self.environment["PATH"]
        )
        self.environment["FAKE_GH_STATE"] = str(self.state_path)
        self.environment["GITHUB_REPOSITORY"] = "owner/repository"

    def tearDown(self):
        self.temporary.cleanup()

    def _write_state(self, **values):
        state = {"tags": {}, "releases": {}, "runs": []}
        state.update(values)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def _run(self, script, *arguments):
        return subprocess.run(
            [sys.executable, str(script), *arguments],
            cwd=REPOSITORY_ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_exact_successful_main_test_run_verifies_a_release_commit(self):
        sha = "1" * 40
        self._write_state(
            runs=[
                {
                    "conclusion": "success",
                    "event": "push",
                    "headBranch": "main",
                    "headSha": sha,
                }
            ]
        )

        result = self._run(VERIFY_SCRIPT, "--sha", sha)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)

    def test_failed_mismatched_or_unavailable_ci_cannot_verify_a_commit(self):
        sha = "2" * 40
        cases = {
            "failed": {
                "runs": [
                    {
                        "conclusion": "failure",
                        "event": "push",
                        "headBranch": "main",
                        "headSha": sha,
                    }
                ]
            },
            "wrong commit": {
                "runs": [
                    {
                        "conclusion": "success",
                        "event": "push",
                        "headBranch": "main",
                        "headSha": "3" * 40,
                    }
                ]
            },
            "API error": {"operational_error": 500},
        }
        for label, state in cases.items():
            with self.subTest(label=label):
                self._write_state(**state)
                result = self._run(VERIFY_SCRIPT, "--sha", sha)
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)

    def test_publisher_atomically_creates_and_revalidates_the_release_tag(self):
        sha = "4" * 40
        self._write_state()
        asset = self.root / "ostriv-macos-player.zip"
        asset.write_bytes(b"player")

        result = self._run(
            PUBLISH_SCRIPT,
            "--tag",
            "v1.2.3",
            "--release-sha",
            sha,
            "--asset",
            str(asset),
        )

        self.assertEqual(0, result.returncode, result.stderr)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(sha, state["tags"]["v1.2.3"])
        self.assertEqual(sha, state["releases"]["v1.2.3"]["sha"])
        self.assertEqual(
            "{}#Ostriv for macOS (Apple Silicon)".format(asset),
            state["releases"]["v1.2.3"]["asset"],
        )

    def test_publisher_rejects_an_existing_or_racing_conflicting_tag(self):
        sha = "5" * 40
        conflicting_sha = "6" * 40
        asset = self.root / "ostriv-macos-player.zip"
        asset.write_bytes(b"player")
        cases = {
            "existing": {"tags": {"v1.2.3": conflicting_sha}},
            "racing": {"race_tag_sha": conflicting_sha},
        }
        for label, state in cases.items():
            with self.subTest(label=label):
                self._write_state(**state)
                result = self._run(
                    PUBLISH_SCRIPT,
                    "--tag",
                    "v1.2.3",
                    "--release-sha",
                    sha,
                    "--asset",
                    str(asset),
                )
                self.assertEqual(2, result.returncode)
                final = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.assertNotIn("v1.2.3", final["releases"])


if __name__ == "__main__":
    unittest.main()
