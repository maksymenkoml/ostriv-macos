"""Verified CrossOver launcher application installation and migration."""

import base64
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .diagnostics import CommandRunner, PatchError
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


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> str:
    return _digest(path.read_bytes())


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
    path = path.resolve(strict=False)
    if path.is_file():
        data = path.read_bytes()
        item: Dict[str, object] = {
            "path": str(path),
            "present": True,
            "content": base64.b64encode(data).decode("ascii"),
            "sha256": _digest(data),
            "mode": _mode(path),
        }
        if after_digest:
            item["allowed_current_sha256"] = [after_digest]
        return item
    item = {"path": str(path), "present": False}
    if after_digest:
        item["remove_sha256"] = after_digest
    return item


def _tree_files(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    return [path for path in sorted(root.rglob("*")) if path.is_file()]


def _tree_directories(root: Path) -> List[Path]:
    if not root.is_dir():
        return []
    return [root, *(path for path in sorted(root.rglob("*")) if path.is_dir())]


def _inventory(root: Path) -> List[Dict[str, object]]:
    return [
        {
            "relative_path": path.relative_to(root).as_posix(),
            "sha256": _file_digest(path),
            "mode": _mode(path),
        }
        for path in _tree_files(root)
    ]


def _saved_file(path: Path) -> Optional[Dict[str, object]]:
    if not path.is_file():
        return None
    data = path.read_bytes()
    return {
        "content": base64.b64encode(data).decode("ascii"),
        "sha256": _digest(data),
        "mode": _mode(path),
    }


def _extract_menu_helper(template: Path, destination: Path) -> None:
    result = subprocess.run(
        [
            "/bin/sh",
            "-c",
            'bunzip2 -c "$1" | cpio -id',
            "extract-menu-helper",
            str(template),
        ],
        cwd=str(destination),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")
        raise OSError("Menu Helper extraction failed: {}".format(detail.strip()))


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

    def _bind_owned_directory_cleanup(
        self, transaction: Transaction, installation: GameInstallation
    ) -> None:
        if getattr(transaction, "_launcher_directory_cleanup_bound", False):
            return
        original = transaction.handlers.get("restore_launcher")
        if original is None:
            return

        def restore_and_prune(record: UndoRecord) -> None:
            original(record)
            if record.data.get("purge_menu") is True:
                command = [
                    str(self._cxmenu(installation)),
                    "--bottle",
                    self._command_bottle(installation),
                ]
                command.extend(installation.bottle.scope_args())
                command.extend(["--purge", "--filter", LAUNCHER_MENU])
                self.runner.run(command, timeout=60.0)
            directories = record.data.get("owned_directories", [])
            if not isinstance(directories, list):
                return
            paths = [Path(item) for item in directories if isinstance(item, str)]
            for directory in sorted(paths, key=lambda path: len(path.parts), reverse=True):
                try:
                    directory.rmdir()
                except (FileNotFoundError, OSError):
                    pass

        handlers = dict(transaction.handlers)
        handlers["restore_launcher"] = restore_and_prune
        transaction.handlers = handlers
        setattr(transaction, "_launcher_directory_cleanup_bound", True)

    def _app_path(self) -> Path:
        if self.launcher_destination.suffix == ".app":
            return self.launcher_destination.resolve(strict=False)
        return (self.launcher_destination / (LAUNCHER_NAME + ".app")).resolve(
            strict=False
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

    def _config_data(self, installation: GameInstallation) -> Dict[str, object]:
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
            "messages": {
                "already_running": "Ostriv is already starting or running.",
                "steam_wait": "Waiting for Steam to finish starting.",
                "steam_login": "Sign in to Steam, then open Ostriv (patched) again.",
                "steam_timeout": "Steam did not finish starting. Quit CrossOver and try again.",
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
                            "owned_directories": [
                                str(path) for path in _tree_directories(pending)
                            ],
                        },
                    )
                )
            except BaseException:
                shutil.rmtree(pending, ignore_errors=True)
                raise

        transaction.step(
            "extract launcher template",
            UndoRecord("restore_launcher", {"snapshots": []}),
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
    ) -> List[Dict[str, object]]:
        paths = (
            app,
            app / "Contents/Info.plist",
            app / "Contents/MacOS/Menu Helper",
            app / "Contents/Resources/CrossOverHelper.icns",
            runtime,
            config,
        )
        return [
            {"path": str(path), **({"sha256": _file_digest(path)} if path.is_file() else {})}
            for path in paths
        ]

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
        files = _tree_files(root)

        def remove() -> None:
            for path in reversed(files):
                path.unlink()
            for directory in sorted(
                _tree_directories(root), key=lambda path: len(path.parts), reverse=True
            ):
                directory.rmdir()

        transaction.step(
            "remove replaced launcher app",
            UndoRecord(
                "restore_launcher",
                {"snapshots": [_snapshot(path) for path in files]},
            ),
            remove,
        )

    def install(
        self, transaction: Transaction, installation: GameInstallation
    ) -> LauncherState:
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
                previous_text = prior.get("previous_app")
                previous = Path(previous_text) if isinstance(previous_text, str) else None
                previous_inventory = list(prior.get("previous_app_inventory", []))
                previous_runtime = prior.get("previous_runtime")
                previous_config = prior.get("previous_config")
                app_backup = app.with_name("." + app.name + ".ostriv-macos.replaced")
            else:
                previous = stable_previous if app.exists() else None
                previous_inventory = _inventory(app) if previous is not None else []
                previous_runtime = _saved_file(
                    installation.bottle.root.resolve() / RUNTIME_NAME
                )
                previous_config = _saved_file(
                    installation.bottle.root.resolve() / CONFIG_NAME
                )
                app_backup = previous
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
                if path.exists():
                    raise PatchError(
                        "install.launcher_ownership",
                        "Installation failed.",
                        "Pending launcher path already exists: {}".format(path),
                    )

            runtime_data = self.runtime_source.read_bytes()
            config_data = self._config_data(installation)
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
                    "owned_directories": (
                        [
                            str(app / path.relative_to(pending))
                            for path in _tree_directories(pending)
                        ]
                        if app_backup is None
                        else [
                            str(app_backup / path.relative_to(app))
                            for path in _tree_directories(app)
                        ]
                    ),
                },
            )

            def swap_app() -> None:
                if app_backup is not None:
                    os.replace(app, app_backup)
                os.replace(pending, app)

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

            menu_command = [
                str(cxmenu),
                "--bottle",
                self._command_bottle(installation),
            ]
            menu_command.extend(installation.bottle.scope_args())
            menu_command.extend(
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

            def register_menu() -> None:
                result = self.runner.run(menu_command, timeout=60.0)
                if result.returncode != 0:
                    raise PatchError(
                        "install.launcher_menu",
                        "Installation failed.",
                        "cxmenu failed: {}".format(result.stderr or result.stdout),
                    )

            transaction.step(
                "register launcher menu",
                UndoRecord(
                    "restore_launcher",
                    {"snapshots": [], "purge_menu": True},
                ),
                register_menu,
            )
            state = LauncherState(
                schema=1,
                artifacts=self._expected_artifacts(app, runtime, config),
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
            expected_identity = {
                "CFBundleName": LAUNCHER_NAME,
                "CFBundleDisplayName": LAUNCHER_NAME,
                "CFBundleIdentifier": self._bundle_id(installation.bottle.name),
                "CrossOverHelperCommand": str(state["command"]),
                "CXHelperAppBottleName": installation.bottle.name,
                "CXHelperAppBottleTag": str(state["bottle_tag"]),
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
            if identity_failures:
                raise PatchError(
                    "install.launcher_verify",
                    "Installation failed.",
                    "\n".join(identity_failures),
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
            if _file_digest(app / "Contents/MacOS/Menu Helper") != state.get(
                "executable_sha256"
            ):
                raise PatchError(
                    "install.launcher_verify",
                    "Installation failed.",
                    "Menu Helper executable digest does not match",
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
            expected = item.get("sha256")
            if isinstance(expected, str) and path.is_file() and _file_digest(path) == expected:
                path.unlink()
        if app.is_dir():
            for directory in sorted(
                (path for path in app.rglob("*") if path.is_dir()),
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

    @staticmethod
    def _restore_previous_app(app: Path, previous: Path, inventory: object) -> None:
        if not previous.is_dir() or not isinstance(inventory, list):
            return
        for item in inventory:
            if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str):
                continue
            relative = Path(item["relative_path"])
            if relative.is_absolute() or ".." in relative.parts:
                continue
            source = previous / relative
            destination = app / relative
            expected = item.get("sha256")
            if not isinstance(expected, str) or not source.is_file() or _file_digest(source) != expected:
                raise PatchError(
                    "restore.launcher_ownership",
                    "Restore failed.",
                    "Recorded previous launcher changed: {}".format(source),
                )
            if destination.exists():
                raise PatchError(
                    "restore.launcher_ownership",
                    "Restore failed.",
                    "Unknown file blocks previous launcher restore: {}".format(destination),
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
        for directory in sorted(
            (path for path in previous.rglob("*") if path.is_dir()),
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

    def restore(
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
                    "cxmenu purge failed: {}".format(result.stderr or result.stdout),
                )

        if state.get("legacy") is True:
            for item in state.get("artifacts", []):
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    continue
                path = Path(item["path"])
                if path.resolve(strict=False) == self._app_path() and path.is_dir():
                    shutil.rmtree(path)
                elif (
                    path.resolve(strict=False)
                    == installation.bottle.root.resolve() / RUNTIME_NAME
                    and path.is_file()
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
