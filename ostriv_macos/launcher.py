"""Verified CrossOver launcher application installation and migration."""

import ast
import base64
import bz2
import hashlib
import json
import os
import plistlib
import re
import secrets
import shlex
import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional

from .diagnostics import CommandRunner, PatchError, command_failure_detail
from .discovery import GameInstallation
from .installer import Transaction, UndoRecord
from .launcher_runtime import LauncherConfig


LAUNCHER_NAME = "Ostriv (patched)"
LAUNCHER_MENU = "StartMenu/" + LAUNCHER_NAME
RUNTIME_NAME = "play-ostriv-patched.py"
CONFIG_NAME = "launcher-config.json"
PLIST_FIELDS = (
    "CFBundleName",
    "CFBundleDisplayName",
    "CFBundleIdentifier",
    "CrossOverHelperCommand",
    "CXHelperAppBottleName",
    "CXHelperAppBottleTag",
)
LEGACY_RUNTIME_NORMALIZED_SHA256 = {
    "0f1cc970c2f14861eff39d99c2494081f4615e395777021ccc7c2c8182a7b474",
    "459c38c2e8c64ea216747161fdc17f22f547e08671bda1e0af195cd28ba74193",
}


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str:
    return _digest(path.read_bytes())


def _lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _filesystem_safe_text(value: str) -> bool:
    """Accept only text that the local filesystem can represent losslessly."""
    contains_surrogate = any(
        0xD800 <= ord(character) <= 0xDFFF for character in value
    )
    if "\0" in value or contains_surrogate:
        return False
    try:
        encoded = os.fsencode(value)
        return os.fsdecode(encoded) == value
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError):
        return False


def _regular_file_no_follow(root: Path, relative: Path) -> Optional[Path]:
    """Return a regular leaf only when every lexical parent is a real directory."""
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return None
    current = root
    try:
        root_mode = current.lstat().st_mode
        if not stat.S_ISDIR(root_mode) or stat.S_ISLNK(root_mode):
            return None
        for part in relative.parts[:-1]:
            current = current / part
            mode = current.lstat().st_mode
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                return None
        leaf = current / relative.parts[-1]
        mode = leaf.lstat().st_mode
        if stat.S_ISREG(mode) and not stat.S_ISLNK(mode):
            return leaf
    except OSError:
        return None
    return None


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        replaced = True
        _sync_directory(path.parent)
    finally:
        if not replaced:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _sync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _snapshot(path: Path, after_digest: Optional[str] = None) -> Dict[str, object]:
    path = path.parent.resolve(strict=False) / path.name
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None and stat.S_ISLNK(metadata.st_mode):
        return {
            "path": str(path),
            "present": True,
            "type": "symlink",
            "target": os.readlink(path),
        }
    if metadata is not None and stat.S_ISREG(metadata.st_mode):
        data = path.read_bytes()
        item: Dict[str, object] = {
            "path": str(path),
            "present": True,
            "type": "file",
            "content": base64.b64encode(data).decode("ascii"),
            "sha256": _digest(data),
            "mode": _mode(path),
        }
        if after_digest:
            item["allowed_current_sha256"] = [after_digest]
        return item
    item = {"path": str(path), "present": False, "type": "absent"}
    if after_digest:
        item["remove_sha256"] = after_digest
    return item


