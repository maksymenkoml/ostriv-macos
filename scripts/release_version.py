#!/usr/bin/env python3
"""Print the release tag declared by the project package."""

import argparse
import ast
import re
import sys
from pathlib import Path
from typing import Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VERSION_FILE = REPOSITORY_ROOT / "ostriv_macos/__init__.py"
SEMANTIC_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


class ReleaseVersionError(ValueError):
    """The project does not declare one canonical release version."""


def parse_release_version(source: str, filename: str = "<version>") -> str:
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as error:
        raise ReleaseVersionError("the version file is not valid Python") from error

    declarations = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in statement.targets
            ):
                declarations.append(statement.value)
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "__version__"
        ):
            declarations.append(statement.value)

    if len(declarations) != 1:
        raise ReleaseVersionError("expected exactly one __version__ declaration")
    value = declarations[0]
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        raise ReleaseVersionError("__version__ must be a string literal")
    if SEMANTIC_VERSION.fullmatch(value.value) is None:
        raise ReleaseVersionError("__version__ must use X.Y.Z without leading zeros")
    return value.value


def read_release_version(path: Path) -> str:
    path = Path(path)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReleaseVersionError("cannot read the version file") from error
    return parse_release_version(source, filename=str(path))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version-file", type=Path, default=DEFAULT_VERSION_FILE)
    arguments = parser.parse_args(argv)
    try:
        version = read_release_version(arguments.version_file)
    except ReleaseVersionError as error:
        print("release-version: {}".format(error), file=sys.stderr)
        return 2
    print("v{}".format(version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
