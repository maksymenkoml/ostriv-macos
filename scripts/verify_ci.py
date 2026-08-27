#!/usr/bin/env python3
"""Verify that an exact commit passed the main Test push workflow."""

import argparse
import re
import sys
from typing import Optional, Sequence

from github_api import (
    GitHubAPIError,
    repository_from_environment,
    request_json,
)


COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


class ReleaseCIError(RuntimeError):
    """The release commit lacks authoritative successful CI evidence."""


def verify_ci(sha: str) -> None:
    if COMMIT_SHA.fullmatch(sha) is None:
        raise ReleaseCIError("the release commit SHA is invalid")
    repository = repository_from_environment()
    runs = request_json(
        [
            "run",
            "list",
            "--repo",
            repository,
            "--workflow",
            "Test",
            "--commit",
            sha,
            "--event",
            "push",
            "--status",
            "success",
            "--limit",
            "100",
            "--json",
            "conclusion,event,headBranch,headSha",
        ]
    )
    if not isinstance(runs, list):
        raise ReleaseCIError("GitHub returned invalid workflow-run data")
    if not any(
        isinstance(run, dict)
        and run.get("headSha") == sha
        and run.get("headBranch") == "main"
        and run.get("event") == "push"
        and run.get("conclusion") == "success"
        for run in runs
    ):
        raise ReleaseCIError("no successful main Test push run exists for {}".format(sha))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    arguments = parser.parse_args(argv)
    try:
        verify_ci(arguments.sha)
    except (GitHubAPIError, ReleaseCIError) as error:
        print("release-ci: {}".format(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
