"""Read-only discovery of CrossOver installations, bottles, and Ostriv games."""

import configparser
import os
import plistlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

from ostriv_macos.diagnostics import CommandRunner, PatchError


DEFAULT_MANAGED_BOTTLES = Path("/Library/Application Support/CrossOver/Bottles")


@dataclass(frozen=True)
class CrossOverInstall:
    app: Path
    shared_support: Path
    version: Optional[str]


@dataclass(frozen=True)
class Bottle:
    name: str
    root: Path
    scope: str
    crossover: CrossOverInstall

    def command_bottle(self) -> str:
        return self.name if self.scope == "managed" else str(self.root)

    def scope_args(self) -> List[str]:
        return ["--scope", "managed"] if self.scope == "managed" else []


@dataclass(frozen=True)
class GameInstallation:
    bottle: Bottle
    game_dir: Path
    version: Optional[str]


def _shared_support(app: Path) -> Path:
    return app / "Contents/SharedSupport/CrossOver"


def _usable_app(app: Path) -> bool:
    return os.access(_shared_support(app) / "bin/wine", os.X_OK)


def _version(app: Path) -> Optional[str]:
    try:
        with (app / "Contents/Info.plist").open("rb") as stream:
            contents = plistlib.load(stream)
        return contents.get("CFBundleShortVersionString") or contents.get("CFBundleVersion")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None


def _add_install(apps: List[CrossOverInstall], seen: set, candidate: Path) -> None:
    try:
        app = candidate.expanduser().resolve()
    except OSError:
        return
    if app in seen or not _usable_app(app):
        return
    seen.add(app)
    apps.append(CrossOverInstall(app, _shared_support(app), _version(app)))


def _acceptable_app_path(path: str) -> bool:
    junk = ("/.Trash/", "/AppTranslocation/", "/private/var/folders/", "/var/folders/")
    return path.endswith("CrossOver.app") and not any(part in path for part in junk)


def _runner_stdout(runner: CommandRunner, argv: Sequence[str], timeout: float) -> str:
    try:
        return runner.run(argv, timeout=timeout).stdout
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return ""


def _spotlight_apps(runner: CommandRunner) -> Iterable[Path]:
    for argv in (
        ["mdfind", "kMDItemCFBundleIdentifier == 'com.codeweavers.CrossOver'"],
        ["mdfind", "-name", "CrossOver.app"],
    ):
        for line in _runner_stdout(runner, argv, 5).splitlines():
            path = line.strip()
            if _acceptable_app_path(path):
                yield Path(path)

    lsregister = (
        "/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    output = _runner_stdout(runner, [lsregister, "-dump"], 10)
    for match in re.finditer(r"(/[^\n]+?/CrossOver\.app)(?=\s|$)", output):
        path = match.group(1).strip().strip('"')
        if _acceptable_app_path(path):
            yield Path(path)


def find_crossover_apps(
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[CommandRunner] = None,
    allow_subprocess: bool = True,
    system_app: Path = Path("/Applications/CrossOver.app"),
) -> List[CrossOverInstall]:
    """Return usable CrossOver bundles, preferring configured and conventional locations.

    ``allow_subprocess=False`` is deliberately process-free for diagnosis callers.
    """
    actual_home = (home or Path.home()).expanduser()
    actual_env = os.environ if env is None else env
    candidates: List[Path] = []
    configured = actual_env.get("OSTRIV_CROSSOVER_APP")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend((actual_home / "Applications/CrossOver.app", system_app))

    apps: List[CrossOverInstall] = []
    seen = set()
    for candidate in candidates:
        _add_install(apps, seen, candidate)

    if allow_subprocess:
        actual_runner = runner or CommandRunner()
        for candidate in _spotlight_apps(actual_runner):
            _add_install(apps, seen, candidate)
    return apps


def _absolute_paths(value: Optional[str]) -> Iterable[Path]:
    if not value:
        return []
    return (Path(item) for item in value.split(os.pathsep) if Path(item).is_absolute())


def _config_bottle_paths(home: Path) -> Iterable[Path]:
    config = configparser.ConfigParser(interpolation=None)
    filename = home / "Library/Application Support/CrossOver/CrossOver.conf"
    try:
        with filename.open(encoding="utf-8") as stream:
            config.read_file(stream)
        value = config.get("CrossOver", "BottlePath", fallback="")
    except (OSError, configparser.Error, UnicodeError):
        return []
    return _absolute_paths(value)


def _root_sources(
    home: Path, env: Mapping[str, str], managed_root: Path
) -> List[Tuple[Path, str]]:
    private_default = home / "Library/Application Support/CrossOver/Bottles"
    private_paths = list(_config_bottle_paths(home))
    private_paths.extend(_absolute_paths(env.get("CX_BOTTLE_PATH")))
    private_paths.append(private_default)
    managed_paths = list(_absolute_paths(env.get("CX_MANAGED_BOTTLE_PATH")))
    managed_paths.append(managed_root)
    return [(path, "managed") for path in managed_paths] + [
        (path, "private") for path in private_paths
    ]


def configured_bottle_roots(
    home: Path, env: Mapping[str, str], managed_root: Path = DEFAULT_MANAGED_BOTTLES
) -> List[Path]:
    """Collect configured, environment, and default bottle directories in stable order."""
    roots: List[Path] = []
    seen = set()
    for root, _scope in _root_sources(home, env, managed_root):
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)
    return roots


