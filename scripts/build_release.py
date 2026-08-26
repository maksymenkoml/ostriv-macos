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
    for member in bundle.infolist():
        name = member.filename
        if not name or "\x00" in name or "\\" in name:
            raise ValueError("unsafe ZIP member: {}".format(name))
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

        canonical = "/".join(
            unicodedata.normalize("NFC", part).casefold() for part in raw_parts
        )
        if canonical in seen:
            raise ValueError("duplicate ZIP member: {}".format(name))
        kind, mode = _member_kind(member)
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
    root = destination.resolve()
    for _, target, kind, _ in validated:
        if target == root:
            raise ValueError("unsafe ZIP member targets extraction root")
        if target.is_symlink():
            raise ValueError("unsafe existing extraction path: {}".format(target))
        if target.exists() and not (kind == "directory" and target.is_dir()):
            raise ValueError("existing extraction path: {}".format(target))

    root.mkdir(parents=True, exist_ok=True)
    for member, target, kind, mode in validated:
        if kind == "directory":
            target.mkdir(parents=True, exist_ok=True)
            permissions = (mode & 0o777) or 0o755
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member, "r") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            permissions = (mode & 0o777) or 0o644
        target.chmod(permissions)


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
