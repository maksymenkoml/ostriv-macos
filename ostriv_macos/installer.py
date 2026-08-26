"""Durable, recoverable installation transactions."""

import copy
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence

from . import __version__
from .diagnostics import CommandRunner, PatchError
from .discovery import Bottle, GameInstallation
from .payload import PayloadEntry, validate_payload


JOURNAL_SCHEMA = 1
JOURNAL_CORRUPT_MESSAGE = "The installation journal is unreadable. Restore before trying again."
RECOVERY_REQUIRED_MESSAGE = "A previous installation needs recovery."
ROLLBACK_FAILED_MESSAGE = "Installation recovery failed. Restore before trying again."
_JOURNAL_REPLACED_ATTRIBUTE = "_ostriv_journal_replaced"

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
    path.parent.mkdir(parents=True, exist_ok=True)
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
        if os.name != "nt":
            directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
            try:
                try:
                    os.fsync(directory_descriptor)
                except OSError as error:
                    setattr(error, _JOURNAL_REPLACED_ATTRIBUTE, True)
                    raise
            finally:
                os.close(directory_descriptor)
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

    def start(self, operation: str) -> None:
        self.journal.start(operation)

    def _undo(self, record_data: Dict[str, object]) -> None:
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
        index = self.journal.begin(name, undo)
        try:
            action()
            self.journal.mark_applied(index)
        except BaseException:
            item = self.journal.data["records"][index]
            try:
                self._undo(item["undo"])
            except BaseException as error:
                raise self._rollback_failed(index, item, error) from error
            self.journal.mark_rolled_back(index)
            raise

    def rollback(self) -> None:
        for index in range(len(self.journal.data["records"]) - 1, -1, -1):
            item = self.journal.data["records"][index]
            if item["status"] in ("pending", "applied"):
                try:
                    self._undo(item["undo"])
                except BaseException as error:
                    raise self._rollback_failed(index, item, error) from error
                self.journal.mark_rolled_back(index)
        self.journal.commit()

    def recover_incomplete(self) -> None:
        if not self.journal.data.get("complete"):
            self.rollback()


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
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=str(path.parent), delete=False) as stream:
            temp_name = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temp_name, mode)
        os.replace(temp_name, str(path))
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


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
        if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
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
        names = [field.name for field in cls.__dataclass_fields__.values()]
        return cls(**{name: data.get(name) for name in names})


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

    def query(self, key: str, value: str) -> Optional[str]:
        result = self.runner.run(self._base() + ["query", key, "/v", value])
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[0] == value:
                return fields[-1]
        return None

    def set(self, key: str, value: str, data: str) -> None:
        last_detail = ""
        for _attempt in range(2):
            result = self.runner.run(
                self._base() + ["add", key, "/v", value, "/d", data, "/f"]
            )
            last_detail = result.stderr
            if result.returncode == 0:
                if self.query(key, value) == data:
                    return
                last_detail = "Registry query did not return the required value"
        raise PatchError("install.registry", "Installation failed.", last_detail)

    def delete(self, key: str, value: str) -> None:
        last_detail = ""
        for _attempt in range(2):
            result = self.runner.run(
                self._base() + ["delete", key, "/v", value, "/f"]
            )
            last_detail = result.stderr
            if result.returncode == 0 and self.query(key, value) is None:
                return
        raise PatchError("restore.registry", "Restore failed.", last_detail)


