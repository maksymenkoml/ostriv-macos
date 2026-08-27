import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Tuple

from .diagnostics import PatchError


LFS_PREFIX = b"version https://git-lfs.github.com/spec/v1"
RECOVERY_MESSAGE = "The download is incomplete. Download the release ZIP again."


@dataclass(frozen=True)
class PayloadEntry:
    relative_path: str
    size: int
    sha256: str
    pe: bool


def _manifest_error(detail: str) -> PatchError:
    return PatchError("payload.manifest", RECOVERY_MESSAGE, detail)


def load_manifest(path: Path) -> Tuple[PayloadEntry, ...]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _manifest_error("Unreadable payload manifest: {}".format(error)) from error

    if not isinstance(data, dict) or data.get("schema") != 1:
        raise _manifest_error("Unsupported payload manifest schema")
    items = data.get("files")
    if not isinstance(items, list) or not items:
        raise _manifest_error("Invalid payload manifest files list")

    entries = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise _manifest_error("Invalid payload manifest entry: {!r}".format(item))
        try:
            relative_path = item["path"]
            size = item["size"]
            digest = item["sha256"]
            pe = item["pe"]
        except KeyError as error:
            raise _manifest_error("Invalid payload manifest entry: {}".format(error)) from error
        if (
            not isinstance(relative_path, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not isinstance(digest, str)
            or not isinstance(pe, bool)
        ):
            raise _manifest_error("Invalid payload manifest entry: {!r}".format(item))

        relative = PurePosixPath(relative_path)
        normalized_path = relative.as_posix()
        digest = digest.lower()
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or normalized_path in seen
            or size < 0
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise _manifest_error("Invalid payload manifest entry: {!r}".format(item))
        seen.add(normalized_path)
        entries.append(PayloadEntry(normalized_path, size, digest, pe))
    return tuple(entries)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_payload(root: Path, entries: Iterable[PayloadEntry]) -> None:
    for entry in entries:
        path = root / entry.relative_path
        if not path.is_file():
            code = "payload.missing"
            detail = "{} is missing".format(entry.relative_path)
        else:
            with path.open("rb") as stream:
                header = stream.read(128)
            if header.startswith(LFS_PREFIX):
                code = "payload.lfs_pointer"
                detail = "{} is a Git LFS pointer".format(entry.relative_path)
            elif entry.pe and not header.startswith(b"MZ"):
                code = "payload.not_pe"
                detail = "{} is not a PE DLL".format(entry.relative_path)
            elif path.stat().st_size != entry.size:
                code = "payload.size_mismatch"
                detail = "{} has the wrong size".format(entry.relative_path)
            elif _digest(path) != entry.sha256:
                code = "payload.hash_mismatch"
                detail = "{} has the wrong SHA-256".format(entry.relative_path)
            else:
                continue
        raise PatchError(code, RECOVERY_MESSAGE, detail)
