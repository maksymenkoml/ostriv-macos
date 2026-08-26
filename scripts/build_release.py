"""Build and verify the Git-free player release archive."""

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable, List, Optional, Sequence, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from ostriv_macos.payload import load_manifest, validate_payload


ASSET_NAME = "ostriv-macos-player.zip"
PLAYER_PATHS = (
    Path("patch.py"),
    Path("ostriv_macos"),
    Path("payload-manifest.json"),
    Path("prebuilt/opengl32.dll"),
    Path("prebuilt/libgallium_wgl.dll"),
    Path("prebuilt/dxil.dll"),
    Path("prebuilt/libwinpthread-1.dll"),
    Path("prebuilt/README.md"),
    Path("assets/settings.data"),
    Path("README.md"),
    Path("LICENSE"),
)
_IGNORED_NAMES = {"__pycache__"}
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_MAX_ZIP_MEMBERS = 64
_MAX_PATH_BYTES = 512
_MAX_PATH_COMPONENT_BYTES = 255
_MAX_MEMBER_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_UNCOMPRESSED_BYTES = 96 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200
_STREAM_CHUNK_BYTES = 1024 * 1024
_SUPPORTED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


def _copy_directory(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in _IGNORED_NAMES for part in relative.parts):
            continue
        if path.suffix == ".pyc":
            continue
        target = destination / relative
        if path.is_symlink():
            raise ValueError("player allowlist path is a symlink: {}".format(path))
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        else:
            raise ValueError("player allowlist path is not a regular file: {}".format(path))


def copy_player_paths(source: Path, stage: Path) -> None:
    """Copy only the explicit player allowlist into a clean staging tree."""
    for relative in PLAYER_PATHS:
        path = source / relative
        target = stage / relative
        if path.is_symlink():
            raise ValueError("player allowlist path is a symlink: {}".format(relative))
        if path.is_dir():
            _copy_directory(path, target)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        else:
            raise FileNotFoundError("missing player allowlist path: {}".format(relative))


