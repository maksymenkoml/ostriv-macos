"""Concise player-facing command line entrypoint."""

import argparse
import json
import logging
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Mapping, Optional, Sequence, Tuple

from .diagnostics import CommandRunner, PatchError, PlayerOutput, configure_logger

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - only non-POSIX hosts
    termios = None
    tty = None


REQUIRED_RELEASE_FILES = (
    "patch.py",
    "ostriv_macos/__init__.py",
    "ostriv_macos/cli.py",
    "ostriv_macos/diagnostics.py",
    "ostriv_macos/discovery.py",
    "ostriv_macos/installer.py",
    "ostriv_macos/launcher.py",
    "ostriv_macos/launcher_runtime.py",
    "ostriv_macos/payload.py",
    "assets/settings.data",
    "README.md",
    "LICENSE",
)


@dataclass(frozen=True)
class DiagnosticContext:
    package_root: Path
    home: Path
    env: Mapping[str, str]
    game_path: Optional[str] = None
    system_app: Path = Path("/Applications/CrossOver.app")


@dataclass(frozen=True)
class DiagnosticSummary:
    python: str
    crossover: str
    bottle_roots: Tuple[Path, ...]
    games: Tuple[str, ...]
    payload: str
    installation: Tuple[str, ...]
    launcher: Tuple[str, ...]
    log_paths: Tuple[Path, ...]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("game_path", nargs="?")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--preflight", action="store_true", help=argparse.SUPPRESS)
    return parser


def shorten_path(path: Path, home: Optional[Path] = None) -> str:
    actual_home = (home or Path.home()).expanduser()
    try:
        actual_home = actual_home.resolve()
        actual_path = path.expanduser().resolve()
        relative = actual_path.relative_to(actual_home)
    except (OSError, ValueError):
        return str(path)
    return "~" if not relative.parts else "~/" + str(relative)


def _null_logger() -> logging.Logger:
    logger = logging.getLogger("ostriv_macos.read_only")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    return logger


def _release_payload(package_root: Path):
    from .payload import load_manifest, validate_payload

    manifest = load_manifest(package_root / "payload-manifest.json")
    validate_payload(package_root, manifest)
    missing = [
        relative
        for relative in REQUIRED_RELEASE_FILES
        if not (package_root / relative).is_file()
    ]
    if missing:
        raise PatchError(
            "payload.release_files",
            "The download is incomplete. Download the release ZIP again.",
            "Missing release files: " + ", ".join(missing),
        )
    return manifest


def preflight(package_root: Path) -> int:
    """Validate the release directly, without CrossOver discovery or writes."""
    _release_payload(Path(package_root).resolve())
    return 0


