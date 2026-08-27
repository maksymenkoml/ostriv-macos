#!/usr/bin/env python3
"""Atomically bind and publish a verified player release."""

import argparse
import re
import sys
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import quote

from github_api import (
    GitHubAPIError,
    repository_from_environment,
    request_json,
    run_gh,
)
from release_version import SEMANTIC_VERSION


COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
ASSET_NAME = "ostriv-macos-player.zip"
ASSET_LABEL = "Ostriv for macOS (Apple Silicon)"


class PublishReleaseError(RuntimeError):
    """The release cannot be bound to the verified commit safely."""


def _optional_json(arguments: Sequence[str]):
    try:
        return request_json(arguments)
    except GitHubAPIError as error:
        if error.status == 404:
            return None
        raise


def _tag_commit(repository: str, tag: str) -> Optional[str]:
    encoded_tag = quote(tag, safe="")
    reference = _optional_json(
        [
            "api",
            "--method",
            "GET",
            "repos/{}/git/ref/tags/{}".format(repository, encoded_tag),
        ]
    )
    if reference is None:
        return None
    commit = request_json(
        [
            "api",
            "--method",
            "GET",
            "repos/{}/commits/{}".format(repository, encoded_tag),
        ]
    )
    if not isinstance(commit, dict) or COMMIT_SHA.fullmatch(str(commit.get("sha", ""))) is None:
        raise PublishReleaseError("GitHub returned invalid tag data")
    return str(commit["sha"])


def publish_release(tag: str, release_sha: str, asset: Path) -> None:
    if not tag.startswith("v") or SEMANTIC_VERSION.fullmatch(tag[1:]) is None:
        raise PublishReleaseError("the release tag is invalid")
    if COMMIT_SHA.fullmatch(release_sha) is None:
        raise PublishReleaseError("the release commit SHA is invalid")
    asset = Path(asset)
    if asset.name != ASSET_NAME or not asset.is_file():
        raise PublishReleaseError("the verified player ZIP is unavailable")

    repository = repository_from_environment()
    encoded_tag = quote(tag, safe="")
    release = _optional_json(
        [
            "api",
            "--method",
            "GET",
            "repos/{}/releases/tags/{}".format(repository, encoded_tag),
        ]
    )
    tag_sha = _tag_commit(repository, tag)
    if release is not None:
        if tag_sha != release_sha:
            raise PublishReleaseError("the existing release tag targets another commit")
        return

    if tag_sha is None:
        try:
            request_json(
                [
                    "api",
                    "--method",
                    "POST",
                    "repos/{}/git/refs".format(repository),
                    "-f",
                    "ref=refs/tags/{}".format(tag),
                    "-f",
                    "sha={}".format(release_sha),
                ]
            )
        except GitHubAPIError as error:
            if error.status != 422:
                raise
        tag_sha = _tag_commit(repository, tag)

    if tag_sha != release_sha:
        raise PublishReleaseError("the release tag targets another commit")

    run_gh(
        [
            "release",
            "create",
            tag,
            "{}#{}".format(asset, ASSET_LABEL),
            "--repo",
            repository,
            "--verify-tag",
            "--generate-notes",
        ]
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--asset", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        publish_release(arguments.tag, arguments.release_sha, arguments.asset)
    except (GitHubAPIError, PublishReleaseError) as error:
        print("release-publish: {}".format(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