def _member_kind(member: zipfile.ZipInfo) -> Tuple[str, int]:
    mode = (member.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if member.is_dir():
        if file_type not in (0, stat.S_IFDIR):
            raise ValueError("unsafe ZIP member type: {}".format(member.filename))
        return "directory", mode
    if file_type not in (0, stat.S_IFREG):
        raise ValueError("unsafe ZIP member type: {}".format(member.filename))
    return "file", mode


def _validated_members(
    bundle: zipfile.ZipFile, destination: Path
) -> List[Tuple[zipfile.ZipInfo, Path, str, int]]:
    root = destination.resolve()
    validated = []
    seen = {}
    members = bundle.infolist()
    if len(members) > _MAX_ZIP_MEMBERS:
        raise ValueError("ZIP contains too many members")
    total_size = 0
    for member in members:
        name = member.filename
        if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
            raise ValueError("unsafe ZIP member: {}".format(name))
        try:
            path_variants = (
                name.encode("utf-8"),
                unicodedata.normalize("NFC", name).encode("utf-8"),
                unicodedata.normalize("NFD", name).encode("utf-8"),
            )
        except UnicodeEncodeError as error:
            raise ValueError("unsafe ZIP member encoding") from error
        if max(len(value) for value in path_variants) > _MAX_PATH_BYTES:
            raise ValueError("ZIP member path is too long: {}".format(name))
        posix = PurePosixPath(name)
        windows = PureWindowsPath(name)
        raw_parts = name.rstrip("/").split("/")
        if (
            posix.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or not raw_parts
            or any(part in ("", ".", "..") for part in raw_parts)
        ):
            raise ValueError("unsafe ZIP member: {}".format(name))
        for part in raw_parts:
            variants = (
                part.encode("utf-8"),
                unicodedata.normalize("NFC", part).encode("utf-8"),
                unicodedata.normalize("NFD", part).encode("utf-8"),
            )
            if max(len(value) for value in variants) > _MAX_PATH_COMPONENT_BYTES:
                raise ValueError("ZIP member path component is too long: {}".format(name))
        if member.flag_bits & 0x1:
            raise ValueError("encrypted ZIP member is not supported: {}".format(name))
        if member.compress_type not in _SUPPORTED_COMPRESSION:
            raise ValueError("unsupported ZIP compression: {}".format(name))
        if member.file_size < 0 or member.compress_size < 0:
            raise ValueError("invalid ZIP member size: {}".format(name))

        canonical = "/".join(
            unicodedata.normalize("NFC", part).casefold() for part in raw_parts
        )
        if canonical in seen:
            raise ValueError("duplicate ZIP member: {}".format(name))
        kind, mode = _member_kind(member)
        if kind == "directory" and (member.file_size or member.compress_size):
            raise ValueError("ZIP directory contains data: {}".format(name))
        if member.file_size > _MAX_MEMBER_UNCOMPRESSED_BYTES:
            raise ValueError("ZIP member is too large: {}".format(name))
        total_size += member.file_size
        if total_size > _MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ValueError("ZIP expands beyond the total size limit")
        if member.file_size:
            if member.compress_size == 0:
                raise ValueError("invalid zero compressed size: {}".format(name))
            if member.file_size > member.compress_size * _MAX_COMPRESSION_RATIO:
                raise ValueError("implausible ZIP compression ratio: {}".format(name))
        seen[canonical] = kind
        target = root.joinpath(*raw_parts)
        resolved = target.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            raise ValueError("unsafe ZIP member: {}".format(name))
        validated.append((member, target, kind, mode))

    for canonical, kind in seen.items():
        parts = canonical.split("/")
        for index in range(1, len(parts)):
            if seen.get("/".join(parts[:index])) == "file":
                raise ValueError("ZIP file conflicts with a child member: {}".format(canonical))
        if kind == "directory":
            continue
    return validated


def safe_extract(bundle: zipfile.ZipFile, destination: Path) -> None:
    """Extract validated regular files/directories without trusting extractall."""
    validated = _validated_members(bundle, destination)
    requested = Path(destination).absolute()
    root = requested.resolve()
    if requested.is_symlink() or root.exists():
        raise ValueError("extraction destination already exists: {}".format(destination))
    root.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=".{}-extract-".format(root.name), dir=str(root.parent)
    ) as directory:
        stage = Path(directory) / "payload"
        stage.mkdir()
        directory_modes = []
        actual_total = 0
        for member, target, kind, mode in validated:
            relative = target.relative_to(root)
            staged_target = stage / relative
            if kind == "directory":
                staged_target.mkdir(parents=True, exist_ok=True)
                directory_modes.append((staged_target, (mode & 0o777) or 0o755))
                continue

            staged_target.parent.mkdir(parents=True, exist_ok=True)
            actual_member = 0
            with bundle.open(member, "r") as source, staged_target.open("xb") as output:
                while True:
                    chunk = source.read(_STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    actual_member += len(chunk)
                    actual_total += len(chunk)
                    if actual_member > member.file_size:
                        raise ValueError(
                            "ZIP member exceeds its declared size: {}".format(member.filename)
                        )
                    if actual_member > _MAX_MEMBER_UNCOMPRESSED_BYTES:
                        raise ValueError("ZIP member exceeded the extraction size limit")
                    if actual_total > _MAX_TOTAL_UNCOMPRESSED_BYTES:
                        raise ValueError("ZIP exceeded the total extraction size limit")
                    output.write(chunk)
            if actual_member != member.file_size:
                raise ValueError(
                    "ZIP member size differs from its declaration: {}".format(
                        member.filename
                    )
                )
            staged_target.chmod((mode & 0o777) or 0o644)

        for path, permissions in sorted(
            directory_modes, key=lambda item: len(item[0].parts), reverse=True
        ):
            path.chmod(permissions)
        stage.chmod(0o755)
        if requested.is_symlink() or root.exists():
            raise ValueError("extraction destination appeared during extraction")
        stage.replace(root)


def _stage_files(stage: Path) -> Iterable[Path]:
    for path in sorted(stage.rglob("*")):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise ValueError("invalid staged release path: {}".format(path))
        if path.is_file():
            yield path


def _write_archive(stage: Path, archive: Path) -> None:
    with zipfile.ZipFile(
        archive,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as bundle:
        for path in _stage_files(stage):
            relative = path.relative_to(stage).as_posix()
            permissions = 0o755 if path.stat().st_mode & 0o111 else 0o644
            info = zipfile.ZipInfo(relative, _ZIP_TIMESTAMP)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | permissions) << 16
            with path.open("rb") as source, bundle.open(info, "w") as output:
                shutil.copyfileobj(source, output)


def _run_preflight(unpacked: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "patch.py", "--preflight"],
        cwd=unpacked,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise RuntimeError("unpacked player ZIP failed preflight: {}".format(detail))


def build_release(output: Path) -> None:
    """Build, validate, unpack, preflight, then atomically publish the player ZIP."""
    output = Path(output).absolute()
    if output.name != ASSET_NAME:
        raise ValueError("release asset must be named {}".format(ASSET_NAME))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".ostriv-release-", dir=str(output.parent)
    ) as directory:
        temporary = Path(directory)
        stage = temporary / "stage"
        unpacked = temporary / "unpacked"
        candidate = temporary / ASSET_NAME
        stage.mkdir()
        copy_player_paths(REPOSITORY_ROOT, stage)
        validate_payload(stage, load_manifest(stage / "payload-manifest.json"))
        _write_archive(stage, candidate)
        with zipfile.ZipFile(candidate) as bundle:
            safe_extract(bundle, unpacked)
        validate_payload(unpacked, load_manifest(unpacked / "payload-manifest.json"))
        _run_preflight(unpacked)
        os.replace(candidate, output)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist") / ASSET_NAME,
    )
    args = parser.parse_args(argv)
    build_release(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
