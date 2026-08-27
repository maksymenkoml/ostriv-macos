#!/usr/bin/env python3
"""Plan a version-driven GitHub release for a verified commit."""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import quote

from github_api import GitHubAPIError, repository_from_environment, request_json
from release_version import (
    ReleaseVersionError,
    parse_release_version,
    read_release_version,
)


VERSION_FILE = Path("ostriv_macos/__init__.py")


class ReleasePlanError(RuntimeError):
    """The verified commit cannot be published safely."""


def _run(command: Sequence[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise ReleasePlanError("cannot run {}".format(command[0])) from error


def _git(*arguments: str) -> str:
    result = _run(["git", *arguments])
    if result.returncode != 0:
        raise ReleasePlanError("git could not inspect the release history")
    return result.stdout.strip()


def _git_status(*arguments: str) -> int:
    return _run(["git", *arguments]).returncode


def _version_at(revision: str, label: str) -> str:
    try:
        return parse_release_version(
            _git("show", "{}:{}".format(revision, VERSION_FILE)),
            filename="{}:{}".format(label, VERSION_FILE),
        )
    except ReleaseVersionError as error:
        raise ReleasePlanError("{} has an invalid version file".format(label)) from error


def plan_release(release_sha: str) -> dict:
    verified_sha = _git("rev-parse", "--verify", "{}^{{commit}}".format(release_sha))
    if _git("rev-parse", "HEAD") != verified_sha:
        raise ReleasePlanError("the checked-out commit is not the verified commit")

    try:
        tag = "v{}".format(read_release_version(VERSION_FILE))
    except ReleaseVersionError as error:
        raise ReleasePlanError(str(error)) from error

    tag_ref = "refs/tags/{}".format(tag)
    tag_status = _git_status("show-ref", "--verify", "--quiet", tag_ref)
    if tag_status not in (0, 1):
        raise ReleasePlanError("git could not inspect the release tag")
    tag_exists = tag_status == 0
    tag_sha = ""
    target_sha = verified_sha
    if tag_exists:
        tag_sha = _git("rev-list", "-n", "1", tag)
        if tag_sha != verified_sha:
            ancestor_status = _git_status(
                "merge-base", "--is-ancestor", tag_sha, verified_sha
            )
            if ancestor_status == 1:
                raise ReleasePlanError("the release tag is outside verified main history")
            if ancestor_status != 0:
                raise ReleasePlanError("git could not verify the release tag")
        tagged_version = _version_at(tag_sha, tag)
        if tagged_version != tag[1:]:
            raise ReleasePlanError(
                "{} does not declare {}".format(tag, tag[1:])
            )
        if tag_sha != verified_sha and _git(
            "rev-list", "{}..{}".format(tag_sha, verified_sha), "--", str(VERSION_FILE)
        ):
            raise ReleasePlanError(
                "the version file changed after {}; choose a new version".format(tag)
            )
        target_sha = tag_sha
    else:
        target_sha = _git(
            "log",
            "--first-parent",
            "-n",
            "1",
            "--format=%H",
            verified_sha,
            "--",
            str(VERSION_FILE),
        )
        if not target_sha or _version_at(target_sha, target_sha) != tag[1:]:
            raise ReleasePlanError("cannot locate the commit that declared {}".format(tag))

    repository = repository_from_environment()
    endpoint = "repos/{}/releases/tags/{}".format(repository, quote(tag, safe=""))
    try:
        request_json(["api", "--method", "GET", endpoint])
        release_exists = True
    except GitHubAPIError as error:
        if error.status != 404:
            raise ReleasePlanError(str(error)) from error
        release_exists = False
    if release_exists and not tag_exists:
        raise ReleasePlanError("the GitHub release exists but its tag is unavailable")

    return {
        "tag": tag,
        "release_sha": target_sha,
        "tag_exists": str(tag_exists).lower(),
        "publish": str(not release_exists).lower(),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-sha", required=True)
    arguments = parser.parse_args(argv)
    try:
        outputs = plan_release(arguments.release_sha)
    except (GitHubAPIError, ReleasePlanError) as error:
        print("release-plan: {}".format(error), file=sys.stderr)
        return 2
    for name in ("tag", "release_sha", "tag_exists", "publish"):
        print("{}={}".format(name, outputs[name]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