def _valid_bottle(root: Path) -> bool:
    return (
        root.is_dir()
        and (root / "cxbottle.conf").is_file()
        and (root / "system.reg").is_file()
        and (root / "drive_c").is_dir()
    )


def _helper_bottles(home: Path) -> Iterable[Path]:
    helpers = home / "Applications/CrossOver"
    try:
        plists = sorted(helpers.rglob("Info.plist"))
    except OSError:
        return []
    results = []
    for plist in plists:
        try:
            with plist.open("rb") as stream:
                contents = plistlib.load(stream)
            name = contents.get("CXHelperAppBottleName")
            tag = contents.get("CXHelperAppBottleTag")
            command = contents.get("CrossOverHelperCommand")
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        if (
            isinstance(name, str)
            and Path(name).is_absolute()
            and str(tag or "private").lower() == "private"
        ):
            results.append(Path(name))
    return results


def _append_bottle(
    bottles: List[Bottle],
    seen: set,
    name: str,
    root: Path,
    scope: str,
    crossover: CrossOverInstall,
) -> None:
    try:
        resolved = root.resolve()
    except OSError:
        return
    if resolved in seen or not _valid_bottle(resolved):
        return
    seen.add(resolved)
    bottles.append(Bottle(name, resolved, scope, crossover))


def discover_bottles(
    crossover: CrossOverInstall,
    home: Path,
    env: Mapping[str, str],
    managed_root: Path = DEFAULT_MANAGED_BOTTLES,
) -> List[Bottle]:
    """Discover valid private and managed bottles without changing CrossOver state."""
    bottles: List[Bottle] = []
    seen = set()
    for root, scope in _root_sources(home, env, managed_root):
        try:
            entries = sorted(os.scandir(root), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir(follow_symlinks=True):
                _append_bottle(bottles, seen, entry.name, Path(entry.path), scope, crossover)
    for root in _helper_bottles(home):
        _append_bottle(bottles, seen, root.name, root, "private", crossover)
    return bottles


def _search_ostriv(root: Path, depth: int) -> Iterable[Path]:
    if depth == 0:
        return
    try:
        entries = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return
    for entry in entries:
        if entry.name.lower() == "ostriv.exe" and entry.is_file():
            yield entry.parent
            return
        if entry.is_dir():
            yield from _search_ostriv(entry, depth - 1)


def _ostriv_version(bottle: Bottle) -> Optional[str]:
    log = bottle.root / "drive_c/users/crossover/Saved Games/Ostriv/log.txt"
    try:
        contents = log.read_text(encoding="utf-8", errors="ignore")[:400]
    except OSError:
        return None
    match = re.search(r"\((\d+\.\d+\.\d+\.\d+)", contents)
    return match.group(1) if match else None


def discover_games(bottles: Iterable[Bottle]) -> List[GameInstallation]:
    """Find one Ostriv installation per supplied bottle."""
    games = []
    for bottle in bottles:
        for game_dir in _search_ostriv(bottle.root / "drive_c", depth=8):
            games.append(GameInstallation(bottle, game_dir, _ostriv_version(bottle)))
    return games


def is_supported_game_directory(game_dir: Path, bottle_root: Path) -> bool:
    """Return whether *game_dir* is a canonical Ostriv directory in drive_c."""
    try:
        candidate = game_dir.resolve()
        drive_c = (bottle_root / "drive_c").resolve()
        executable = candidate / "ostriv.exe"
        return (
            candidate.is_dir()
            and drive_c in candidate.parents
            and executable.is_file()
            and not executable.is_symlink()
        )
    except OSError:
        return False


def resolve_explicit_game(
    game_dir: Path, crossovers: Sequence[CrossOverInstall]
) -> GameInstallation:
    """Resolve a game directory to its enclosing bottle, including external bottles."""
    candidate = game_dir.resolve()
    if candidate.is_file():
        candidate = candidate.parent
    current = candidate
    while current != current.parent:
        if _valid_bottle(current):
            if not is_supported_game_directory(candidate, current):
                raise PatchError(
                    "discovery.explicit_not_ostriv",
                    "The selected folder is not a supported Ostriv installation.",
                    str(game_dir),
                )
            if not crossovers:
                raise PatchError(
                    "discovery.no_crossover",
                    "CrossOver could not be found.",
                    "No CrossOver installation was available to run the selected game.",
                )
            bottle = Bottle(current.name, current, "private", crossovers[0])
            return GameInstallation(bottle, candidate, _ostriv_version(bottle))
        current = current.parent
    raise PatchError(
        "discovery.explicit_not_in_bottle",
        "The selected Ostriv folder is not inside a valid CrossOver bottle.",
        str(game_dir),
    )