def _read_json_state(path: Path) -> Optional[Mapping[str, object]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _diagnostic_installation(game) -> str:
    from .installer import JOURNAL_NAME, STATE_NAME

    journal_path = game.bottle.root / JOURNAL_NAME
    if journal_path.is_file():
        journal = _read_json_state(journal_path)
        if journal is None:
            return "journal unreadable"
        if journal.get("complete") is not True:
            return "recovery pending"
    state_path = game.bottle.root / STATE_NAME
    if not state_path.is_file():
        return "not installed"
    return "installed" if _read_json_state(state_path) is not None else "state unreadable"


def _diagnostic_launcher(game, home: Path) -> str:
    app = home / "Applications/CrossOver/Ostriv (patched).app"
    runtime = game.bottle.root / "play-ostriv-patched.py"
    config = game.bottle.root / "launcher-config.json"
    present = sum(path.is_file() or path.is_dir() for path in (app, runtime, config))
    if present == 0:
        return "not installed"
    return "installed" if present == 3 else "incomplete"


def diagnose(context: DiagnosticContext) -> DiagnosticSummary:
    """Read direct package and CrossOver state without starting any process."""
    from .discovery import (
        configured_bottle_roots,
        discover_bottles,
        discover_games,
        find_crossover_apps,
        resolve_explicit_game,
    )

    home = context.home.expanduser()
    crossovers = find_crossover_apps(
        home=home,
        env=context.env,
        allow_subprocess=False,
        system_app=context.system_app,
    )
    if crossovers:
        crossover = "; ".join(
            "{} · {}".format(shorten_path(item.app, home), item.version or "unknown")
            for item in crossovers
        )
    else:
        crossover = "not found"

    roots = tuple(configured_bottle_roots(home, context.env))
    bottles = []
    seen_bottles = set()
    for item in crossovers:
        for bottle in discover_bottles(item, home, context.env):
            if bottle.root not in seen_bottles:
                seen_bottles.add(bottle.root)
                bottles.append(bottle)
    games = discover_games(bottles)
    if context.game_path and crossovers:
        try:
            explicit = resolve_explicit_game(Path(context.game_path), crossovers)
        except PatchError:
            pass
        else:
            games = [explicit]

    try:
        _release_payload(context.package_root.resolve())
        payload = "OK"
    except PatchError:
        payload = "FAILED"

    game_lines = tuple(
        "{} · Ostriv {} · {}".format(
            game.bottle.name,
            game.version or "unknown",
            shorten_path(game.game_dir, home),
        )
        for game in games
    )
    installation = tuple(_diagnostic_installation(game) for game in games)
    launcher = tuple(_diagnostic_launcher(game, home) for game in games)
    log_root = home / "Library/Logs/ostriv-macos"
    installer_log = log_root / "install.log"
    log_paths = [installer_log]
    try:
        for path in sorted(log_root.glob("*.log")):
            if path != installer_log:
                log_paths.append(path)
    except OSError:
        pass
    return DiagnosticSummary(
        python=platform.python_version(),
        crossover=crossover,
        bottle_roots=roots,
        games=game_lines,
        payload=payload,
        installation=installation,
        launcher=launcher,
        log_paths=tuple(log_paths),
    )


def _summary_value(items: Sequence[str], empty: str = "none") -> str:
    return "; ".join(items) if items else empty


def render_diagnosis(summary: DiagnosticSummary, output: PlayerOutput) -> None:
    output._line("Python: " + summary.python)
    output._line("CrossOver: " + summary.crossover)
    output._line(
        "Bottle roots: "
        + _summary_value([shorten_path(path) for path in summary.bottle_roots])
    )
    output._line("Ostriv: " + _summary_value(summary.games))
    output._line("Package: " + summary.payload)
    output._line("Installation: " + _summary_value(summary.installation, "not found"))
    output._line("Launcher: " + _summary_value(summary.launcher, "not found"))
    output._line(
        "Logs: " + _summary_value([shorten_path(path) for path in summary.log_paths])
    )


class ProductionServices:
    def __init__(
        self,
        package_root: Path,
        installer,
        runner: CommandRunner,
        stdin: IO[str],
        output: PlayerOutput,
        home: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.package_root = package_root.resolve()
        self.installer = installer
        self.runner = runner
        self.stdin = stdin
        self.output = output
        self.home = (home or Path.home()).expanduser()
        self.env = os.environ if env is None else env

    def validate_package(self):
        return _release_payload(self.package_root)

    def find_games(self, game_path=None):
        from .discovery import (
            discover_bottles,
            discover_games,
            find_crossover_apps,
            resolve_explicit_game,
        )

        crossovers = find_crossover_apps(
            home=self.home,
            env=self.env,
            runner=self.runner,
        )
        if game_path:
            return [resolve_explicit_game(Path(game_path).expanduser(), crossovers)]
        games = []
        seen = set()
        for crossover in crossovers:
            for game in discover_games(discover_bottles(crossover, self.home, self.env)):
                resolved = game.game_dir.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    games.append(game)
        return games

    def is_installed(self, installation) -> bool:
        return self.installer.state_path(installation).is_file() or (
            installation.game_dir / "libgallium_wgl.dll"
        ).is_file()

    def install(self, installation, payload):
        return self.installer.install(installation, payload)

    def restore(self, installation):
        return self.installer.restore(installation)

    def print_diagnosis(self, game_path=None):
        summary = diagnose(DiagnosticContext(self.package_root, self.home, self.env, game_path))
        render_diagnosis(summary, self.output)
        return summary


def build_services(package_root: Path, logger, stdin: IO[str], output: PlayerOutput):
    from .installer import Installer
    from .launcher import LauncherInstaller

    runner = CommandRunner()
    launcher = LauncherInstaller(package_root, runner=runner)
    installer = Installer(package_root, launcher, runner=runner)
    return ProductionServices(package_root, installer, runner, stdin, output)


def _found_detail(installation) -> str:
    crossover = installation.bottle.crossover.version or "unknown"
    ostriv = installation.version or "unknown"
    return "CrossOver {} · Ostriv {} · {}".format(
        crossover, ostriv, installation.bottle.name
    )


def _game_label(installation) -> str:
    return "{} · Ostriv {} · {}".format(
        installation.bottle.name,
        installation.version or "unknown",
        shorten_path(installation.game_dir),
    )


def select(
    prompt: str,
    options: Sequence[str],
    stdin: IO[str] = sys.stdin,
    stdout: IO[str] = sys.stdout,
) -> Optional[int]:
    """Select with arrow keys on a TTY and numbered input otherwise."""
    count = len(options)
    if not (
        termios
        and tty
        and getattr(stdin, "isatty", lambda: False)()
        and getattr(stdout, "isatty", lambda: False)()
    ):
        print(prompt, file=stdout)
        for index, label in enumerate(options, 1):
            print("  [{}] {}".format(index, label), file=stdout)
        while True:
            stdout.write("Select (number, or Q to quit): ")
            stdout.flush()
            value = stdin.readline()
            if value == "":
                stdout.write("\n")
                return None
            stdout.write("\n")
            value = value.strip().lower()
            if value in ("q", "quit"):
                return None
            if value.isdigit() and 1 <= int(value) <= count:
                return int(value) - 1
            print("Enter 1-{} or Q".format(count), file=stdout)

    print(prompt, file=stdout)
    print("  (↑/↓ to move · Enter to select · q to quit)", file=stdout)
    selected = 0

    def draw(first: bool = False) -> None:
        if not first:
            stdout.write("\x1b[{}A".format(count))
        for index, label in enumerate(options):
            marker = "❯ " if index == selected else "  "
            stdout.write("\r\x1b[K  {}{}\r\n".format(marker, label))
        stdout.flush()

    descriptor = stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        draw(first=True)
        while True:
            character = stdin.read(1)
            if character == "\x1b" and stdin.read(1) == "[":
                arrow = stdin.read(1)
                if arrow == "A":
                    selected = (selected - 1) % count
                    draw()
                elif arrow == "B":
                    selected = (selected + 1) % count
                    draw()
            elif character in ("\r", "\n"):
                return selected
            elif character in ("q", "Q"):
                return None
            elif character == "\x03":
                raise KeyboardInterrupt
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def run_interactive(
    services,
    game_path,
    output: PlayerOutput,
    log_path: Path,
    stdin: IO[str] = sys.stdin,
    stdout: IO[str] = sys.stdout,
) -> int:
    try:
        payload = services.validate_package()
    except PatchError:
        output.stage("Package", "FAILED")
        raise

    try:
        games = services.find_games(game_path)
        if not games:
            raise PatchError(
                "discovery.no_game",
                "Ostriv could not be found. Choose its folder and try again.",
            )
        if len(games) == 1:
            installation = games[0]
        else:
            selected = select(
                "Select an Ostriv installation:",
                [_game_label(game) for game in games],
                stdin,
                stdout,
            )
            if selected is None:
                output.failure("Cancelled.", log_path)
                return 0
            installation = games[selected]
    except PatchError:
        output.stage("Discovery", "FAILED")
        raise

    output.stage("Found", _found_detail(installation))
    output.stage("Package", "OK")
    restore = False
    if services.is_installed(installation):
        output._line("Reinstall reapplies the patch; Restore removes it.")
        selected = select("Choose an action:", ("Reinstall", "Restore"), stdin, stdout)
        if selected is None:
            output.failure("Cancelled.", log_path)
            return 0
        restore = selected == 1

    try:
        if restore:
            services.restore(installation)
        else:
            services.install(installation, payload)
    except Exception:
        output.stage("Installation", "FAILED")
        raise
    output.stage("Installation", "OK")
    output.success(log_path)
    return 0


def main(
    argv: Optional[Sequence[str]] = None,
    services=None,
    stdin: IO[str] = sys.stdin,
    stdout: IO[str] = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    package_root = Path(__file__).resolve().parent.parent
    log_path = Path.home() / "Library/Logs/ostriv-macos/install.log"

    if args.preflight:
        logger = _null_logger()
        try:
            if services is None:
                return preflight(package_root)
            services.validate_package()
            return 0
        except PatchError as error:
            logger.error("%s: %s", error.code, error.detail or error.player_message)
            return 2
        except Exception:
            logger.exception("unexpected preflight failure")
            return 3

    output = PlayerOutput(stdout)
    output.title()
    if args.diagnose:
        _null_logger()
        try:
            if services is not None:
                services.print_diagnosis(args.game_path)
            else:
                render_diagnosis(
                    diagnose(
                        DiagnosticContext(package_root, Path.home(), os.environ, args.game_path)
                    ),
                    output,
                )
        except Exception:
            output._line("Diagnosis: some state could not be read.")
        return 0

    logger = _null_logger() if services is not None else configure_logger(log_path)
    try:
        active = services or build_services(package_root, logger, stdin, output)
        return run_interactive(
            active, args.game_path, output, log_path, stdin=stdin, stdout=stdout
        )
    except KeyboardInterrupt:
        output.failure("Cancelled.", log_path)
        return 130
    except PatchError as error:
        logger.error("%s: %s", error.code, error.detail or error.player_message)
        output.failure(error.player_message, log_path)
        return 2
    except Exception:
        logger.exception("unexpected installer failure")
        output.failure("Something went wrong. Try Reinstall once.", log_path)
        return 3
