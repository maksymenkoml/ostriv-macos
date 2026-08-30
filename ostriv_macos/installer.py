"""Durable, recoverable installation transactions."""

import copy
import base64
import ctypes
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import struct
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from . import __version__
from .diagnostics import CommandRunner, PatchError, command_failure_detail
from .discovery import Bottle, GameInstallation, is_supported_game_directory
from .payload import PayloadEntry, validate_payload


JOURNAL_SCHEMA = 1
JOURNAL_CORRUPT_MESSAGE = "The installation journal is unreadable. Restore before trying again."
RECOVERY_REQUIRED_MESSAGE = "A previous installation needs recovery."
ROLLBACK_FAILED_MESSAGE = "Installation recovery failed. Restore before trying again."
_JOURNAL_REPLACED_ATTRIBUTE = "_ostriv_journal_replaced"
_RESTORE_LOCK_IDENTITY_MAX = (1 << 64) - 1

logger = logging.getLogger("ostriv_macos")
if not logger.handlers:
    logger.addHandler(logging.NullHandler())
    logger.propagate = False


@dataclass(frozen=True)
class UndoRecord:
    kind: str
    data: Dict[str, object]


def atomic_write_json(path: Path, data: Dict[str, object]) -> None:
    """Replace *path* only after its complete JSON contents reach disk."""
    _ensure_durable_directory(path.parent)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            delete=False,
        ) as stream:
            temp_name = stream.name
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, str(path))
        try:
            _fsync_directory(path.parent)
        except OSError as error:
            setattr(error, _JOURNAL_REPLACED_ATTRIBUTE, True)
            raise
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


class InstallJournal:
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> Dict[str, object]:
        if not self.path.exists():
            return {"schema": JOURNAL_SCHEMA, "complete": True, "records": []}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self._validate(loaded)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise PatchError(
                "install.journal_corrupt",
                JOURNAL_CORRUPT_MESSAGE,
                "Unable to load journal {}: {}: {}".format(
                    self.path, type(error).__name__, error
                ),
            ) from error
        return loaded

    @staticmethod
    def _validate(data: object) -> None:
        if not isinstance(data, dict):
            raise ValueError("journal root is not an object")
        if data.get("schema") != JOURNAL_SCHEMA:
            raise ValueError("unsupported journal schema {!r}".format(data.get("schema")))
        if not isinstance(data.get("complete"), bool):
            raise ValueError("journal complete flag is invalid")
        records = data.get("records")
        if not isinstance(records, list):
            raise ValueError("journal records are invalid")
        if "operation" in data and not isinstance(data["operation"], str):
            raise ValueError("journal operation is invalid")
        for index, item in enumerate(records):
            if not isinstance(item, dict):
                raise ValueError("journal record {} is not an object".format(index))
            if not isinstance(item.get("name"), str):
                raise ValueError("journal record {} name is invalid".format(index))
            if item.get("status") not in ("pending", "applied", "rolled_back"):
                raise ValueError("journal record {} status is invalid".format(index))
            undo = item.get("undo")
            if not isinstance(undo, dict):
                raise ValueError("journal record {} undo is invalid".format(index))
            if not isinstance(undo.get("kind"), str) or not isinstance(
                undo.get("data"), dict
            ):
                raise ValueError("journal record {} undo data is invalid".format(index))

    def _save(self, candidate: Dict[str, object]) -> None:
        try:
            atomic_write_json(self.path, candidate)
        except BaseException as error:
            if getattr(error, _JOURNAL_REPLACED_ATTRIBUTE, False):
                self.data = candidate
            raise
        else:
            self.data = candidate

    def start(self, operation: str) -> None:
        if self.data["records"] and not self.data.get("complete"):
            raise PatchError(
                "install.recovery_required",
                RECOVERY_REQUIRED_MESSAGE,
                "Cannot replace incomplete journal at {}".format(self.path),
            )
        candidate = {
            "schema": JOURNAL_SCHEMA,
            "operation": operation,
            "complete": False,
            "records": [],
        }
        self._save(candidate)

    def begin(self, name: str, undo: UndoRecord) -> int:
        candidate = copy.deepcopy(self.data)
        records = candidate["records"]
        records.append(
            {
                "name": name,
                "status": "pending",
                "undo": {"kind": undo.kind, "data": undo.data},
            }
        )
        self._save(candidate)
        return len(records) - 1

    def mark_applied(self, index: int) -> None:
        candidate = copy.deepcopy(self.data)
        candidate["records"][index]["status"] = "applied"
        self._save(candidate)

    def checkpoint_undo(self, index: int, undo: UndoRecord) -> None:
        candidate = copy.deepcopy(self.data)
        item = candidate["records"][index]
        if item["status"] != "pending":
            raise ValueError("only a pending journal record can be checkpointed")
        item["undo"] = {"kind": undo.kind, "data": undo.data}
        self._save(candidate)

    def mark_rolled_back(self, index: int) -> None:
        candidate = copy.deepcopy(self.data)
        candidate["records"][index]["status"] = "rolled_back"
        self._save(candidate)

    def commit(self) -> None:
        candidate = copy.deepcopy(self.data)
        candidate["complete"] = True
        self._save(candidate)


class Transaction:
    def __init__(
        self,
        journal: InstallJournal,
        handlers: Mapping[str, Callable[[UndoRecord], None]],
    ):
        self.journal = journal
        self.handlers = handlers
        self._active_step: Optional[int] = None
        self._recovery_plan: Optional[Dict[int, UndoRecord]] = None

    def use_recovery_plan(
        self, records: Sequence[Tuple[int, UndoRecord]]
    ) -> None:
        """Use only preflighted deep copies while replaying this journal."""
        prepared: Dict[int, UndoRecord] = {}
        for index, record in records:
            if type(index) is not int or index in prepared:
                raise ValueError("recovery plan record index is invalid")
            prepared[index] = UndoRecord(record.kind, copy.deepcopy(record.data))
        self._recovery_plan = prepared

    def start(self, operation: str) -> None:
        logger.info("journal start operation=%s path=%s", operation, self.journal.path)
        self.journal.start(operation)

    def _undo(
        self, record_data: Dict[str, object], index: Optional[int] = None
    ) -> None:
        if self._recovery_plan is not None:
            if index is None or index not in self._recovery_plan:
                raise ValueError("journal record is absent from the recovery plan")
            planned = self._recovery_plan[index]
            record = UndoRecord(planned.kind, copy.deepcopy(planned.data))
        else:
            record = UndoRecord(record_data["kind"], record_data["data"])
        self.handlers[record.kind](record)

    def _rollback_failed(
        self, index: int, item: Dict[str, object], error: BaseException
    ) -> PatchError:
        undo = item["undo"]
        detail = (
            "Undo failed for operation={!r}, record={!r}, index={}, kind={!r}: {}: {}"
        ).format(
            self.journal.data.get("operation"),
            item.get("name"),
            index,
            undo.get("kind"),
            type(error).__name__,
            error,
        )
        logger.exception("install.rollback_failed: %s", detail)
        return PatchError("install.rollback_failed", ROLLBACK_FAILED_MESSAGE, detail)

    def step(
        self,
        name: str,
        undo: UndoRecord,
        action: Callable[[], None],
    ) -> None:
        logger.info("journal step begin name=%s", name)
        index = self.journal.begin(name, undo)
        previous_step = self._active_step
        self._active_step = index
        try:
            action()
            self.journal.mark_applied(index)
            logger.info("journal step applied name=%s", name)
        except BaseException:
            logger.warning("journal step failed name=%s", name)
            item = self.journal.data["records"][index]
            try:
                self._undo(item["undo"])
            except BaseException as error:
                raise self._rollback_failed(index, item, error) from error
            self.journal.mark_rolled_back(index)
            logger.info("journal step rolled_back name=%s", name)
            raise
        finally:
            self._active_step = previous_step

    def checkpoint_undo(self, undo: UndoRecord) -> None:
        if self._active_step is None:
            raise RuntimeError("no active transaction step to checkpoint")
        self.journal.checkpoint_undo(self._active_step, undo)

    def rollback(self) -> None:
        operation = self.journal.data.get("operation")
        logger.info("journal rollback start operation=%s", operation)
        for index in range(len(self.journal.data["records"]) - 1, -1, -1):
            item = self.journal.data["records"][index]
            if item["status"] in ("pending", "applied"):
                logger.info(
                    "journal rollback record name=%s index=%s kind=%s",
                    item.get("name"),
                    index,
                    item.get("undo", {}).get("kind"),
                )
                try:
                    self._undo(item["undo"], index)
                except BaseException as error:
                    raise self._rollback_failed(index, item, error) from error
                self.journal.mark_rolled_back(index)
        self.journal.commit()
        logger.info("journal rollback complete operation=%s", operation)

    def recover_incomplete(self) -> None:
        if not self.journal.data.get("complete"):
            operation = self.journal.data.get("operation")
            logger.info("journal recovery start operation=%s", operation)
            self.rollback()
            logger.info("journal recovery complete operation=%s", operation)


BOTTLE_ENV = {
    "GALLIUM_DRIVER": "d3d12",
    "wgl_require_gdi_compat": "true",
    "MESA_D3D12_ASYNC_PRESENT": "1",
    "MESA_OSTRIV_TREE_SHADER_HACK": "1",
    "MESA_OSTRIV_FLAT_VARYING_HACK": "1",
    "MESA_GLSL_DISABLE_IO_OPT": "true",
    "MESA_GL_VERSION_OVERRIDE": "4.3",
    "MESA_GLSL_VERSION_OVERRIDE": "430",
}

DRIVER_NAMES = (
    "opengl32.dll",
    "libgallium_wgl.dll",
    "dxil.dll",
    "libwinpthread-1.dll",
)
DIAGNOSTIC_LOGS = ("mesa_ostriv_hack_log.txt", "mesa_ostriv_pso_log.txt")
REGISTRY_KEY = r"HKCU\Software\Wine\AppDefaults\ostriv.exe\DllOverrides"
REGISTRY_VALUE = "opengl32"
REGISTRY_DATA = "native"
STATE_SCHEMA = 1
STATE_NAME = "ostriv-macos-state.json"
JOURNAL_NAME = ".ostriv-macos-journal.json"


def _file_digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def _bytes_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _same_file(path: Path, expected_digest: str) -> bool:
    try:
        return path.is_file() and _file_digest(path) == expected_digest
    except OSError:
        return False


def _atomic_write_bytes(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    _ensure_durable_directory(path.parent)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=str(path.parent), delete=False) as stream:
            temp_name = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temp_name, mode)
        _durable_replace(Path(temp_name), path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_durable_directory(path: Path) -> None:
    """Create a missing directory chain and sync each new link and directory."""
    path = Path(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        if current == current.parent:
            break
        current = current.parent
    if not current.is_dir():
        raise NotADirectoryError(str(current))
    for directory in reversed(missing):
        directory.mkdir()
        _fsync_directory(directory)
        _fsync_directory(directory.parent)


def _durable_replace(source: Path, destination: Path) -> None:
    source = Path(source)
    destination = Path(destination)
    source_parent = source.parent
    destination_parent = destination.parent
    os.replace(str(source), str(destination))
    _fsync_directory(destination_parent)
    if source_parent != destination_parent:
        _fsync_directory(source_parent)


def _durable_unlink(path: Path, *, missing_ok: bool = False) -> None:
    path = Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    _fsync_directory(path.parent)


def _durable_rmdir(path: Path) -> None:
    path = Path(path)
    path.rmdir()
    _fsync_directory(path.parent)


_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 1
_AT_FDCWD = -100
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAME_EXCLUSIVE = getattr(_LIBC, "renamex_np", None)
if _RENAME_EXCLUSIVE is not None:
    _RENAME_EXCLUSIVE.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    _RENAME_EXCLUSIVE.restype = ctypes.c_int
_RENAMEAT_EXCLUSIVE = getattr(_LIBC, "renameatx_np", None)
if _RENAMEAT_EXCLUSIVE is not None:
    _RENAMEAT_EXCLUSIVE.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAMEAT_EXCLUSIVE.restype = ctypes.c_int
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAMEAT2.restype = ctypes.c_int


def _rename_exclusive(source: Path, destination: Path) -> None:
    """Atomically move *source* without replacing an existing destination."""
    if _RENAME_EXCLUSIVE is not None:
        result = _RENAME_EXCLUSIVE(
            os.fsencode(source), os.fsencode(destination), _RENAME_EXCL
        )
    elif _RENAMEAT2 is not None:
        result = _RENAMEAT2(
            _AT_FDCWD,
            os.fsencode(source),
            _AT_FDCWD,
            os.fsencode(destination),
            _RENAME_NOREPLACE,
        )
    else:
        raise OSError(errno.ENOTSUP, "exclusive rename is unavailable")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            str(destination),
        )


def _renameat_exclusive(
    source_directory: int,
    source_name: str,
    destination_directory: int,
    destination_name: str,
) -> None:
    """Atomically capture a directory entry without replacing another one."""
    if _RENAMEAT_EXCLUSIVE is not None:
        result = _RENAMEAT_EXCLUSIVE(
            source_directory,
            os.fsencode(source_name),
            destination_directory,
            os.fsencode(destination_name),
            _RENAME_EXCL,
        )
    elif _RENAMEAT2 is not None:
        result = _RENAMEAT2(
            source_directory,
            os.fsencode(source_name),
            destination_directory,
            os.fsencode(destination_name),
            _RENAME_NOREPLACE,
        )
    else:
        raise OSError(
            errno.ENOTSUP,
            "descriptor-relative exclusive rename is unavailable",
        )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination_name)


@dataclass(frozen=True)
class _CopyStaging:
    directory: Path
    cleanup_directory: Path
    path: Path
    capture_name: str
    directory_device: int
    directory_inode: int
    device: int
    inode: int


def _directory_open_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not no_follow or not directory:
        raise OSError(errno.ENOTSUP, "safe directory opens are unavailable")
    return os.O_RDONLY | no_follow | directory | getattr(os, "O_CLOEXEC", 0)