def _tree_files(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    files = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directory_names[:] = [
            name for name in directory_names if not (current_path / name).is_symlink()
        ]
        files.extend(
            current_path / name
            for name in file_names
            if not (current_path / name).is_symlink()
        )
    return sorted(files)


def _tree_directories(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    directories = [root]
    for current, directory_names, _file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directory_names[:] = [
            name for name in directory_names if not (current_path / name).is_symlink()
        ]
        directories.extend(current_path / name for name in directory_names)
    return sorted(set(directories))


def _inventory(root: Path) -> List[Dict[str, object]]:
    if not root.is_dir() or root.is_symlink():
        return []
    inventory: List[Dict[str, object]] = [
        {"relative_path": ".", "type": "directory", "mode": _mode(root)}
    ]
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        names = sorted([*directory_names, *file_names])
        for name in names:
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                inventory.append(
                    {
                        "relative_path": relative,
                        "type": "symlink",
                        "target": os.readlink(path),
                    }
                )
            elif stat.S_ISDIR(metadata.st_mode):
                inventory.append(
                    {
                        "relative_path": relative,
                        "type": "directory",
                        "mode": stat.S_IMODE(metadata.st_mode),
                    }
                )
            elif stat.S_ISREG(metadata.st_mode):
                inventory.append(
                    {
                        "relative_path": relative,
                        "type": "file",
                        "sha256": _file_digest(path),
                        "mode": stat.S_IMODE(metadata.st_mode),
                    }
                )
            else:
                inventory.append({"relative_path": relative, "type": "unsupported"})
        directory_names[:] = [
            name for name in directory_names if not (current_path / name).is_symlink()
        ]
    return sorted(inventory, key=lambda item: str(item["relative_path"]))


def _remove_inventory_tree(root: Path, inventory: object) -> None:
    if not isinstance(inventory, list) or _inventory(root) != inventory:
        raise PatchError(
            "restore.launcher_ownership",
            "Restore failed.",
            "Owned launcher tree changed: {}".format(root),
        )
    for item in sorted(
        inventory,
        key=lambda entry: len(Path(str(entry.get("relative_path", ""))).parts),
    ):
        if item.get("type") == "directory":
            relative = Path(str(item.get("relative_path", "")))
            path = root if relative == Path(".") else root / relative
            path.chmod(int(item.get("mode", 0o755)) | 0o700)
    for item in sorted(
        inventory,
        key=lambda entry: len(Path(str(entry.get("relative_path", ""))).parts),
        reverse=True,
    ):
        relative = Path(str(item.get("relative_path", "")))
        path = root if relative == Path(".") else root / relative
        if item.get("type") in ("file", "symlink"):
            path.unlink()
        elif item.get("type") == "directory":
            path.rmdir()


def _captured_tree(root: Path) -> Dict[str, object]:
    entries = _inventory(root)
    captured = []
    for item in entries:
        saved = dict(item)
        if item.get("type") == "file":
            path = root / Path(str(item["relative_path"]))
            saved["content"] = base64.b64encode(path.read_bytes()).decode("ascii")
        captured.append(saved)
    return {"root": str(root), "present": bool(entries), "entries": captured}


def _validated_captured_entries(snapshot: Mapping[str, object]) -> List[Dict[str, object]]:
    present = snapshot.get("present")
    entries = snapshot.get("entries")
    if not isinstance(present, bool) or not isinstance(entries, list):
        raise ValueError("captured launcher tree schema is invalid")
    if not present:
        if entries:
            raise ValueError("absent captured launcher tree contains entries")
        return []
    validated = []
    by_path = {}
    for item in entries:
        if not isinstance(item, dict) or not isinstance(
            item.get("relative_path"), str
        ):
            raise ValueError("captured launcher tree entry is invalid")
        relative_text = str(item["relative_path"])
        relative = PurePosixPath(relative_text)
        if (
            not relative_text
            or not _filesystem_safe_text(relative_text)
            or any(not _filesystem_safe_text(part) for part in relative.parts)
            or relative.is_absolute()
            or ".." in relative.parts
            or relative_text != relative.as_posix()
            or relative in by_path
        ):
            raise ValueError("captured launcher path is unsafe or duplicated")
        item_type = item.get("type")
        if item_type not in ("directory", "file", "symlink"):
            raise ValueError("captured launcher entry type is invalid")
        if item_type in ("directory", "file"):
            mode = item.get("mode")
            if not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 0o7777:
                raise ValueError("captured launcher entry mode is invalid")
        if item_type == "file":
            content = item.get("content")
            digest = item.get("sha256")
            if not isinstance(content, str) or not isinstance(digest, str):
                raise ValueError("captured launcher file metadata is invalid")
            try:
                data = base64.b64decode(content, validate=True)
            except (ValueError, TypeError) as error:
                raise ValueError("captured launcher file content is invalid") from error
            if _digest(data) != digest:
                raise ValueError("captured launcher file digest is invalid")
        if item_type == "symlink":
            target = item.get("target")
            if (
                not isinstance(target, str)
                or not target
                or not _filesystem_safe_text(target)
            ):
                raise ValueError("captured launcher symlink metadata is invalid")
        saved = dict(item)
        by_path[relative] = saved
        validated.append(saved)
    root = by_path.get(PurePosixPath("."))
    if root is None or root.get("type") != "directory":
        raise ValueError("captured launcher root directory is missing")
    for relative, item in by_path.items():
        if relative == PurePosixPath("."):
            continue
        parent = relative.parent
        while parent != PurePosixPath("."):
            parent_item = by_path.get(parent)
            if parent_item is None or parent_item.get("type") != "directory":
                raise ValueError("captured launcher entry has a non-directory parent")
            parent = parent.parent
    return validated


def _restore_captured_tree(snapshot: Mapping[str, object]) -> None:
    root = Path(str(snapshot["root"]))
    entries = _validated_captured_entries(snapshot)
    if _lexists(root):
        _remove_inventory_tree(root, _inventory(root))
    if snapshot.get("present") is not True:
        return
    directories = [item for item in entries if item.get("type") == "directory"]
    for item in sorted(
        directories,
        key=lambda entry: len(Path(str(entry["relative_path"])).parts),
    ):
        relative = Path(str(item["relative_path"]))
        path = root if relative == Path(".") else root / relative
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(int(item.get("mode", 0o755)) | 0o700)
        _sync_directory(path.parent)
    for item in entries:
        relative = Path(str(item.get("relative_path", "")))
        if relative == Path(".") or item.get("type") == "directory":
            continue
        path = root / relative
        if item.get("type") == "symlink":
            path.symlink_to(str(item["target"]))
            _sync_directory(path.parent)
        elif item.get("type") == "file":
            data = base64.b64decode(str(item["content"]), validate=True)
            if _digest(data) != item.get("sha256"):
                raise ValueError("captured launcher file digest is invalid")
            _atomic_write(path, data, int(item.get("mode", 0o644)))
    for item in sorted(
        directories,
        key=lambda entry: len(Path(str(entry["relative_path"])).parts),
        reverse=True,
    ):
        relative = Path(str(item["relative_path"]))
        path = root if relative == Path(".") else root / relative
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fchmod(descriptor, int(item.get("mode", 0o755)))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _saved_file(path: Path) -> Optional[Dict[str, object]]:
    if not path.is_file():
        return None
    data = path.read_bytes()
    return {
        "content": base64.b64encode(data).decode("ascii"),
        "sha256": _digest(data),
        "mode": _mode(path),
    }


def _validated_cpio_entry(
    name: str,
    mode: int,
    links: int,
    content: bytes,
    seen: set,
) -> Optional[tuple[PurePosixPath, int, bytes]]:
    if name == "TRAILER!!!":
        return None
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or path == PurePosixPath(".")
        or ".." in path.parts
        or path in seen
    ):
        raise OSError("Unsafe Menu Helper archive path: {!r}".format(name))
    kind = stat.S_IFMT(mode)
    if kind == stat.S_IFREG:
        if links != 1:
            raise OSError("Menu Helper archive hard links are not allowed")
    elif kind == stat.S_IFDIR:
        if content:
            raise OSError("Menu Helper archive directory contains data")
    else:
        raise OSError("Menu Helper archive contains a link or special file")
    seen.add(path)
    return path, mode, content


def _newc_entries(data: bytes) -> List[tuple[PurePosixPath, int, bytes]]:
    entries = []
    offset = 0
    seen = set()
    while True:
        if len(data) - offset < 110 or data[offset : offset + 6] not in (
            b"070701",
            b"070702",
        ):
            raise OSError("Menu Helper archive is not a supported newc archive")
        header = data[offset + 6 : offset + 110]
        try:
            values = [int(header[index : index + 8], 16) for index in range(0, 104, 8)]
        except ValueError as error:
            raise OSError("Menu Helper archive header is invalid") from error
        mode, links, size, name_size = values[1], values[4], values[6], values[11]
        offset += 110
        if name_size < 1 or offset + name_size > len(data):
            raise OSError("Menu Helper archive name is truncated")
        encoded_name = data[offset : offset + name_size]
        if not encoded_name.endswith(b"\0"):
            raise OSError("Menu Helper archive name is not terminated")
        try:
            name = encoded_name[:-1].decode("utf-8")
        except UnicodeError as error:
            raise OSError("Menu Helper archive name is not UTF-8") from error
        offset += name_size
        offset += (-offset) % 4
        if offset + size > len(data):
            raise OSError("Menu Helper archive entry is truncated")
        content = data[offset : offset + size]
        offset += size
        offset += (-offset) % 4
        entry = _validated_cpio_entry(name, mode, links, content, seen)
        if entry is None:
            return entries
        entries.append(entry)


def _odc_entries(data: bytes) -> List[tuple[PurePosixPath, int, bytes]]:
    entries = []
    offset = 0
    seen = set()
    widths = (6, 6, 6, 6, 6, 6, 6, 11, 6, 11)
    while True:
        if len(data) - offset < 76 or data[offset : offset + 6] != b"070707":
            raise OSError("Menu Helper archive is not a supported odc archive")
        header = data[offset + 6 : offset + 76]
        values = []
        cursor = 0
        try:
            for width in widths:
                values.append(int(header[cursor : cursor + width], 8))
                cursor += width
        except ValueError as error:
            raise OSError("Menu Helper archive header is invalid") from error
        mode, links, name_size, size = values[2], values[5], values[8], values[9]
        offset += 76
        if name_size < 1 or offset + name_size > len(data):
            raise OSError("Menu Helper archive name is truncated")
        encoded_name = data[offset : offset + name_size]
        if not encoded_name.endswith(b"\0"):
            raise OSError("Menu Helper archive name is not terminated")
        try:
            name = encoded_name[:-1].decode("utf-8")
        except UnicodeError as error:
            raise OSError("Menu Helper archive name is not UTF-8") from error
        offset += name_size
        offset += offset % 2
        if offset + size > len(data):
            raise OSError("Menu Helper archive entry is truncated")
        content = data[offset : offset + size]
        offset += size
        offset += offset % 2
        entry = _validated_cpio_entry(name, mode, links, content, seen)
        if entry is None:
            return entries
        entries.append(entry)


def _extract_menu_helper(template: Path, destination: Path) -> None:
    try:
        archive = bz2.decompress(template.read_bytes())
        entries = (
            _odc_entries(archive)
            if archive.startswith(b"070707")
            else _newc_entries(archive)
        )
    except (OSError, EOFError, ValueError) as error:
        raise OSError("Menu Helper extraction failed: {}".format(error)) from error
    if any(destination.iterdir()):
        raise OSError("Menu Helper extraction destination is not empty")
    for relative, mode, _content in entries:
        if stat.S_ISDIR(mode):
            path = destination.joinpath(*relative.parts)
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(stat.S_IMODE(mode))
    for relative, mode, content in entries:
        if stat.S_ISREG(mode):
            path = destination.joinpath(*relative.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(stat.S_IMODE(mode))


@dataclass(frozen=True)
class LauncherState(Mapping[str, object]):
    schema: int
    artifacts: List[Dict[str, object]]
    app: str
    runtime: str
    config: str
    menu_entry: Dict[str, object]
    previous_app: Optional[str]
    previous_app_inventory: List[Dict[str, object]]
    previous_runtime: Optional[Dict[str, object]]
    previous_config: Optional[Dict[str, object]]
    app_inventory: List[Dict[str, object]]
    runtime_sha256: str
    config_sha256: str
    executable_sha256: str
    plist_sha256: str
    icon_sha256: str
    plist_fields: List[str]
    command: str
    bottle_realpath: str
    bottle_name: str
    bottle_argument: str
    scope: str
    bottle_tag: str
    lock_path: str
    lock_sha256: str
    recovery_marker: str
    profile_owner_token: str

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__dataclass_fields__)

    def __len__(self) -> int:
        return len(self.__dataclass_fields__)


class LauncherInstaller:
    def __init__(
        self,
        package_root: Path,
        launcher_destination: Optional[Path] = None,
        runner: Optional[CommandRunner] = None,
        runtime_source: Optional[Path] = None,
        extractor: Optional[Callable[[Path, Path], None]] = None,
        profile_backend_factory: Optional[Callable[[], object]] = None,
        lock_factory: Optional[Callable[[Path], object]] = None,
    ) -> None:
        self.package_root = Path(package_root).resolve()
        self.launcher_destination = Path(
            launcher_destination
            if launcher_destination is not None
            else Path.home() / "Applications/CrossOver"
        )
        self.runner = runner or CommandRunner()
        self.runtime_source = Path(
            runtime_source
            if runtime_source is not None
            else self.package_root / "ostriv_macos/launcher_runtime.py"
        )
        self.extractor = extractor or _extract_menu_helper
        self.profile_backend_factory = profile_backend_factory
        self.lock_factory = lock_factory

    def _bind_owned_directory_cleanup(
        self, transaction: Transaction, installation: GameInstallation
    ) -> None:
        if getattr(transaction, "_launcher_directory_cleanup_bound", False):
            return
        original = transaction.handlers.get("restore_launcher")
        if original is None:
            return
        if getattr(original, "_ostriv_launcher_handler", False):
            setattr(transaction, "_launcher_directory_cleanup_bound", True)
            return

        handlers = dict(transaction.handlers)
        handlers["restore_launcher"] = self.undo_handler(installation, original)
        transaction.handlers = handlers
        setattr(transaction, "_launcher_directory_cleanup_bound", True)

    def undo_handler(
        self,
        installation: GameInstallation,
        fallback: Callable[[UndoRecord], None],
    ) -> Callable[[UndoRecord], None]:
        """Build the complete durable launcher undo handler before recovery starts."""
        app = self._app_path()
        allowed_roots = {
            app,
            app.with_name(app.name + ".pending"),
            app.with_name("." + app.name + ".ostriv-macos.previous"),
            app.with_name("." + app.name + ".ostriv-macos.replaced"),
        }
        allowed_files = {
            installation.bottle.root.resolve() / RUNTIME_NAME,
            installation.bottle.root.resolve() / CONFIG_NAME,
            installation.bottle.root.resolve() / ".ostriv-launcher.lock",
            installation.bottle.root.resolve() / ".ostriv-profile-recovery.json",
        }

        def owned_paths(record: UndoRecord) -> tuple[Optional[Path], List[Path]]:
            root_text = record.data.get("owned_root")
            directories = record.data.get("owned_directories", [])
            if root_text is None and directories == []:
                return None, []
            if not isinstance(root_text, str) or not isinstance(directories, list):
                raise PatchError(
                    "restore.launcher_ownership",
                    "Restore failed.",
                    "Launcher undo ownership metadata is invalid",
                )
            root = Path(root_text)
            if root not in allowed_roots:
                raise PatchError(
                    "restore.launcher_ownership",
                    "Restore failed.",
                    "Launcher undo root is outside the allowlist: {}".format(root),
                )
            paths = []
            for item in directories:
                if not isinstance(item, str):
                    raise PatchError(
                        "restore.launcher_ownership",
                        "Restore failed.",
                        "Launcher undo directory inventory is invalid",
                    )
                path = Path(item)
                if path != root and root not in path.parents:
                    raise PatchError(
                        "restore.launcher_ownership",
                        "Restore failed.",
                        "Launcher undo directory is outside its owned root: {}".format(path),
                    )
                paths.append(path)
            if paths != [root]:
                raise PatchError(
                    "restore.launcher_ownership",
                    "Restore failed.",
                    "Launcher undo directory inventory is not the exact owned root",
                )
            return root, paths

        def remove_owned_tree(root: Path) -> None:
            try:
                root_mode = root.lstat().st_mode
            except FileNotFoundError:
                return
            if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
                raise PatchError(
                    "restore.launcher_ownership",
                    "Restore failed.",
                    "Owned launcher tree changed type: {}".format(root),
                )
            for current, directory_names, file_names in os.walk(
                root, topdown=False, followlinks=False
            ):
                current_path = Path(current)
                for name in file_names:
                    (current_path / name).unlink()
                for name in directory_names:
                    child = current_path / name
                    if child.is_symlink():
                        child.unlink()
                    else:
                        child.rmdir()
            root.rmdir()

        def restore_and_prune(record: UndoRecord) -> None:
            root, directories = owned_paths(record)
            tree_snapshots = record.data.get("restore_trees", [])
            if not isinstance(tree_snapshots, list):
                raise PatchError(
                    "restore.launcher_ownership",
                    "Restore failed.",
                    "Launcher tree snapshots are invalid",
                )
            seen_tree_roots = set()
            for snapshot in tree_snapshots:
                snapshot_root = (
                    Path(str(snapshot.get("root", "")))
                    if isinstance(snapshot, dict)
                    else None
                )
                if (
                    not isinstance(snapshot, dict)
                    or snapshot_root not in allowed_roots
                    or snapshot_root in seen_tree_roots
                ):
                    raise PatchError(
                        "restore.launcher_ownership",
                        "Restore failed.",
                        "Launcher tree snapshot is outside the allowlist",
                    )
                seen_tree_roots.add(snapshot_root)
                try:
                    _validated_captured_entries(snapshot)
                except (KeyError, TypeError, ValueError) as error:
                    raise PatchError(
                        "restore.launcher_ownership",
                        "Restore failed.",
                        "Launcher tree snapshot is invalid: {}".format(error),
                    ) from error
            moved = record.data.get("moved_tree")
            if moved is not None:
                if not isinstance(moved, dict):
                    raise PatchError(
                        "restore.launcher_ownership",
                        "Restore failed.",
                        "Launcher moved-tree undo metadata is invalid",
                    )
                source = Path(str(moved.get("source", "")))
                destination = Path(str(moved.get("destination", "")))
                if source not in allowed_roots or destination != app:
                    raise PatchError(
                        "restore.launcher_ownership",
                        "Restore failed.",
                        "Launcher moved-tree undo is outside the allowlist",
                    )
                source_inventory = moved.get("source_inventory")
                replacement_inventory = moved.get("replacement_inventory")
                if _lexists(source):
                    if _inventory(source) != source_inventory:
                        raise PatchError(
                            "restore.launcher_ownership",
                            "Restore failed.",
                            "Moved launcher backup changed: {}".format(source),
                        )
                    if _lexists(destination):
                        _remove_inventory_tree(destination, replacement_inventory)
                    os.replace(source, destination)
                    _sync_directory(destination.parent)
            fallback(record)
            for snapshot in tree_snapshots:
                _restore_captured_tree(snapshot)
            file_snapshots = record.data.get("restore_files", [])
            if not isinstance(file_snapshots, list):
                raise PatchError(
                    "restore.launcher_ownership",
                    "Restore failed.",
                    "Launcher file snapshots are invalid",
                )
            for snapshot in file_snapshots:
                if (
                    not isinstance(snapshot, dict)
                    or Path(str(snapshot.get("path", ""))) not in allowed_files
                ):
                    raise PatchError(
                        "restore.launcher_ownership",
                        "Restore failed.",
                        "Launcher file snapshot is outside the allowlist",
                    )
                path = Path(str(snapshot["path"]))
                if snapshot.get("present") is True and snapshot.get("type") == "symlink":
                    target = snapshot.get("target")
                    if not isinstance(target, str) or not _filesystem_safe_text(target):
                        raise PatchError(
                            "restore.launcher_ownership",
                            "Restore failed.",
                            "Launcher symlink snapshot is invalid",
                        )
                    if _lexists(path):
                        if path.is_symlink() and os.readlink(path) == target:
                            continue
                        if path.is_dir() and not path.is_symlink():
                            raise PatchError(
                                "restore.launcher_ownership",
                                "Restore failed.",
                                "Launcher snapshot leaf changed type: {}".format(path),
                            )
                        path.unlink()
                    path.symlink_to(target)
                    _sync_directory(path.parent)
                elif snapshot.get("present") is True:
                    data = base64.b64decode(str(snapshot["content"]), validate=True)
                    if _digest(data) != snapshot.get("sha256"):
                        raise PatchError(
                            "restore.launcher_ownership",
                            "Restore failed.",
                            "Launcher file snapshot digest is invalid",
                        )
                    _atomic_write(path, data, int(snapshot.get("mode", 0o644)))
                elif path.is_file() and not path.is_symlink():
                    path.unlink()
            if record.data.get("purge_menu") is True:
                command = [
                    str(self._cxmenu(installation)),
                    "--bottle",
                    self._command_bottle(installation),
                ]
                command.extend(installation.bottle.scope_args())
                command.extend(["--purge", "--filter", LAUNCHER_MENU])
                result = self.runner.run(command, timeout=60.0)
                if result.returncode != 0:
                    raise PatchError(
                        "restore.launcher_menu",
                        "Restore failed.",
                        command_failure_detail(result, "cxmenu purge failed"),
                    )
            if record.data.get("recreate_menu") is True:
                recreate_command = self._expected_command(installation)
                if record.data.get("legacy_menu") is True:
                    recreate_command = "exec /usr/bin/env python3 {}".format(
                        installation.bottle.root.resolve() / RUNTIME_NAME
                    )
                result = self.runner.run(
                    self._menu_create_command(
                        installation, recreate_command
                    ),
                    timeout=60.0,
                )
                if result.returncode != 0:
                    raise PatchError(
                        "restore.launcher_menu",
                        "Restore failed.",
                        command_failure_detail(
                            result, "cxmenu create failed during rollback"
                        ),
                    )
            if root is not None and record.data.get("remove_owned_tree") is True:
                remove_owned_tree(root)
                return
            for directory in sorted(
                directories, key=lambda path: len(path.parts), reverse=True
            ):
                try:
                    directory.rmdir()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

        setattr(restore_and_prune, "_ostriv_launcher_handler", True)
        return restore_and_prune

    def _expected_command(self, installation: GameInstallation) -> str:
        bottle_root = installation.bottle.root.resolve()
        return "exec /usr/bin/env python3 {} {}".format(
            shlex.quote(str(bottle_root / RUNTIME_NAME)),
            shlex.quote(str(bottle_root / CONFIG_NAME)),
        )

    def _menu_create_command(
        self, installation: GameInstallation, command: str
    ) -> List[str]:
        argv = [
            str(self._cxmenu(installation)),
            "--bottle",
            self._command_bottle(installation),
        ]
        argv.extend(installation.bottle.scope_args())
        argv.extend(
            [
                "--create",
                LAUNCHER_MENU,
                "--type",
                "raw",
                "--command",
                command,
                "--description",
                "Ostriv with the sRGB color-profile FPS fix",
                "--install",
            ]
        )
        return argv

    def _restore_undo_data(
        self,
        installation: GameInstallation,
        state: Mapping[str, object],
    ) -> Dict[str, object]:
        app = self._app_path()
        previous = app.with_name("." + app.name + ".ostriv-macos.previous")
        runtime = installation.bottle.root.resolve() / RUNTIME_NAME
        config = installation.bottle.root.resolve() / CONFIG_NAME
        lock, marker = self._recovery_paths(installation)
        if state.get("legacy") is True:
            self._validate_legacy_paths(installation, state)
            return {
                "restore_trees": [_captured_tree(app)],
                "restore_files": [_snapshot(runtime)],
                "recreate_menu": True,
                "legacy_menu": True,
            }
        self._validate_state_paths(
            installation,
            state,
            "restore.launcher_ownership",
            "Restore failed.",
        )
        menu = state.get("menu_entry")
        return {
            "restore_trees": [_captured_tree(app), _captured_tree(previous)],
            "restore_files": [
                _snapshot(runtime),
                _snapshot(config),
                _snapshot(lock),
                _snapshot(marker),
            ],
            "recreate_menu": isinstance(menu, dict)
            and menu.get("name") == LAUNCHER_MENU,
        }

    def restore_undo_data(
        self,
        installation: GameInstallation,
        state: Mapping[str, object],
    ) -> Dict[str, object]:
        try:
            return self._restore_undo_data(installation, state)
        except PatchError:
            raise
        except (OSError, UnicodeError, ValueError, plistlib.InvalidFileException) as error:
            raise PatchError(
                "restore.launcher_prepare",
                "Restore failed.",
                "Unable to capture launcher rollback state: {}: {}".format(
                    type(error).__name__, error
                ),
            ) from error

    def validate_recovery_state(
        self,
        installation: GameInstallation,
        state: Mapping[str, object],
    ) -> None:
        """Authenticate the complete launcher-state schema without mutation."""
        def valid_digest(value: object) -> bool:
            return isinstance(value, str) and re.fullmatch(
                r"[0-9a-f]{64}", value
            ) is not None

        def validate_inventory(value: object, *, optional: bool) -> None:
            if not isinstance(value, list) or (not optional and not value):
                raise ValueError("launcher recovery tree inventory is invalid")
            by_path = {}
            for entry in value:
                if not isinstance(entry, dict) or not isinstance(
                    entry.get("relative_path"), str
                ):
                    raise ValueError("launcher recovery tree entry is invalid")
                relative_text = entry["relative_path"]
                relative = PurePosixPath(relative_text)
                item_type = entry.get("type")
                if item_type == "directory":
                    expected = {"relative_path", "type", "mode"}
                elif item_type == "file":
                    expected = {"relative_path", "type", "sha256", "mode"}
                elif item_type == "symlink":
                    expected = {"relative_path", "type", "target"}
                else:
                    raise ValueError("launcher recovery tree entry type is invalid")
                if (
                    set(entry) != expected
                    or not relative_text
                    or not _filesystem_safe_text(relative_text)
                    or any(not _filesystem_safe_text(part) for part in relative.parts)
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or relative_text != relative.as_posix()
                    or relative in by_path
                ):
                    raise ValueError("launcher recovery tree path is invalid")
                if item_type in ("directory", "file") and (
                    type(entry.get("mode")) is not int
                    or not 0 <= entry["mode"] <= 0o7777
                ):
                    raise ValueError("launcher recovery tree mode is invalid")
                if item_type == "file" and not valid_digest(entry.get("sha256")):
                    raise ValueError("launcher recovery tree digest is invalid")
                if item_type == "symlink" and (
                    not isinstance(entry.get("target"), str)
                    or not entry["target"]
                    or not _filesystem_safe_text(entry["target"])
                ):
                    raise ValueError("launcher recovery tree target is invalid")
                by_path[relative] = entry
            if not value:
                return
            root = by_path.get(PurePosixPath("."))
            if root is None or root.get("type") != "directory":
                raise ValueError("launcher recovery tree root is missing")
            for relative in by_path:
                parent = relative.parent
                while relative != PurePosixPath(".") and parent != PurePosixPath("."):
                    if by_path.get(parent, {}).get("type") != "directory":
                        raise ValueError("launcher recovery tree parent is missing")
                    parent = parent.parent

        def validate_saved_file(value: object) -> None:
            if value is None:
                return
            if not isinstance(value, dict) or set(value) != {
                "content",
                "sha256",
                "mode",
            }:
                raise ValueError("launcher recovery saved-file schema is invalid")
            if type(value.get("mode")) is not int or not 0 <= value["mode"] <= 0o7777:
                raise ValueError("launcher recovery saved-file mode is invalid")
            try:
                data = base64.b64decode(value.get("content"), validate=True)
            except (TypeError, ValueError) as error:
                raise ValueError("launcher recovery saved-file content is invalid") from error
            if not valid_digest(value.get("sha256")) or _digest(data) != value["sha256"]:
                raise ValueError("launcher recovery saved-file digest is invalid")

        expected_fields = set(LauncherState.__dataclass_fields__)
        if not isinstance(state, dict) or set(state) != expected_fields:
            raise ValueError("launcher recovery state schema is invalid")
        if type(state.get("schema")) is not int or state.get("schema") != 1:
            raise ValueError("launcher recovery state schema is unsupported")
        bottle_root = installation.bottle.root.resolve()
        app = self._app_path()
        exact_paths = {
            "app": app,
            "runtime": bottle_root / RUNTIME_NAME,
            "config": bottle_root / CONFIG_NAME,
            "lock_path": bottle_root / ".ostriv-launcher.lock",
            "recovery_marker": bottle_root / ".ostriv-profile-recovery.json",
        }
        for key, expected in exact_paths.items():
            if state.get(key) != str(expected):
                raise ValueError("launcher recovery {} path is not exact".format(key))
        previous = state.get("previous_app")
        expected_previous = app.with_name(
            "." + app.name + ".ostriv-macos.previous"
        )
        if previous is not None and previous != str(expected_previous):
            raise ValueError("launcher recovery previous-app path is not exact")
        if state.get("bottle_realpath") != str(bottle_root):
            raise ValueError("launcher recovery bottle identity is invalid")
        expected_command = "exec /usr/bin/env python3 {} {}".format(
            shlex.quote(str(exact_paths["runtime"])),
            shlex.quote(str(exact_paths["config"])),
        )
        expected_tag = "CrossOver-{}/".format(self._bottle_id(installation))
        if (
            state.get("menu_entry")
            != {"name": LAUNCHER_MENU, "installed": True}
            or state.get("plist_fields") != list(PLIST_FIELDS)
            or state.get("command") != expected_command
            or state.get("bottle_name") != installation.bottle.name
            or state.get("bottle_argument") != self._command_bottle(installation)
            or state.get("scope") != installation.bottle.scope
            or state.get("bottle_tag") != expected_tag
        ):
            raise ValueError("launcher recovery identity fields are invalid")
        digest_fields = (
            "runtime_sha256",
            "config_sha256",
            "executable_sha256",
            "plist_sha256",
            "icon_sha256",
            "lock_sha256",
        )
        if any(not valid_digest(state.get(name)) for name in digest_fields):
            raise ValueError("launcher recovery digest fields are invalid")
        validate_inventory(state.get("app_inventory"), optional=False)
        validate_inventory(state.get("previous_app_inventory"), optional=True)
        if previous is None and state.get("previous_app_inventory"):
            raise ValueError("launcher recovery previous-app inventory is unowned")
        validate_saved_file(state.get("previous_runtime"))
        validate_saved_file(state.get("previous_config"))
        owner_token = state.get("profile_owner_token")
        lock_digest = state.get("lock_sha256")
        if (
            not self._valid_owner_token(owner_token)
            or not isinstance(lock_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", lock_digest) is None
            or _digest(self._lock_data(str(owner_token))) != lock_digest
        ):
            raise ValueError("launcher recovery lock identity is invalid")
        artifacts = state.get("artifacts")
        expected_artifact_paths = [
            str(app),
            str(app / "Contents/Info.plist"),
            str(app / "Contents/MacOS/Menu Helper"),
            str(app / "Contents/Resources/CrossOverHelper.icns"),
            str(exact_paths["runtime"]),
            str(exact_paths["config"]),
            str(exact_paths["lock_path"]),
            str(exact_paths["recovery_marker"]),
        ]
        if (
            not isinstance(artifacts, list)
            or [item.get("path") if isinstance(item, dict) else None for item in artifacts]
            != expected_artifact_paths
        ):
            raise ValueError("launcher recovery artifact inventory is invalid")
        expected_artifacts = [
            {"path": expected_artifact_paths[0]},
            {"path": expected_artifact_paths[1], "sha256": state["plist_sha256"]},
            {"path": expected_artifact_paths[2], "sha256": state["executable_sha256"]},
            {"path": expected_artifact_paths[3], "sha256": state["icon_sha256"]},
            {"path": expected_artifact_paths[4], "sha256": state["runtime_sha256"]},
            {"path": expected_artifact_paths[5], "sha256": state["config_sha256"]},
            {"path": expected_artifact_paths[6], "sha256": lock_digest},
            {"path": expected_artifact_paths[7], "reserved": True},
        ]
        if artifacts != expected_artifacts:
            raise ValueError("launcher recovery artifact metadata is invalid")
        app_digests = {
            item["relative_path"]: item.get("sha256")
            for item in state["app_inventory"]
            if item.get("type") == "file"
        }
        if (
            app_digests.get("Contents/Info.plist") != state["plist_sha256"]
            or app_digests.get("Contents/MacOS/Menu Helper")
            != state["executable_sha256"]
            or app_digests.get("Contents/Resources/CrossOverHelper.icns")
            != state["icon_sha256"]
        ):
            raise ValueError("launcher recovery app inventory is inconsistent")
        self._validate_state_paths(
            installation,
            state,
            "restore.launcher_recovery",
            "Restore failed.",
        )

    def preflight_recovery_artifacts(
        self,
        installation: GameInstallation,
        state: Mapping[str, object],
        *,
        allow_missing_lock: bool,
    ) -> None:
        """Authenticate reserved recovery leaves without creating or removing them."""
        lock, marker = self._recovery_paths(installation)
        owner_token = state.get("profile_owner_token")
        if not self._valid_owner_token(owner_token):
            raise ValueError("launcher recovery ownership token is invalid")
        expected_lock = self._lock_data(str(owner_token))
        if _lexists(lock):
            metadata = lock.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or lock.read_bytes() != expected_lock
                or _file_digest(lock) != state.get("lock_sha256")
            ):
                raise ValueError("launcher recovery lock is invalid or unowned")
        elif not allow_missing_lock:
            raise ValueError("launcher recovery lock is unexpectedly absent")
        if _lexists(marker):
            metadata = marker.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or not self._valid_marker(marker, str(owner_token))
            ):
                raise ValueError("profile recovery marker is invalid or unowned")
            try:
                marker_data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError) as error:
                raise ValueError("profile recovery marker is unreadable") from error
            if set(marker_data) != {"owner", "original"}:
                raise ValueError("profile recovery marker schema is invalid")

    def prepare_restore(
        self,
        installation: GameInstallation,
        state: Mapping[str, object],
        *,
        recover_profile: bool = True,
        existing_lock=None,
    ):
        """Hold the launcher lock and optionally recover an owned profile marker."""
        lock = existing_lock
        try:
            self._validate_state_paths(
                installation,
                state,
                "restore.launcher_recovery",
                "Restore failed.",
            )
            owner_token = state.get("profile_owner_token")
            lock_path, marker = self._recovery_paths(installation)
            if not self._valid_owner_token(owner_token):
                raise ValueError("launcher profile ownership token is invalid")
            expected_lock = self._lock_data(str(owner_token))

            def validate_owned_leaves() -> None:
                if (
                    lock_path.is_symlink()
                    or not lock_path.is_file()
                    or lock_path.read_bytes() != expected_lock
                    or _file_digest(lock_path) != state.get("lock_sha256")
                ):
                    raise ValueError("launcher lock is invalid or unowned")
                if _lexists(marker) and not self._valid_marker(
                    marker, str(owner_token)
                ):
                    raise ValueError("profile recovery marker is invalid or unowned")

            validate_owned_leaves()
            if lock is None:
                if self.lock_factory is None:
                    from .launcher_runtime import ProcessLock

                    lock = ProcessLock(lock_path)
                else:
                    lock = self.lock_factory(lock_path)
                if not lock.acquire():
                    raise ValueError("launcher is active for the selected bottle")
            validate_owned_leaves()
            validate_current_path = getattr(lock, "validate_current_path", None)
            if callable(validate_current_path):
                validate_current_path(expected_lock, 0o600)
            if recover_profile and _lexists(marker):
                from .launcher_runtime import (
                    ColorSyncProfileBackend,
                    ProfileGuard,
                    SRGB_PROFILE,
                )

                backend = (
                    self.profile_backend_factory()
                    if self.profile_backend_factory is not None
                    else ColorSyncProfileBackend()
                )
                ProfileGuard(
                    backend,
                    marker,
                    SRGB_PROFILE,
                    str(owner_token),
                ).recover()
            return lock
        except PatchError:
            if lock is not None:
                lock.close()
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            if lock is not None:
                lock.close()
            raise PatchError(
                "restore.launcher_recovery",
                "Restore failed.",
                "Launcher recovery coordination failed: {}: {}".format(
                    type(error).__name__, error
                ),
            ) from error

    def finalize_restore(
        self,
        installation: GameInstallation,
        state: Mapping[str, object],
        *,
        existing_lock=None,
        expected_identity=None,
    ) -> None:
        """Remove only the still-owned recovery lock after Restore verification."""
        lock, marker = self._recovery_paths(installation)
        owner_token = state.get("profile_owner_token")
        if not self._valid_owner_token(owner_token):
            raise PatchError(
                "restore.launcher_recovery",
                "Restore failed.",
                "Launcher recovery ownership token is invalid",
            )
        if _lexists(marker):
            raise PatchError(
                "restore.launcher_recovery",
                "Restore failed.",
                "A profile recovery marker appeared during Restore",
            )
        if (
            lock.is_symlink()
            or not lock.is_file()
            or lock.read_bytes() != self._lock_data(str(owner_token))
            or _file_digest(lock) != state.get("lock_sha256")
        ):
            raise PatchError(
                "restore.launcher_recovery",
                "Restore failed.",
                "Launcher lock changed during Restore",
            )
        expected_data = self._lock_data(str(owner_token))
        validate_current_path = getattr(existing_lock, "validate_current_path", None)
        if not callable(validate_current_path):
            raise PatchError(
                "restore.launcher_recovery",
                "Restore failed.",
                "Launcher recovery lock lease is unavailable at final unlink",
            )
        try:
            validate_current_path(expected_data, 0o600)
            descriptor = getattr(existing_lock, "fd", None)
            if type(descriptor) is not int:
                raise OSError("launcher lock descriptor is unavailable")
            opened = os.fstat(descriptor)
            current = os.lstat(str(lock))
            if (
                type(expected_identity) is not tuple
                or len(expected_identity) != 2
                or (opened.st_dev, opened.st_ino) != expected_identity
                or (current.st_dev, current.st_ino) != expected_identity
                or opened.st_nlink != 1
                or current.st_nlink != 1
            ):
                raise OSError("launcher lock identity changed before final unlink")
        except (OSError, TypeError, ValueError) as error:
            raise PatchError(
                "restore.launcher_recovery",
                "Restore failed.",
                "Launcher lock changed before final unlink: {}".format(error),
            ) from error
        lock.unlink()
        _sync_directory(lock.parent)

    def _app_path(self) -> Path:
        if self.launcher_destination.suffix == ".app":
            return (
                self.launcher_destination.parent.resolve(strict=False)
                / self.launcher_destination.name
            )
        return self.launcher_destination.resolve(strict=False) / (LAUNCHER_NAME + ".app")

    @staticmethod
    def _recovery_paths(installation: GameInstallation) -> tuple[Path, Path]:
        root = installation.bottle.root.resolve()
        return root / ".ostriv-launcher.lock", root / ".ostriv-profile-recovery.json"

    @staticmethod
    def _lock_data(owner_token: str) -> bytes:
        return (owner_token + "\n").encode("ascii")

    @staticmethod
    def _valid_owner_token(value: object) -> bool:
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None

    @classmethod
    def _valid_marker(cls, path: Path, owner_token: str) -> bool:
        safe = _regular_file_no_follow(path.parent, Path(path.name))
        if safe is None:
            return False
        try:
            data = json.loads(safe.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return False
        return (
            isinstance(data, dict)
            and data.get("owner") == owner_token
            and "original" in data
            and (data["original"] is None or isinstance(data["original"], str))
        )

    def preflight(self, installation: GameInstallation) -> None:
        """Reject reserved launcher leaves that could redirect later mutations."""
        app = self._app_path()
        bottle_root = installation.bottle.root.resolve()
        runtime = bottle_root / RUNTIME_NAME
        config = bottle_root / CONFIG_NAME
        runtime_pending = runtime.with_name("." + runtime.name + ".pending")
        config_pending = config.with_name("." + config.name + ".pending")
        lock, marker = self._recovery_paths(installation)
        reserved = (
            app,
            app.with_name(app.name + ".pending"),
            app.with_name("." + app.name + ".ostriv-macos.previous"),
            app.with_name("." + app.name + ".ostriv-macos.replaced"),
            runtime,
            config,
            runtime_pending,
            config_pending,
            lock,
            marker,
        )
        for path in reserved:
            try:
                mode = path.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError as error:
                raise PatchError(
                    "install.launcher_ownership",
                    "Installation failed.",
                    "Unable to inspect reserved launcher path {}: {}".format(path, error),
                ) from error
            if stat.S_ISLNK(mode):
                raise PatchError(
                    "install.launcher_ownership",
                    "Installation failed.",
                    "Reserved launcher path is a symbolic link: {}".format(path),
                )
        for path in (
            app.with_name(app.name + ".pending"),
            runtime_pending,
            config_pending,
        ):
            if _lexists(path):
                raise PatchError(
                    "install.launcher_ownership",
                    "Installation failed.",
                    "Pending launcher path already exists: {}".format(path),
                )
        prior = self._prior_launcher_state(installation)
        if prior is None:
            conflicts = [path for path in (lock, marker) if _lexists(path)]
            if conflicts:
                raise PatchError(
                    "install.launcher_ownership",
                    "Installation failed.",
                    "Unowned launcher recovery path exists: {}".format(conflicts[0]),
                )
            return
        owner_token = prior.get("profile_owner_token")
        if (
            not self._valid_owner_token(owner_token)
            or prior.get("lock_path") != str(lock)
            or prior.get("recovery_marker") != str(marker)
        ):
            raise PatchError(
                "install.launcher_ownership",
                "Installation failed.",
                "Existing launcher recovery ownership is invalid",
            )
        expected_lock = self._lock_data(str(owner_token))
        if (
            not lock.is_file()
            or lock.is_symlink()
            or lock.read_bytes() != expected_lock
        ):
            raise PatchError(
                "install.launcher_ownership",
                "Installation failed.",
                "Owned launcher lock is missing or changed: {}".format(lock),
            )
        if _lexists(marker) and not self._valid_marker(marker, str(owner_token)):
            raise PatchError(
                "install.launcher_ownership",
                "Installation failed.",
                "Launcher recovery marker is invalid or unowned: {}".format(marker),
            )

    @staticmethod
    def _prior_launcher_state(
        installation: GameInstallation,
    ) -> Optional[Mapping[str, object]]:
        state_path = installation.bottle.root.resolve() / "ostriv-macos-state.json"
        if not state_path.is_file():
            return None
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise PatchError(
                "install.launcher_ownership",
                "Installation failed.",
                "Existing launcher ownership state is unreadable: {}".format(error),
            ) from error
        launcher = data.get("launcher_artifacts") if isinstance(data, dict) else None
        if not isinstance(launcher, dict):
            raise PatchError(
                "install.launcher_ownership",
                "Installation failed.",
                "Existing launcher ownership state is invalid",
            )
        return launcher

    @staticmethod
    def _template(installation: GameInstallation) -> Path:
        return installation.bottle.crossover.app / "Contents/Resources/Menu Helper.cpbz2"

    @staticmethod
    def _cxmenu(installation: GameInstallation) -> Path:
        return installation.bottle.crossover.shared_support / "bin/cxmenu"

    @staticmethod
    def _wine(installation: GameInstallation) -> Path:
        return installation.bottle.crossover.shared_support / "bin/wine"

    @staticmethod
    def _command_bottle(installation: GameInstallation) -> str:
        bottle = installation.bottle
        return bottle.name if bottle.scope == "managed" else str(bottle.root.resolve())

    @staticmethod
    def _bottle_id(installation: GameInstallation) -> str:
        configuration = installation.bottle.root.resolve() / "cxbottle.conf"
        try:
            text = configuration.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise PatchError(
                "install.launcher_bottle",
                "Installation failed.",
                "Unable to read bottle identity {}: {}".format(configuration, error),
            ) from error
        match = re.search(r'^\s*"BottleID"\s*=\s*"([^"]+)"\s*$', text, re.MULTILINE)
        if match is None:
            raise PatchError(
                "install.launcher_bottle",
                "Installation failed.",
                "BottleID is missing from {}".format(configuration),
            )
        return match.group(1)

    @staticmethod
    def _bundle_id(bottle_name: str) -> str:
        return "com.codeweavers.CrossOverHelper.{}.{}".format(
            hashlib.md5(bottle_name.encode("utf-8")).hexdigest().upper(),
            hashlib.md5(LAUNCHER_NAME.encode("utf-8")).hexdigest().upper(),
        )

    def _find_game_icon(self, installation: GameInstallation, app: Path) -> Optional[Path]:
        root = self._app_path().parent
        if not root.is_dir():
            return None
        folders = [root]
        try:
            folders.extend(
                path for path in root.iterdir() if path.is_dir() and path.suffix != ".app"
            )
        except OSError:
            return None
        for folder in folders:
            try:
                entries = sorted(folder.iterdir())
            except OSError:
                continue
            for candidate in entries:
                if candidate == app or not candidate.is_dir() or candidate.suffix != ".app":
                    continue
                try:
                    with (candidate / "Contents/Info.plist").open("rb") as stream:
                        properties = plistlib.load(stream)
                except (OSError, ValueError, plistlib.InvalidFileException):
                    continue
                command = properties.get("CrossOverHelperCommand", "")
                if (
                    properties.get("CXHelperAppBottleName") != installation.bottle.name
                    or not isinstance(command, str)
                    or not command.rstrip('"').lower().endswith(("/ostriv.lnk", "/ostriv.url"))
                ):
                    continue
                icon = candidate / "Contents/Resources/CrossOverHelper.icns"
                if icon.is_file():
                    return icon
        return None

    @staticmethod
    def _windows_game(installation: GameInstallation) -> str:
        drive_c = installation.bottle.root.resolve() / "drive_c"
        executable = installation.game_dir.resolve() / "ostriv.exe"
        try:
            relative = executable.relative_to(drive_c)
        except ValueError as error:
            raise PatchError(
                "install.launcher_game",
                "Installation failed.",
                "Game executable is outside the selected bottle: {}".format(executable),
            ) from error
        return "C:/" + relative.as_posix()

    def _config_data(
        self, installation: GameInstallation, profile_owner_token: str
    ) -> Dict[str, object]:
        bottle = installation.bottle
        bottle_root = bottle.root.resolve()
        wine = str(self._wine(installation))
        bottle_argument = self._command_bottle(installation)
        game_command = [wine, "--bottle", bottle_argument]
        game_command.extend(bottle.scope_args())
        game_command.extend(
            ["--check", "--wait-children", "--start", self._windows_game(installation)]
        )
        steam_candidates = (
            (
                bottle_root
                / "drive_c/users/crossover/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Steam/Steam.lnk",
                "C:/users/crossover/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Steam/Steam.lnk",
            ),
            (
                bottle_root
                / "drive_c/ProgramData/Microsoft/Windows/Start Menu/Programs/Steam/Steam.lnk",
                "C:/ProgramData/Microsoft/Windows/Start Menu/Programs/Steam/Steam.lnk",
            ),
        )
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", bottle.name).strip("-") or "bottle"
        identity = hashlib.sha256(str(bottle_root).encode("utf-8")).hexdigest()[:12]
        log_root = Path.home() / "Library/Logs/ostriv-macos"
        return {
            "schema": 1,
            "bottle_name": bottle.name,
            "bottle_argument": bottle_argument,
            "scope": bottle.scope,
            "bottle_realpath": str(bottle_root),
            "bottle_tag": "CrossOver-{}/".format(self._bottle_id(installation)),
            "wine": wine,
            "game_command": game_command,
            "steam_apps_root": str(self._app_path().parent),
            "steam_links": [windows for path, windows in steam_candidates if path.is_file()],
            "game_log": str(
                bottle_root / "drive_c/users/crossover/Saved Games/Ostriv/log.txt"
            ),
            "launcher_log": str(log_root / (safe_name + "-" + identity + ".log")),
            "lock_path": str(bottle_root / ".ostriv-launcher.lock"),
            "recovery_marker": str(bottle_root / ".ostriv-profile-recovery.json"),
            "profile_owner_token": profile_owner_token,
            "messages": {
                "already_running": "Ostriv is already starting or running.",
                "steam_wait": "Waiting for Steam to finish starting.",
                "steam_login": "Sign in to Steam, then open Ostriv (patched) again.",
                "steam_timeout": "Steam did not finish starting. Quit CrossOver and try again.",
                "game_failed": "Ostriv could not start. Quit and reopen CrossOver, then try again.",
                "error": "Unable to start Ostriv. See the launcher log for details.",
            },
        }

    @staticmethod
    def _journal_file(
        transaction: Transaction,
        name: str,
        path: Path,
        data: bytes,
        mode: int = 0o644,
    ) -> None:
        desired = _digest(data)
        transaction.step(
            name,
            UndoRecord("restore_launcher", {"snapshots": [_snapshot(path, desired)]}),
            lambda: _atomic_write(path, data, mode),
        )

    @staticmethod
    def _promote_file(
        transaction: Transaction,
        name: str,
        pending: Path,
        destination: Path,
        desired_digest: str,
    ) -> None:
        def promote() -> None:
            os.replace(pending, destination)
            _sync_directory(destination.parent)

        transaction.step(
            name,
            UndoRecord(
                "restore_launcher",
                {
                    "snapshots": [
                        _snapshot(destination, desired_digest),
                        _snapshot(pending),
                    ]
                },
            ),
            promote,
        )

    def _build_pending(
        self,
        transaction: Transaction,
        installation: GameInstallation,
        pending: Path,
        expected_fields: Mapping[str, str],
        icon_source: Path,
    ) -> None:
        if pending.exists():
            raise PatchError(
                "install.launcher_ownership",
                "Installation failed.",
                "Pending launcher path already exists: {}".format(pending),
            )

        def extract() -> None:
            pending.mkdir(mode=0o700)
            try:
                self.extractor(self._template(installation), pending)
                snapshots = [
                    {
                        "path": str(path.resolve(strict=False)),
                        "present": False,
                        "remove_sha256": _file_digest(path),
                    }
                    for path in _tree_files(pending)
                ]
                transaction.checkpoint_undo(
                    UndoRecord(
                        "restore_launcher",
                        {
                            "snapshots": snapshots,
                            "owned_root": str(pending),
                            "owned_directories": [str(pending)],
                            "remove_owned_tree": True,
                        },
                    )
                )
            except BaseException:
                shutil.rmtree(pending, ignore_errors=True)
                raise

        transaction.step(
            "extract launcher template",
            UndoRecord(
                "restore_launcher",
                {
                    "snapshots": [],
                    "owned_root": str(pending),
                    "owned_directories": [str(pending)],
                    "remove_owned_tree": True,
                },
            ),
            extract,
        )
        plist_path = pending / "Contents/Info.plist"
        try:
            properties = plistlib.loads(plist_path.read_bytes())
            properties.update(expected_fields)
        except (OSError, ValueError, plistlib.InvalidFileException) as error:
            raise PatchError(
                "install.launcher_materialize",
                "Installation failed.",
                "Menu Helper template has no valid Info.plist: {}".format(error),
            ) from error
        self._journal_file(
            transaction,
            "write launcher identity",
            plist_path,
            plistlib.dumps(properties),
        )
        icon = pending / "Contents/Resources/CrossOverHelper.icns"
        self._journal_file(
            transaction,
            "copy launcher icon",
            icon,
            icon_source.read_bytes(),
            _mode(icon_source),
        )

    @staticmethod
    def _expected_artifacts(
        app: Path,
        runtime: Path,
        config: Path,
        lock: Path,
        marker: Path,
    ) -> List[Dict[str, object]]:
        paths = (
            app,
            app / "Contents/Info.plist",
            app / "Contents/MacOS/Menu Helper",
            app / "Contents/Resources/CrossOverHelper.icns",
            runtime,
            config,
            lock,
        )
        artifacts = [
            {"path": str(path), **({"sha256": _file_digest(path)} if path.is_file() else {})}
            for path in paths
        ]
        artifacts.append({"path": str(marker), "reserved": True})
        return artifacts

    def _verify_materialized(
        self,
        installation: GameInstallation,
        app: Path,
        runtime: Path,
        config: Path,
        runtime_sha256: str,
        config_sha256: str,
        fields: Mapping[str, str],
        icon_sha256: str,
    ) -> None:
        failures = []
        executable = app / "Contents/MacOS/Menu Helper"
        if not executable.is_file() or not os.access(executable, os.X_OK):
            failures.append("Menu Helper executable is missing or not executable")
        try:
            properties = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException) as error:
            properties = {}
            failures.append("Info.plist is unreadable: {}".format(error))
        for key, value in fields.items():
            if properties.get(key) != value:
                failures.append("{} does not match".format(key))
        if not runtime.is_file() or _file_digest(runtime) != runtime_sha256:
            failures.append("launcher runtime digest does not match")
        if not config.is_file() or _file_digest(config) != config_sha256:
            failures.append("launcher config digest does not match")
        else:
            try:
                loaded = LauncherConfig.load(config)
                if (
                    loaded.bottle_name != installation.bottle.name
                    or loaded.bottle_argument != self._command_bottle(installation)
                    or loaded.scope != installation.bottle.scope
                ):
                    failures.append("launcher config bottle identity does not match")
            except (OSError, RuntimeError, ValueError) as error:
                failures.append("launcher config is invalid: {}".format(error))
        icon = app / "Contents/Resources/CrossOverHelper.icns"
        if not icon.is_file() or _file_digest(icon) != icon_sha256:
            failures.append("launcher icon digest does not match")
        if failures:
            raise PatchError(
                "install.launcher_verify",
                "Installation failed.",
                "\n".join(failures),
            )

    def _canonical_plist(
        self,
        installation: GameInstallation,
        identity: Mapping[str, str],
    ) -> Dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="ostriv-launcher-plist-") as temporary:
            destination = Path(temporary) / "template"
            destination.mkdir()
            self.extractor(self._template(installation), destination)
            properties = plistlib.loads(
                (destination / "Contents/Info.plist").read_bytes()
            )
        if not isinstance(properties, dict):
            raise ValueError("Menu Helper template plist is not a dictionary")
        properties.update(identity)
        return properties

    @staticmethod
    def _swap_snapshots(app: Path, pending: Path, previous: Optional[Path]) -> List[Dict[str, object]]:
        pending_by_relative = {
            path.relative_to(pending): _file_digest(path) for path in _tree_files(pending)
        }
        app_by_relative = {
            path.relative_to(app): _file_digest(path) for path in _tree_files(app)
        }
        snapshots = []
        for path in _tree_files(app):
            relative = path.relative_to(app)
            snapshots.append(_snapshot(path, pending_by_relative.get(relative)))
        for relative, pending_digest in pending_by_relative.items():
            if relative not in app_by_relative:
                snapshots.append(_snapshot(app / relative, pending_digest))
        for path in _tree_files(pending):
            snapshots.append(_snapshot(path))
        if previous is not None:
            for path in _tree_files(app):
                snapshots.append(
                    _snapshot(previous / path.relative_to(app), _file_digest(path))
                )
        return snapshots

    @staticmethod
    def _remove_verified_tree(
        transaction: Transaction,
        root: Path,
        expected_inventory: object,
    ) -> None:
        if not isinstance(expected_inventory, list) or _inventory(root) != expected_inventory:
            raise PatchError(
                "install.launcher_ownership",
                "Installation failed.",
                "Existing installed launcher changed: {}".format(root),
            )
        inventory = list(expected_inventory)
        captured = _captured_tree(root)

        def remove() -> None:
            for item in reversed(inventory):
                relative = Path(str(item.get("relative_path", "")))
                if relative == Path("."):
                    continue
                path = root / relative
                if item.get("type") in ("file", "symlink"):
                    path.unlink()
            for directory in sorted(
                _tree_directories(root), key=lambda path: len(path.parts), reverse=True
            ):
                directory.rmdir()

        transaction.step(
            "remove replaced launcher app",
            UndoRecord(
                "restore_launcher",
                {"snapshots": [], "restore_trees": [captured]},
            ),
            remove,
        )

    def install(
        self, transaction: Transaction, installation: GameInstallation
    ) -> LauncherState:
        self.preflight(installation)
        self._bind_owned_directory_cleanup(transaction, installation)
        try:
            template = self._template(installation)
            if not template.is_file():
                raise PatchError(
                    "install.launcher_template",
                    "Installation failed.",
                    "Menu Helper.cpbz2 is missing: {}".format(template),
                )
            if not self.runtime_source.is_file():
                raise PatchError(
                    "install.launcher_runtime",
                    "Installation failed.",
                    "Launcher runtime is missing: {}".format(self.runtime_source),
                )
            cxmenu = self._cxmenu(installation)
            if not os.access(cxmenu, os.X_OK):
                raise PatchError(
                    "install.launcher_menu",
                    "Installation failed.",
                    "cxmenu is missing or not executable: {}".format(cxmenu),
                )
            app = self._app_path()
            app.parent.mkdir(parents=True, exist_ok=True)
            pending = app.with_name(app.name + ".pending")
            stable_previous = app.with_name("." + app.name + ".ostriv-macos.previous")
            prior = self._prior_launcher_state(installation)
            if prior is not None:
                self.verify(installation, prior)
                app_before_inventory = _inventory(app)
                previous_text = prior.get("previous_app")
                previous = Path(previous_text) if isinstance(previous_text, str) else None
                previous_inventory = list(prior.get("previous_app_inventory", []))
                previous_runtime = prior.get("previous_runtime")
                previous_config = prior.get("previous_config")
                app_backup = app.with_name("." + app.name + ".ostriv-macos.replaced")
            else:
                previous = stable_previous if app.exists() else None
                previous_inventory = _inventory(app) if previous is not None else []
                app_before_inventory = list(previous_inventory)
                previous_runtime = _saved_file(
                    installation.bottle.root.resolve() / RUNTIME_NAME
                )
                previous_config = _saved_file(
                    installation.bottle.root.resolve() / CONFIG_NAME
                )
                app_backup = previous
            profile_owner_token = (
                str(prior["profile_owner_token"])
                if prior is not None
                else secrets.token_hex(32)
            )
            lock, recovery_marker = self._recovery_paths(installation)
            if app_backup is not None and app_backup.exists():
                raise PatchError(
                    "install.launcher_ownership",
                    "Installation failed.",
                    "Launcher backup already exists: {}".format(app_backup),
                )
            runtime = installation.bottle.root.resolve() / RUNTIME_NAME
            config = installation.bottle.root.resolve() / CONFIG_NAME
            runtime_pending = runtime.with_name("." + runtime.name + ".pending")
            config_pending = config.with_name("." + config.name + ".pending")
            for path in (runtime_pending, config_pending):
                if _lexists(path):
                    raise PatchError(
                        "install.launcher_ownership",
                        "Installation failed.",
                        "Pending launcher path already exists: {}".format(path),
                    )

            runtime_data = self.runtime_source.read_bytes()
            config_data = self._config_data(installation, profile_owner_token)
            config_bytes = (
                json.dumps(config_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            runtime_sha256 = _digest(runtime_data)
            config_sha256 = _digest(config_bytes)
            command = "exec /usr/bin/env python3 {} {}".format(
                shlex.quote(str(runtime)), shlex.quote(str(config))
            )
            bottle_tag = "CrossOver-{}/".format(self._bottle_id(installation))
            fields = {
                "CFBundleName": LAUNCHER_NAME,
                "CFBundleDisplayName": LAUNCHER_NAME,
                "CFBundleIdentifier": self._bundle_id(installation.bottle.name),
                "CrossOverHelperCommand": command,
                "CXHelperAppBottleName": installation.bottle.name,
                "CXHelperAppBottleTag": bottle_tag,
                "CFBundleExecutable": "Menu Helper",
                "CFBundleIconFile": "CrossOverHelper.icns",
            }
            icon_source = self._find_game_icon(installation, app)
            if icon_source is None:
                raise PatchError(
                    "install.launcher_icon",
                    "Installation failed.",
                    "No verified Ostriv CrossOver icon was found for {}".format(
                        installation.bottle.name
                    ),
                )

            self._journal_file(
                transaction,
                "reserve launcher lock",
                lock,
                self._lock_data(profile_owner_token),
                0o600,
            )

            self._journal_file(
                transaction, "stage launcher runtime", runtime_pending, runtime_data, 0o755
            )
            self._journal_file(
                transaction, "stage launcher config", config_pending, config_bytes
            )
            self._build_pending(transaction, installation, pending, fields, icon_source)
            icon_sha256 = _file_digest(icon_source)
            self._verify_materialized(
                installation,
                pending,
                runtime_pending,
                config_pending,
                runtime_sha256,
                config_sha256,
                fields,
                icon_sha256,
            )

            swap_undo = UndoRecord(
                "restore_launcher",
                {
                    "snapshots": self._swap_snapshots(app, pending, app_backup),
                    "owned_root": str(app if app_backup is None else app_backup),
                    "owned_directories": [
                        str(app if app_backup is None else app_backup)
                    ],
                    "remove_owned_tree": app_backup is None,
                    **(
                        {
                            "moved_tree": {
                                "source": str(app_backup),
                                "destination": str(app),
                                "source_inventory": app_before_inventory,
                                "replacement_inventory": _inventory(pending),
                            }
                        }
                        if app_backup is not None
                        else {}
                    ),
                },
            )

            def swap_app() -> None:
                if app_backup is not None:
                    os.replace(app, app_backup)
                    _sync_directory(app.parent)
                os.replace(pending, app)
                _sync_directory(app.parent)

            transaction.step("replace launcher app", swap_undo, swap_app)
            self._verify_materialized(
                installation,
                app,
                runtime_pending,
                config_pending,
                runtime_sha256,
                config_sha256,
                fields,
                icon_sha256,
            )

            self._promote_file(
                transaction,
                "install launcher runtime",
                runtime_pending,
                runtime,
                runtime_sha256,
            )
            self._promote_file(
                transaction,
                "install launcher config",
                config_pending,
                config,
                config_sha256,
            )
            self._verify_materialized(
                installation,
                app,
                runtime,
                config,
                runtime_sha256,
                config_sha256,
                fields,
                icon_sha256,
            )
            if prior is not None:
                self._remove_verified_tree(
                    transaction,
                    app_backup,
                    prior.get("app_inventory"),
                )

            menu_command = self._menu_create_command(installation, command)

            def register_menu() -> None:
                result = self.runner.run(menu_command, timeout=60.0)
                if result.returncode != 0:
                    raise PatchError(
                        "install.launcher_menu",
                        "Installation failed.",
                        command_failure_detail(result, "cxmenu failed"),
                    )

            transaction.step(
                "register launcher menu",
                UndoRecord(
                    "restore_launcher",
                    {
                        "snapshots": [],
                        "purge_menu": True,
                        "recreate_menu": prior is not None,
                    },
                ),
                register_menu,
            )
            state = LauncherState(
                schema=1,
                artifacts=self._expected_artifacts(
                    app, runtime, config, lock, recovery_marker
                ),
                app=str(app),
                runtime=str(runtime),
                config=str(config),
                menu_entry={"name": LAUNCHER_MENU, "installed": True},
                previous_app=str(previous) if previous is not None else None,
                previous_app_inventory=previous_inventory,
                previous_runtime=previous_runtime,
                previous_config=previous_config,
                app_inventory=_inventory(app),
                runtime_sha256=runtime_sha256,
                config_sha256=config_sha256,
                executable_sha256=_file_digest(app / "Contents/MacOS/Menu Helper"),
                plist_sha256=_file_digest(app / "Contents/Info.plist"),
                icon_sha256=icon_sha256,
                plist_fields=list(PLIST_FIELDS),
                command=command,
                bottle_realpath=str(installation.bottle.root.resolve()),
                bottle_name=installation.bottle.name,
                bottle_argument=self._command_bottle(installation),
                scope=installation.bottle.scope,
                bottle_tag=bottle_tag,
                lock_path=str(lock),
                lock_sha256=_file_digest(lock),
                recovery_marker=str(recovery_marker),
                profile_owner_token=profile_owner_token,
            )
            self.verify(installation, state)
            return state
        except PatchError:
            transaction.rollback()
            raise
        except (OSError, ValueError, plistlib.InvalidFileException) as error:
            transaction.rollback()
            raise PatchError(
                "install.launcher_materialize",
                "Installation failed.",
                "Launcher materialization failed: {}: {}".format(
                    type(error).__name__, error
                ),
            ) from error

    def _validate_state_paths(
        self,
        installation: GameInstallation,
        state: Mapping[str, object],
        code: str,
        player_message: str,
    ) -> None:
        app = self._app_path()
        bottle_root = installation.bottle.root.resolve()
        expected = {
            "app": app,
            "runtime": bottle_root / RUNTIME_NAME,
            "config": bottle_root / CONFIG_NAME,
            "lock_path": bottle_root / ".ostriv-launcher.lock",
            "recovery_marker": bottle_root / ".ostriv-profile-recovery.json",
        }
        failures = []
        for key, path in expected.items():
            value = state.get(key)
            if not isinstance(value, str) or Path(value).resolve(strict=False) != path:
                failures.append("{} path is outside the launcher inventory".format(key))
        previous = state.get("previous_app")
        expected_previous = app.with_name("." + app.name + ".ostriv-macos.previous")
        if previous is not None and (
            not isinstance(previous, str)
            or Path(previous).resolve(strict=False) != expected_previous
        ):
            failures.append("previous app path is outside the launcher inventory")
        allowed_artifacts = {
            app,
            app / "Contents/Info.plist",
            app / "Contents/MacOS/Menu Helper",
            app / "Contents/Resources/CrossOverHelper.icns",
            bottle_root / RUNTIME_NAME,
            bottle_root / CONFIG_NAME,
            bottle_root / ".ostriv-launcher.lock",
            bottle_root / ".ostriv-profile-recovery.json",
        }
        artifacts = state.get("artifacts")
        if not isinstance(artifacts, list):
            failures.append("launcher artifact inventory is invalid")
        else:
            for item in artifacts:
                path_text = item.get("path") if isinstance(item, dict) else None
                if (
                    not isinstance(path_text, str)
                    or Path(path_text).resolve(strict=False) not in allowed_artifacts
                ):
                    failures.append("launcher artifact path is outside the inventory")
                    break
        if failures:
            raise PatchError(code, player_message, "\n".join(failures))

    def _validate_legacy_paths(
        self,
        installation: GameInstallation,
        state: Mapping[str, object],
    ) -> None:
        allowed = {
            self._app_path(),
            installation.bottle.root.resolve() / RUNTIME_NAME,
        }
        artifacts = state.get("artifacts")
        if not isinstance(artifacts, list):
            raise PatchError(
                "restore.launcher_ownership",
                "Restore failed.",
                "Legacy launcher artifact inventory is invalid",
            )
        for item in artifacts:
            path_text = item.get("path") if isinstance(item, dict) else None
            if (
                not isinstance(path_text, str)
                or Path(path_text).resolve(strict=False) not in allowed
            ):
                raise PatchError(
                    "restore.launcher_ownership",
                    "Restore failed.",
                    "Legacy launcher artifact path is outside the inventory",
                )

    def verify(
        self,
        installation: GameInstallation,
        launcher_state: Mapping[str, object],
    ) -> None:
        state = launcher_state
        try:
            self._validate_state_paths(
                installation,
                state,
                "install.launcher_verify",
                "Installation failed.",
            )
            app = Path(str(state["app"]))
            runtime = Path(str(state["runtime"]))
            config = Path(str(state["config"]))
            expected_command = "exec /usr/bin/env python3 {} {}".format(
                shlex.quote(str(runtime)), shlex.quote(str(config))
            )
            expected_tag = "CrossOver-{}/".format(self._bottle_id(installation))
            expected_identity = {
                "CFBundleName": LAUNCHER_NAME,
                "CFBundleDisplayName": LAUNCHER_NAME,
                "CFBundleIdentifier": self._bundle_id(installation.bottle.name),
                "CrossOverHelperCommand": expected_command,
                "CXHelperAppBottleName": installation.bottle.name,
                "CXHelperAppBottleTag": expected_tag,
                "CFBundleExecutable": "Menu Helper",
                "CFBundleIconFile": "CrossOverHelper.icns",
            }
            identity_failures = []
            if state.get("schema") != 1:
                identity_failures.append("launcher state schema does not match")
            if state.get("bottle_realpath") != str(installation.bottle.root.resolve()):
                identity_failures.append("launcher bottle realpath does not match")
            if state.get("bottle_name") != installation.bottle.name:
                identity_failures.append("launcher bottle name does not match")
            if state.get("bottle_argument") != self._command_bottle(installation):
                identity_failures.append("launcher bottle argument does not match")
            if state.get("scope") != installation.bottle.scope:
                identity_failures.append("launcher bottle scope does not match")
            if state.get("command") != expected_command:
                identity_failures.append("CrossOverHelperCommand does not match")
            if state.get("bottle_tag") != expected_tag:
                identity_failures.append("CXHelperAppBottleTag does not match")
            owner_token = state.get("profile_owner_token")
            if not self._valid_owner_token(owner_token):
                identity_failures.append("launcher profile ownership token is invalid")
            if state.get("plist_fields") != list(PLIST_FIELDS):
                identity_failures.append("launcher plist field inventory does not match")
            if identity_failures:
                raise PatchError(
                    "install.launcher_verify",
                    "Installation failed.",
                    "\n".join(identity_failures),
                )
            actual_plist = plistlib.loads(
                (app / "Contents/Info.plist").read_bytes()
            )
            canonical_plist = self._canonical_plist(
                installation, expected_identity
            )
            if actual_plist != canonical_plist:
                differing = sorted(
                    key
                    for key in set(actual_plist) | set(canonical_plist)
                    if actual_plist.get(key) != canonical_plist.get(key)
                )
                raise PatchError(
                    "install.launcher_verify",
                    "Installation failed.",
                    "Launcher plist is not canonical: {}".format(
                        ", ".join(differing)
                    ),
                )
            self._verify_materialized(
                installation,
                app,
                runtime,
                config,
                str(state["runtime_sha256"]),
                str(state["config_sha256"]),
                expected_identity,
                str(state["icon_sha256"]),
            )
            loaded_config = LauncherConfig.load(config)
            if (
                loaded_config.bottle_realpath
                != str(installation.bottle.root.resolve())
                or loaded_config.bottle_tag != expected_tag
                or loaded_config.profile_owner_token != owner_token
            ):
                raise PatchError(
                    "install.launcher_verify",
                    "Installation failed.",
                    "Launcher runtime ownership identity does not match",
                )
            lock = Path(str(state["lock_path"]))
            expected_lock = self._lock_data(str(owner_token))
            if (
                lock.is_symlink()
                or not lock.is_file()
                or lock.read_bytes() != expected_lock
                or _file_digest(lock) != state.get("lock_sha256")
            ):
                raise PatchError(
                    "install.launcher_verify",
                    "Installation failed.",
                    "Launcher lock ownership does not match",
                )
            marker = Path(str(state["recovery_marker"]))
            if _lexists(marker) and not self._valid_marker(marker, str(owner_token)):
                raise PatchError(
                    "install.launcher_verify",
                    "Installation failed.",
                    "Launcher recovery marker is invalid or unowned",
                )
            if _file_digest(app / "Contents/MacOS/Menu Helper") != state.get(
                "executable_sha256"
            ):
                raise PatchError(
                    "install.launcher_verify",
                    "Installation failed.",
                    "Menu Helper executable digest does not match",
                )
            if _file_digest(app / "Contents/Info.plist") != state.get("plist_sha256"):
                raise PatchError(
                    "install.launcher_verify",
                    "Installation failed.",
                    "Info.plist digest does not match",
                )
        except PatchError:
            raise
        except (KeyError, OSError, TypeError, ValueError) as error:
            raise PatchError(
                "install.launcher_verify",
                "Installation failed.",
                "Launcher state is invalid: {}: {}".format(type(error).__name__, error),
            ) from error

    @staticmethod
    def _restore_saved(path: Path, installed_digest: str, saved: object) -> None:
        if path.is_symlink():
            return
        if path.is_file() and _file_digest(path) != installed_digest:
            return
        if isinstance(saved, dict):
            try:
                data = base64.b64decode(str(saved["content"]), validate=True)
                expected = str(saved["sha256"])
                mode = int(saved.get("mode", 0o644))
            except (KeyError, TypeError, ValueError):
                return
            if _digest(data) == expected:
                _atomic_write(path, data, mode)
        elif path.is_file() and _file_digest(path) == installed_digest:
            path.unlink()

    @staticmethod
    def _remove_owned_app(app: Path, inventory: object) -> None:
        if not isinstance(inventory, list):
            return
        for item in reversed(inventory):
            if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str):
                continue
            relative = Path(item["relative_path"])
            if relative.is_absolute() or ".." in relative.parts:
                continue
            path = app / relative
            item_type = item.get("type", "file")
            expected = item.get("sha256")
            if (
                item_type == "file"
                and isinstance(expected, str)
                and path.is_file()
                and not path.is_symlink()
                and _file_digest(path) == expected
            ):
                path.unlink()
            elif (
                item_type == "symlink"
                and path.is_symlink()
                and os.readlink(path) == item.get("target")
            ):
                path.unlink()
        if app.is_dir() and not app.is_symlink():
            directory_entries = [
                app / Path(str(item["relative_path"]))
                for item in inventory
                if isinstance(item, dict)
                and item.get("type") == "directory"
                and item.get("relative_path") != "."
            ]
            for directory in sorted(
                directory_entries,
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                app.rmdir()
            except OSError:
                pass

    def _remove_legacy_app(
        self, installation: GameInstallation, app: Path
    ) -> None:
        """Remove only the entries whose legacy bundle identity proves ownership."""
        plist_path = _regular_file_no_follow(app, Path("Contents/Info.plist"))
        if plist_path is None:
            return
        try:
            properties = plistlib.loads(plist_path.read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            return
        runtime = installation.bottle.root.resolve() / RUNTIME_NAME
        expected = {
            "CFBundleName": LAUNCHER_NAME,
            "CFBundleDisplayName": LAUNCHER_NAME,
            "CFBundleIdentifier": self._bundle_id(installation.bottle.name),
            "CXHelperAppBottleName": installation.bottle.name,
            "CXHelperAppBottleTag": "CrossOver-{}/".format(
                self._bottle_id(installation)
            ),
        }
        legacy_commands = {
            "exec /usr/bin/env python3 {}".format(runtime),
            "exec /usr/bin/env python3 {}".format(
                installation.bottle.root / RUNTIME_NAME
            ),
        }
        if (
            any(properties.get(key) != value for key, value in expected.items())
            or properties.get("CrossOverHelperCommand") not in legacy_commands
        ):
            return
        owned_files = (
            Path("Contents/MacOS/Menu Helper"),
            Path("Contents/Resources/CrossOverHelper.icns"),
            Path("Contents/Info.plist"),
        )
        for relative in owned_files:
            path = _regular_file_no_follow(app, relative)
            if path is not None:
                path.unlink()
        for directory in (
            app / "Contents/MacOS",
            app / "Contents/Resources",
            app / "Contents",
            app,
        ):
            try:
                mode = directory.lstat().st_mode
                if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
                    directory.rmdir()
            except OSError:
                pass

    def _legacy_runtime_owned(
        self, installation: GameInstallation, path: Path
    ) -> bool:
        safe = _regular_file_no_follow(path.parent, Path(path.name))
        if safe is None:
            return False
        try:
            text = safe.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (OSError, UnicodeError, SyntaxError):
            return False
        assignments = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in ("GAME_CMD", "WINE", "BOTTLE"):
                try:
                    assignments[target.id] = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    return False
        wine = str(self._wine(installation))
        expected_game = [
            wine,
            "--bottle",
            installation.bottle.name,
            "--check",
            "--wait-children",
            "--start",
            self._windows_game(installation),
        ]
        if assignments != {
            "GAME_CMD": expected_game,
            "WINE": wine,
            "BOTTLE": installation.bottle.name,
        }:
            return False
        normalized, count = re.subn(
            r"^(GAME_CMD|WINE|BOTTLE) = .*?$",
            lambda match: match.group(1) + " = <dynamic>",
            text,
            flags=re.MULTILINE,
        )
        return (
            count == 3
            and _digest(normalized.encode("utf-8"))
            in LEGACY_RUNTIME_NORMALIZED_SHA256
        )

    @staticmethod
    def _restore_previous_app(app: Path, previous: Path, inventory: object) -> None:
        if not previous.is_dir() or previous.is_symlink() or not isinstance(inventory, list):
            return
        if _inventory(previous) != inventory:
            raise PatchError(
                "restore.launcher_ownership",
                "Restore failed.",
                "Recorded previous launcher changed: {}".format(previous),
            )
        directories = [
            item
            for item in inventory
            if isinstance(item, dict) and item.get("type") == "directory"
        ]
        for item in sorted(
            directories,
            key=lambda entry: len(Path(str(entry["relative_path"])).parts),
        ):
            relative = Path(str(item["relative_path"]))
            source_directory = previous if relative == Path(".") else previous / relative
            source_directory.chmod(int(item.get("mode", 0o755)) | 0o700)
        for item in sorted(
            directories,
            key=lambda entry: len(Path(str(entry["relative_path"])).parts),
        ):
            relative = Path(str(item["relative_path"]))
            destination = app if relative == Path(".") else app / relative
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.chmod(int(item.get("mode", 0o755)) | 0o700)
            _sync_directory(destination.parent)
        for item in inventory:
            if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str):
                continue
            relative = Path(item["relative_path"])
            if relative == Path(".") or relative.is_absolute() or ".." in relative.parts:
                continue
            source = previous / relative
            destination = app / relative
            if item.get("type") == "directory":
                continue
            if _lexists(destination):
                raise PatchError(
                    "restore.launcher_ownership",
                    "Restore failed.",
                    "Unknown file blocks previous launcher restore: {}".format(destination),
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            _sync_directory(source.parent)
            if destination.parent != source.parent:
                _sync_directory(destination.parent)
        for directory in sorted(
            _tree_directories(previous),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            previous.rmdir()
        except OSError:
            pass
        for item in sorted(
            directories,
            key=lambda entry: len(Path(str(entry["relative_path"])).parts),
            reverse=True,
        ):
            relative = Path(str(item["relative_path"]))
            destination = app if relative == Path(".") else app / relative
            descriptor = os.open(str(destination), os.O_RDONLY)
            try:
                os.fchmod(descriptor, int(item.get("mode", 0o755)))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _restore(
        self,
        installation: GameInstallation,
        launcher_state: Mapping[str, object],
    ) -> None:
        state = launcher_state
        if state.get("legacy") is True:
            self._validate_legacy_paths(installation, state)
        else:
            self._validate_state_paths(
                installation,
                state,
                "restore.launcher_ownership",
                "Restore failed.",
            )
        menu_name = LAUNCHER_MENU
        menu = state.get("menu_entry")
        should_purge = state.get("legacy") is True or (
            isinstance(menu, dict) and menu.get("name") == LAUNCHER_MENU
        )
        if should_purge:
            command = [
                str(self._cxmenu(installation)),
                "--bottle",
                self._command_bottle(installation),
            ]
            command.extend(installation.bottle.scope_args())
            command.extend(["--purge", "--filter", menu_name])
            result = self.runner.run(command, timeout=60.0)
            if result.returncode != 0:
                raise PatchError(
                    "restore.launcher_menu",
                    "Restore failed.",
                    command_failure_detail(result, "cxmenu purge failed"),
                )

        if state.get("legacy") is True:
            for item in state.get("artifacts", []):
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    continue
                path = Path(item["path"])
                if path.resolve(strict=False) == self._app_path() and path.is_dir():
                    self._remove_legacy_app(installation, path)
                elif (
                    path.resolve(strict=False)
                    == installation.bottle.root.resolve() / RUNTIME_NAME
                    and self._legacy_runtime_owned(installation, path)
                ):
                    path.unlink()
            return

        app = Path(str(state.get("app", self._app_path())))
        runtime = Path(str(state.get("runtime", installation.bottle.root.resolve() / RUNTIME_NAME)))
        config = Path(str(state.get("config", installation.bottle.root.resolve() / CONFIG_NAME)))
        self._restore_saved(runtime, str(state.get("runtime_sha256", "")), state.get("previous_runtime"))
        self._restore_saved(config, str(state.get("config_sha256", "")), state.get("previous_config"))
        self._remove_owned_app(app, state.get("app_inventory"))
        previous_text = state.get("previous_app")
        if isinstance(previous_text, str):
            self._restore_previous_app(
                app, Path(previous_text), state.get("previous_app_inventory")
            )

    def restore(
        self,
        installation: GameInstallation,
        launcher_state: Mapping[str, object],
    ) -> None:
        try:
            self._restore(installation, launcher_state)
        except PatchError:
            raise
        except (OSError, UnicodeError, ValueError, plistlib.InvalidFileException) as error:
            raise PatchError(
                "restore.launcher_filesystem",
                "Restore failed.",
                "Launcher restore failed: {}: {}".format(
                    type(error).__name__, error
                ),
            ) from error
