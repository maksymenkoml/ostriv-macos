"""Standalone runtime copied next to the installed CrossOver launcher.

This module intentionally imports only the Python standard library: the installed copy must
keep working after the release directory that supplied it has gone away.
"""

import atexit
import ctypes
import fcntl
import hashlib
import json
import logging
import os
import plistlib
import signal
import stat
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


SRGB_PROFILE = "/System/Library/ColorSync/Profiles/sRGB Profile.icc"
DISPLAY_RECOVERY_SUFFIX = ".display-recovery.json"
FALLBACK_ERROR_MESSAGE = "Unable to start Ostriv."


class LauncherRuntimeError(RuntimeError):
    """A handled launcher failure whose player message comes from configuration."""

    def __init__(self, message_key: str, detail: str = "") -> None:
        super().__init__(detail or message_key)
        self.message_key = message_key
        self.detail = detail or message_key


class ProcessLock:
    """A kernel-owned advisory lock whose on-disk path may safely outlive a process."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd = None

    def _validate_descriptor_path(self, descriptor: int) -> os.stat_result:
        opened = os.fstat(descriptor)
        current = os.lstat(str(self.path))
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
        ):
            raise OSError("launcher lock path changed after open")
        return opened

    def validate_current_path(
        self,
        expected_content: Optional[bytes] = None,
        expected_mode: Optional[int] = None,
    ) -> None:
        """Prove the held descriptor still names the exact owned lock leaf."""
        if self.fd is None:
            raise OSError("launcher lock is not acquired")
        opened = self._validate_descriptor_path(self.fd)
        if (
            expected_mode is not None
            and stat.S_IMODE(opened.st_mode) != expected_mode
        ):
            raise OSError("launcher lock mode changed after open")
        if expected_content is not None:
            content = os.pread(self.fd, len(expected_content) + 1, 0)
            if content != expected_content:
                raise OSError("launcher lock content changed after open")

    def acquire(self) -> bool:
        if self.fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            str(self.path),
            os.O_RDWR
            | os.O_CREAT
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise OSError("launcher lock is not an owned regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._validate_descriptor_path(descriptor)
        except BlockingIOError:
            os.close(descriptor)
            return False
        except BaseException:
            os.close(descriptor)
            raise
        self.fd = descriptor
        return True

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


MAX_LOG_EVIDENCE_BYTES = 256 * 1024
LOG_TOKEN_TAIL_BYTES = 64 * 1024
MAX_STEAM_HOST_PIDS = 64
MAX_PID_TEXT_DIGITS = 10
MAX_USER_REGISTRY_BYTES = 4 * 1024 * 1024
ACTIVE_USER_CONFIRMATION_SECONDS = 10.0
_AMBIGUOUS_LOG_EVIDENCE = "<changed log generation outside bounded evidence>"


def _parse_pid(value) -> Optional[int]:
    text = value.strip() if isinstance(value, str) else ""
    if (
        not text
        or len(text) > MAX_PID_TEXT_DIGITS
        or not text.isascii()
        or not text.isdigit()
    ):
        return None
    try:
        pid = int(text, 10)
    except ValueError:
        return None
    return pid if 0 < pid < 2**31 else None


@dataclass(frozen=True)
class LogGeneration:
    present: bool
    device: int = 0
    inode: int = 0
    size: int = 0
    mtime_ns: int = 0
    head_size: int = 0
    head_sha256: str = ""
    tail_start: int = 0
    tail_sha256: str = ""


def _segment_digest(path: Path, start: int, size: int) -> str:
    checksum = hashlib.sha256()
    remaining = max(0, size)
    with Path(path).open("rb") as stream:
        stream.seek(start)
        while remaining:
            chunk = stream.read(min(remaining, 1024 * 1024))
            if not chunk:
                break
            checksum.update(chunk)
            remaining -= len(chunk)
    return checksum.hexdigest()


def capture_log_generation(path: Path) -> LogGeneration:
    """Capture bounded identity/content evidence for a possibly overwritten log."""
    path = Path(path)
    try:
        status = path.stat()
    except FileNotFoundError:
        return LogGeneration(False)
    head_size = min(status.st_size, LOG_TOKEN_TAIL_BYTES)
    tail_start = max(head_size, status.st_size - LOG_TOKEN_TAIL_BYTES)
    return LogGeneration(
        True,
        status.st_dev,
        status.st_ino,
        status.st_size,
        getattr(status, "st_mtime_ns", int(status.st_mtime * 1_000_000_000)),
        head_size,
        _segment_digest(path, 0, head_size),
        tail_start,
        _segment_digest(path, tail_start, status.st_size - tail_start),
    )


def _matches_generation_samples(path: Path, generation: LogGeneration) -> bool:
    return (
        _segment_digest(path, 0, generation.head_size) == generation.head_sha256
        and _segment_digest(
            path,
            generation.tail_start,
            generation.size - generation.tail_start,
        )
        == generation.tail_sha256
    )


def _read_bounded_segment(path: Path, start: int, end: int) -> bytes:
    length = max(0, end - start)
    with Path(path).open("rb") as stream:
        stream.seek(start)
        if length <= MAX_LOG_EVIDENCE_BYTES:
            return stream.read(length)
        half = MAX_LOG_EVIDENCE_BYTES // 2
        first = stream.read(half)
        stream.seek(end - half)
        last = stream.read(half)
    return first + b"\n<fresh log evidence truncated>\n" + last


def read_new_log(path: Path, generation) -> str:
    """Return only bounded evidence written after *generation*."""
    path = Path(path)
    try:
        status = path.stat()
    except FileNotFoundError:
        return ""
    if isinstance(generation, int):
        data = _read_bounded_segment(path, min(generation, status.st_size), status.st_size)
        return data.decode("utf-8", errors="replace")
    if not isinstance(generation, LogGeneration):
        raise TypeError("invalid log generation token")
    if not generation.present:
        start = 0
    else:
        same_identity = (
            status.st_dev == generation.device and status.st_ino == generation.inode
        )
        if (
            same_identity
            and status.st_size == generation.size
            and _matches_generation_samples(path, generation)
        ):
            current_mtime_ns = getattr(
                status, "st_mtime_ns", int(status.st_mtime * 1_000_000_000)
            )
            if current_mtime_ns == generation.mtime_ns:
                return ""
            return _AMBIGUOUS_LOG_EVIDENCE
        append_only = (
            same_identity
            and status.st_size >= generation.size
            and _matches_generation_samples(path, generation)
        )
        start = generation.size if append_only else 0
    return _read_bounded_segment(path, start, status.st_size).decode(
        "utf-8", errors="replace"
    )


def classify_launch(text: str) -> str:
    if text == _AMBIGUOUS_LOG_EVIDENCE:
        return "ambiguous_log"
    if "SteamAPI_Init() failed" in text:
        return "steam_api"
    if "windows_createWindow FAILED" in text:
        return "graphics_context"
    if "done exiting." in text:
        return "clean_exit"
    return "other"


def _create_launcher_log(path: Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ostriv_macos.launcher")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = logging.FileHandler(
        str(path), encoding="utf-8", errors="backslashreplace"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


@dataclass(frozen=True)
class SteamSignals:
    process: bool
    active_user: bool
    renderer: bool
    process_known: bool = True

    @property
    def ready(self) -> bool:
        return self.process and self.active_user and self.renderer


@dataclass(frozen=True)
class HostSteamProbe:
    process: bool
    renderer: bool
    process_known: bool
    steam_pids: frozenset
    started_at: Optional[float]


@dataclass(frozen=True)
class ActiveUserSnapshot:
    value: Optional[bool]
    generation: Optional[tuple] = None
    mtime: float = 0.0


@dataclass(frozen=True)
class ExternalResult:
    returncode: int
    stdout: str
    stderr: str
    diagnostic: str = ""


class ExternalCommandError(RuntimeError):
    """An external command failure safe to serialize into the launcher log."""


class ExternalProcessRunner:
    """Run external commands without ever exposing their raw output to the player."""

    ALLOWED_EXECUTABLES = frozenset(
        {"lsof", "open", "osascript", "pgrep", "ps", "wine"}
    )
    SENSITIVE_OPTIONS = frozenset(
        {"--api-key", "--bottle", "--password", "--secret", "--token", "/d"}
    )

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("ostriv_macos.launcher")

    @classmethod
    def _command(cls, argv):
        command = list(argv)
        if not command or not isinstance(command[0], str):
            raise ValueError("external command is empty or invalid")
        executable = Path(command[0]).name
        if executable not in cls.ALLOWED_EXECUTABLES:
            raise ValueError("external executable is not allowed: {}".format(executable))
        return command

    @classmethod
    def _safe_argv(cls, argv):
        redacted = []
        hide_next = False
        executable = Path(str(argv[0])).name if argv else ""
        for index, value in enumerate(argv):
            text = str(value)
            if hide_next or (executable == "open" and index > 0):
                redacted.append("<redacted>")
                hide_next = False
                continue
            redacted.append(text)
            hide_next = text.lower() in cls.SENSITIVE_OPTIONS
        return json.dumps(redacted, ensure_ascii=False)

    @classmethod
    def _sensitive_values(cls, argv):
        values = tuple(
            str(argv[index + 1])
            for index, value in enumerate(argv[:-1])
            if str(value).lower() in cls.SENSITIVE_OPTIONS and str(argv[index + 1])
        )
        if argv and Path(str(argv[0])).name == "open":
            values += tuple(str(value) for value in argv[1:] if str(value))
        return values

    @staticmethod
    def _bounded(text, sensitive_values=(), limit=2048):
        for value in sensitive_values:
            text = text.replace(value, "<redacted>")
        if len(text) <= limit:
            return repr(text)
        return repr(text[:limit]) + " <truncated {} chars>".format(len(text) - limit)

    @staticmethod
    def _decode(value):
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return "" if value is None else str(value)

    @classmethod
    def _diagnostic(cls, command, returncode, stdout, stderr, status=None):
        if Path(command[0]).name in {"lsof", "pgrep", "ps"}:
            stdout = "<process details omitted>"
            stderr = "<process details omitted>"
        sensitive_values = cls._sensitive_values(command)
        fields = (
            ["returncode={}".format(returncode)]
            if status is None
            else ["status={}".format(status)]
        )
        fields.append("argv={}".format(cls._safe_argv(command)))
        if status is not None:
            fields.append("returncode={}".format(returncode))
        fields.extend(
            (
                "output={}".format(cls._bounded(stdout, sensitive_values)),
                "stderr={}".format(cls._bounded(stderr, sensitive_values)),
            )
        )
        return " ".join(fields)

    def run(self, argv, timeout=None) -> ExternalResult:
        import subprocess

        command = self._command(argv)
        self.logger.info(
            "command start argv=%s timeout=%s", self._safe_argv(command), timeout
        )
        try:
            result = subprocess.run(
                command, capture_output=True, check=False, timeout=timeout
            )
        except subprocess.TimeoutExpired as error:
            diagnostic = self._diagnostic(
                command,
                "timeout",
                self._decode(error.output),
                self._decode(error.stderr),
                status="timeout",
            )
            self.logger.error("command failure %s", diagnostic)
            raise ExternalCommandError(diagnostic) from None
        decoded = ExternalResult(
            result.returncode,
            result.stdout.decode("utf-8", errors="replace"),
            result.stderr.decode("utf-8", errors="replace"),
        )
        diagnostic = self._diagnostic(
            command,
            decoded.returncode,
            decoded.stdout,
            decoded.stderr,
        )
        self.logger.info("command result %s", diagnostic)
        return ExternalResult(
            decoded.returncode,
            decoded.stdout,
            decoded.stderr,
            diagnostic,
        )


def _display_dialog(message: str) -> None:
    import subprocess

    script = (
        "on run argv\n"
        'display dialog (item 1 of argv) with title "Ostriv for macOS" '
        'buttons {"OK"} default button "OK"\n'
        "end run"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script, message],
            capture_output=True,
            check=False,
            timeout=30.0,
        )
    except Exception:
        pass


def _send_notification(message: str, runner) -> None:
    script = (
        "on run argv\n"
        'display notification (item 1 of argv) with title "Ostriv for macOS"\n'
        "end run"
    )
    try:
        runner.run(["osascript", "-e", script, message], timeout=30.0)
    except Exception:
        pass


class SteamController:
    """Wait for Steam's process, login, and renderer signals to become stable."""

    def __init__(
        self,
        probe=None,
        open_steam=None,
        monotonic=time.monotonic,
        wall_time=time.time,
        sleep=time.sleep,
        poll_seconds: float = 2.0,
        transition_stable_seconds: float = 15.0,
        timeout_seconds: float = 300.0,
        notify=lambda: None,
        config=None,
        runner=None,
        logger=None,
    ) -> None:
        self._probe = probe
        self.config = config
        self.runner = runner
        self.open_steam = open_steam or self._open_configured_steam
        self.monotonic = monotonic
        self.wall_time = wall_time
        self.sleep = sleep
        self.poll_seconds = poll_seconds
        self.transition_stable_seconds = transition_stable_seconds
        self.timeout_seconds = timeout_seconds
        self.notify = notify
        self.logger = logger or logging.getLogger("ostriv_macos.launcher")
        self._opened = False
        self._notified = False
        self._active_user_confirmation = None

    def _wine_command(self, *arguments: str) -> List[str]:
        command = [self.config.wine, "--bottle", self.config.bottle_argument]
        if self.config.scope == "managed":
            command.extend(["--scope", "managed"])
        command.extend(arguments)
        return command

    def _open_configured_steam(self) -> bool:
        if self.config is None or self.runner is None:
            return False
        root = Path(self.config.steam_apps_root)
        folders = [root]
        try:
            folders.extend(
                entry
                for entry in root.iterdir()
                if entry.is_dir() and entry.suffix != ".app"
            )
        except OSError:
            folders = []
        for folder in folders:
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir() or entry.suffix != ".app":
                    continue
                try:
                    with (entry / "Contents/Info.plist").open("rb") as stream:
                        properties = plistlib.load(stream)
                except (OSError, plistlib.InvalidFileException, ValueError):
                    continue
                command = properties.get("CrossOverHelperCommand", "")
                if (
                    properties.get("CXHelperAppBottleName") == self.config.bottle_name
                    and properties.get("CXHelperAppBottleTag")
                    == self.config.bottle_tag
                    and isinstance(command, str)
                    and command.rstrip('"').lower().endswith("/steam.lnk")
                ):
                    self.runner.run(["open", str(entry)], timeout=10.0)
                    return True

        if not self.config.steam_links:
            return False
        command = self._wine_command("--start", self.config.steam_links[0])
        self.runner.run(command, timeout=10.0)
        return True

    @staticmethod
    def _process_image(command: str) -> str:
        normalized = command.strip().lower().replace("\\", "/")
        if (
            len(normalized) < 4
            or not normalized[0].isascii()
            or not normalized[0].isalpha()
            or normalized[1:3] != ":/"
        ):
            return ""
        executable_end = normalized.find(".exe")
        if executable_end < 0:
            return ""
        suffix = normalized[executable_end + 4 : executable_end + 5]
        if suffix and not suffix.isspace():
            return ""
        return normalized[: executable_end + 4].rsplit("/", 1)[-1]

    @staticmethod
    def _elapsed_seconds(value: str) -> Optional[int]:
        text = value.strip()
        if not text or len(text) > 20 or not text.isascii():
            return None
        days = 0
        if "-" in text:
            day_text, text = text.split("-", 1)
            if not day_text.isdigit():
                return None
            days = int(day_text, 10)
        fields = text.split(":")
        if len(fields) == 2:
            hours = 0
            minute_text, second_text = fields
        elif len(fields) == 3:
            hour_text, minute_text, second_text = fields
            if not hour_text.isdigit():
                return None
            hours = int(hour_text, 10)
        else:
            return None
        if not minute_text.isdigit() or not second_text.isdigit():
            return None
        minutes = int(minute_text, 10)
        seconds = int(second_text, 10)
        if hours > 23 or minutes > 59 or seconds > 59:
            return None
        return days * 86400 + hours * 3600 + minutes * 60 + seconds

    @staticmethod
    def _unknown_host_probe() -> HostSteamProbe:
        return HostSteamProbe(False, False, False, frozenset(), None)

    def _host_process_roles(self) -> HostSteamProbe:
        candidates = [
            "/usr/bin/pgrep",
            "-f",
            "steam[.]exe|steamwebhelper[.]exe",
        ]
        try:
            result = self.runner.run(candidates, timeout=5.0)
        except (ExternalCommandError, OSError, ValueError):
            return self._unknown_host_probe()
        if result.returncode == 1:
            return HostSteamProbe(False, False, True, frozenset(), None)
        if result.returncode != 0:
            return self._unknown_host_probe()
        output = result.stdout
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        host_pids = {
            pid
            for line in str(output)[:MAX_LOG_EVIDENCE_BYTES].splitlines()
            if (pid := _parse_pid(line.strip())) is not None
        }
        if not host_pids or len(host_pids) > MAX_STEAM_HOST_PIDS:
            return self._unknown_host_probe()

        ordered_pids = sorted(host_pids)
        details = [
            "/bin/ps",
            "-ww",
            "-o",
            "pid=,etime=,command=",
            "-p",
            ",".join(str(pid) for pid in ordered_pids),
        ]
        try:
            result = self.runner.run(details, timeout=5.0)
        except (ExternalCommandError, OSError, ValueError):
            return self._unknown_host_probe()
        if result.returncode != 0:
            return self._unknown_host_probe()
        output = result.stdout
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        candidate_pids = set(ordered_pids)
        steam_pids = set()
        steam_started_at = {}
        renderer_pids = set()
        candidate_rows = 0
        observed_at = self.wall_time()
        for line in str(output)[:MAX_LOG_EVIDENCE_BYTES].splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) != 3:
                continue
            pid = _parse_pid(fields[0])
            if pid is None or pid not in candidate_pids:
                continue
            candidate_rows += 1
            image = self._process_image(fields[2])
            if image == "steam.exe":
                steam_pids.add(pid)
                elapsed = self._elapsed_seconds(fields[1])
                if elapsed is not None:
                    steam_started_at[pid] = float(int(observed_at - elapsed))
            elif (
                image == "steamwebhelper.exe"
                and "--type=renderer" in fields[2].split()
            ):
                renderer_pids.add(pid)
        role_pids = steam_pids | renderer_pids
        if candidate_rows == 0:
            return self._unknown_host_probe()
        if not role_pids:
            return HostSteamProbe(False, False, True, frozenset(), None)

        cwd_query = [
            "/usr/sbin/lsof",
            "-w",
            "-b",
            "-a",
            "-n",
            "-p",
            ",".join(str(pid) for pid in sorted(role_pids)),
            "-d",
            "cwd",
            "-Fn",
        ]
        try:
            result = self.runner.run(cwd_query, timeout=5.0)
        except (ExternalCommandError, OSError, ValueError):
            return self._unknown_host_probe()
        if result.returncode != 0 or self.config is None:
            return self._unknown_host_probe()
        output = result.stdout
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        bottle = Path(
            os.path.realpath(
                self.config.bottle_realpath or self.config.bottle_argument
            )
        )
        cwd_by_pid = {}
        current_pid = None
        has_cwd_field = False
        for line in str(output)[:MAX_LOG_EVIDENCE_BYTES].splitlines():
            if line.startswith("p"):
                current_pid = _parse_pid(line[1:])
                has_cwd_field = False
                continue
            if line == "fcwd":
                has_cwd_field = current_pid in role_pids
                continue
            if (
                current_pid not in role_pids
                or not has_cwd_field
                or not line.startswith("n/")
                or len(line) > 4097
                or current_pid in cwd_by_pid
            ):
                continue
            cwd_by_pid[current_pid] = Path(os.path.realpath(line[1:]))
            has_cwd_field = False
        if set(cwd_by_pid) != role_pids:
            return self._unknown_host_probe()
        owned_pids = set()
        for pid, cwd in cwd_by_pid.items():
            if cwd == bottle or bottle in cwd.parents:
                owned_pids.add(pid)
        owned_steam_pids = steam_pids & owned_pids
        start_times = [
            steam_started_at[pid]
            for pid in owned_steam_pids
            if pid in steam_started_at
        ]
        return HostSteamProbe(
            bool(owned_steam_pids),
            bool(renderer_pids & owned_pids),
            True,
            frozenset(owned_steam_pids),
            max(start_times) if start_times else None,
        )

    def _active_user_from_file(self) -> ActiveUserSnapshot:
        if self.config is None:
            return ActiveUserSnapshot(None)
        bottle = Path(
            os.path.realpath(
                self.config.bottle_realpath or self.config.bottle_argument
            )
        )
        try:
            with (bottle / "user.reg").open("rb") as stream:
                status = os.fstat(stream.fileno())
                data = stream.read(MAX_USER_REGISTRY_BYTES + 1)
        except OSError:
            return ActiveUserSnapshot(None)
        if len(data) > MAX_USER_REGISTRY_BYTES:
            return ActiveUserSnapshot(None)
        generation = (
            status.st_dev,
            status.st_ino,
            status.st_size,
            getattr(
                status,
                "st_mtime_ns",
                int(status.st_mtime * 1_000_000_000),
            ),
        )

        target_section = r"[Software\\Valve\\Steam\\ActiveProcess]".casefold()
        in_section = False
        for raw_line in data.decode("utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if line.startswith("["):
                section = line.split("]", 1)[0] + "]" if "]" in line else ""
                in_section = section.casefold() == target_section
                continue
            if not in_section or not line.startswith('"ActiveUser"=dword:'):
                continue
            value = line.split(":", 1)[1]
            if len(value) != 8 or any(
                character not in "0123456789abcdefABCDEF" for character in value
            ):
                return ActiveUserSnapshot(None, generation, status.st_mtime)
            return ActiveUserSnapshot(
                int(value, 16) != 0,
                generation,
                status.st_mtime,
            )
        return ActiveUserSnapshot(None, generation, status.st_mtime)

    def _active_user_from_wine(self) -> Optional[bool]:
        registry_command = self._wine_command(
            "--no-update",
            "--no-lock",
            "reg",
            "query",
            r"HKCU\Software\Valve\Steam\ActiveProcess",
        )
        try:
            registry = self.runner.run(registry_command, timeout=10.0)
        except (ExternalCommandError, OSError, ValueError):
            return None
        if registry.returncode != 0:
            return None
        output = registry.stdout
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        for line in str(output).splitlines():
            parts = line.split()
            if (
                len(parts) == 3
                and parts[0] == "ActiveUser"
                and parts[2].startswith("0x")
            ):
                try:
                    return int(parts[2], 16) != 0
                except ValueError:
                    return None
        return None

    def _active_user(self, host: HostSteamProbe) -> bool:
        if not host.process:
            return False
        snapshot = self._active_user_from_file()
        if (
            snapshot.value is not None
            and host.started_at is not None
            and snapshot.mtime + 2.0 >= host.started_at
        ):
            self._active_user_confirmation = None
            return snapshot.value

        confirmation_key = (
            host.steam_pids,
            host.started_at,
            snapshot.generation,
        )
        now = self.monotonic()
        if self._active_user_confirmation is not None:
            cached_key, cached_value, expires_at = self._active_user_confirmation
            if cached_key == confirmation_key and now < expires_at:
                return cached_value
        confirmed = self._active_user_from_wine()
        if confirmed is not True:
            self._active_user_confirmation = None
            return False
        self._active_user_confirmation = (
            confirmation_key,
            True,
            now + ACTIVE_USER_CONFIRMATION_SECONDS,
        )
        return True

    def probe(self) -> SteamSignals:
        if self._probe is not None:
            signals = self._probe()
        else:
            if self.config is None or self.runner is None:
                raise TypeError("SteamController requires a probe or config and runner")

            host = self._host_process_roles()
            signals = SteamSignals(
                host.process,
                self._active_user(host),
                host.renderer,
                host.process_known,
            )
        self.logger.info(
            "steam probe process=%s process_known=%s active_user=%s renderer=%s ready=%s",
            signals.process,
            signals.process_known,
            signals.active_user,
            signals.renderer,
            signals.ready,
        )
        return signals

    def ensure_ready(
        self, timeout_seconds: Optional[float] = None, retry: bool = False
    ) -> None:
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        deadline = self.monotonic() + timeout
        stable_seconds = 30.0 if retry else self.transition_stable_seconds

        def timeout_failure(signals: SteamSignals) -> LauncherRuntimeError:
            message_key = (
                "steam_login"
                if signals.process and not signals.active_user
                else "steam_timeout"
            )
            return LauncherRuntimeError(message_key, "Steam did not become ready")

        def check_deadline(signals: SteamSignals) -> float:
            now = self.monotonic()
            if now >= deadline:
                raise timeout_failure(signals)
            return now

        def open_if_absent(signals: SteamSignals) -> None:
            if signals.process_known and not signals.process and not self._opened:
                self.open_steam()
                self._opened = True

        signals = self.probe()
        now = check_deadline(signals)
        open_if_absent(signals)
        now = check_deadline(signals)
        if signals.ready and not retry:
            self.sleep(min(self.poll_seconds, deadline - now))
            check_deadline(signals)
            signals = self.probe()
            now = check_deadline(signals)
            open_if_absent(signals)
            now = check_deadline(signals)
            if signals.ready:
                return

        if not self._notified:
            self.notify()
            self._notified = True
        ready_since = now if signals.ready else None
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise timeout_failure(signals)
            self.sleep(min(self.poll_seconds, remaining))
            check_deadline(signals)
            signals = self.probe()
            now = check_deadline(signals)
            open_if_absent(signals)
            now = check_deadline(signals)
            if signals.ready:
                if ready_since is None:
                    ready_since = now
                if now - ready_since >= stable_seconds:
                    return
            else:
                ready_since = None


def atomic_json(path: Path, data: Dict[str, Any]) -> None:
    """Durably replace *path* with JSON written through a sibling temporary file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    replaced = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        replaced = True
        _fsync_directory(path.parent)
    finally:
        if not replaced:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class LauncherConfig:
    schema: int
    bottle_name: str
    bottle_argument: str
    scope: str
    wine: str
    game_command: List[str]
    steam_apps_root: str
    steam_links: List[str]
    game_log: str
    launcher_log: str
    lock_path: str
    recovery_marker: str
    messages: Dict[str, str]
    bottle_realpath: str = ""
    bottle_tag: str = ""
    profile_owner_token: str = ""

    @classmethod
    def load(cls, path: Path) -> "LauncherConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or type(data.get("schema")) is not int:
            raise RuntimeError("Invalid launcher configuration")
        if data.get("schema") != 1:
            raise RuntimeError("Unsupported launcher configuration")
        string_fields = (
            "bottle_name",
            "bottle_argument",
            "scope",
            "wine",
            "steam_apps_root",
            "game_log",
            "launcher_log",
            "lock_path",
            "recovery_marker",
            "bottle_realpath",
            "bottle_tag",
            "profile_owner_token",
        )
        if any(type(data.get(field)) is not str for field in string_fields):
            raise RuntimeError("Invalid launcher configuration")
        for field in ("game_command", "steam_links"):
            value = data.get(field)
            if not isinstance(value, list) or any(type(item) is not str for item in value):
                raise RuntimeError("Invalid launcher configuration")
        messages = data.get("messages")
        if not isinstance(messages, dict) or any(
            type(key) is not str or type(value) is not str
            for key, value in messages.items()
        ):
            raise RuntimeError("Invalid launcher configuration")
        try:
            return cls(**data)
        except TypeError as error:
            raise RuntimeError("Invalid launcher configuration") from error


class ColorSyncProfileBackend:
    """ColorSync bridge, initialized only on an actual macOS launcher run."""

    def __init__(self) -> None:
        value = ctypes.c_void_p
        self._ctypes = ctypes
        self._value = value
        self._cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self._cg = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        self._cs = ctypes.CDLL(
            "/System/Library/Frameworks/ColorSync.framework/ColorSync"
        )
        for function, result, arguments in [
            (self._cf.CFStringCreateWithCString, value, [value, ctypes.c_char_p, ctypes.c_uint32]),
            (self._cf.CFStringGetCString, ctypes.c_bool, [value, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]),
            (self._cf.CFDictionaryGetValue, value, [value, value]),
            (self._cf.CFDictionaryCreateMutable, value, [value, ctypes.c_long, value, value]),
            (self._cf.CFDictionarySetValue, None, [value, value, value]),
            (self._cf.CFURLCreateWithFileSystemPath, value, [value, value, ctypes.c_long, ctypes.c_bool]),
            (self._cf.CFURLCopyFileSystemPath, value, [value, ctypes.c_long]),
            (self._cg.CGMainDisplayID, ctypes.c_uint32, []),
            # This symbol lives in ColorSync.framework, not CoreGraphics.framework.
            (self._cs.CGDisplayCreateUUIDFromDisplayID, value, [ctypes.c_uint32]),
            (self._cs.ColorSyncDeviceCopyDeviceInfo, value, [value, value]),
            (self._cs.ColorSyncDeviceSetCustomProfiles, ctypes.c_bool, [value, value, value]),
        ]:
            function.restype, function.argtypes = result, arguments

        self._display_class = value.in_dll(self._cs, "kColorSyncDisplayDeviceClass")
        self._profile_id = value.in_dll(self._cs, "kColorSyncDeviceDefaultProfileID")
        self._custom_key = value.in_dll(self._cs, "kColorSyncCustomProfiles")
        self._key_callbacks = ctypes.addressof(
            ctypes.c_char.in_dll(self._cf, "kCFTypeDictionaryKeyCallBacks")
        )
        self._value_callbacks = ctypes.addressof(
            ctypes.c_char.in_dll(self._cf, "kCFTypeDictionaryValueCallBacks")
        )

    def _cfstr(self, value: str):
        return self._cf.CFStringCreateWithCString(None, value.encode(), 0x08000100)

    def _py_str(self, reference) -> Optional[str]:
        buffer = self._ctypes.create_string_buffer(2048)
        if reference and self._cf.CFStringGetCString(reference, buffer, 2048, 0x08000100):
            return buffer.value.decode("utf-8", "replace")
        return None

    def _display_uuid(self):
        return self._cs.CGDisplayCreateUUIDFromDisplayID(self._cg.CGMainDisplayID())

    def get(self) -> Optional[str]:
        """Return the current custom ICC path, or None for the factory default."""
        info = self._cs.ColorSyncDeviceCopyDeviceInfo(
            self._display_class, self._display_uuid()
        )
        if not info:
            return None
        custom = self._cf.CFDictionaryGetValue(info, self._custom_key)
        if not custom:
            return None
        # System Settings stores the custom profile under slot "1"; PROFILE_ID is a
        # compatibility fallback for values written before ColorSync normalizes the key.
        url = self._cf.CFDictionaryGetValue(custom, self._cfstr("1")) or self._cf.CFDictionaryGetValue(
            custom, self._profile_id
        )
        if not url:
            return None
        return self._py_str(self._cf.CFURLCopyFileSystemPath(url, 0))

    def set(self, icc_path: Optional[str]) -> bool:
        """Set the custom ICC path; None deliberately restores the factory default."""
        profiles = self._cf.CFDictionaryCreateMutable(
            None, 0, self._key_callbacks, self._value_callbacks
        )
        if icc_path:
            value = self._cf.CFURLCreateWithFileSystemPath(
                None, self._cfstr(icc_path), 0, False
            )
        else:
            value = self._value.in_dll(self._cf, "kCFNull")
        self._cf.CFDictionarySetValue(profiles, self._profile_id, value)
        return bool(
            self._cs.ColorSyncDeviceSetCustomProfiles(
                self._display_class, self._display_uuid(), profiles
            )
        )


def _display_recovery_path(config: "LauncherConfig") -> Path:
    """Keep display-mode recovery beside the launcher log, not inside the bottle.

    The display mode is a property of the Mac rather than of the bottle, and the
    bottle's leaves are a validated ownership inventory that this state does not
    belong in.
    """
    log = Path(config.launcher_log)
    return log.with_name(log.stem + DISPLAY_RECOVERY_SUFFIX)


class CoreGraphicsDisplayBackend:
    """CoreGraphics bridge for notch-safe display modes, macOS-only."""

    SAFE_RATIO = 1.6
    RATIO_TOLERANCE = 0.005

    def __init__(self) -> None:
        value = ctypes.c_void_p
        self._cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        self._cg = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        for function, result, arguments in [
            (self._cg.CGMainDisplayID, ctypes.c_uint32, []),
            (self._cg.CGDisplayCopyAllDisplayModes, value, [ctypes.c_uint32, value]),
            (self._cg.CGDisplayCopyDisplayMode, value, [ctypes.c_uint32]),
            (self._cg.CGDisplayModeGetWidth, ctypes.c_size_t, [value]),
            (self._cg.CGDisplayModeGetHeight, ctypes.c_size_t, [value]),
            (self._cg.CGDisplayModeGetIODisplayModeID, ctypes.c_int32, [value]),
            (self._cg.CGDisplayModeRelease, None, [value]),
            (
                self._cg.CGDisplaySetDisplayMode,
                ctypes.c_int32,
                [ctypes.c_uint32, value, value],
            ),
            (self._cf.CFArrayGetCount, ctypes.c_long, [value]),
            (self._cf.CFArrayGetValueAtIndex, value, [value, ctypes.c_long]),
            (self._cf.CFRelease, None, [value]),
            (
                self._cf.CFDictionaryCreate,
                value,
                [
                    value,
                    ctypes.POINTER(value),
                    ctypes.POINTER(value),
                    ctypes.c_long,
                    value,
                    value,
                ],
            ),
        ]:
            function.restype = result
            function.argtypes = arguments
        self._options = self._every_mode_option()

    def _every_mode_option(self):
        """Ask for scaled modes too; the active mode is usually one of them."""
        try:
            key = ctypes.c_void_p.in_dll(
                self._cg, "kCGDisplayShowDuplicateLowResolutionModes"
            )
            true = ctypes.c_void_p.in_dll(self._cf, "kCFBooleanTrue")
        except ValueError:
            return None
        keys = (ctypes.c_void_p * 1)(key)
        values = (ctypes.c_void_p * 1)(true)
        return self._cf.CFDictionaryCreate(None, keys, values, 1, None, None)

    @staticmethod
    def _describe(width, height, mode_id):
        return {"width": int(width), "height": int(height), "mode_id": int(mode_id)}

    def _read(self, mode):
        return self._describe(
            self._cg.CGDisplayModeGetWidth(mode),
            self._cg.CGDisplayModeGetHeight(mode),
            self._cg.CGDisplayModeGetIODisplayModeID(mode),
        )

    def get(self):
        display = self._cg.CGMainDisplayID()
        mode = self._cg.CGDisplayCopyDisplayMode(display)
        if not mode:
            return None
        try:
            return self._read(mode)
        finally:
            self._cg.CGDisplayModeRelease(mode)

    def _catalogue(self):
        display = self._cg.CGMainDisplayID()
        array = self._cg.CGDisplayCopyAllDisplayModes(display, self._options)
        if not array:
            return display, array, []
        entries = []
        for index in range(self._cf.CFArrayGetCount(array)):
            mode = self._cf.CFArrayGetValueAtIndex(array, index)
            entries.append((mode, self._read(mode)))
        return display, array, entries

    def _is_safe(self, described):
        height = described["height"]
        if height <= 0:
            return False
        ratio = described["width"] / height
        return abs(ratio - self.SAFE_RATIO) <= self.RATIO_TOLERANCE

    def safe_mode(self):
        """The 16:10 twin of the active mode, or None when there is nothing to do."""
        current = self.get()
        if current is None or self._is_safe(current):
            return None
        _display, array, entries = self._catalogue()
        try:
            candidates = [
                described
                for _mode, described in entries
                if described["width"] == current["width"]
                and described["height"] < current["height"]
                and self._is_safe(described)
            ]
        finally:
            if array:
                self._cf.CFRelease(array)
        if not candidates:
            return None
        return max(candidates, key=lambda described: described["height"])

    def set(self, mode):
        if not isinstance(mode, dict):
            return False
        display, array, entries = self._catalogue()
        try:
            match = None
            for reference, described in entries:
                if described["mode_id"] == mode.get("mode_id"):
                    match = reference
                    break
                if (
                    match is None
                    and described["width"] == mode.get("width")
                    and described["height"] == mode.get("height")
                ):
                    match = reference
            if match is None:
                return False
            return self._cg.CGDisplaySetDisplayMode(display, match, None) == 0
        finally:
            if array:
                self._cf.CFRelease(array)


class ProfileGuard:
    """Persist and restore the exact display profile around a game launch."""

    def __init__(self, backend, marker: Path, srgb_path: str, owner_token: str = ""):
        self.backend = backend
        self.marker = Path(marker)
        self.srgb_path = srgb_path
        self.owner_token = owner_token
        self.original = None
        self.switched = False
        self.restored = False
        self._restoring = False

    def _marker_original(self):
        try:
            data = json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as error:
            raise RuntimeError("Invalid profile recovery marker") from error
        if not isinstance(data, dict) or "original" not in data:
            raise RuntimeError("Invalid profile recovery marker")
        if self.owner_token and data.get("owner") != self.owner_token:
            raise RuntimeError("Invalid profile recovery marker")
        original = data["original"]
        if original is not None and not isinstance(original, str):
            raise RuntimeError("Invalid profile recovery marker")
        return original

    def recover(self) -> None:
        if not self.marker.exists():
            return
        original = self._marker_original()
        if not self.backend.set(original):
            raise RuntimeError("Could not restore display profile")
        self.marker.unlink()
        _fsync_directory(self.marker.parent)

    def switch(self) -> None:
        self.original = self.backend.get()
        marker = {"original": self.original}
        if self.owner_token:
            marker["owner"] = self.owner_token
        atomic_json(self.marker, marker)
        if not self.backend.set(self.srgb_path):
            raise RuntimeError("Could not switch display profile")
        self.switched = True

    def restore_once(self) -> None:
        if self.restored or self._restoring:
            return
        self._restoring = True
        try:
            if self.switched or self.marker.exists():
                original = self.original if self.switched else self._marker_original()
                if not self.backend.set(original):
                    raise RuntimeError("Could not restore display profile")
                self.marker.unlink()
                _fsync_directory(self.marker.parent)
            self.restored = True
        finally:
            if not self.restored:
                self._restoring = False


class DisplayModeGuard:
    """Persist and restore the display mode around a game launch.

    A Wine game runs from a bare executable with no application bundle, so it
    cannot carry NSPrefersDisplaySafeAreaCompatibilityMode and macOS always hands
    it the whole panel, camera housing included. Selecting the display's 16:10
    mode keeps the game below the housing instead. Displays without such a mode
    are left untouched.
    """

    def __init__(self, backend, marker: Path, owner_token: str = ""):
        self.backend = backend
        self.marker = Path(marker)
        self.owner_token = owner_token
        self.original = None
        self.switched = False
        self.restored = False
        self._restoring = False

    def _marker_original(self):
        try:
            data = json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as error:
            raise RuntimeError("Invalid display recovery marker") from error
        if not isinstance(data, dict) or "original" not in data:
            raise RuntimeError("Invalid display recovery marker")
        if self.owner_token and data.get("owner") != self.owner_token:
            raise RuntimeError("Invalid display recovery marker")
        original = data["original"]
        if not isinstance(original, dict):
            raise RuntimeError("Invalid display recovery marker")
        return original

    def recover(self) -> None:
        if not self.marker.exists():
            return
        original = self._marker_original()
        if not self.backend.set(original):
            raise RuntimeError("Could not restore display mode")
        self.marker.unlink()
        _fsync_directory(self.marker.parent)

    def switch(self) -> None:
        target = self.backend.safe_mode()
        if target is None:
            return
        self.original = self.backend.get()
        marker = {"original": self.original}
        if self.owner_token:
            marker["owner"] = self.owner_token
        atomic_json(self.marker, marker)
        if not self.backend.set(target):
            raise RuntimeError("Could not switch display mode")
        self.switched = True

    def restore_once(self) -> None:
        if self.restored or self._restoring:
            return
        self._restoring = True
        try:
            if self.switched or self.marker.exists():
                original = self.original if self.switched else self._marker_original()
                if not self.backend.set(original):
                    raise RuntimeError("Could not restore display mode")
                self.marker.unlink()
                _fsync_directory(self.marker.parent)
            self.restored = True
        finally:
            if not self.restored:
                self._restoring = False


class InactiveDisplayGuard:
    """Stand-in where no display can be driven, so the launch is unaffected."""

    switched = False

    def recover(self) -> None:
        return None

    def switch(self) -> None:
        return None

    def restore_once(self) -> None:
        return None


class GuardChain:
    """Restore several guards as one, newest boundary first."""

    def __init__(self, *guards):
        self.guards = [guard for guard in guards if guard is not None]

    def restore_once(self) -> None:
        failure = None
        for guard in self.guards:
            try:
                guard.restore_once()
            except Exception as error:  # keep restoring the remaining guards
                failure = failure or error
        if failure is not None:
            raise failure


def install_signal_handlers(guard: ProfileGuard) -> None:
    """Restore profiles during normal exit and preserve SIGINT/SIGTERM semantics."""
    def restore_at_exit() -> None:
        try:
            guard.restore_once()
        except Exception:
            pass

    atexit.register(restore_at_exit)
    previous = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }

    def cleanup_then_resignal(signum, _frame):
        # Restore both dispositions before cleanup. A second signal therefore follows its
        # original handler immediately instead of entering this wrapper recursively.
        for protected_signum, previous_handler in previous.items():
            signal.signal(protected_signum, previous_handler)
        try:
            guard.restore_once()
        finally:
            os.kill(os.getpid(), signum)

    for signum in previous:
        signal.signal(signum, cleanup_then_resignal)


def _default_display_guard(config: LauncherConfig):
    """Only macOS has a camera housing to avoid, and only macOS has CoreGraphics."""
    if sys.platform != "darwin":
        return InactiveDisplayGuard()
    return DisplayModeGuard(
        CoreGraphicsDisplayBackend(),
        _display_recovery_path(config),
        config.profile_owner_token,
    )


def run_game(config: LauncherConfig, runner):
    """Run the configured game command through the caller's tolerant runner."""
    return runner.run(config.game_command)


def run_launcher(
    config: LauncherConfig,
    *,
    lock=None,
    log_factory=None,
    runner=None,
    steam=None,
    profile=None,
    display=None,
    dialog=None,
    install_handlers=None,
) -> int:
    """Run one locked launcher lifecycle using injectable platform boundaries."""
    actual_lock = lock or ProcessLock(Path(config.lock_path))
    if not actual_lock.acquire():
        message = config.messages.get(
            "already_running", "Ostriv is already starting or running."
        )
        if dialog is None:
            _display_dialog(message)
        else:
            dialog(message)
        return 0

    logger = None
    actual_profile = profile
    actual_display = display
    final_state = "failed"
    try:
        actual_log_factory = log_factory or _create_launcher_log
        logger = actual_log_factory(Path(config.launcher_log))
        actual_runner = runner or ExternalProcessRunner()
        if actual_profile is None:
            actual_profile = ProfileGuard(
                ColorSyncProfileBackend(),
                Path(config.recovery_marker),
                SRGB_PROFILE,
                config.profile_owner_token,
            )
        if actual_display is None:
            actual_display = _default_display_guard(config)
        actual_steam = steam or SteamController(
            config=config,
            runner=actual_runner,
            logger=logger,
            notify=lambda: _send_notification(
                config.messages.get(
                    "steam_wait", "Waiting for Steam to finish starting."
                ),
                actual_runner,
            ),
        )
        logger.info("launcher boundary=recovery status=start")
        actual_profile.recover()
        actual_display.recover()
        logger.info("launcher boundary=recovery status=OK")
        logger.info("launcher boundary=steam_readiness status=start")
        actual_steam.ensure_ready()
        logger.info("launcher boundary=steam_readiness status=OK")
        (install_handlers or install_signal_handlers)(
            GuardChain(actual_display, actual_profile)
        )
        logger.info("launcher boundary=profile_switch status=start")
        actual_profile.switch()
        logger.info("launcher boundary=profile_switch status=OK")
        logger.info("launcher boundary=display_switch status=start")
        actual_display.switch()
        logger.info(
            "launcher boundary=display_switch status=OK switched=%s",
            actual_display.switched,
        )

        game_log = Path(config.game_log)
        generation = capture_log_generation(game_log)
        logger.info(
            "launcher boundary=game_launch status=start attempt=1 generation_size=%s",
            generation.size,
        )
        result = run_game(config, actual_runner)
        logger.info(
            "launcher boundary=game_launch status=finished attempt=1 returncode=%s",
            getattr(result, "returncode", "unknown"),
        )
        final_state = classify_launch(read_new_log(game_log, generation))
        logger.info("launcher classification=%s attempt=1", final_state)
        if final_state == "steam_api":
            logger.info("launcher boundary=steam_retry_readiness status=start")
            actual_steam.ensure_ready(retry=True)
            logger.info("launcher boundary=steam_retry_readiness status=OK")
            generation = capture_log_generation(game_log)
            logger.info(
                "launcher boundary=game_launch status=start attempt=2 generation_size=%s",
                generation.size,
            )
            result = run_game(config, actual_runner)
            logger.info(
                "launcher boundary=game_launch status=finished attempt=2 returncode=%s",
                getattr(result, "returncode", "unknown"),
            )
            final_state = classify_launch(read_new_log(game_log, generation))
            logger.info("launcher classification=%s attempt=2", final_state)
        returncode = getattr(result, "returncode", None)
        if final_state in (
            "steam_api",
            "graphics_context",
            "ambiguous_log",
        ) or (returncode != 0 and final_state != "clean_exit"):
            raise LauncherRuntimeError(
                "game_failed",
                "Game launch failed: classification={} returncode={}".format(
                    final_state, returncode
                )[:256],
            )
        return 0
    except Exception:
        if logger is not None:
            logger.exception("launcher failed")
        raise
    finally:
        try:
            try:
                GuardChain(actual_display, actual_profile).restore_once()
            except Exception:
                final_state = "failed"
                if logger is not None:
                    logger.exception("display restoration failed")
                raise
            finally:
                if logger is not None:
                    logger.info("launcher final state: %s", final_state)
        finally:
            actual_lock.close()


def _record_entrypoint_failure(path: Path, error: Exception) -> None:
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", errors="backslashreplace") as stream:
            stream.write(
                "launcher entrypoint failure: {}: {}\n".format(
                    type(error).__name__, error
                )
            )
            stream.write(traceback.format_exc())
    except Exception:
        pass


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        _display_dialog(FALLBACK_ERROR_MESSAGE)
        return 2
    try:
        config = LauncherConfig.load(Path(arguments[0]))
    except Exception:
        _display_dialog(FALLBACK_ERROR_MESSAGE)
        return 2

    try:
        return run_launcher(config)
    except Exception as error:
        _record_entrypoint_failure(Path(config.launcher_log), error)
        message_key = getattr(error, "message_key", "error")
        message = config.messages.get(
            message_key,
            config.messages.get("error", FALLBACK_ERROR_MESSAGE),
        )
        _display_dialog(message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