def _relative_directory_descriptor(
    root: Path, directory: Path, *, create: bool
) -> tuple[int, List[Path]]:
    """Open *directory* below *root* without following any lexical component."""
    root = Path(root).resolve()
    directory = Path(directory)
    try:
        relative = directory.relative_to(root)
    except ValueError as error:
        raise OSError(errno.EPERM, "directory is outside trusted root", str(directory)) from error
    if ".." in relative.parts:
        raise OSError(errno.EPERM, "directory traversal is not allowed", str(directory))
    descriptor = os.open(str(root), _directory_open_flags())
    created: List[Path] = []
    current = root
    try:
        for part in relative.parts:
            try:
                child = os.open(part, _directory_open_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                os.fsync(descriptor)
                current = current / part
                child = os.open(part, _directory_open_flags(), dir_fd=descriptor)
                os.fsync(child)
                created.append(current)
            else:
                current = current / part
            os.close(descriptor)
            descriptor = child
        return descriptor, created
    except BaseException:
        os.close(descriptor)
        raise


def _validate_real_directory_ancestry(root: Path, directory: Path) -> None:
    """Reject symlink/non-directory ancestors while allowing a missing suffix."""
    root = Path(root).resolve()
    directory = Path(directory)
    try:
        relative = directory.relative_to(root)
    except ValueError as error:
        raise OSError(errno.EPERM, "directory is outside trusted root", str(directory)) from error
    if ".." in relative.parts:
        raise OSError(errno.EPERM, "directory traversal is not allowed", str(directory))
    descriptor = os.open(str(root), _directory_open_flags())
    try:
        for part in relative.parts:
            try:
                child = os.open(part, _directory_open_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                return
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)


def _read_relative_file(root: Path, path: Path) -> tuple[bytes, int]:
    parent, _created = _relative_directory_descriptor(root, path.parent, create=False)
    descriptor = None
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent,
        )
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise OSError(errno.EPERM, "settings leaf is not a regular file", str(path))
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read()
        return data, stat.S_IMODE(status.st_mode)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _atomic_write_relative(
    root: Path, path: Path, data: bytes, mode: Optional[int] = None
) -> List[Path]:
    parent, created = _relative_directory_descriptor(root, path.parent, create=True)
    token = secrets.token_hex(16)
    temporary = ".{}.ostriv-write-{}".format(path.name, token)
    descriptor = None
    replaced = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
        if mode is not None:
            os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.replace(
            temporary,
            path.name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        replaced = True
        os.fsync(parent)
        return created
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not replaced:
            try:
                os.unlink(temporary, dir_fd=parent)
                os.fsync(parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def _replace_relative(root: Path, source: Path, destination: Path) -> None:
    if source.parent != destination.parent:
        raise OSError(errno.EPERM, "relative replace crosses directories")
    parent, _created = _relative_directory_descriptor(
        root, destination.parent, create=False
    )
    try:
        os.replace(
            source.name,
            destination.name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        os.fsync(parent)
    finally:
        os.close(parent)


def _unlink_relative(root: Path, path: Path) -> None:
    parent, _created = _relative_directory_descriptor(root, path.parent, create=False)
    try:
        os.unlink(path.name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def _owned_directory_status(
    descriptor: int, staging: _CopyStaging
) -> os.stat_result:
    status = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_dev != staging.directory_device
        or status.st_ino != staging.directory_inode
    ):
        raise OSError(
            errno.EPERM,
            "copy staging directory identity changed",
            str(staging.directory),
        )
    return status


def _owned_file_status(descriptor: int, staging: _CopyStaging) -> os.stat_result:
    status = os.fstat(descriptor)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or status.st_dev != staging.device
        or status.st_ino != staging.inode
    ):
        raise OSError(
            errno.EPERM,
            "copy staging file identity changed",
            str(staging.path),
        )
    return status


def _cleanup_owned_staging(staging: _CopyStaging) -> None:
    """Move a stage out of the destination namespace before deleting it."""
    source_exists = os.path.lexists(staging.directory)
    cleanup_exists = os.path.lexists(staging.cleanup_directory)
    if source_exists and cleanup_exists:
        raise OSError(
            errno.EEXIST,
            "copy staging cleanup handoff is occupied",
            str(staging.cleanup_directory),
        )
    if source_exists:
        try:
            _rename_exclusive(staging.directory, staging.cleanup_directory)
        except FileExistsError as error:
            if os.path.lexists(staging.directory):
                raise OSError(
                    errno.EEXIST,
                    "copy staging cleanup handoff is occupied",
                    str(staging.cleanup_directory),
                ) from error
            cleanup_exists = os.path.lexists(staging.cleanup_directory)
        _fsync_directory(staging.directory.parent)
        if staging.cleanup_directory.parent != staging.directory.parent:
            _fsync_directory(staging.cleanup_directory.parent)
        cleanup_exists = True
    if not cleanup_exists:
        return

    directory_descriptor = None
    staging_descriptor = None
    remove_directory = False
    try:
        try:
            directory_descriptor = os.open(
                str(staging.cleanup_directory), _directory_open_flags()
            )
            _owned_directory_status(directory_descriptor, staging)
        except OSError:
            return
        entries = os.listdir(directory_descriptor)
        if entries == []:
            remove_directory = True
        elif entries == [staging.path.name]:
            _renameat_exclusive(
                directory_descriptor,
                staging.path.name,
                directory_descriptor,
                staging.capture_name,
            )
            os.fsync(directory_descriptor)
            entries = [staging.capture_name]
        if entries == [staging.capture_name]:
            no_follow = getattr(os, "O_NOFOLLOW", 0)
            try:
                staging_descriptor = os.open(
                    staging.capture_name,
                    os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory_descriptor,
                )
                _owned_file_status(staging_descriptor, staging)
            except OSError:
                return
            os.unlink(staging.capture_name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
            remove_directory = os.listdir(directory_descriptor) == []
        elif entries:
            return
    finally:
        if staging_descriptor is not None:
            os.close(staging_descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)

    if remove_directory:
        try:
            os.rmdir(staging.cleanup_directory)
        except FileNotFoundError:
            return
        _fsync_directory(staging.cleanup_directory.parent)


def _atomic_copy_file(
    source: Path, destination: Path, staging: _CopyStaging
) -> None:
    if staging.directory.parent != destination.parent:
        raise ValueError("copy staging directory must be a sibling of its destination")
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow:
        raise OSError(errno.ENOTSUP, "no-follow file opens are unavailable")
    descriptor = None
    directory_descriptor = None
    destination_directory_descriptor = None
    try:
        directory_descriptor = os.open(
            str(staging.directory), _directory_open_flags()
        )
        _owned_directory_status(directory_descriptor, staging)
        descriptor = os.open(
            staging.path.name,
            os.O_WRONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_descriptor,
        )
        _owned_file_status(descriptor, staging)
        os.ftruncate(descriptor, 0)
        with source.open("rb") as source_stream, os.fdopen(
            descriptor, "wb", closefd=False
        ) as destination_stream:
            shutil.copyfileobj(source_stream, destination_stream)
            destination_stream.flush()
        os.fchmod(descriptor, stat_mode(source))
        os.fsync(descriptor)
        _owned_directory_status(directory_descriptor, staging)
        _owned_file_status(descriptor, staging)
        destination_directory_descriptor = os.open(
            str(destination.parent), _directory_open_flags()
        )
        os.replace(
            staging.path.name,
            destination.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=destination_directory_descriptor,
        )
        os.fsync(directory_descriptor)
        installed_status = os.stat(
            destination.name,
            dir_fd=destination_directory_descriptor,
            follow_symlinks=False,
        )
        if (
            installed_status.st_dev != staging.device
            or installed_status.st_ino != staging.inode
        ):
            raise OSError(
                errno.EPERM,
                "installed file identity changed",
                str(destination),
            )
        os.fsync(destination_directory_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if destination_directory_descriptor is not None:
            os.close(destination_directory_descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
    _cleanup_owned_staging(staging)


class LauncherPort(Protocol):
    def install(
        self, transaction: Transaction, installation: GameInstallation
    ) -> Mapping[str, object]:
        ...

    def verify(
        self,
        installation: GameInstallation,
        launcher_state: Mapping[str, object],
    ) -> None:
        ...

    def restore(
        self,
        installation: GameInstallation,
        launcher_state: Mapping[str, object],
    ) -> None:
        ...


@dataclass(frozen=True)
class InstallState:
    schema: int
    project_version: str
    bottle_realpath: str
    game_realpath: str
    owned_files: List[Dict[str, object]]
    backup_files: List[Dict[str, object]]
    prior_registry_value: Optional[str]
    original_config_backup: Optional[str]
    original_config_digest: str
    installed_config_digest: str
    original_settings_backup: Optional[str]
    original_settings_digest: str
    installed_settings_digest: str
    launcher_artifacts: Dict[str, object]
    completed_verification_time: str

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> "InstallState":
        field_names = {field.name for field in cls.__dataclass_fields__.values()}
        if (
            not isinstance(data, dict)
            or type(data.get("schema")) is not int
            or data.get("schema") != STATE_SCHEMA
            or set(data) != field_names
        ):
            raise ValueError("unsupported ownership state schema")
        required = {
            "project_version": str,
            "bottle_realpath": str,
            "game_realpath": str,
            "owned_files": list,
            "backup_files": list,
            "original_config_digest": str,
            "installed_config_digest": str,
            "original_settings_digest": str,
            "installed_settings_digest": str,
            "launcher_artifacts": dict,
            "completed_verification_time": str,
        }
        for name, expected_type in required.items():
            if not isinstance(data.get(name), expected_type):
                raise ValueError("invalid ownership state field {!r}".format(name))
        for name in ("original_config_backup", "original_settings_backup"):
            if data.get(name) is not None and not isinstance(data.get(name), str):
                raise ValueError("invalid ownership state field {!r}".format(name))
        if data.get("prior_registry_value") is not None and not isinstance(
            data.get("prior_registry_value"), str
        ):
            raise ValueError("invalid prior registry value")
        for collection_name in ("owned_files", "backup_files"):
            for item in data[collection_name]:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    raise ValueError("invalid {} entry".format(collection_name))
        for item in data["owned_files"]:
            if set(item) not in (
                {"path", "sha256"},
                {"path", "sha256", "owned_directories"},
            ) or re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))) is None:
                raise ValueError("invalid owned_files entry")
        for item in data["backup_files"]:
            if set(item) != {
                "path",
                "backup_path",
                "original_sha256",
                "installed_sha256",
            } or any(
                re.fullmatch(r"[0-9a-f]{64}", str(item.get(name, ""))) is None
                for name in ("original_sha256", "installed_sha256")
            ):
                raise ValueError("invalid backup_files entry")
        names = [field.name for field in cls.__dataclass_fields__.values()]
        return cls(**{name: data[name] for name in names})


@dataclass(frozen=True)
class _RestoreRecoveryPlan:
    state: Optional[InstallState]
    records: Tuple[Tuple[int, UndoRecord], ...]
    has_final_unlink_snapshot: bool


class WineRegistry:
    def __init__(self, wine: Path, bottle: Bottle, runner: CommandRunner):
        self.wine = wine
        self.bottle = bottle
        self.runner = runner

    def _base(self) -> List[str]:
        return (
            [str(self.wine), "--bottle", self.bottle.command_bottle()]
            + self.bottle.scope_args()
            + ["--no-update", "--no-lock", "reg"]
        )

    @staticmethod
    def _missing(result) -> bool:
        missing = re.compile(
            r"^\s*reg(?:\.exe)?\s*:\s*"
            r"(?:the system was )?unable to find the specified registry "
            r"(?:key or value|key|value)\.?\s*$",
            re.IGNORECASE,
        )
        return any(
            missing.fullmatch(line) is not None
            for line in "{}\n{}".format(result.stdout, result.stderr).splitlines()
        )

    def query(
        self, key: str, value: str, error_code: str = "install.registry"
    ) -> Optional[str]:
        last_detail = ""
        pattern = re.compile(
            r"^\s*{}\s+REG_[A-Za-z0-9_]+\s+(.*?)\s*$".format(
                re.escape(value)
            )
        )
        for _attempt in range(2):
            result = self.runner.run(
                self._base() + ["query", key, "/v", value], timeout=90.0
            )
            if result.returncode != 0:
                if self._missing(result):
                    return None
                last_detail = command_failure_detail(result)
                continue
            for line in result.stdout.splitlines():
                match = pattern.match(line)
                if match:
                    return match.group(1)
            last_detail = "Registry query succeeded without the requested value"
        player_message = (
            "Restore failed."
            if error_code.startswith("restore.")
            else "Installation failed."
        )
        raise PatchError(error_code, player_message, last_detail)

    def set(self, key: str, value: str, data: str) -> None:
        last_detail = ""
        for _attempt in range(2):
            result = self.runner.run(
                self._base() + ["add", key, "/v", value, "/d", data, "/f"],
                timeout=90.0,
            )
            last_detail = command_failure_detail(result)
            if result.returncode == 0:
                try:
                    if self.query(key, value) == data:
                        return
                    last_detail = "Registry query did not return the required value"
                except PatchError as error:
                    if error.code == "command.timeout":
                        raise
                    last_detail = error.detail
        raise PatchError("install.registry", "Installation failed.", last_detail)

    def delete(self, key: str, value: str) -> None:
        last_detail = ""
        for _attempt in range(2):
            result = self.runner.run(
                self._base() + ["delete", key, "/v", value, "/f"],
                timeout=90.0,
            )
            last_detail = command_failure_detail(result)
            if result.returncode == 0:
                try:
                    if self.query(key, value, "restore.registry") is None:
                        return
                    last_detail = "Registry value remains after deletion"
                except PatchError as error:
                    if error.code == "command.timeout":
                        raise
                    last_detail = error.detail
        raise PatchError("restore.registry", "Restore failed.", last_detail)


class Installer:
    def __init__(
        self,
        package_root: Path,
        launcher: LauncherPort,
        runner: Optional[CommandRunner] = None,
        launcher_destination: Optional[Path] = None,
        clock: Optional[Callable[[], datetime]] = None,
        progress: Optional[Callable[[str, str], None]] = None,
    ):
        self.package_root = package_root.resolve()
        self.launcher = launcher
        self.runner = runner or CommandRunner()
        self.launcher_destination = (
            launcher_destination
            if launcher_destination is not None
            else Path.home() / "Applications/CrossOver"
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.progress = progress or (lambda _label, _detail: None)
        self._existing_state: Optional[InstallState] = None
        self._owned_files: List[Dict[str, object]] = []
        self._backup_files: List[Dict[str, object]] = []
        self._config: Dict[str, object] = {}
        self._settings: Dict[str, object] = {}
        self._registry_before: Optional[str] = None
        self._registry_verified = False

    def state_path(self, installation: GameInstallation) -> Path:
        return installation.bottle.root.resolve() / STATE_NAME

    def journal_path(self, installation: GameInstallation) -> Path:
        return installation.bottle.root.resolve() / JOURNAL_NAME

    def _tool(self, installation: GameInstallation, name: str) -> Path:
        return installation.bottle.crossover.shared_support / "bin" / name

    def _registry(self, installation: GameInstallation) -> WineRegistry:
        return WineRegistry(self._tool(installation, "wine"), installation.bottle, self.runner)

    def _allowed(self, installation: GameInstallation, path: Path) -> bool:
        try:
            resolved = path.resolve(strict=False)
            roots = (
                installation.bottle.root.resolve(),
                installation.game_dir.resolve(),
                self.launcher_destination.resolve(strict=False),
            )
            return any(resolved == root or root in resolved.parents for root in roots)
        except OSError:
            return False

    def _launcher_app_path(self) -> Path:
        if self.launcher_destination.suffix == ".app":
            return (
                self.launcher_destination.parent.resolve(strict=False)
                / self.launcher_destination.name
            )
        return self.launcher_destination.resolve(strict=False) / "Ostriv (patched).app"

    def _allowed_launcher_artifacts(
        self, installation: GameInstallation
    ) -> set:
        app = self._launcher_app_path()
        bottle = installation.bottle.root.resolve()
        previous = app.with_name("." + app.name + ".ostriv-macos.previous")
        return {
            app,
            previous,
            app / "launcher",
            app / "Contents/Info.plist",
            app / "Contents/MacOS/Menu Helper",
            app / "Contents/Resources/CrossOverHelper.icns",
            bottle / "play-ostriv-patched.py",
            bottle / "launcher-config.json",
            bottle / ".ostriv-launcher.lock",
            bottle / ".ostriv-profile-recovery.json",
        }

    def _allowed_owned_targets(self, installation: GameInstallation) -> set:
        game_dir = installation.game_dir.resolve()
        return {
            *(game_dir / name for name in DRIVER_NAMES),
            game_dir / "steam_appid.txt",
            self._settings_path(installation),
        }

    def _allowed_settings_directories(
        self, installation: GameInstallation
    ) -> set:
        directories = set()
        current = self._settings_path(installation).parent
        drive_c = installation.bottle.root.resolve() / "drive_c"
        while current != drive_c and drive_c in current.parents:
            directories.add(current)
            current = current.parent
        return directories

    def _allowed_atomic_copy_destination(
        self, installation: GameInstallation, destination: Path
    ) -> bool:
        destination = destination.parent.resolve(strict=False) / destination.name
        raw_targets = self._allowed_owned_targets(installation) | {
            installation.bottle.root.resolve() / "cxbottle.conf",
            self._settings_path(installation),
        }
        targets = {
            target.parent.resolve(strict=False) / target.name
            for target in raw_targets
        }
        return destination in targets or any(
            self._valid_backup_relationship(target, destination)
            for target in targets
        )

    @staticmethod
    def _valid_backup_relationship(target: Path, backup: Path) -> bool:
        if backup.parent != target.parent:
            return False
        allowed_names = {
            target.name + ".bak",
            target.name + ".ostriv-macos.bak",
        }
        if backup.name in allowed_names:
            return True
        prefix = target.name + ".ostriv-macos-"
        return backup.name.startswith(prefix) and backup.name.endswith(".bak")

    def _validate_state_inventory(
        self, installation: GameInstallation, state: InstallState
    ) -> None:
        allowed_owned = self._allowed_owned_targets(installation)
        allowed_owned_text = {str(path) for path in allowed_owned}
        owned_by_path = {}
        for item in state.owned_files:
            path_text = str(item["path"])
            path = Path(path_text)
            digest = item.get("sha256")
            if (
                not path.is_absolute()
                or path_text not in allowed_owned_text
                or not isinstance(digest, str)
            ):
                raise ValueError("invalid owned-file inventory entry")
            owned_by_path[path] = item
            owned_directories = item.get("owned_directories", [])
            if not isinstance(owned_directories, list) or any(
                not isinstance(directory, str)
                or directory
                not in {
                    str(path)
                    for path in self._allowed_settings_directories(installation)
                }
                for directory in owned_directories
            ):
                raise ValueError("invalid owned-directory inventory entry")

        for item in state.backup_files:
            target_text = str(item["path"])
            target = Path(target_text)
            backup_text = item.get("backup_path")
            if not isinstance(backup_text, str):
                raise ValueError("invalid backup ownership entry")
            backup = Path(backup_text)
            owned = owned_by_path.get(target)
            if (
                target_text not in allowed_owned_text
                or not backup.is_absolute()
                or backup_text
                != str(backup.parent.resolve(strict=False) / backup.name)
                or not self._valid_backup_relationship(
                    target, backup
                )
                or owned is None
                or item.get("installed_sha256") != owned.get("sha256")
                or not isinstance(item.get("original_sha256"), str)
            ):
                raise ValueError("invalid backup-file inventory entry")

        config = installation.bottle.root.resolve() / "cxbottle.conf"
        settings = self._settings_path(installation)
        for target, backup_text in (
            (config, state.original_config_backup),
            (settings, state.original_settings_backup),
        ):
            if backup_text is None:
                continue
            backup = Path(backup_text)
            if (
                not backup.is_absolute()
                or backup_text
                != str(backup.parent.resolve(strict=False) / backup.name)
                or not self._valid_backup_relationship(
                    target, backup
                )
            ):
                raise ValueError("invalid configuration backup inventory")

        allowed_launcher = self._allowed_launcher_artifacts(installation)
        for path in self._launcher_paths(state.launcher_artifacts):
            if (
                not path.is_absolute()
                or path.resolve(strict=False) not in allowed_launcher
            ):
                raise ValueError("invalid launcher artifact inventory")

    def undo_handlers(
        self, installation: GameInstallation
    ) -> Mapping[str, Callable[[UndoRecord], None]]:
        restore_launcher = lambda record: self._undo_restore_snapshots(
            installation, record
        )
        launcher_undo = getattr(self.launcher, "undo_handler", None)
        if callable(launcher_undo):
            restore_launcher = launcher_undo(installation, restore_launcher)
        return {
            "remove_path": lambda record: self._undo_remove_path(installation, record),
            "remove_staging": lambda record: self._undo_remove_staging(
                installation, record
            ),
            "restore_file": lambda record: self._undo_restore_file(installation, record),
            "restore_registry": lambda record: self._undo_restore_registry(
                installation, record
            ),
            "restore_config": lambda record: self._undo_restore_named(
                installation, record
            ),
            "restore_settings": lambda record: self._undo_restore_named(
                installation, record
            ),
            "restore_launcher": restore_launcher,
        }

    def transaction_for(self, installation: GameInstallation) -> Transaction:
        return Transaction(
            InstallJournal(self.journal_path(installation)),
            self.undo_handlers(installation),
        )

    def _cleanup_completed_journal(self, transaction: Transaction) -> None:
        if transaction.journal.data.get("complete"):
            try:
                _durable_unlink(transaction.journal.path)
            except FileNotFoundError:
                pass

    def _validate_loaded_state(
        self, installation: GameInstallation, state: InstallState
    ) -> InstallState:
        if state.bottle_realpath != str(installation.bottle.root.resolve()):
            raise ValueError("ownership state belongs to another bottle")
        if state.game_realpath != str(installation.game_dir.resolve()):
            raise ValueError("ownership state belongs to another game")
        self._validate_state_inventory(installation, state)
        claimed_paths = [Path(str(item["path"])) for item in state.owned_files]
        for item in state.backup_files:
            backup_path = item.get("backup_path")
            if not isinstance(backup_path, str):
                raise ValueError("invalid backup ownership entry")
            claimed_paths.extend((Path(str(item["path"])), Path(backup_path)))
        for backup_path in (
            state.original_config_backup,
            state.original_settings_backup,
        ):
            if backup_path is not None:
                claimed_paths.append(Path(backup_path))
        claimed_paths.extend(self._launcher_paths(state.launcher_artifacts))
        if any(
            not path.is_absolute() or not self._allowed(installation, path)
            for path in claimed_paths
        ):
            raise ValueError("ownership state claims a path outside the installation")
        return state

    def _decode_state_bytes(
        self,
        installation: GameInstallation,
        data: bytes,
        error_prefix: str,
        source: Path,
    ) -> InstallState:
        try:
            state = InstallState.from_dict(json.loads(data.decode("utf-8")))
            return self._validate_loaded_state(installation, state)
        except (UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise PatchError(
                "{}.state_corrupt".format(error_prefix),
                "The installation ownership record is unreadable.",
                "Unable to load {}: {}: {}".format(
                    source, type(error).__name__, error
                ),
            ) from error

    def _load_state_with_bytes(
        self, installation: GameInstallation, error_prefix: str
    ) -> Tuple[Optional[InstallState], Optional[bytes], Optional[int]]:
        path = self.state_path(installation)
        if not os.path.lexists(str(path)):
            return None, None, None
        descriptor = None
        try:
            descriptor = os.open(
                str(path),
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            opened = os.fstat(descriptor)
            current = os.lstat(str(path))
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or opened.st_dev != current.st_dev
                or opened.st_ino != current.st_ino
            ):
                raise OSError(errno.EPERM, "ownership state leaf is unsafe", str(path))
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read()
            state = self._decode_state_bytes(
                installation, data, error_prefix, path
            )
            return state, data, stat.S_IMODE(opened.st_mode)
        except PatchError:
            raise
        except OSError as error:
            raise PatchError(
                "{}.state_corrupt".format(error_prefix),
                "The installation ownership record is unreadable.",
                "Unable to load {}: {}: {}".format(
                    path, type(error).__name__, error
                ),
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _load_state(
        self, installation: GameInstallation, error_prefix: str
    ) -> Optional[InstallState]:
        state, _data, _mode = self._load_state_with_bytes(
            installation, error_prefix
        )
        return state

    @staticmethod
    def _writable_location(path: Path) -> bool:
        candidate = path
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)

    def _validate_settings_ancestry(
        self, installation: GameInstallation, code: str, player_message: str
    ) -> None:
        path = self._settings_path(installation)
        root = installation.bottle.root.resolve()
        try:
            _validate_real_directory_ancestry(root, path.parent)
        except OSError as error:
            raise PatchError(
                code,
                player_message,
                "Unsafe settings directory ancestry {}: {}".format(path.parent, error),
            ) from error

    def preflight(
        self, installation: GameInstallation, payload: Sequence[PayloadEntry]
    ) -> None:
        issues = []
        launcher_preflight = getattr(self.launcher, "preflight", None)
        if callable(launcher_preflight):
            launcher_preflight(installation)
        expected_payload = {
            *("prebuilt/{}".format(name) for name in DRIVER_NAMES),
            "assets/settings.data",
        }
        actual_payload = [entry.relative_path for entry in payload]
        inventory_invalid = (
            len(actual_payload) != len(expected_payload)
            or set(actual_payload) != expected_payload
        )
        if inventory_invalid:
            issues.append(
                "Required payload inventory mismatch: expected {!r}, received {!r}".format(
                    sorted(expected_payload), sorted(actual_payload)
                )
            )
        bottle_root = installation.bottle.root.resolve()
        game_dir = installation.game_dir.resolve()
        config = bottle_root / "cxbottle.conf"
        registry_file = bottle_root / "system.reg"
        settings = self._settings_path(installation)
        try:
            self._validate_settings_ancestry(
                installation, "install.preflight", "Installation cannot start."
            )
        except PatchError as error:
            issues.append(error.detail)
        mutable_destinations = [
            *(game_dir / name for name in DRIVER_NAMES),
            game_dir / "steam_appid.txt",
            config,
            settings,
        ]
        if not is_supported_game_directory(game_dir, bottle_root):
            issues.append(
                "Selected game directory is not a supported Ostriv installation: "
                "{} (ostriv.exe must be inside drive_c)".format(game_dir)
            )
        for destination in mutable_destinations:
            if destination.is_symlink():
                issues.append(
                    "Unsafe symbolic-link destination: {}".format(destination)
                )
        if not game_dir.is_dir() or not os.access(game_dir, os.W_OK | os.X_OK):
            issues.append("Ostriv destination is not writable: {}".format(game_dir))
        if not config.is_file() or not os.access(config, os.R_OK | os.W_OK):
            issues.append("cxbottle.conf is missing or not writable: {}".format(config))
        if not registry_file.is_file() or not os.access(registry_file, os.R_OK):
            issues.append("system.reg is missing or unreadable: {}".format(registry_file))
        if not self._writable_location(settings.parent):
            issues.append("settings location is not writable: {}".format(settings.parent))

        tools = {}
        for name in ("wine", "cxbottle", "cxmenu"):
            tool = self._tool(installation, name)
            tools[name] = tool
            if not os.access(tool, os.X_OK):
                issues.append("{} is missing or not executable: {}".format(name, tool))
        menu_helper = (
            installation.bottle.crossover.app
            / "Contents/Resources/Menu Helper.cpbz2"
        )
        if not menu_helper.is_file():
            issues.append("Menu Helper.cpbz2 is missing: {}".format(menu_helper))
        if not self._writable_location(self.launcher_destination):
            issues.append(
                "launcher destination is not writable: {}".format(
                    self.launcher_destination
                )
            )

        cxbottle = tools["cxbottle"]
        if os.access(cxbottle, os.X_OK):
            argv = (
                [
                    str(cxbottle),
                    "--bottle",
                    installation.bottle.command_bottle(),
                ]
                + installation.bottle.scope_args()
                + ["--status"]
            )
            result = self.runner.run(argv, timeout=60.0)
            if result.returncode != 0:
                issues.append(
                    command_failure_detail(
                        result, "cxbottle rejected the selected bottle"
                    )
                )
        self._existing_state = self._load_state(installation, "install")
        if issues:
            detail = "\n".join(issues)
            code = "install.payload_inventory" if inventory_invalid else "install.preflight"
            logger.error("%s: %s", code, detail)
            raise PatchError(code, "Installation cannot start.", detail)

    def _start_ownership(self) -> None:
        if self._existing_state is None:
            self._owned_files = []
            self._backup_files = []
            self._config = {}
            self._settings = {}
            return
        self._owned_files = copy.deepcopy(self._existing_state.owned_files)
        self._backup_files = copy.deepcopy(self._existing_state.backup_files)
        self._config = {
            "backup": self._existing_state.original_config_backup,
            "original_digest": self._existing_state.original_config_digest,
            "installed_digest": self._existing_state.installed_config_digest,
        }
        self._settings = {
            "backup": self._existing_state.original_settings_backup,
            "original_digest": self._existing_state.original_settings_digest,
            "installed_digest": self._existing_state.installed_settings_digest,
        }

    @staticmethod
    def _upsert(items: List[Dict[str, object]], item: Dict[str, object]) -> None:
        for index, current in enumerate(items):
            if current.get("path") == item.get("path"):
                items[index] = item
                return
        items.append(item)

    def _owned(self, path: Path) -> Optional[Dict[str, object]]:
        path_text = str(path)
        return next((item for item in self._owned_files if item.get("path") == path_text), None)

    def _backup_for(self, path: Path) -> Optional[Dict[str, object]]:
        path_text = str(path)
        return next((item for item in self._backup_files if item.get("path") == path_text), None)

    def _choose_backup(self, path: Path) -> Path:
        preferred = path.with_name(path.name + ".bak")
        if not preferred.exists():
            return preferred
        candidate = path.with_name(path.name + ".ostriv-macos.bak")
        counter = 2
        while candidate.exists():
            candidate = path.with_name(
                path.name + ".ostriv-macos-{}.bak".format(counter)
            )
            counter += 1
        return candidate

    @staticmethod
    def _staging_name(destination: Path, token: str) -> str:
        return ".{}.ostriv-macos-stage-{}".format(destination.name, token)

    @staticmethod
    def _staging_cleanup_name(token: str) -> str:
        return ".ostriv-macos-stage-cleanup-{}".format(token)

    @staticmethod
    def _staging_capture_name(token: str) -> str:
        return ".payload-capture-{}".format(token)

    def _prepare_copy_staging(
        self,
        transaction: Transaction,
        installation: GameInstallation,
        destination: Path,
    ) -> _CopyStaging:
        if not destination.is_absolute():
            raise PatchError(
                "install.ownership_conflict",
                "Installation cannot use an unsafe destination.",
                str(destination),
            )
        destination = destination.parent.resolve(strict=False) / destination.name
        if destination.is_symlink() or not self._allowed_atomic_copy_destination(
            installation, destination
        ):
            raise PatchError(
                "install.ownership_conflict",
                "Installation cannot use an unsafe destination.",
                str(destination),
            )
        while True:
            token = secrets.token_hex(16)
            directory = destination.with_name(
                self._staging_name(destination, token)
            )
            cleanup_directory = (
                installation.bottle.root.resolve()
                / self._staging_cleanup_name(token)
            )
            if not os.path.lexists(directory) and not os.path.lexists(
                cleanup_directory
            ):
                break
        staging = directory / "payload"
        capture_name = self._staging_capture_name(token)

        record_data: Dict[str, object] = {
            "path": str(staging),
            "directory": str(directory),
            "cleanup_directory": str(cleanup_directory),
            "destination": str(destination),
        }

        def create_owned_staging() -> None:
            no_follow = getattr(os, "O_NOFOLLOW", 0)
            if not no_follow:
                raise OSError(errno.ENOTSUP, "no-follow file opens are unavailable")
            os.mkdir(directory, mode=0o700)
            directory_descriptor = None
            descriptor = None
            try:
                directory_descriptor = os.open(
                    str(directory), _directory_open_flags()
                )
                os.fchmod(directory_descriptor, 0o700)
                directory_status = os.fstat(directory_descriptor)
                if not stat.S_ISDIR(directory_status.st_mode):
                    raise OSError(
                        errno.EPERM,
                        "copy staging is not an exclusive directory",
                        str(directory),
                    )
                descriptor = os.open(
                    staging.name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | no_follow
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=directory_descriptor,
                )
                os.fchmod(descriptor, 0o600)
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    raise OSError(
                        errno.EPERM,
                        "copy staging is not an exclusive regular file",
                        str(staging),
                    )
                record_data["directory_device"] = directory_status.st_dev
                record_data["directory_inode"] = directory_status.st_ino
                record_data["device"] = status.st_dev
                record_data["inode"] = status.st_ino
                transaction.checkpoint_undo(
                    UndoRecord("remove_staging", copy.deepcopy(record_data))
                )
                os.fsync(descriptor)
                os.fsync(directory_descriptor)
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if directory_descriptor is not None:
                    os.close(directory_descriptor)
            _fsync_directory(directory.parent)

        transaction.step(
            "own copy staging for {}".format(destination.name),
            UndoRecord("remove_staging", record_data),
            create_owned_staging,
        )
        return _CopyStaging(
            directory,
            cleanup_directory,
            staging,
            capture_name,
            int(record_data["directory_device"]),
            int(record_data["directory_inode"]),
            int(record_data["device"]),
            int(record_data["inode"]),
        )

    def _install_file(
        self,
        transaction: Transaction,
        installation: GameInstallation,
        path: Path,
        source: Path,
        name: str,
    ) -> None:
        if path.is_symlink():
            raise PatchError(
                "install.ownership_conflict",
                "Installation cannot replace a symbolic-link destination.",
                str(path),
            )
        desired_digest = _file_digest(source)
        existing_owned = self._owned(path)
        if existing_owned is not None:
            if not _same_file(path, str(existing_owned["sha256"])):
                raise PatchError(
                    "install.ownership_conflict",
                    "Installation cannot replace a modified file.",
                    str(path),
                )
            if desired_digest == existing_owned["sha256"]:
                return
        if _same_file(path, desired_digest):
            return

        if path.exists():
            current_digest = _file_digest(path)
            backup_entry = self._backup_for(path)
            backup = (
                Path(str(backup_entry["backup_path"]))
                if backup_entry is not None
                else self._choose_backup(path)
            )
            if backup_entry is not None:
                original_digest = str(backup_entry["original_sha256"])
                if not _same_file(backup, original_digest):
                    raise PatchError(
                        "install.ownership_conflict",
                        "Installation cannot replace a modified file.",
                        "Original backup is missing or changed: {}".format(backup),
                    )
                snapshots = self._snapshots([path, backup])
                for snapshot in snapshots:
                    if snapshot.get("path") == str(path.resolve(strict=False)):
                        snapshot["allowed_current_sha256"] = [desired_digest]
                undo = UndoRecord("restore_file", {"snapshots": snapshots})
            else:
                original_digest = current_digest
                undo = UndoRecord(
                    "restore_file",
                    {
                        "path": str(path),
                        "owned_path": str(path),
                        "backup_path": str(backup),
                        "installed_sha256": desired_digest,
                        "original_sha256": original_digest,
                    },
                )

            backup_staging = (
                self._prepare_copy_staging(transaction, installation, backup)
                if not backup.exists()
                else None
            )
            destination_staging = self._prepare_copy_staging(
                transaction, installation, path
            )

            def replace() -> None:
                if not backup.exists():
                    if backup_staging is None:
                        raise RuntimeError("missing backup staging path")
                    _atomic_copy_file(path, backup, backup_staging)
                _atomic_copy_file(source, path, destination_staging)

            transaction.step(name, undo, replace)
            self._upsert(
                self._backup_files,
                {
                    "path": str(path),
                    "backup_path": str(backup),
                    "original_sha256": original_digest,
                    "installed_sha256": desired_digest,
                },
            )
        else:
            destination_staging = self._prepare_copy_staging(
                transaction, installation, path
            )
            undo = UndoRecord(
                "remove_path",
                {
                    "path": str(path),
                    "owned_path": str(path),
                    "expected_sha256": desired_digest,
                },
            )
            transaction.step(
                name,
                undo,
                lambda: _atomic_copy_file(source, path, destination_staging),
            )
        self._upsert(
            self._owned_files, {"path": str(path), "sha256": desired_digest}
        )

    def stage_driver_files(
        self,
        transaction: Transaction,
        installation: GameInstallation,
        payload: Sequence[PayloadEntry],
    ) -> None:
        entries = {
            Path(entry.relative_path).name: entry
            for entry in payload
            if Path(entry.relative_path).name in DRIVER_NAMES
        }
        for name in DRIVER_NAMES:
            entry = entries.get(name)
            if entry is None:
                raise PatchError(
                    "install.payload",
                    "Installation failed.",
                    "Payload does not include {}".format(name),
                )
            self._install_file(
                transaction,
                installation,
                installation.game_dir.resolve() / name,
                self.package_root / entry.relative_path,
                "stage {}".format(name),
            )

    def write_app_id(
        self,
        transaction: Transaction,
        installation: GameInstallation,
        app_id: str,
    ) -> None:
        path = installation.game_dir.resolve() / "steam_appid.txt"
        desired = app_id.encode("ascii")
        desired_digest = _bytes_digest(desired)
        existing_owned = self._owned(path)
        if existing_owned is not None and not _same_file(path, str(existing_owned["sha256"])):
            raise PatchError(
                "install.ownership_conflict",
                "Installation cannot replace a modified file.",
                str(path),
            )
        if _same_file(path, desired_digest):
            return
        if path.exists():
            original_digest = _file_digest(path)
            backup = self._choose_backup(path)
            backup_staging = self._prepare_copy_staging(
                transaction, installation, backup
            )
            undo = UndoRecord(
                "restore_file",
                {
                    "path": str(path),
                    "owned_path": str(path),
                    "backup_path": str(backup),
                    "installed_sha256": desired_digest,
                    "original_sha256": original_digest,
                },
            )

            def replace() -> None:
                _atomic_copy_file(path, backup, backup_staging)
                _atomic_write_bytes(path, desired, stat_mode(path))

            transaction.step("write game app id", undo, replace)
            self._upsert(
                self._backup_files,
                {
                    "path": str(path),
                    "backup_path": str(backup),
                    "original_sha256": original_digest,
                    "installed_sha256": desired_digest,
                },
            )
        else:
            transaction.step(
                "write game app id",
                UndoRecord(
                    "remove_path",
                    {
                        "path": str(path),
                        "owned_path": str(path),
                        "expected_sha256": desired_digest,
                    },
                ),
                lambda: _atomic_write_bytes(path, desired),
            )
        self._upsert(self._owned_files, {"path": str(path), "sha256": desired_digest})

    def set_native_override(
        self, transaction: Transaction, installation: GameInstallation
    ) -> None:
        registry = self._registry(installation)
        current = registry.query(REGISTRY_KEY, REGISTRY_VALUE)
        if self._existing_state is None:
            self._registry_before = current
        else:
            self._registry_before = self._existing_state.prior_registry_value
        if current == REGISTRY_DATA:
            self._registry_verified = True
            return
        transaction.step(
            "set game registry override",
            UndoRecord(
                "restore_registry",
                {
                    "key": REGISTRY_KEY,
                    "value": REGISTRY_VALUE,
                    "before": current,
                    "after": REGISTRY_DATA,
                },
            ),
            lambda: registry.set(REGISTRY_KEY, REGISTRY_VALUE, REGISTRY_DATA),
        )
        self._registry_verified = True

    @staticmethod
    def _mutate_environment(data: bytes) -> bytes:
        try:
            text = data.decode("utf-8")
        except UnicodeError as error:
            raise PatchError(
                "install.config", "Installation failed.", "cxbottle.conf is not UTF-8"
            ) from error
        lines = text.splitlines(keepends=True)
        newline = "\r\n" if "\r\n" in text else "\n"
        header_index = next(
            (index for index, line in enumerate(lines) if line.strip() == "[EnvironmentVariables]"),
            None,
        )
        if header_index is None:
            if text and not text.endswith(("\n", "\r")):
                lines.append(newline)
            if lines and lines[-1].strip():
                lines.append(newline)
            lines.append("[EnvironmentVariables]" + newline)
            header_index = len(lines) - 1
        section_end = len(lines)
        for index in range(header_index + 1, len(lines)):
            if lines[index].lstrip().startswith("["):
                section_end = index
                break
        present = set()
        for index in range(header_index + 1, section_end):
            for key, value in BOTTLE_ENV.items():
                if re.match(r'^\s*"{}"\s*='.format(re.escape(key)), lines[index]):
                    ending = "\r\n" if lines[index].endswith("\r\n") else newline
                    lines[index] = '"{}" = "{}"{}'.format(key, value, ending)
                    present.add(key)
                    break
        additions = [
            '"{}" = "{}"{}'.format(key, value, newline)
            for key, value in BOTTLE_ENV.items()
            if key not in present
        ]
        lines[header_index + 1 : header_index + 1] = additions
        return "".join(lines).encode("utf-8")

    def set_bottle_environment(
        self, transaction: Transaction, installation: GameInstallation
    ) -> None:
        path = installation.bottle.root.resolve() / "cxbottle.conf"
        current = path.read_bytes()
        desired = self._mutate_environment(current)
        if self._existing_state is not None:
            expected = self._existing_state.installed_config_digest
            if _bytes_digest(current) != expected:
                raise PatchError(
                    "install.ownership_conflict",
                    "Installation cannot replace modified bottle settings.",
                    str(path),
                )
            self._config = {
                "backup": self._existing_state.original_config_backup,
                "original_digest": self._existing_state.original_config_digest,
                "installed_digest": expected,
            }
            return
        original_digest = _bytes_digest(current)
        if desired == current:
            self._config = {
                "backup": None,
                "original_digest": original_digest,
                "installed_digest": original_digest,
            }
            return
        backup = self._choose_backup(path)
        installed_digest = _bytes_digest(desired)
        backup_staging = self._prepare_copy_staging(
            transaction, installation, backup
        )
        transaction.step(
            "set bottle environment",
            UndoRecord(
                "restore_config",
                {
                    "path": str(path),
                    "owned_path": str(path),
                    "backup_path": str(backup),
                    "installed_sha256": installed_digest,
                    "original_sha256": original_digest,
                },
            ),
            lambda: self._backup_and_write(path, backup, backup_staging, desired),
        )
        self._config = {
            "backup": str(backup),
            "original_digest": original_digest,
            "installed_digest": installed_digest,
        }

    @staticmethod
    def _settings_path(installation: GameInstallation) -> Path:
        return (
            installation.bottle.root.resolve()
            / "drive_c/users/crossover/Saved Games/Ostriv/settings.data"
        )

    @staticmethod
    def _safe_settings(data: bytes) -> bytes:
        key = b"bMultisampling"
        marker = struct.pack("<i", len(key)) + key
        index = data.find(marker)
        if index < 0 or index + len(marker) >= len(data):
            raise PatchError(
                "install.settings",
                "Installation failed.",
                "settings.data has no multisampling value",
            )
        value_position = index + len(marker)
        updated = bytearray(data)
        updated[value_position] = 0
        return bytes(updated)

    def set_safe_graphics(
        self, transaction: Transaction, installation: GameInstallation
    ) -> None:
        path = self._settings_path(installation)
        bottle_root = installation.bottle.root.resolve()
        self._validate_settings_ancestry(
            installation, "install.settings", "Installation failed."
        )
        if self._existing_state is not None:
            try:
                existing_data, existing_mode = _read_relative_file(bottle_root, path)
            except OSError:
                existing_data = b""
                existing_mode = 0
            desired = self._safe_settings(existing_data)
            existing_digest = _bytes_digest(existing_data)
            installed_digest = _bytes_digest(desired)
            if existing_digest != self._existing_state.installed_settings_digest:
                if desired != existing_data:
                    snapshots = self._snapshots([path])
                    snapshots[0]["allowed_current_sha256"] = [installed_digest]
                    transaction.step(
                        "refresh safe graphics settings",
                        UndoRecord("restore_settings", {"snapshots": snapshots}),
                        lambda: self._replace_modified_settings(
                            installation,
                            path,
                            existing_data,
                            existing_mode,
                            existing_digest,
                            desired,
                        ),
                    )
                owned = self._owned(path)
                if owned is not None:
                    updated_owned = copy.deepcopy(owned)
                    updated_owned["sha256"] = installed_digest
                    self._upsert(self._owned_files, updated_owned)
            self._settings = {
                "backup": self._existing_state.original_settings_backup,
                "original_digest": self._existing_state.original_settings_digest,
                "installed_digest": installed_digest,
            }
            return
        if path.is_file():
            current, current_mode = _read_relative_file(bottle_root, path)
            desired = self._safe_settings(current)
            original_digest = _bytes_digest(current)
            if desired == current:
                self._settings = {
                    "backup": None,
                    "original_digest": original_digest,
                    "installed_digest": original_digest,
                }
                return
            backup = self._choose_backup(path)
            installed_digest = _bytes_digest(desired)
            transaction.step(
                "set safe graphics",
                UndoRecord(
                    "restore_settings",
                    {
                        "path": str(path),
                        "owned_path": str(path),
                        "backup_path": str(backup),
                        "installed_sha256": installed_digest,
                        "original_sha256": original_digest,
                    },
                ),
                lambda: self._backup_and_write_settings(
                    installation,
                    path,
                    backup,
                    current,
                    current_mode,
                    original_digest,
                    desired,
                ),
            )
            self._settings = {
                "backup": str(backup),
                "original_digest": original_digest,
                "installed_digest": installed_digest,
            }
            return
        template = self.package_root / "assets/settings.data"
        desired = self._safe_settings(template.read_bytes())
        installed_digest = _bytes_digest(desired)
        owned_directories = []
        current = path.parent
        while not current.exists() and current != current.parent:
            owned_directories.append(str(current.resolve(strict=False)))
            current = current.parent
        transaction.step(
            "install safe graphics settings",
            UndoRecord(
                "remove_path",
                {
                    "path": str(path),
                    "owned_path": str(path),
                    "expected_sha256": installed_digest,
                    "owned_directories": owned_directories,
                },
            ),
            lambda: _atomic_write_relative(bottle_root, path, desired),
        )
        self._upsert(
            self._owned_files,
            {
                "path": str(path),
                "sha256": installed_digest,
                "owned_directories": owned_directories,
            },
        )
        self._settings = {
            "backup": None,
            "original_digest": "",
            "installed_digest": installed_digest,
        }

    @staticmethod
    def _replace_modified_settings(
        installation: GameInstallation,
        path: Path,
        original_data: bytes,
        original_mode: int,
        original_digest: str,
        data: bytes,
    ) -> None:
        root = installation.bottle.root.resolve()
        current, current_mode = _read_relative_file(root, path)
        if (
            current != original_data
            or _bytes_digest(current) != original_digest
            or current_mode != original_mode
        ):
            raise PatchError(
                "install.ownership_conflict",
                "Installation cannot replace modified game settings.",
                str(path),
            )
        _atomic_write_relative(root, path, data, original_mode)

    @staticmethod
    def _backup_and_write(
        path: Path, backup: Path, backup_staging: _CopyStaging, data: bytes
    ) -> None:
        mode = stat_mode(path)
        if not backup.exists():
            _atomic_copy_file(path, backup, backup_staging)
        _atomic_write_bytes(path, data, mode)

    @staticmethod
    def _backup_and_write_settings(
        installation: GameInstallation,
        path: Path,
        backup: Path,
        original_data: bytes,
        original_mode: int,
        original_digest: str,
        data: bytes,
    ) -> None:
        root = installation.bottle.root.resolve()
        current, current_mode = _read_relative_file(root, path)
        if _bytes_digest(current) != original_digest or current_mode != original_mode:
            raise PatchError(
                "install.ownership_conflict",
                "Installation cannot replace modified game settings.",
                str(path),
            )
        if not os.path.lexists(backup):
            _atomic_write_relative(root, backup, original_data, original_mode)
        _atomic_write_relative(root, path, data, original_mode)

    def _completed_time(self) -> str:
        if self._existing_state is not None:
            return self._existing_state.completed_verification_time
        return self.clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def verify(
        self,
        installation: GameInstallation,
        payload: Sequence[PayloadEntry],
        launcher_state: Mapping[str, object],
    ) -> InstallState:
        logger.info(
            "install verification start bottle=%s payload_files=%s",
            installation.bottle.name,
            len(payload),
        )
        failures = []
        for entry in payload:
            name = Path(entry.relative_path).name
            if name not in DRIVER_NAMES:
                continue
            destination = installation.game_dir.resolve() / name
            if not _same_file(destination, entry.sha256):
                failures.append("payload verification failed: {}".format(destination))
        app_id = installation.game_dir.resolve() / "steam_appid.txt"
        if not _same_file(app_id, _bytes_digest(b"773790")):
            failures.append("game app id verification failed")
        if not self._registry_verified and (
            self._registry(installation).query(REGISTRY_KEY, REGISTRY_VALUE)
            != REGISTRY_DATA
        ):
            failures.append("registry override verification failed")
        config = installation.bottle.root.resolve() / "cxbottle.conf"
        try:
            config_text = config.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            failures.append("bottle environment is unreadable: {}".format(error))
            config_text = ""
        for key, value in BOTTLE_ENV.items():
            matches = re.findall(
                r'^\s*"{}"\s*=\s*"([^"]*)"\s*$'.format(re.escape(key)),
                config_text,
                re.MULTILINE,
            )
            if matches != [value]:
                failures.append("bottle environment verification failed: {}".format(key))
        settings = self._settings_path(installation)
        try:
            if self._safe_settings(settings.read_bytes()) != settings.read_bytes():
                failures.append("safe graphics verification failed")
        except (OSError, PatchError) as error:
            failures.append("safe graphics verification failed: {}".format(error))
        verification_state = self._existing_state
        if verification_state is None and self.state_path(installation).is_file():
            verification_state = self._load_state(installation, "install")
        expected_config_digest = self._config.get("installed_digest")
        expected_settings_digest = self._settings.get("installed_digest")
        if verification_state is not None:
            expected_config_digest = (
                expected_config_digest or verification_state.installed_config_digest
            )
            expected_settings_digest = (
                expected_settings_digest or verification_state.installed_settings_digest
            )
        if isinstance(expected_config_digest, str) and not _same_file(
            config, expected_config_digest
        ):
            failures.append("unrelated bottle configuration changed")
        if isinstance(expected_settings_digest, str) and not _same_file(
            settings, expected_settings_digest
        ):
            failures.append("unrelated game settings changed")
        try:
            self.launcher.verify(installation, launcher_state)
        except PatchError:
            raise
        except BaseException as error:
            failures.append("launcher verification failed: {}".format(error))
        if failures:
            detail = "\n".join(failures)
            logger.error("install.verify: %s", detail)
            raise PatchError("install.verify", "Installation could not be verified.", detail)

        if not self._config:
            self._config = {
                "backup": None,
                "original_digest": _file_digest(config),
                "installed_digest": _file_digest(config),
            }
        if not self._settings:
            self._settings = {
                "backup": None,
                "original_digest": _file_digest(settings),
                "installed_digest": _file_digest(settings),
            }
        logger.info("install verification status=OK bottle=%s", installation.bottle.name)
        return InstallState(
            schema=STATE_SCHEMA,
            project_version=__version__,
            bottle_realpath=str(installation.bottle.root.resolve()),
            game_realpath=str(installation.game_dir.resolve()),
            owned_files=copy.deepcopy(self._owned_files),
            backup_files=copy.deepcopy(self._backup_files),
            prior_registry_value=self._registry_before,
            original_config_backup=self._config.get("backup"),
            original_config_digest=str(self._config["original_digest"]),
            installed_config_digest=str(self._config["installed_digest"]),
            original_settings_backup=self._settings.get("backup"),
            original_settings_digest=str(self._settings["original_digest"]),
            installed_settings_digest=str(self._settings["installed_digest"]),
            launcher_artifacts=copy.deepcopy(dict(launcher_state)),
            completed_verification_time=self._completed_time(),
        )

    def write_install_state(
        self,
        transaction: Transaction,
        installation: GameInstallation,
        state: InstallState,
    ) -> None:
        path = self.state_path(installation)
        desired = state.to_dict()
        desired_bytes = (json.dumps(desired, ensure_ascii=False, sort_keys=True, indent=2)).encode(
            "utf-8"
        )
        desired_digest = _bytes_digest(desired_bytes)
        snapshots = self._snapshots([path])
        for snapshot in snapshots:
            if snapshot.get("present") is False:
                snapshot["remove_sha256"] = desired_digest
        transaction.step(
            "write ownership state",
            UndoRecord("restore_file", {"snapshots": snapshots}),
            lambda: atomic_write_json(path, desired),
        )
        if not _same_file(path, desired_digest):
            raise PatchError(
                "install.state_verify", "Installation failed.", "Ownership state mismatch"
            )

    def install(
        self,
        installation: GameInstallation,
        payload: Sequence[PayloadEntry],
    ) -> InstallState:
        # Route Restore recovery before binding generic or launcher undo handlers.
        journal = InstallJournal(self.journal_path(installation))
        if (
            journal.data.get("complete") is False
            and journal.data.get("operation") == "restore"
        ):
            raise PatchError(
                "install.recovery_required",
                RECOVERY_REQUIRED_MESSAGE,
                "Install cannot recover an incomplete Restore journal",
            )
        logger.info(
            "install start bottle=%s game=%s payload_files=%s",
            installation.bottle.name,
            installation.game_dir.resolve(),
            len(payload),
        )
        self._registry_verified = False
        validate_payload(self.package_root, payload)
        logger.info("install stage=payload_validation status=OK")
        self.progress("Installing", "checking CrossOver (may take a minute)")
        self.preflight(installation, payload)
        logger.info("install stage=preflight status=OK")
        transaction = self.transaction_for(installation)
        transaction.recover_incomplete()
        self._existing_state = self._load_state(installation, "install")
        transaction.start("install")
        self._start_ownership()
        try:
            self.progress("Installing", "copying graphics driver")
            self.stage_driver_files(transaction, installation, payload)
            self.write_app_id(transaction, installation, "773790")
            self.progress("Installing", "configuring CrossOver (may take a minute)")
            self.set_native_override(transaction, installation)
            self.set_bottle_environment(transaction, installation)
            self.progress("Installing", "applying game settings")
            self.set_safe_graphics(transaction, installation)
            self.progress("Installing", "creating launcher")
            launcher_state = self.launcher.install(transaction, installation)
            self.progress("Installing", "verifying (may take a minute)")
            state = self.verify(installation, payload, launcher_state)
            self.write_install_state(transaction, installation, state)
            transaction.journal.commit()
            self._cleanup_completed_journal(transaction)
            logger.info("install complete bottle=%s", installation.bottle.name)
            return state
        except BaseException:
            logger.exception("install failed bottle=%s", installation.bottle.name)
            self.progress("Installing", "undoing incomplete changes")
            transaction.rollback()
            self._cleanup_completed_journal(transaction)
            raise

    def _snapshots(
        self, paths: Sequence[Path], *, include_identity: bool = False
    ) -> List[Dict[str, object]]:
        snapshots = []
        seen = set()
        for path in paths:
            path = path.resolve(strict=False)
            if path in seen:
                continue
            seen.add(path)
            if path.is_dir():
                for child in sorted(path.rglob("*")):
                    if child.is_file():
                        snapshots.extend(self._snapshots([child]))
                continue
            if path.is_file():
                data = path.read_bytes()
                snapshots.append(
                    {
                        "path": str(path),
                        "present": True,
                        "type": "file",
                        "content": base64.b64encode(data).decode("ascii"),
                        "sha256": _bytes_digest(data),
                        "mode": stat_mode(path),
                    }
                )
                if include_identity:
                    metadata = os.lstat(str(path))
                    if not stat.S_ISREG(metadata.st_mode):
                        raise OSError(
                            errno.EPERM,
                            "snapshot identity is not a regular file",
                            str(path),
                        )
                    snapshots[-1]["device"] = metadata.st_dev
                    snapshots[-1]["inode"] = metadata.st_ino
            else:
                snapshots.append(
                    {"path": str(path), "present": False, "type": "absent"}
                )
        return snapshots

    def _undo_restore_snapshots(
        self, installation: GameInstallation, record: UndoRecord
    ) -> None:
        snapshots = record.data.get("snapshots")
        if not isinstance(snapshots, list):
            return
        for snapshot in snapshots:
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("path"), str):
                continue
            path = Path(snapshot["path"])
            if not self._allowed(installation, path):
                continue
            settings = self._settings_path(installation)
            settings_family = path == settings or self._valid_backup_relationship(
                settings, path
            )
            present = snapshot.get("present") is True
            if present:
                try:
                    data = base64.b64decode(str(snapshot["content"]), validate=True)
                except (KeyError, ValueError):
                    continue
                expected = str(snapshot.get("sha256", ""))
                if _bytes_digest(data) != expected:
                    continue
                if settings_family:
                    try:
                        self._validate_settings_ancestry(
                            installation, "restore.settings_path", "Restore failed."
                        )
                        current, _current_mode = _read_relative_file(
                            installation.bottle.root.resolve(), path
                        )
                        current_digest = _bytes_digest(current)
                    except FileNotFoundError:
                        current_digest = None
                    except (OSError, PatchError):
                        continue
                    allowed = snapshot.get("allowed_current_sha256", [])
                    if current_digest not in (None, expected) and (
                        not isinstance(allowed, list)
                        or current_digest not in allowed
                    ):
                        continue
                    _atomic_write_relative(
                        installation.bottle.root.resolve(),
                        path,
                        data,
                        int(snapshot.get("mode", 0o644)),
                    )
                    continue
                if path.exists() and not _same_file(path, expected):
                    allowed = snapshot.get("allowed_current_sha256", [])
                    if not isinstance(allowed, list) or not any(
                        isinstance(item, str) and _same_file(path, item)
                        for item in allowed
                    ):
                        continue
                _atomic_write_bytes(path, data, int(snapshot.get("mode", 0o644)))
            elif path.is_file() and snapshot.get("remove_sha256"):
                if _same_file(path, str(snapshot["remove_sha256"])):
                    if settings_family:
                        try:
                            _unlink_relative(installation.bottle.root.resolve(), path)
                        except OSError:
                            pass
                    else:
                        _durable_unlink(path)

    def _undo_remove_path(
        self, installation: GameInstallation, record: UndoRecord
    ) -> None:
        path_text = record.data.get("path")
        if not isinstance(path_text, str) or record.data.get("owned_path") != path_text:
            return
        path = Path(path_text)
        if not self._allowed(installation, path):
            return
        expected = record.data.get("expected_sha256")
        if isinstance(expected, str) and _same_file(path, expected):
            _durable_unlink(path)
        if path.exists():
            return
        self._remove_empty_owned_directories(
            installation, record.data.get("owned_directories", [])
        )

    def _undo_remove_staging(
        self, installation: GameInstallation, record: UndoRecord
    ) -> None:
        path_text = record.data.get("path")
        directory_text = record.data.get("directory")
        cleanup_directory_text = record.data.get("cleanup_directory")
        destination_text = record.data.get("destination")
        directory_device = record.data.get("directory_device")
        directory_inode = record.data.get("directory_inode")
        device = record.data.get("device")
        inode = record.data.get("inode")
        if (
            not isinstance(path_text, str)
            or not isinstance(directory_text, str)
            or not isinstance(cleanup_directory_text, str)
            or not isinstance(destination_text, str)
            or not isinstance(directory_device, int)
            or not isinstance(directory_inode, int)
            or not isinstance(device, int)
            or not isinstance(inode, int)
        ):
            return
        path = Path(path_text)
        directory = Path(directory_text)
        cleanup_directory = Path(cleanup_directory_text)
        destination = Path(destination_text)
        lexical_directory = (
            directory.parent.resolve(strict=False) / directory.name
        )
        lexical_destination = (
            destination.parent.resolve(strict=False) / destination.name
        )
        lexical_cleanup = (
            cleanup_directory.parent.resolve(strict=False)
            / cleanup_directory.name
        )
        if (
            not path.is_absolute()
            or not directory.is_absolute()
            or not cleanup_directory.is_absolute()
            or not destination.is_absolute()
            or directory != lexical_directory
            or path != directory / "payload"
            or destination != lexical_destination
            or directory.parent != lexical_destination.parent
            or cleanup_directory != lexical_cleanup
            or cleanup_directory.parent != installation.bottle.root.resolve()
            or not self._allowed_atomic_copy_destination(
                installation, lexical_destination
            )
        ):
            return
        name_pattern = re.compile(
            r"^{}(?P<token>[0-9a-f]{{32}})$".format(
                re.escape(".{}.ostriv-macos-stage-".format(destination.name))
            )
        )
        name_match = name_pattern.fullmatch(directory.name)
        if (
            name_match is None
            or cleanup_directory.name
            != self._staging_cleanup_name(name_match.group("token"))
        ):
            return
        capture_name = self._staging_capture_name(name_match.group("token"))
        _cleanup_owned_staging(
            _CopyStaging(
                directory,
                cleanup_directory,
                path,
                capture_name,
                directory_device,
                directory_inode,
                device,
                inode,
            )
        )

    def _remove_empty_owned_directories(
        self, installation: GameInstallation, directories: object
    ) -> None:
        if not isinstance(directories, list):
            return
        allowed = self._allowed_settings_directories(installation)
        for directory_text in directories:
            if not isinstance(directory_text, str):
                continue
            directory = Path(directory_text).resolve(strict=False)
            if directory not in allowed:
                continue
            try:
                _durable_rmdir(directory)
            except (FileNotFoundError, OSError):
                pass

    def _undo_restore_file(
        self, installation: GameInstallation, record: UndoRecord
    ) -> None:
        if "snapshots" in record.data:
            self._undo_restore_snapshots(installation, record)
            return
        path_text = record.data.get("path")
        backup_text = record.data.get("backup_path")
        if (
            not isinstance(path_text, str)
            or not isinstance(backup_text, str)
            or record.data.get("owned_path") != path_text
        ):
            return
        path = Path(path_text)
        backup = Path(backup_text)
        if not self._allowed(installation, path) or not self._allowed(installation, backup):
            return
        original = str(record.data.get("original_sha256", ""))
        installed = str(record.data.get("installed_sha256", ""))
        if path == self._settings_path(installation):
            try:
                self._validate_settings_ancestry(
                    installation, "restore.settings_path", "Restore failed."
                )
                root = installation.bottle.root.resolve()
                backup_data, _backup_mode = _read_relative_file(root, backup)
                try:
                    current, _current_mode = _read_relative_file(root, path)
                except FileNotFoundError:
                    current = None
            except OSError:
                return
            if _bytes_digest(backup_data) != original:
                return
            if current is None or _bytes_digest(current) == installed:
                _replace_relative(root, backup, path)
            elif _bytes_digest(current) == original:
                _unlink_relative(root, backup)
            return
        if not _same_file(backup, original):
            return
        if not path.exists() or _same_file(path, installed):
            _durable_replace(backup, path)
        elif _same_file(path, original):
            _durable_unlink(backup)

    def _undo_restore_named(
        self, installation: GameInstallation, record: UndoRecord
    ) -> None:
        self._undo_restore_file(installation, record)

    def _undo_restore_registry(
        self, installation: GameInstallation, record: UndoRecord
    ) -> None:
        key = record.data.get("key")
        value = record.data.get("value")
        before = record.data.get("before")
        after = record.data.get("after")
        if not isinstance(key, str) or not isinstance(value, str):
            return
        registry = self._registry(installation)
        current = registry.query(key, value, "restore.registry")
        if current == before:
            return
        if current != after:
            return
        if before is None:
            registry.delete(key, value)
        elif isinstance(before, str):
            registry.set(key, value, before)

    def _restore_backup_action(
        self,
        installation: GameInstallation,
        item: Mapping[str, object],
    ) -> None:
        path = Path(str(item["path"]))
        backup = Path(str(item["backup_path"]))
        installed = str(item["installed_sha256"])
        original = str(item["original_sha256"])
        settings = self._settings_path(installation)
        if path == settings:
            self._validate_settings_ancestry(
                installation, "restore.settings_path", "Restore failed."
            )
            root = installation.bottle.root.resolve()
            try:
                current, _mode = _read_relative_file(root, path)
            except FileNotFoundError:
                current = None
            try:
                backup_data, _backup_mode = _read_relative_file(root, backup)
            except FileNotFoundError:
                backup_data = None
            if current is not None and _bytes_digest(current) == original and backup_data is None:
                return
            if backup_data is None or _bytes_digest(backup_data) != original:
                return
            _replace_relative(root, backup, path)
            return
        if _same_file(path, original) and not backup.exists():
            return
        if not _same_file(path, installed) or not _same_file(backup, original):
            return
        _durable_replace(backup, path)

    def _journal_restore_backup(
        self,
        transaction: Transaction,
        installation: GameInstallation,
        item: Mapping[str, object],
        kind: str = "restore_file",
    ) -> None:
        path = Path(str(item["path"]))
        backup = Path(str(item["backup_path"]))
        snapshots = self._snapshots([path, backup])
        for snapshot in snapshots:
            if snapshot.get("path") == str(path.resolve(strict=False)):
                snapshot["allowed_current_sha256"] = [
                    str(item["original_sha256"])
                ]
        transaction.step(
            "restore {}".format(path.name),
            UndoRecord(kind, {"snapshots": snapshots}),
            lambda: self._restore_backup_action(installation, item),
        )

    def _journal_remove_owned(
        self,
        transaction: Transaction,
        installation: GameInstallation,
        item: Mapping[str, object],
        unconditional: bool = False,
    ) -> None:
        path = Path(str(item["path"]))
        expected = str(item.get("sha256", ""))
        snapshots = self._snapshots([path])
        def remove_owned() -> None:
            if _same_file(path, expected) or (unconditional and path.is_file()):
                _durable_unlink(path)
                self._remove_empty_owned_directories(
                    installation, item.get("owned_directories", [])
                )

        transaction.step(
            "remove {}".format(path.name),
            UndoRecord("restore_file", {"snapshots": snapshots}),
            remove_owned,
        )

    @staticmethod
    def _launcher_paths(launcher_state: Mapping[str, object]) -> List[Path]:
        paths = []
        artifacts = launcher_state.get("artifacts", [])
        if isinstance(artifacts, list):
            for item in artifacts:
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    paths.append(Path(item["path"]))
        previous = launcher_state.get("previous_app")
        if isinstance(previous, str):
            paths.append(Path(previous))
        return paths

    def _launcher_restore_undo(
        self,
        installation: GameInstallation,
        launcher_state: Mapping[str, object],
    ) -> Dict[str, object]:
        try:
            restore_undo_data = getattr(self.launcher, "restore_undo_data", None)
            if callable(restore_undo_data):
                return {
                    "snapshots": [],
                    **restore_undo_data(installation, launcher_state),
                }
            return {
                "snapshots": self._snapshots(self._launcher_paths(launcher_state))
            }
        except PatchError:
            raise
        except (OSError, UnicodeError, ValueError) as error:
            raise PatchError(
                "restore.launcher_prepare",
                "Restore failed.",
                "Unable to prepare launcher rollback: {}: {}".format(
                    type(error).__name__, error
                ),
            ) from error

    def _legacy_launcher_restore_undo(
        self, installation: GameInstallation
    ) -> Optional[tuple[Dict[str, object], Dict[str, object]]]:
        legacy_app = self._launcher_app_path()
        legacy_runtime = installation.bottle.root.resolve() / "play-ostriv-patched.py"
        if not os.path.lexists(str(legacy_app)) and not os.path.lexists(
            str(legacy_runtime)
        ):
            return None
        state: Dict[str, object] = {
            "legacy": True,
            "artifacts": [
                {"path": str(legacy_app)},
                {"path": str(legacy_runtime)},
            ],
        }
        return state, self._launcher_restore_undo(installation, state)

    @staticmethod
    def _settings_restore_failure(
        state: InstallState, settings: Path
    ) -> Optional[str]:
        """Report how Restore failed settings.data, or None when it owed it nothing.

        An install that found the file already safe copied no backup and claimed
        no ownership, so its content after Restore is the player's business.
        """
        if (
            state.original_settings_backup is None
            and state.original_settings_digest == state.installed_settings_digest
        ):
            return None
        if state.original_settings_digest:
            if not _same_file(settings, state.original_settings_digest):
                return "game settings were not restored"
            return None
        if _same_file(settings, state.installed_settings_digest):
            return "installed game settings remain"
        return None

    def _verify_restored(
        self, installation: GameInstallation, state: InstallState
    ) -> None:
        failures = []
        backups = {str(item["path"]): item for item in state.backup_files}
        for item in state.owned_files:
            path = Path(str(item["path"]))
            backup = backups.get(str(path))
            if backup is not None:
                if not _same_file(path, str(backup["original_sha256"])):
                    failures.append("original file was not restored: {}".format(path))
            elif _same_file(path, str(item.get("sha256", ""))):
                failures.append("owned file remains: {}".format(path))
        registry_value = self._registry(installation).query(
            REGISTRY_KEY, REGISTRY_VALUE, "restore.registry"
        )
        if registry_value != state.prior_registry_value:
            failures.append("registry value was not restored")
        config = installation.bottle.root.resolve() / "cxbottle.conf"
        if not _same_file(config, state.original_config_digest):
            failures.append("bottle configuration was not restored")
        settings_failure = self._settings_restore_failure(
            state, self._settings_path(installation)
        )
        if settings_failure is not None:
            failures.append(settings_failure)
        for artifact in self._launcher_paths(state.launcher_artifacts):
            if artifact in {
                installation.bottle.root.resolve() / ".ostriv-launcher.lock",
                installation.bottle.root.resolve()
                / ".ostriv-profile-recovery.json",
            }:
                continue
            matching = next(
                (
                    item
                    for item in state.launcher_artifacts.get("artifacts", [])
                    if isinstance(item, dict) and item.get("path") == str(artifact)
                ),
                None,
            )
            if matching and _same_file(artifact, str(matching.get("sha256", ""))):
                failures.append("launcher artifact remains: {}".format(artifact))
        if failures:
            detail = "\n".join(failures)
            logger.error("restore.verify: %s", detail)
            raise PatchError("restore.verify", "Restore could not be verified.", detail)

    @staticmethod
    def _restore_registry_value(
        registry: WineRegistry, value: Optional[str]
    ) -> None:
        try:
            if value is None:
                registry.delete(REGISTRY_KEY, REGISTRY_VALUE)
            else:
                registry.set(REGISTRY_KEY, REGISTRY_VALUE, value)
        except PatchError as error:
            if error.code == "restore.registry":
                raise
            raise PatchError(
                "restore.registry", "Restore failed.", error.detail
            ) from error

    @staticmethod
    def _restore_recovery_failure(detail: str) -> PatchError:
        return PatchError(
            "restore.launcher_recovery",
            "Restore failed.",
            detail,
        )

    @staticmethod
    def _restore_lock_identity_components(
        device: object, inode: object
    ) -> Tuple[int, int]:
        """Return one concrete platform-safe device/inode identity."""
        if (
            type(device) is not int
            or not 0 <= device <= _RESTORE_LOCK_IDENTITY_MAX
            or type(inode) is not int
            or not 0 < inode <= _RESTORE_LOCK_IDENTITY_MAX
        ):
            raise ValueError("invalid restore lock identity")
        return device, inode

    @classmethod
    def _restore_lock_identity_integrity(
        cls,
        owner_token: object,
        lock_digest: object,
        device: object,
        inode: object,
        mode: object = 0o600,
    ) -> str:
        """Bind the pre-unlink lock inode to its authenticated ownership data."""
        device, inode = cls._restore_lock_identity_components(device, inode)
        if (
            type(owner_token) is not str
            or not owner_token
            or type(lock_digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", lock_digest) is None
            or type(mode) is not int
            or mode != 0o600
        ):
            raise ValueError("invalid restore lock identity integrity input")
        identity = json.dumps(
            [
                "ostriv-restore-lock-identity-v1",
                owner_token,
                lock_digest,
                device,
                inode,
                mode,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        return _bytes_digest(identity)

    @staticmethod
    def _recovery_filesystem_safe_text(value: str) -> bool:
        if "\0" in value or any(
            0xD800 <= ord(character) <= 0xDFFF for character in value
        ):
            return False
        try:
            encoded = os.fsencode(value)
            return os.fsdecode(encoded) == value
        except (UnicodeEncodeError, UnicodeDecodeError, ValueError):
            return False

    def _recovery_file_snapshot(
        self, value: object, location: str
    ) -> Dict[str, object]:
        failure = self._restore_recovery_failure
        if not isinstance(value, dict):
            raise failure("{} is not an object".format(location))
        snapshot = copy.deepcopy(value)
        path_text = snapshot.get("path")
        present = snapshot.get("present")
        snapshot_type = snapshot.get("type")
        if not isinstance(path_text, str) or not path_text:
            raise failure("{} path is invalid".format(location))
        try:
            path = Path(path_text)
            lexical = path.parent.resolve(strict=False) / path.name
        except (OSError, ValueError):
            path = Path(".")
            lexical = path
        if (
            not path.is_absolute()
            or not self._recovery_filesystem_safe_text(path_text)
            or path_text != str(lexical)
        ):
            raise failure("{} path is not lexical".format(location))
        if type(present) is not bool:
            raise failure("{} presence flag is invalid".format(location))
        if present and snapshot_type == "file":
            allowed = {
                "path",
                "present",
                "type",
                "content",
                "sha256",
                "mode",
                "allowed_current_sha256",
                "device",
                "inode",
                "identity_integrity",
            }
            required = {"path", "present", "type", "content", "sha256", "mode"}
            if not required.issubset(snapshot) or not set(snapshot).issubset(allowed):
                raise failure("{} file fields are invalid".format(location))
            content = snapshot.get("content")
            digest = snapshot.get("sha256")
            mode = snapshot.get("mode")
            if (
                not isinstance(content, str)
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or type(mode) is not int
                or not 0 <= mode <= 0o7777
            ):
                raise failure("{} file metadata is invalid".format(location))
            try:
                data = base64.b64decode(content, validate=True)
            except (TypeError, ValueError) as error:
                raise failure("{} file content is invalid".format(location)) from error
            if _bytes_digest(data) != digest:
                raise failure("{} file digest is invalid".format(location))
            allowed_current = snapshot.get("allowed_current_sha256", [])
            if not isinstance(allowed_current, list) or any(
                not isinstance(item, str)
                or re.fullmatch(r"[0-9a-f]{64}", item) is None
                for item in allowed_current
            ):
                raise failure("{} current-digest list is invalid".format(location))
            identity_fields = {
                name
                for name in ("device", "inode", "identity_integrity")
                if name in snapshot
            }
            if identity_fields and identity_fields != {
                "device",
                "inode",
                "identity_integrity",
            }:
                raise failure("{} identity is incomplete".format(location))
            if identity_fields and (
                not isinstance(snapshot["identity_integrity"], str)
                or re.fullmatch(
                    r"[0-9a-f]{64}", snapshot["identity_integrity"]
                )
                is None
            ):
                raise failure("{} identity integrity is invalid".format(location))
            if identity_fields:
                try:
                    self._restore_lock_identity_components(
                        snapshot["device"], snapshot["inode"]
                    )
                except ValueError as error:
                    raise failure(
                        "{} identity is invalid".format(location)
                    ) from error
            return snapshot
        if present and snapshot_type == "symlink":
            target = snapshot.get("target")
            if (
                set(snapshot) != {"path", "present", "type", "target"}
                or not isinstance(target, str)
                or not target
                or not self._recovery_filesystem_safe_text(target)
            ):
                raise failure("{} symlink fields are invalid".format(location))
            return snapshot
        if not present and snapshot_type == "absent":
            if not set(snapshot).issubset(
                {"path", "present", "type", "remove_sha256"}
            ):
                raise failure("{} absent fields are invalid".format(location))
            remove_digest = snapshot.get("remove_sha256")
            if remove_digest is not None and (
                not isinstance(remove_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", remove_digest) is None
            ):
                raise failure("{} removal digest is invalid".format(location))
            return snapshot
        raise failure("{} representation is invalid".format(location))

    def _recovery_inventory(
        self,
        value: object,
        location: str,
        *,
        captured: bool,
        present: bool = True,
    ) -> List[Dict[str, object]]:
        failure = self._restore_recovery_failure

        if not isinstance(value, list):
            raise failure("{} is not a list".format(location))
        if not present:
            if value:
                raise failure("{} has entries for an absent tree".format(location))
            return []
        entries: List[Dict[str, object]] = []
        seen = set()
        for entry_index, raw in enumerate(value):
            entry_location = "{}[{}]".format(location, entry_index)
            if not isinstance(raw, dict):
                raise failure("{} is not an object".format(entry_location))
            entry = copy.deepcopy(raw)
            relative_text = entry.get("relative_path")
            if not isinstance(relative_text, str):
                raise failure("{} relative path is invalid".format(entry_location))
            relative = PurePosixPath(relative_text)
            if (
                not relative_text
                or not self._recovery_filesystem_safe_text(relative_text)
                or any(
                    not self._recovery_filesystem_safe_text(part)
                    for part in relative.parts
                )
                or relative.is_absolute()
                or ".." in relative.parts
                or relative_text != relative.as_posix()
                or relative in seen
            ):
                raise failure("{} relative path is unsafe".format(entry_location))
            seen.add(relative)
            item_type = entry.get("type")
            if item_type == "directory":
                expected = {"relative_path", "type", "mode"}
            elif item_type == "file":
                expected = {"relative_path", "type", "sha256", "mode"}
                if captured:
                    expected.add("content")
            elif item_type == "symlink":
                expected = {"relative_path", "type", "target"}
            else:
                raise failure("{} type is invalid".format(entry_location))
            if set(entry) != expected:
                raise failure("{} fields are invalid".format(entry_location))
            if item_type in ("directory", "file"):
                mode = entry.get("mode")
                if type(mode) is not int or not 0 <= mode <= 0o7777:
                    raise failure("{} mode is invalid".format(entry_location))
            if item_type == "file":
                digest = entry.get("sha256")
                if (
                    not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                ):
                    raise failure("{} digest is invalid".format(entry_location))
                if captured:
                    content = entry.get("content")
                    try:
                        data = base64.b64decode(content, validate=True)
                    except (TypeError, ValueError) as error:
                        raise failure(
                            "{} content is invalid".format(entry_location)
                        ) from error
                    if _bytes_digest(data) != digest:
                        raise failure("{} digest does not match".format(entry_location))
            if item_type == "symlink":
                target = entry.get("target")
                if (
                    not isinstance(target, str)
                    or not target
                    or not self._recovery_filesystem_safe_text(target)
                ):
                    raise failure("{} target is invalid".format(entry_location))
            entries.append(entry)
        if present:
            roots = [
                item
                for item in entries
                if item.get("relative_path") == "."
                and item.get("type") == "directory"
            ]
            if len(roots) != 1:
                raise failure("{} has no exact directory root".format(location))
            by_path = {
                PurePosixPath(str(item["relative_path"])): item for item in entries
            }
            for relative in by_path:
                if relative == PurePosixPath("."):
                    continue
                parent = relative.parent
                while parent != PurePosixPath("."):
                    parent_entry = by_path.get(parent)
                    if parent_entry is None or parent_entry.get("type") != "directory":
                        raise failure(
                            "{} has a missing parent directory".format(location)
                        )
                    parent = parent.parent
        return entries

    def _recovery_tree_snapshot(
        self, value: object, location: str
    ) -> Dict[str, object]:
        failure = self._restore_recovery_failure
        if not isinstance(value, dict) or set(value) != {
            "root",
            "present",
            "entries",
        }:
            raise failure("{} fields are invalid".format(location))
        root = value.get("root")
        present = value.get("present")
        if (
            not isinstance(root, str)
            or not root
            or not Path(root).is_absolute()
            or type(present) is not bool
        ):
            raise failure("{} root is invalid".format(location))
        return {
            "root": root,
            "present": present,
            "entries": self._recovery_inventory(
                value.get("entries"),
                "{}.entries".format(location),
                captured=True,
                present=present,
            ),
        }

    def _validate_recovery_record_paths(
        self,
        installation: GameInstallation,
        index: int,
        name: str,
        record: UndoRecord,
    ) -> None:
        """Validate the complete path grammar for one Restore undo record."""
        failure = self._restore_recovery_failure
        if record.kind in ("restore_file", "restore_config", "restore_settings"):
            config = installation.bottle.root.resolve() / "cxbottle.conf"
            settings = self._settings_path(installation)
            if record.kind == "restore_config":
                targets = {config}
            elif record.kind == "restore_settings":
                targets = {settings}
            else:
                targets = self._allowed_owned_targets(installation) | {
                    self.state_path(installation),
                    *(installation.game_dir.resolve() / item for item in DIAGNOSTIC_LOGS),
                }
            for snapshot in record.data["snapshots"]:
                path = Path(str(snapshot["path"]))
                if snapshot.get("type") not in ("file", "absent") or not (
                    path in targets
                    or any(
                        self._valid_backup_relationship(target, path)
                        for target in targets | {config, settings}
                    )
                ):
                    raise failure(
                        "record {} {} path is outside its Restore schema".format(
                            index, record.kind
                        )
                    )
            return
        if record.kind != "restore_launcher":
            return

        data = record.data
        lock = installation.bottle.root.resolve() / ".ostriv-launcher.lock"
        if name == "remove launcher recovery lock":
            if set(data) != {"snapshots"} or len(data["snapshots"]) != 1:
                raise failure("Final launcher-lock undo shape is invalid")
            if data["snapshots"][0].get("path") != str(lock):
                raise failure("Final launcher-lock undo path is invalid")
            return
        if name not in ("restore launcher", "restore legacy launcher"):
            raise failure("record {} launcher handler identity is invalid".format(index))

        allowed_snapshot_paths = {
            str(path) for path in self._allowed_launcher_artifacts(installation)
        }
        if any(
            snapshot.get("path") not in allowed_snapshot_paths
            for snapshot in data["snapshots"]
        ):
            raise failure("record {} launcher snapshot path is invalid".format(index))
        extended = set(data) != {"snapshots"}
        if not extended:
            return
        expected_fields = {
            "snapshots",
            "restore_files",
            "restore_trees",
            "recreate_menu",
        }
        if name == "restore legacy launcher":
            expected_fields.add("legacy_menu")
        if set(data) != expected_fields or data["snapshots"]:
            raise failure("record {} launcher undo shape is invalid".format(index))

        app = self._launcher_app_path()
        previous = app.with_name("." + app.name + ".ostriv-macos.previous")
        expected_roots = (
            [str(app)]
            if name == "restore legacy launcher"
            else [str(app), str(previous)]
        )
        roots = [str(tree["root"]) for tree in data["restore_trees"]]
        if roots != expected_roots:
            raise failure("record {} launcher tree roots are invalid".format(index))
        bottle = installation.bottle.root.resolve()
        expected_files = (
            [str(bottle / "play-ostriv-patched.py")]
            if name == "restore legacy launcher"
            else [
                str(bottle / "play-ostriv-patched.py"),
                str(bottle / "launcher-config.json"),
                str(lock),
                str(bottle / ".ostriv-profile-recovery.json"),
            ]
        )
        files = [str(snapshot["path"]) for snapshot in data["restore_files"]]
        if files != expected_files:
            raise failure("record {} launcher file paths are invalid".format(index))
        if name == "restore legacy launcher" and (
            data.get("legacy_menu") is not True
            or data.get("recreate_menu") is not True
        ):
            raise failure("record {} legacy launcher flags are invalid".format(index))

    def _recovery_undo_record(
        self,
        transaction: Transaction,
        index: int,
        item: object,
    ) -> UndoRecord:
        failure = self._restore_recovery_failure
        if not isinstance(item, dict) or set(item) != {
            "name",
            "status",
            "undo",
        }:
            raise failure("record {} fields are invalid".format(index))
        name = item.get("name")
        undo = item.get("undo")
        if not isinstance(name, str) or not isinstance(undo, dict) or set(undo) != {
            "kind",
            "data",
        }:
            raise failure("record {} undo is invalid".format(index))
        kind = undo.get("kind")
        data = undo.get("data")
        if (
            not isinstance(kind, str)
            or kind not in {
                "restore_file",
                "restore_config",
                "restore_settings",
                "restore_registry",
                "restore_launcher",
            }
            or not callable(transaction.handlers.get(kind))
            or not isinstance(data, dict)
        ):
            raise failure("record {} handler is invalid".format(index))
        location = "record {} {}".format(index, kind)
        if kind in ("restore_file", "restore_config", "restore_settings"):
            if set(data) != {"snapshots"} or not isinstance(data["snapshots"], list):
                raise failure("{} data fields are invalid".format(location))
            return UndoRecord(
                kind,
                {
                    "snapshots": [
                        self._recovery_file_snapshot(
                            snapshot,
                            "{}.snapshots[{}]".format(location, snapshot_index),
                        )
                        for snapshot_index, snapshot in enumerate(data["snapshots"])
                    ]
                },
            )
        if kind == "restore_registry":
            if set(data) != {"key", "value", "before", "after"}:
                raise failure("{} data fields are invalid".format(location))
            if (
                data.get("key") != REGISTRY_KEY
                or data.get("value") != REGISTRY_VALUE
                or any(
                    value is not None and not isinstance(value, str)
                    for value in (data.get("before"), data.get("after"))
                )
            ):
                raise failure("{} registry identity is invalid".format(location))
            return UndoRecord(kind, copy.deepcopy(data))

        allowed = {
            "snapshots",
            "restore_files",
            "restore_trees",
            "recreate_menu",
            "legacy_menu",
        }
        if "snapshots" not in data or not set(data).issubset(allowed):
            raise failure("{} data fields are invalid".format(location))
        sanitized: Dict[str, object] = {}
        for field in ("snapshots", "restore_files"):
            if field not in data:
                continue
            if not isinstance(data[field], list):
                raise failure("{}.{} is not a list".format(location, field))
            sanitized[field] = [
                self._recovery_file_snapshot(
                    snapshot,
                    "{}.{}[{}]".format(location, field, snapshot_index),
                )
                for snapshot_index, snapshot in enumerate(data[field])
            ]
        if "restore_trees" in data:
            if not isinstance(data["restore_trees"], list):
                raise failure("{}.restore_trees is not a list".format(location))
            sanitized["restore_trees"] = [
                self._recovery_tree_snapshot(
                    tree,
                    "{}.restore_trees[{}]".format(location, tree_index),
                )
                for tree_index, tree in enumerate(data["restore_trees"])
            ]
        for field in ("recreate_menu", "legacy_menu"):
            if field in data:
                if type(data[field]) is not bool:
                    raise failure("{}.{} is invalid".format(location, field))
                sanitized[field] = data[field]
        if sanitized.get("legacy_menu") is True and sanitized.get("recreate_menu") is not True:
            raise failure("{}.legacy_menu has no menu recreation".format(location))
        return UndoRecord(kind, sanitized)

    def _recovery_path_locations(
        self, index: int, name: str, record: UndoRecord
    ):
        data = record.data
        for field in ("snapshots", "restore_files"):
            for snapshot_index, snapshot in enumerate(data.get(field, [])):
                location = "record {} {}[{}]".format(index, field, snapshot_index)
                path_text = snapshot["path"]
                yield path_text, None, location, field, snapshot_index
                if snapshot.get("type") == "symlink":
                    yield (
                        snapshot["target"],
                        Path(path_text).parent,
                        location + ".target",
                        None,
                        None,
                    )
        for tree_index, tree in enumerate(data.get("restore_trees", [])):
            root_text = tree["root"]
            tree_location = "record {} restore_trees[{}]".format(index, tree_index)
            yield root_text, None, tree_location + ".root", None, None
            for entry_index, entry in enumerate(tree["entries"]):
                relative = Path(entry["relative_path"])
                entry_path = Path(root_text) if relative == Path(".") else Path(root_text) / relative
                entry_location = "{}.entries[{}]".format(tree_location, entry_index)
                yield str(entry_path), None, entry_location, None, None
                if entry.get("type") == "symlink":
                    yield entry["target"], entry_path.parent, entry_location + ".target", None, None

    @staticmethod
    def _recovery_regular_file(
        path: Path, maximum_size: int
    ) -> Optional[Tuple[bytes, int]]:
        """Return stable single-link no-follow contents and mode for one leaf."""
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if not no_follow:
            return None
        descriptor = None
        try:
            before = os.lstat(str(path))
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                return None
            descriptor = os.open(
                str(path),
                os.O_RDONLY
                | no_follow
                | getattr(os, "O_CLOEXEC", 0),
            )
            opened = os.fstat(descriptor)
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or identity != (before.st_dev, before.st_ino)
            ):
                return None
            data = b""
            while len(data) <= maximum_size:
                chunk = os.read(descriptor, maximum_size + 1 - len(data))
                if not chunk:
                    break
                data += chunk
            after = os.fstat(descriptor)
            current = os.lstat(str(path))
            modes = {
                stat.S_IMODE(item.st_mode)
                for item in (before, opened, after, current)
            }
            if (
                not stat.S_ISREG(current.st_mode)
                or (after.st_dev, after.st_ino) != identity
                or (current.st_dev, current.st_ino) != identity
                or after.st_nlink != 1
                or current.st_nlink != 1
                or len(modes) != 1
            ):
                return None
            return data, modes.pop()
        except (OSError, ValueError):
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _recovery_protected_lock_content(
        candidate: Path,
        expected_data: Optional[bytes],
        expected_digest: Optional[str],
    ) -> Optional[bool]:
        """Classify exact lock content without granting mutation ownership."""
        if expected_data is None or expected_digest is None:
            return False
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if not no_follow:
            return None
        descriptor = None
        try:
            before = os.lstat(str(candidate))
            if not stat.S_ISREG(before.st_mode):
                return False
            descriptor = os.open(
                str(candidate),
                os.O_RDONLY
                | no_follow
                | getattr(os, "O_CLOEXEC", 0),
            )
            opened = os.fstat(descriptor)
            identity = (opened.st_dev, opened.st_ino)
            if (
                not stat.S_ISREG(opened.st_mode)
                or identity != (before.st_dev, before.st_ino)
            ):
                return None
            maximum_size = len(expected_data)
            data = b""
            while len(data) <= maximum_size:
                chunk = os.read(
                    descriptor, maximum_size + 1 - len(data)
                )
                if not chunk:
                    break
                data += chunk
            after = os.fstat(descriptor)
            current = os.lstat(str(candidate))
            statuses = (before, opened, after, current)
            modes = {stat.S_IMODE(item.st_mode) for item in statuses}
            link_counts = {item.st_nlink for item in statuses}
            sizes = {item.st_size for item in statuses}
            if (
                not stat.S_ISREG(after.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or any(
                    (item.st_dev, item.st_ino) != identity
                    for item in (after, current)
                )
                or len(modes) != 1
                or len(link_counts) != 1
                or next(iter(link_counts)) < 1
                or len(sizes) != 1
            ):
                return None
            size = next(iter(sizes))
            if size > maximum_size:
                return False
            if len(data) != size:
                return None
            return (
                modes.pop() == 0o600
                and data == expected_data
                and _bytes_digest(data) == expected_digest
            )
        except (OSError, ValueError):
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @classmethod
    def _recovery_path_relation(
        cls,
        path_text: object,
        lock: Path,
        old_identity: Optional[Tuple[int, int]],
        base: Optional[Path] = None,
        expected_data: Optional[bytes] = None,
        expected_digest: Optional[str] = None,
    ) -> Optional[str]:
        if not isinstance(path_text, str):
            return None
        candidate = Path(path_text)
        if base is not None and not candidate.is_absolute():
            candidate = base / candidate
        if path_text == str(lock):
            return "exact"
        try:
            if candidate.resolve(strict=False) == lock:
                return "alias"
        except (OSError, RuntimeError, ValueError):
            return "unsafe"
        try:
            status = os.lstat(str(candidate))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return "unsafe"
        identities = []
        if old_identity is not None:
            identities.append(old_identity)
        try:
            lock_status = os.lstat(str(lock))
        except FileNotFoundError:
            lock_status = None
        except (OSError, ValueError):
            return "unsafe"
        if lock_status is not None:
            identities.append((lock_status.st_dev, lock_status.st_ino))
        if stat.S_ISREG(status.st_mode) and (status.st_dev, status.st_ino) in identities:
            return "alias"
        protected_content = cls._recovery_protected_lock_content(
            candidate, expected_data, expected_digest
        )
        if protected_content is None:
            return "unsafe"
        if protected_content:
            return "alias"
        return None

    @classmethod
    def _recovery_snapshot_matches_live(
        cls, snapshot: Mapping[str, object]
    ) -> bool:
        """Authenticate a captured leaf against the current no-follow leaf."""
        path = Path(str(snapshot.get("path", "")))
        snapshot_type = snapshot.get("type")
        if snapshot_type == "absent":
            return not os.path.lexists(str(path))
        try:
            before = os.lstat(str(path))
        except (OSError, ValueError):
            return False
        if snapshot_type == "symlink":
            try:
                return stat.S_ISLNK(before.st_mode) and os.readlink(
                    path
                ) == snapshot.get("target")
            except (OSError, ValueError):
                return False
        if snapshot_type != "file" or not stat.S_ISREG(before.st_mode):
            return False
        try:
            expected = base64.b64decode(snapshot.get("content"), validate=True)
        except (TypeError, ValueError):
            return False
        observed = cls._recovery_regular_file(path, len(expected))
        return (
            observed is not None
            and observed[1] == snapshot.get("mode")
            and observed[0] == expected
            and _bytes_digest(observed[0]) == snapshot.get("sha256")
        )

    def _recovery_snapshot_has_semantic_content(
        self,
        snapshot: Mapping[str, object],
        expected_digests: Sequence[str],
    ) -> bool:
        return (
            snapshot.get("type") == "file"
            and snapshot.get("sha256") in expected_digests
        ) or self._recovery_snapshot_matches_live(snapshot)

    def _expected_restore_recovery_records(
        self,
        installation: GameInstallation,
        state: InstallState,
    ) -> List[Tuple[str, str, str, object]]:
        """Describe the only state-consistent forward Restore journal prefix."""
        expected: List[Tuple[str, str, str, object]] = [
            ("restore launcher", "restore_launcher", "launcher", None)
        ]
        handled = set()
        config = installation.bottle.root.resolve() / "cxbottle.conf"
        settings = self._settings_path(installation)
        for item in reversed(state.backup_files):
            path = Path(str(item["path"]))
            kind = "restore_file"
            if path == config:
                kind = "restore_config"
            elif path == settings:
                kind = "restore_settings"
            expected.append(
                ("restore {}".format(path.name), kind, "backup", item)
            )
            handled.add(str(path))
        if state.original_config_backup and str(config) not in handled:
            expected.append(
                (
                    "restore {}".format(config.name),
                    "restore_config",
                    "backup",
                    {
                        "path": str(config),
                        "backup_path": state.original_config_backup,
                        "installed_sha256": state.installed_config_digest,
                        "original_sha256": state.original_config_digest,
                    },
                )
            )
            handled.add(str(config))
        if state.original_settings_backup and str(settings) not in handled:
            expected.append(
                (
                    "restore {}".format(settings.name),
                    "restore_settings",
                    "backup",
                    {
                        "path": str(settings),
                        "backup_path": state.original_settings_backup,
                        "installed_sha256": state.installed_settings_digest,
                        "original_sha256": state.original_settings_digest,
                    },
                )
            )
            handled.add(str(settings))
        if state.prior_registry_value != REGISTRY_DATA:
            expected.append(
                (
                    "restore registry override",
                    "restore_registry",
                    "registry",
                    None,
                )
            )
        for item in reversed(state.owned_files):
            path = Path(str(item["path"]))
            if str(path) not in handled:
                expected.append(
                    (
                        "remove {}".format(path.name),
                        "restore_file",
                        "owned",
                        item,
                    )
                )
        if callable(getattr(self.launcher, "finalize_restore", None)):
            expected.append(
                (
                    "remove launcher recovery lock",
                    "restore_launcher",
                    "final_lock",
                    None,
                )
            )
        expected.append(
            (
                "remove ownership state",
                "restore_file",
                "state",
                None,
            )
        )
        return expected

    def _validate_recovery_backup_semantics(
        self,
        record: UndoRecord,
        item: Mapping[str, object],
        location: str,
    ) -> None:
        failure = self._restore_recovery_failure
        snapshots = record.data["snapshots"]
        path = str(item["path"])
        backup = str(item["backup_path"])
        installed = str(item["installed_sha256"])
        original = str(item["original_sha256"])
        if len(snapshots) != 2 or [
            snapshot.get("path") for snapshot in snapshots
        ] != [path, backup]:
            raise failure("{} backup snapshot identity is invalid".format(location))
        target, saved = snapshots
        if target.get("allowed_current_sha256") != [original] or not (
            self._recovery_snapshot_has_semantic_content(
                target, (installed, original)
            )
        ):
            raise failure("{} target snapshot is state-inconsistent".format(location))
        if "allowed_current_sha256" in saved or not (
            self._recovery_snapshot_has_semantic_content(saved, (original,))
        ):
            raise failure("{} backup snapshot is state-inconsistent".format(location))

    def _validate_recovery_owned_semantics(
        self,
        record: UndoRecord,
        item: Mapping[str, object],
        location: str,
    ) -> None:
        failure = self._restore_recovery_failure
        snapshots = record.data["snapshots"]
        if len(snapshots) != 1 or snapshots[0].get("path") != str(item["path"]):
            raise failure("{} owned snapshot identity is invalid".format(location))
        snapshot = snapshots[0]
        if snapshot.get("type") == "absent":
            if set(snapshot) != {"path", "present", "type"} or not (
                self._recovery_snapshot_matches_live(snapshot)
            ):
                raise failure("{} absent owned snapshot is invalid".format(location))
            return
        if any(
            name in snapshot
            for name in (
                "allowed_current_sha256",
                "device",
                "inode",
                "identity_integrity",
            )
        ) or not self._recovery_snapshot_has_semantic_content(
            snapshot, (str(item["sha256"]),)
        ):
            raise failure("{} owned snapshot is state-inconsistent".format(location))

    @staticmethod
    def _recovery_inventory_without_content(
        entries: Sequence[Mapping[str, object]],
    ) -> List[Dict[str, object]]:
        normalized = []
        for entry in entries:
            item = copy.deepcopy(dict(entry))
            item.pop("content", None)
            normalized.append(item)
        return normalized

    def _validate_recovery_launcher_semantics(
        self,
        installation: GameInstallation,
        state: InstallState,
        record: UndoRecord,
        location: str,
    ) -> None:
        failure = self._restore_recovery_failure
        data = record.data
        launcher_state = state.launcher_artifacts
        if data.get("snapshots") != [] or data.get("recreate_menu") is not True:
            raise failure("{} launcher flags are state-inconsistent".format(location))
        trees = data.get("restore_trees", [])
        files = data.get("restore_files", [])
        if len(trees) != 2 or len(files) != 4:
            raise failure("{} launcher cardinality is invalid".format(location))
        app_tree, previous_tree = trees
        app_inventory = self._recovery_inventory_without_content(
            app_tree["entries"]
        )
        if (
            app_tree.get("root") != launcher_state["app"]
            or app_tree.get("present") is not True
            or app_inventory != launcher_state["app_inventory"]
        ):
            raise failure("{} launcher app snapshot is state-inconsistent".format(location))
        expected_previous = launcher_state.get("previous_app_inventory", [])
        previous_path = self._launcher_app_path().with_name(
            "." + self._launcher_app_path().name + ".ostriv-macos.previous"
        )
        if (
            previous_tree.get("root") != str(previous_path)
            or previous_tree.get("present") is not bool(expected_previous)
            or self._recovery_inventory_without_content(previous_tree["entries"])
            != expected_previous
        ):
            raise failure(
                "{} previous launcher snapshot is state-inconsistent".format(
                    location
                )
            )
        runtime, config, lock, marker = files
        previous_runtime = launcher_state.get("previous_runtime")
        previous_config = launcher_state.get("previous_config")
        runtime_digests = [str(launcher_state["runtime_sha256"])]
        config_digests = [str(launcher_state["config_sha256"])]
        if isinstance(previous_runtime, dict):
            runtime_digests.append(str(previous_runtime.get("sha256", "")))
        if isinstance(previous_config, dict):
            config_digests.append(str(previous_config.get("sha256", "")))
        if not self._recovery_snapshot_has_semantic_content(
            runtime, tuple(runtime_digests)
        ):
            raise failure("{} launcher runtime snapshot is state-inconsistent".format(location))
        if not self._recovery_snapshot_has_semantic_content(
            config, tuple(config_digests)
        ):
            raise failure("{} launcher config snapshot is state-inconsistent".format(location))
        expected_lock_data = (
            str(launcher_state["profile_owner_token"]) + "\n"
        ).encode("ascii")
        if (
            lock.get("present") is not True
            or lock.get("type") != "file"
            or lock.get("sha256") != launcher_state["lock_sha256"]
            or lock.get("mode") != 0o600
            or base64.b64decode(lock.get("content"), validate=True)
            != expected_lock_data
        ):
            raise failure("{} launcher lock snapshot is state-inconsistent".format(location))
        if set(marker) != {"path", "present", "type"} or (
            marker.get("present") is not False
            or marker.get("type") != "absent"
        ):
            raise failure("{} launcher marker snapshot is state-inconsistent".format(location))

    def _reconcile_restore_recovery_records(
        self,
        installation: GameInstallation,
        records: Sequence[Mapping[str, object]],
        parsed: Sequence[Tuple[int, str, UndoRecord]],
        state: InstallState,
        *,
        validate_launcher: bool,
    ) -> None:
        """Reject state-inconsistent Restore undo data before any replay mutation."""
        failure = self._restore_recovery_failure
        expected = self._expected_restore_recovery_records(installation, state)
        if len(records) > len(expected):
            raise failure("Restore journal has extra records")
        rolled_back = False
        pending_indexes = []
        active_indexes = []
        for index, item in enumerate(records):
            status = item.get("status")
            if status == "rolled_back":
                rolled_back = True
            else:
                if rolled_back:
                    raise failure("Restore journal active records are not a prefix")
                active_indexes.append(index)
                if status == "pending":
                    pending_indexes.append(index)
            expected_name, expected_kind, _semantic, _payload = expected[index]
            undo = item.get("undo")
            if (
                item.get("name") != expected_name
                or not isinstance(undo, dict)
                or undo.get("kind") != expected_kind
            ):
                raise failure("Restore journal topology differs at record {}".format(index))
        if len(pending_indexes) > 1 or (
            pending_indexes and pending_indexes[-1] != active_indexes[-1]
        ):
            raise failure("Restore journal pending-record topology is invalid")
        parsed_by_index = {index: record for index, _name, record in parsed}
        if set(parsed_by_index) != set(active_indexes):
            raise failure("Restore recovery plan does not cover every active record")
        for index in active_indexes:
            _name, _kind, semantic, payload = expected[index]
            record = parsed_by_index[index]
            location = "record {} {}".format(index, semantic)
            if semantic == "registry":
                expected_data = {
                    "key": REGISTRY_KEY,
                    "value": REGISTRY_VALUE,
                    "before": REGISTRY_DATA,
                    "after": state.prior_registry_value,
                }
                if record.data != expected_data:
                    raise failure(
                        "{} rollback transition is state-inconsistent".format(
                            location
                        )
                    )
            elif semantic == "backup":
                self._validate_recovery_backup_semantics(
                    record, payload, location
                )
            elif semantic == "owned":
                self._validate_recovery_owned_semantics(
                    record, payload, location
                )
            elif semantic == "launcher" and validate_launcher:
                self._validate_recovery_launcher_semantics(
                    installation, state, record, location
                )

    def _build_restore_recovery_plan(
        self,
        installation: GameInstallation,
        transaction: Transaction,
    ) -> _RestoreRecoveryPlan:
        failure = self._restore_recovery_failure
        journal = transaction.journal.data
        if (
            set(journal) != {"schema", "operation", "complete", "records"}
            or type(journal.get("schema")) is not int
            or journal.get("schema") != JOURNAL_SCHEMA
            or journal.get("complete") is not False
            or journal.get("operation") != "restore"
        ):
            raise failure("Recovery journal is not an incomplete Restore")
        records = journal.get("records")
        if not isinstance(records, list):
            raise failure("Recovery journal records are invalid")
        parsed: List[Tuple[int, str, UndoRecord]] = []
        for index, item in enumerate(records):
            if not isinstance(item, dict):
                raise failure("record {} is invalid".format(index))
            if item.get("status") not in ("pending", "applied"):
                continue
            name = str(item.get("name", ""))
            record = self._recovery_undo_record(transaction, index, item)
            self._validate_recovery_record_paths(
                installation, index, name, record
            )
            parsed.append((index, name, record))

        state_path = self.state_path(installation)
        state_text = str(state_path)
        state_candidates = []
        for index, name, record in parsed:
            for path_text, _base, location, field, snapshot_index in self._recovery_path_locations(
                index, name, record
            ):
                if path_text == state_text:
                    if field != "snapshots" or record.kind != "restore_file":
                        raise failure("Ownership state appears in {}".format(location))
                    snapshot = record.data["snapshots"][int(snapshot_index)]
                    state_candidates.append((index, name, snapshot))
                elif isinstance(path_text, str):
                    try:
                        aliases_state = Path(path_text).resolve(strict=False) == state_path
                    except (OSError, RuntimeError, ValueError):
                        aliases_state = False
                    if aliases_state:
                        raise failure("Ownership state path is not lexical")
        if len(state_candidates) > 1:
            raise failure("Ownership state journal snapshot is ambiguous")
        live_state, live_bytes, live_mode = self._load_state_with_bytes(
            installation, "restore"
        )
        recovered_state = live_state
        if state_candidates:
            _index, state_name, snapshot = state_candidates[0]
            if state_name != "remove ownership state":
                raise failure("Ownership state snapshot is in an unexpected record")
            if (
                snapshot.get("present") is not True
                or snapshot.get("type") != "file"
                or snapshot.get("mode") != 0o600
                or set(snapshot)
                != {"path", "present", "type", "content", "sha256", "mode"}
            ):
                raise failure("Ownership state snapshot is invalid")
            state_bytes = base64.b64decode(snapshot["content"], validate=True)
            if _bytes_digest(state_bytes) != snapshot["sha256"]:
                raise failure("Ownership state snapshot digest is invalid")
            try:
                snapshot_state = self._decode_state_bytes(
                    installation, state_bytes, "restore", state_path
                )
            except PatchError as error:
                raise failure("Ownership state snapshot schema is invalid: {}".format(error.detail)) from error
            if live_state is None:
                recovered_state = snapshot_state
            elif live_bytes != state_bytes or live_mode != snapshot["mode"]:
                raise failure("Live ownership state does not match its journal snapshot")
        prepare_restore = getattr(self.launcher, "prepare_restore", None)
        protected_state = recovered_state is not None and callable(prepare_restore)
        if protected_state:
            validator = getattr(self.launcher, "validate_recovery_state", None)
            if not callable(validator):
                raise failure("Launcher recovery-state validator is unavailable")
            try:
                validator(installation, recovered_state.launcher_artifacts)
            except (PatchError, TypeError, ValueError) as error:
                raise failure("Launcher recovery state is invalid: {}".format(error)) from error
        if recovered_state is not None:
            self._reconcile_restore_recovery_records(
                installation,
                records,
                parsed,
                recovered_state,
                validate_launcher=protected_state,
            )
        elif any(
            item.get("name")
            in {
                "restore launcher",
                "restore registry override",
                "remove launcher recovery lock",
                "remove ownership state",
            }
            for item in records
            if isinstance(item, dict)
        ):
            raise failure("Restore ownership state is unavailable")

        lock = installation.bottle.root.resolve() / ".ostriv-launcher.lock"
        expected_data = None
        expected_digest = None
        if protected_state:
            launcher_state = recovered_state.launcher_artifacts
            owner_token = launcher_state.get("profile_owner_token")
            expected_digest = launcher_state.get("lock_sha256")
            expected_data = (str(owner_token) + "\n").encode("ascii")

        restore_candidates = []
        final_candidates = []
        for index, name, record in parsed:
            for path_text, _base, location, field, snapshot_index in self._recovery_path_locations(
                index, name, record
            ):
                if path_text != str(lock) or field not in ("snapshots", "restore_files"):
                    continue
                snapshot = record.data[field][int(snapshot_index)]
                role = None
                if record.kind == "restore_launcher" and name == "restore launcher":
                    role = "restore"
                elif (
                    record.kind == "restore_launcher"
                    and name == "remove launcher recovery lock"
                    and field == "snapshots"
                ):
                    role = "final"
                if role is None or not protected_state:
                    raise failure("Launcher lock snapshot is in unexpected {}".format(location))
                if (
                    snapshot.get("present") is not True
                    or snapshot.get("type") != "file"
                    or snapshot.get("sha256") != expected_digest
                    or snapshot.get("mode") != 0o600
                    or base64.b64decode(snapshot["content"], validate=True) != expected_data
                ):
                    raise failure("Launcher lock snapshot is not owned")
                candidate = (index, field, int(snapshot_index), snapshot)
                if role == "restore":
                    restore_candidates.append(candidate)
                else:
                    if set(snapshot) != {
                        "path",
                        "present",
                        "type",
                        "content",
                        "sha256",
                        "mode",
                        "device",
                        "inode",
                        "identity_integrity",
                    }:
                        raise failure("Final launcher lock identity is invalid")
                    final_candidates.append(candidate)
        if protected_state and parsed and len(restore_candidates) != 1:
            raise failure("Launcher lock restore snapshot is ambiguous")
        if len(final_candidates) > 1:
            raise failure("Launcher lock final-unlink snapshot is ambiguous")
        if protected_state and not os.path.lexists(lock) and not final_candidates:
            raise failure("Launcher lock has no final-unlink snapshot")
        old_identity = None
        if final_candidates:
            final_snapshot = final_candidates[0][3]
            expected_integrity = self._restore_lock_identity_integrity(
                str(owner_token),
                str(expected_digest),
                final_snapshot["device"],
                final_snapshot["inode"],
            )
            if final_snapshot["identity_integrity"] != expected_integrity:
                raise failure("Final launcher lock identity integrity is invalid")
            old_identity = (final_snapshot["device"], final_snapshot["inode"])
            if os.path.lexists(lock):
                status = os.lstat(str(lock))
                if (
                    not stat.S_ISREG(status.st_mode)
                    or status.st_nlink != 1
                    or (status.st_dev, status.st_ino) != old_identity
                ):
                    raise failure("Launcher lock identity changed before recovery")
        if protected_state:
            artifact_preflight = getattr(
                self.launcher, "preflight_recovery_artifacts", None
            )
            if not callable(artifact_preflight):
                raise failure("Launcher recovery-artifact preflight is unavailable")
            try:
                artifact_preflight(
                    installation,
                    recovered_state.launcher_artifacts,
                    allow_missing_lock=bool(final_candidates),
                )
            except (OSError, PatchError, TypeError, ValueError) as error:
                raise failure(
                    "Launcher recovery artifacts are invalid: {}".format(error)
                ) from error

        omitted = {
            (index, field, snapshot_index)
            for index, field, snapshot_index, _snapshot in (
                restore_candidates + final_candidates
            )
        }
        for index, name, record in parsed:
            for path_text, base, location, field, snapshot_index in self._recovery_path_locations(
                index, name, record
            ):
                relation = self._recovery_path_relation(
                    path_text,
                    lock,
                    old_identity,
                    base,
                    expected_data,
                    expected_digest,
                )
                if relation == "alias":
                    raise failure("Launcher lock alias appears in {}".format(location))
                if relation == "unsafe":
                    raise failure(
                        "Launcher lock content could not be classified in {}".format(
                            location
                        )
                    )
                if relation == "exact" and (
                    field is None
                    or (index, field, int(snapshot_index)) not in omitted
                ):
                    raise failure("Launcher lock appears in unexpected {}".format(location))

        sanitized_records = []
        for index, _name, record in parsed:
            data = copy.deepcopy(record.data)
            for field in ("snapshots", "restore_files"):
                if field not in data:
                    continue
                data[field] = [
                    snapshot
                    for snapshot_index, snapshot in enumerate(data[field])
                    if (index, field, snapshot_index) not in omitted
                ]
            sanitized_records.append((index, UndoRecord(record.kind, data)))
        return _RestoreRecoveryPlan(
            recovered_state,
            tuple(sanitized_records),
            bool(final_candidates),
        )


    def _recreate_journaled_restore_lock(
        self,
        installation: GameInstallation,
        state: InstallState,
        has_final_unlink_snapshot: bool,
    ) -> None:
        """Recreate only a missing lock whose late Restore unlink is journaled."""
        launcher_state = state.launcher_artifacts
        root = installation.bottle.root.resolve()
        lock = root / ".ostriv-launcher.lock"
        if os.path.lexists(lock):
            return
        if not has_final_unlink_snapshot:
            return
        owner_token = launcher_state.get("profile_owner_token")
        expected_digest = launcher_state.get("lock_sha256")
        if (
            launcher_state.get("lock_path") != str(lock)
            or not isinstance(owner_token, str)
            or re.fullmatch(r"[0-9a-f]{64}", owner_token) is None
            or not isinstance(expected_digest, str)
        ):
            return
        expected_data = (owner_token + "\n").encode("ascii")
        if _bytes_digest(expected_data) != expected_digest:
            return

        descriptor = None
        created = False
        try:
            descriptor = os.open(
                str(lock),
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            created = True
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(expected_data)
                stream.flush()
            os.fsync(descriptor)
            _fsync_directory(lock.parent)
        except OSError as error:
            if created:
                try:
                    _durable_unlink(lock)
                except OSError:
                    pass
            raise PatchError(
                "restore.launcher_recovery",
                "Restore failed.",
                "Unable to recreate the journaled launcher lock: {}: {}".format(
                    type(error).__name__, error
                ),
            ) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _handlers_holding_restore_lock(
        self,
        installation: GameInstallation,
        state: InstallState,
        lease,
        handlers: Mapping[str, Callable[[UndoRecord], None]],
        *,
        sanitize_lock_snapshots: bool,
    ) -> Mapping[str, Callable[[UndoRecord], None]]:
        """Revalidate the held lock before every undo and never replay it."""
        lock = installation.bottle.root.resolve() / ".ostriv-launcher.lock"

        def failure(detail: str) -> PatchError:
            return PatchError(
                "restore.launcher_recovery",
                "Restore failed.",
                detail,
            )

        owner_token = state.launcher_artifacts["profile_owner_token"]
        expected_data = (str(owner_token) + "\n").encode("ascii")
        expected_digest = state.launcher_artifacts["lock_sha256"]
        validate_current_path = getattr(lease, "validate_current_path", None)
        if not callable(validate_current_path):
            raise failure("Launcher recovery lock lease cannot be validated")

        def validate_lease() -> None:
            try:
                validate_current_path(expected_data, 0o600)
            except (OSError, TypeError, ValueError) as error:
                raise failure(
                    "Launcher recovery lock changed during journal recovery: {}".format(
                        error
                    )
                ) from error

        def sanitized(record: UndoRecord) -> UndoRecord:
            data = copy.deepcopy(record.data)
            for field in ("snapshots", "restore_files"):
                snapshots = data.get(field)
                if snapshots is None:
                    continue
                if not isinstance(snapshots, list):
                    raise failure("Launcher recovery snapshot container is invalid")
                kept = []
                for snapshot in snapshots:
                    if not isinstance(snapshot, dict) or snapshot.get("path") != str(lock):
                        kept.append(snapshot)
                        continue
                    try:
                        captured = base64.b64decode(
                            snapshot["content"], validate=True
                        )
                    except (KeyError, TypeError, ValueError) as error:
                        raise failure(
                            "Launcher recovery lock snapshot is invalid"
                        ) from error
                    if (
                        snapshot.get("present") is not True
                        or snapshot.get("type") != "file"
                        or snapshot.get("sha256") != expected_digest
                        or snapshot.get("mode") != 0o600
                        or captured != expected_data
                    ):
                        raise failure("Launcher recovery lock snapshot is not owned")
                data[field] = kept
            return UndoRecord(record.kind, data)

        protected = {}
        for kind, handler in handlers.items():
            def guard(
                record: UndoRecord,
                original_handler: Callable[[UndoRecord], None] = handler,
            ) -> None:
                validate_lease()
                original_handler(
                    sanitized(record)
                    if sanitize_lock_snapshots and record.kind == "restore_launcher"
                    else record
                )

            protected[kind] = guard
        return protected

    def restore(self, installation: GameInstallation) -> None:
        logger.info(
            "restore start bottle=%s game=%s",
            installation.bottle.name,
            installation.game_dir.resolve(),
        )
        self.progress("Restoring", "checking previous changes")
        self._validate_settings_ancestry(
            installation, "restore.settings_path", "Restore failed."
        )
        transaction = self.transaction_for(installation)
        recovering_incomplete = not transaction.journal.data.get("complete")
        recovering_restore = (
            recovering_incomplete
            and transaction.journal.data.get("operation") == "restore"
        )
        recovery_plan = (
            self._build_restore_recovery_plan(installation, transaction)
            if recovering_restore
            else None
        )
        if recovery_plan is not None:
            transaction.use_recovery_plan(recovery_plan.records)
            state = recovery_plan.state
        else:
            state = self._load_state(installation, "restore")
        restore_lease = None
        prepare_restore = getattr(self.launcher, "prepare_restore", None)
        preserving_recovery_lock = (
            recovery_plan is not None
            and state is not None
            and callable(prepare_restore)
        )
        if state is not None and callable(prepare_restore):
            if preserving_recovery_lock:
                self._recreate_journaled_restore_lock(
                    installation,
                    state,
                    recovery_plan.has_final_unlink_snapshot,
                )
            restore_lease = prepare_restore(
                installation,
                state.launcher_artifacts,
                recover_profile=not recovering_incomplete,
            )
        try:
            if preserving_recovery_lock:
                if restore_lease is None:
                    raise PatchError(
                        "restore.launcher_recovery",
                        "Restore failed.",
                        "Launcher recovery lock lease is unavailable",
                    )
                transaction.handlers = self._handlers_holding_restore_lock(
                    installation,
                    state,
                    restore_lease,
                    transaction.handlers,
                    sanitize_lock_snapshots=False,
                )
            transaction.recover_incomplete()
            if (
                recovering_incomplete
                and restore_lease is not None
                and not preserving_recovery_lock
            ):
                restore_lease.close()
                restore_lease = None
            state = self._load_state(installation, "restore")
            if preserving_recovery_lock and restore_lease is not None:
                if state is None:
                    restore_lease.close()
                    restore_lease = None
                else:
                    restore_lease = prepare_restore(
                        installation,
                        state.launcher_artifacts,
                        existing_lock=restore_lease,
                    )
            if (
                restore_lease is None
                and state is not None
                and callable(prepare_restore)
            ):
                restore_lease = prepare_restore(
                    installation, state.launcher_artifacts
                )
            self._cleanup_completed_journal(transaction)
            launcher_undo = None
            legacy_launcher_undo = None
            if state is not None:
                launcher_undo = self._launcher_restore_undo(
                    installation, state.launcher_artifacts
                )
            else:
                legacy_launcher_undo = self._legacy_launcher_restore_undo(installation)
            transaction = self.transaction_for(installation)
            if state is not None and restore_lease is not None:
                transaction.handlers = self._handlers_holding_restore_lock(
                    installation,
                    state,
                    restore_lease,
                    transaction.handlers,
                    sanitize_lock_snapshots=True,
                )
            transaction.start("restore")
        except BaseException:
            if restore_lease is not None:
                restore_lease.close()
            raise
        try:
            if state is None:
                self.progress("Restoring", "removing patch (may take a minute)")
                self._restore_legacy(
                    transaction, installation, legacy_launcher_undo
                )
                self.progress("Restoring", "verifying")
                transaction.journal.commit()
                self._cleanup_completed_journal(transaction)
                logger.info("restore verification status=OK mode=legacy")
                logger.info("restore complete bottle=%s", installation.bottle.name)
                return

            self.progress("Restoring", "restoring launcher")
            transaction.step(
                "restore launcher",
                UndoRecord("restore_launcher", launcher_undo or {}),
                lambda: self.launcher.restore(installation, state.launcher_artifacts),
            )
            self.progress("Restoring", "restoring files")
            handled = set()
            for item in reversed(state.backup_files):
                path = str(item["path"])
                kind = "restore_file"
                if path == str(installation.bottle.root.resolve() / "cxbottle.conf"):
                    kind = "restore_config"
                elif path == str(self._settings_path(installation)):
                    kind = "restore_settings"
                self._journal_restore_backup(transaction, installation, item, kind)
                handled.add(path)

            config_path = installation.bottle.root.resolve() / "cxbottle.conf"
            if state.original_config_backup and str(config_path) not in handled:
                self._journal_restore_backup(
                    transaction,
                    installation,
                    {
                        "path": str(config_path),
                        "backup_path": state.original_config_backup,
                        "installed_sha256": state.installed_config_digest,
                        "original_sha256": state.original_config_digest,
                    },
                    "restore_config",
                )
            settings_path = self._settings_path(installation)
            if state.original_settings_backup and str(settings_path) not in handled:
                self._journal_restore_backup(
                    transaction,
                    installation,
                    {
                        "path": str(settings_path),
                        "backup_path": state.original_settings_backup,
                        "installed_sha256": state.installed_settings_digest,
                        "original_sha256": state.original_settings_digest,
                    },
                    "restore_settings",
                )
            self.progress("Restoring", "configuring CrossOver (may take a minute)")
            registry = self._registry(installation)
            current_registry = registry.query(
                REGISTRY_KEY, REGISTRY_VALUE, "restore.registry"
            )
            if current_registry == REGISTRY_DATA and current_registry != state.prior_registry_value:
                transaction.step(
                    "restore registry override",
                    UndoRecord(
                        "restore_registry",
                        {
                            "key": REGISTRY_KEY,
                            "value": REGISTRY_VALUE,
                            "before": REGISTRY_DATA,
                            "after": state.prior_registry_value,
                        },
                    ),
                    lambda: self._restore_registry_value(
                        registry, state.prior_registry_value
                    ),
                )
            for item in reversed(state.owned_files):
                if str(item["path"]) not in handled:
                    self._journal_remove_owned(
                        transaction,
                        installation,
                        item,
                        unconditional=Path(str(item["path"])) == settings_path,
                    )
            self.progress("Restoring", "verifying")
            self._verify_restored(installation, state)
            logger.info("restore verification status=OK mode=owned")
            finalize_restore = getattr(self.launcher, "finalize_restore", None)
            if callable(finalize_restore):
                lock_path = installation.bottle.root.resolve() / ".ostriv-launcher.lock"
                owner_token = state.launcher_artifacts.get("profile_owner_token")
                validate_lock = getattr(
                    restore_lease, "validate_current_path", None
                )
                descriptor = getattr(restore_lease, "fd", None)
                if not isinstance(owner_token, str) or not callable(validate_lock):
                    raise PatchError(
                        "restore.launcher_recovery",
                        "Restore failed.",
                        "Launcher recovery lock lease is unavailable before final unlink",
                    )
                try:
                    validate_lock((owner_token + "\n").encode("ascii"), 0o600)
                    opened_lock = os.fstat(descriptor)
                except (OSError, TypeError, UnicodeError, ValueError) as error:
                    raise PatchError(
                        "restore.launcher_recovery",
                        "Restore failed.",
                        "Launcher recovery lock cannot be authenticated: {}".format(
                            error
                        ),
                    ) from error
                final_lock_snapshots = self._snapshots(
                    [lock_path], include_identity=True
                )
                if (
                    len(final_lock_snapshots) != 1
                    or final_lock_snapshots[0].get("present") is not True
                    or type(final_lock_snapshots[0].get("device")) is not int
                    or type(final_lock_snapshots[0].get("inode")) is not int
                    or (
                        final_lock_snapshots[0]["device"],
                        final_lock_snapshots[0]["inode"],
                    )
                    != (opened_lock.st_dev, opened_lock.st_ino)
                    or opened_lock.st_nlink != 1
                ):
                    raise PatchError(
                        "restore.launcher_recovery",
                        "Restore failed.",
                        "Unable to authenticate the launcher lock before final unlink",
                    )
                final_lock_snapshots[0]["identity_integrity"] = (
                    self._restore_lock_identity_integrity(
                        owner_token,
                        str(state.launcher_artifacts.get("lock_sha256")),
                        final_lock_snapshots[0]["device"],
                        final_lock_snapshots[0]["inode"],
                    )
                )
                final_lock_identity = (
                    final_lock_snapshots[0]["device"],
                    final_lock_snapshots[0]["inode"],
                )
                transaction.step(
                    "remove launcher recovery lock",
                    UndoRecord(
                        "restore_launcher",
                        {"snapshots": final_lock_snapshots},
                    ),
                    lambda: finalize_restore(
                        installation,
                        state.launcher_artifacts,
                        existing_lock=restore_lease,
                        expected_identity=final_lock_identity,
                    ),
                )
            state_path = self.state_path(installation)
            state_digest = _file_digest(state_path)
            transaction.step(
                "remove ownership state",
                UndoRecord("restore_file", {"snapshots": self._snapshots([state_path])}),
                lambda: _durable_unlink(state_path)
                if _same_file(state_path, state_digest)
                else None,
            )
            transaction.journal.commit()
            self._cleanup_completed_journal(transaction)
            logger.info("restore complete bottle=%s", installation.bottle.name)
        except BaseException:
            logger.exception("restore failed bottle=%s", installation.bottle.name)
            self.progress("Restoring", "undoing incomplete changes")
            transaction.rollback()
            self._cleanup_completed_journal(transaction)
            raise
        finally:
            if restore_lease is not None:
                restore_lease.close()

    def _journal_legacy_change(
        self,
        transaction: Transaction,
        name: str,
        paths: Sequence[Path],
        action: Callable[[], None],
        kind: str = "restore_file",
    ) -> None:
        transaction.step(name, UndoRecord(kind, {"snapshots": self._snapshots(paths)}), action)

    def _restore_legacy(
        self,
        transaction: Transaction,
        installation: GameInstallation,
        launcher_undo: Optional[tuple[Dict[str, object], Dict[str, object]]] = None,
    ) -> None:
        game_dir = installation.game_dir.resolve()
        for name in DRIVER_NAMES:
            source = self.package_root / "prebuilt" / name
            if not source.is_file():
                continue
            installed_digest = _file_digest(source)
            path = game_dir / name
            backup = path.with_name(path.name + ".bak")
            if _same_file(path, installed_digest) and backup.is_file():
                backup_digest = _file_digest(backup)
                if backup_digest == installed_digest:
                    self._journal_legacy_change(
                        transaction,
                        "remove stale legacy {}".format(name),
                        [path, backup],
                        lambda path=path, backup=backup: (
                            _durable_unlink(path),
                            _durable_unlink(backup),
                        ),
                    )
                else:
                    self._journal_legacy_change(
                        transaction,
                        "restore legacy {}".format(name),
                        [path, backup],
                        lambda path=path, backup=backup: _durable_replace(backup, path),
                    )
            elif _same_file(path, installed_digest) and not backup.exists():
                self._journal_legacy_change(
                    transaction,
                    "remove legacy {}".format(name),
                    [path],
                    lambda path=path: _durable_unlink(path),
                )
        app_id = game_dir / "steam_appid.txt"
        if _same_file(app_id, _bytes_digest(b"773790")):
            self._journal_legacy_change(
                transaction,
                "remove legacy app id",
                [app_id],
                lambda: _durable_unlink(app_id),
            )
        for name in DIAGNOSTIC_LOGS:
            path = game_dir / name
            if path.is_file():
                self._journal_legacy_change(
                    transaction,
                    "remove legacy diagnostic {}".format(name),
                    [path],
                    lambda path=path: _durable_unlink(path),
                )
        registry = self._registry(installation)
        if (
            registry.query(REGISTRY_KEY, REGISTRY_VALUE, "restore.registry")
            == REGISTRY_DATA
        ):
            transaction.step(
                "remove legacy registry override",
                UndoRecord(
                    "restore_registry",
                    {
                        "key": REGISTRY_KEY,
                        "value": REGISTRY_VALUE,
                        "before": REGISTRY_DATA,
                        "after": None,
                    },
                ),
                lambda: registry.delete(REGISTRY_KEY, REGISTRY_VALUE),
            )
        config = installation.bottle.root.resolve() / "cxbottle.conf"
        if config.is_file():
            current = config.read_bytes()
            desired = self._remove_known_environment(current)
            if desired != current:
                self._journal_legacy_change(
                    transaction,
                    "remove legacy bottle environment",
                    [config],
                    lambda: _atomic_write_bytes(config, desired, stat_mode(config)),
                    "restore_config",
                )
        settings = self._settings_path(installation)
        backup = settings.with_name(settings.name + ".bak")
        if settings.is_file() and backup.is_file():
            current = settings.read_bytes()
            original = backup.read_bytes()
            try:
                recognizable = self._safe_settings(original) == current
            except PatchError:
                recognizable = False
            if recognizable:
                self._journal_legacy_change(
                    transaction,
                    "restore legacy settings",
                    [settings, backup],
                    lambda: _replace_relative(
                        installation.bottle.root.resolve(), backup, settings
                    ),
                    "restore_settings",
                )
        elif settings.is_file():
            template = self.package_root / "assets/settings.data"
            if template.is_file() and _same_file(settings, _file_digest(template)):
                self._journal_legacy_change(
                    transaction,
                    "remove legacy settings",
                    [settings],
                    lambda: _unlink_relative(
                        installation.bottle.root.resolve(), settings
                    ),
                    "restore_settings",
                )
        if launcher_undo is not None:
            legacy_state, undo_data = launcher_undo
            transaction.step(
                "restore legacy launcher",
                UndoRecord("restore_launcher", undo_data),
                lambda: self.launcher.restore(installation, legacy_state),
            )

    @staticmethod
    def _remove_known_environment(data: bytes) -> bytes:
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            return data
        kept = []
        in_environment = False
        patterns = tuple(
            re.compile(
                r'^\s*"{}"\s*=\s*"{}"\s*$'.format(
                    re.escape(key), re.escape(value)
                )
            )
            for key, value in BOTTLE_ENV.items()
        )
        for line in text.splitlines(keepends=True):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_environment = stripped == "[EnvironmentVariables]"
                kept.append(line)
                continue
            if in_environment and any(pattern.match(stripped) for pattern in patterns):
                continue
            kept.append(line)
        return "".join(kept).encode("utf-8")


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o7777