class Installer:
    def __init__(
        self,
        package_root: Path,
        launcher: LauncherPort,
        runner: Optional[CommandRunner] = None,
        launcher_destination: Optional[Path] = None,
        clock: Optional[Callable[[], datetime]] = None,
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
        self._existing_state: Optional[InstallState] = None
        self._owned_files: List[Dict[str, object]] = []
        self._backup_files: List[Dict[str, object]] = []
        self._config: Dict[str, object] = {}
        self._settings: Dict[str, object] = {}
        self._registry_before: Optional[str] = None

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

    def undo_handlers(
        self, installation: GameInstallation
    ) -> Mapping[str, Callable[[UndoRecord], None]]:
        return {
            "remove_path": lambda record: self._undo_remove_path(installation, record),
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
            "restore_launcher": lambda record: self._undo_restore_snapshots(
                installation, record
            ),
        }

    def transaction_for(self, installation: GameInstallation) -> Transaction:
        return Transaction(
            InstallJournal(self.journal_path(installation)),
            self.undo_handlers(installation),
        )

    def _cleanup_completed_journal(self, transaction: Transaction) -> None:
        if transaction.journal.data.get("complete"):
            try:
                transaction.journal.path.unlink()
            except FileNotFoundError:
                pass

    def _load_state(
        self, installation: GameInstallation, error_prefix: str
    ) -> Optional[InstallState]:
        path = self.state_path(installation)
        if not path.exists():
            return None
        try:
            state = InstallState.from_dict(json.loads(path.read_text(encoding="utf-8")))
            if state.bottle_realpath != str(installation.bottle.root.resolve()):
                raise ValueError("ownership state belongs to another bottle")
            if state.game_realpath != str(installation.game_dir.resolve()):
                raise ValueError("ownership state belongs to another game")
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
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise PatchError(
                "{}.state_corrupt".format(error_prefix),
                "The installation ownership record is unreadable.",
                "Unable to load {}: {}: {}".format(path, type(error).__name__, error),
            ) from error
        return state

    @staticmethod
    def _writable_location(path: Path) -> bool:
        candidate = path
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)

    def preflight(
        self, installation: GameInstallation, payload: Sequence[PayloadEntry]
    ) -> None:
        issues = []
        bottle_root = installation.bottle.root.resolve()
        game_dir = installation.game_dir.resolve()
        config = bottle_root / "cxbottle.conf"
        registry_file = bottle_root / "system.reg"
        settings = self._settings_path(installation)
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
            result = self.runner.run(argv)
            if result.returncode != 0:
                issues.append(
                    "cxbottle rejected the selected bottle: {}".format(
                        result.stderr or result.stdout
                    )
                )
        self._existing_state = self._load_state(installation, "install")
        if issues:
            detail = "\n".join(issues)
            logger.error("install.preflight: %s", detail)
            raise PatchError("install.preflight", "Installation cannot start.", detail)

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

    def _install_file(
        self,
        transaction: Transaction,
        path: Path,
        source: Path,
        name: str,
    ) -> None:
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
            original_digest = _file_digest(path)
            backup_entry = self._backup_for(path)
            backup = (
                Path(str(backup_entry["backup_path"]))
                if backup_entry is not None
                else self._choose_backup(path)
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
                if not backup.exists():
                    shutil.copy2(path, backup)
                shutil.copy2(source, path)

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
            undo = UndoRecord(
                "remove_path",
                {
                    "path": str(path),
                    "owned_path": str(path),
                    "expected_sha256": desired_digest,
                },
            )
            transaction.step(name, undo, lambda: shutil.copy2(source, path))
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
                shutil.copy2(path, backup)
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
            lambda: self._backup_and_write(path, backup, desired),
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
        if self._existing_state is not None:
            if not path.is_file() or _file_digest(path) != self._existing_state.installed_settings_digest:
                raise PatchError(
                    "install.ownership_conflict",
                    "Installation cannot replace modified game settings.",
                    str(path),
                )
            self._settings = {
                "backup": self._existing_state.original_settings_backup,
                "original_digest": self._existing_state.original_settings_digest,
                "installed_digest": self._existing_state.installed_settings_digest,
            }
            return
        if path.is_file():
            current = path.read_bytes()
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
                lambda: self._backup_and_write(path, backup, desired),
            )
            self._settings = {
                "backup": str(backup),
                "original_digest": original_digest,
                "installed_digest": installed_digest,
            }
            return
        template = self.package_root / "assets/settings.data"
        desired = template.read_bytes()
        self._safe_settings(desired)
        installed_digest = _bytes_digest(desired)
        transaction.step(
            "install safe graphics settings",
            UndoRecord(
                "remove_path",
                {
                    "path": str(path),
                    "owned_path": str(path),
                    "expected_sha256": installed_digest,
                },
            ),
            lambda: _atomic_write_bytes(path, desired),
        )
        self._upsert(self._owned_files, {"path": str(path), "sha256": installed_digest})
        self._settings = {
            "backup": None,
            "original_digest": "",
            "installed_digest": installed_digest,
        }

    @staticmethod
    def _backup_and_write(path: Path, backup: Path, data: bytes) -> None:
        mode = stat_mode(path)
        if not backup.exists():
            shutil.copy2(path, backup)
        _atomic_write_bytes(path, data, mode)

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
        if self._registry(installation).query(REGISTRY_KEY, REGISTRY_VALUE) != REGISTRY_DATA:
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
        validate_payload(self.package_root, payload)
        self.preflight(installation, payload)
        transaction = self.transaction_for(installation)
        transaction.recover_incomplete()
        self._existing_state = self._load_state(installation, "install")
        transaction.start("install")
        self._start_ownership()
        try:
            self.stage_driver_files(transaction, installation, payload)
            self.write_app_id(transaction, installation, "773790")
            self.set_native_override(transaction, installation)
            self.set_bottle_environment(transaction, installation)
            self.set_safe_graphics(transaction, installation)
            launcher_state = self.launcher.install(transaction, installation)
            state = self.verify(installation, payload, launcher_state)
            self.write_install_state(transaction, installation, state)
            transaction.journal.commit()
            self._cleanup_completed_journal(transaction)
            return state
        except BaseException:
            transaction.rollback()
            self._cleanup_completed_journal(transaction)
            raise

    def _snapshots(self, paths: Sequence[Path]) -> List[Dict[str, object]]:
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
                        "content": base64.b64encode(data).decode("ascii"),
                        "sha256": _bytes_digest(data),
                        "mode": stat_mode(path),
                    }
                )
            else:
                snapshots.append({"path": str(path), "present": False})
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
            present = snapshot.get("present") is True
            if present:
                try:
                    data = base64.b64decode(str(snapshot["content"]), validate=True)
                except (KeyError, ValueError):
                    continue
                expected = str(snapshot.get("sha256", ""))
                if _bytes_digest(data) != expected:
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
                    path.unlink()

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
            path.unlink()

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
        if not _same_file(backup, original):
            return
        if not path.exists() or _same_file(path, installed):
            os.replace(str(backup), str(path))
        elif _same_file(path, original):
            backup.unlink()

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
        current = registry.query(key, value)
        if current == before:
            return
        if current != after:
            return
        if before is None:
            registry.delete(key, value)
        elif isinstance(before, str):
            registry.set(key, value, before)

    def _restore_backup_action(self, item: Mapping[str, object]) -> None:
        path = Path(str(item["path"]))
        backup = Path(str(item["backup_path"]))
        installed = str(item["installed_sha256"])
        original = str(item["original_sha256"])
        if _same_file(path, original) and not backup.exists():
            return
        if not _same_file(path, installed) or not _same_file(backup, original):
            return
        os.replace(str(backup), str(path))

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
            lambda: self._restore_backup_action(item),
        )

    def _journal_remove_owned(
        self,
        transaction: Transaction,
        installation: GameInstallation,
        item: Mapping[str, object],
    ) -> None:
        path = Path(str(item["path"]))
        expected = str(item.get("sha256", ""))
        snapshots = self._snapshots([path])
        transaction.step(
            "remove {}".format(path.name),
            UndoRecord("restore_file", {"snapshots": snapshots}),
            lambda: path.unlink() if _same_file(path, expected) else None,
        )

    @staticmethod
    def _launcher_paths(launcher_state: Mapping[str, object]) -> List[Path]:
        paths = []
        artifacts = launcher_state.get("artifacts", [])
        if isinstance(artifacts, list):
            for item in artifacts:
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    paths.append(Path(item["path"]))
        return paths

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
        registry_value = self._registry(installation).query(REGISTRY_KEY, REGISTRY_VALUE)
        if registry_value != state.prior_registry_value:
            failures.append("registry value was not restored")
        config = installation.bottle.root.resolve() / "cxbottle.conf"
        if not _same_file(config, state.original_config_digest):
            failures.append("bottle configuration was not restored")
        settings = self._settings_path(installation)
        if state.original_settings_digest:
            if not _same_file(settings, state.original_settings_digest):
                failures.append("game settings were not restored")
        elif _same_file(settings, state.installed_settings_digest):
            failures.append("installed game settings remain")
        for artifact in self._launcher_paths(state.launcher_artifacts):
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

    def restore(self, installation: GameInstallation) -> None:
        transaction = self.transaction_for(installation)
        transaction.recover_incomplete()
        self._cleanup_completed_journal(transaction)
        state = self._load_state(installation, "restore")
        transaction = self.transaction_for(installation)
        transaction.start("restore")
        try:
            if state is None:
                self._restore_legacy(transaction, installation)
                transaction.journal.commit()
                self._cleanup_completed_journal(transaction)
                return

            launcher_paths = self._launcher_paths(state.launcher_artifacts)
            transaction.step(
                "restore launcher",
                UndoRecord(
                    "restore_launcher", {"snapshots": self._snapshots(launcher_paths)}
                ),
                lambda: self.launcher.restore(installation, state.launcher_artifacts),
            )
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
            registry = self._registry(installation)
            current_registry = registry.query(REGISTRY_KEY, REGISTRY_VALUE)
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
                    self._journal_remove_owned(transaction, installation, item)
            self._verify_restored(installation, state)
            state_path = self.state_path(installation)
            state_digest = _file_digest(state_path)
            transaction.step(
                "remove ownership state",
                UndoRecord("restore_file", {"snapshots": self._snapshots([state_path])}),
                lambda: state_path.unlink() if _same_file(state_path, state_digest) else None,
            )
            transaction.journal.commit()
            self._cleanup_completed_journal(transaction)
        except BaseException:
            transaction.rollback()
            self._cleanup_completed_journal(transaction)
            raise

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
        self, transaction: Transaction, installation: GameInstallation
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
                        lambda path=path, backup=backup: (path.unlink(), backup.unlink()),
                    )
                else:
                    self._journal_legacy_change(
                        transaction,
                        "restore legacy {}".format(name),
                        [path, backup],
                        lambda path=path, backup=backup: os.replace(str(backup), str(path)),
                    )
            elif _same_file(path, installed_digest) and not backup.exists():
                self._journal_legacy_change(
                    transaction,
                    "remove legacy {}".format(name),
                    [path],
                    lambda path=path: path.unlink(),
                )
        app_id = game_dir / "steam_appid.txt"
        if _same_file(app_id, _bytes_digest(b"773790")):
            self._journal_legacy_change(
                transaction, "remove legacy app id", [app_id], app_id.unlink
            )
        for name in DIAGNOSTIC_LOGS:
            path = game_dir / name
            if path.is_file():
                self._journal_legacy_change(
                    transaction,
                    "remove legacy diagnostic {}".format(name),
                    [path],
                    path.unlink,
                )
        registry = self._registry(installation)
        if registry.query(REGISTRY_KEY, REGISTRY_VALUE) == REGISTRY_DATA:
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
                    lambda: os.replace(str(backup), str(settings)),
                    "restore_settings",
                )
        elif settings.is_file():
            template = self.package_root / "assets/settings.data"
            if template.is_file() and _same_file(settings, _file_digest(template)):
                self._journal_legacy_change(
                    transaction,
                    "remove legacy settings",
                    [settings],
                    settings.unlink,
                    "restore_settings",
                )
        legacy_app = self.launcher_destination
        if legacy_app.suffix != ".app":
            legacy_app = legacy_app / "Ostriv (patched).app"
        legacy_runtime = installation.bottle.root.resolve() / "play-ostriv-patched.py"
        if legacy_app.exists() or legacy_runtime.exists():
            legacy_state = {
                "legacy": True,
                "artifacts": [
                    {"path": str(legacy_app)},
                    {"path": str(legacy_runtime)},
                ],
            }
            transaction.step(
                "restore legacy launcher",
                UndoRecord(
                    "restore_launcher",
                    {"snapshots": self._snapshots([legacy_app, legacy_runtime])},
                ),
                lambda: self.launcher.restore(installation, legacy_state),
            )

    @staticmethod
    def _remove_known_environment(data: bytes) -> bytes:
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            return data
        for key, value in BOTTLE_ENV.items():
            text = re.sub(
                r'^\s*"{}"\s*=\s*"{}"\s*\r?\n'.format(
                    re.escape(key), re.escape(value)
                ),
                "",
                text,
                flags=re.MULTILINE,
            )
        return text.encode("utf-8")


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o7777
