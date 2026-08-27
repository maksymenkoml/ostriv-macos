"""Small fail-closed boundary around the GitHub CLI."""

import json
import os
import re
import subprocess
from typing import Any, Optional, Sequence


_HTTP_STATUS = re.compile(r"HTTP(?:/[0-9.]+)?[ ):]+([0-9]{3})")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class GitHubAPIError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def repository_from_environment() -> str:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if _REPOSITORY.fullmatch(repository) is None:
        raise GitHubAPIError("GITHUB_REPOSITORY is unavailable")
    return repository


def run_gh(arguments: Sequence[str]) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["gh", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise GitHubAPIError("cannot run the GitHub CLI") from error
    if result.returncode != 0:
        match = _HTTP_STATUS.search(result.stderr)
        status = int(match.group(1)) if match else None
        suffix = " (HTTP {})".format(status) if status is not None else ""
        raise GitHubAPIError("GitHub API request failed{}".format(suffix), status)
    return result


def request_json(arguments: Sequence[str]) -> Any:
    result = run_gh(arguments)
    try:
        return json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise GitHubAPIError("GitHub returned invalid JSON") from error
