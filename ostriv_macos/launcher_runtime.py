"""Standalone runtime copied next to the installed CrossOver launcher.

This module intentionally imports only the Python standard library: the installed copy must
keep working after the release directory that supplied it has gone away.
"""

import atexit
import csv
import ctypes
import fcntl
import hashlib
import io
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
MAX_RENDERER_HELPER_PIDS = 64
_AMBIGUOUS_LOG_EVIDENCE = "<changed log generation outside bounded evidence>"


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

    @property
    def ready(self) -> bool:
        return self.process and self.active_user and self.renderer


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

    ALLOWED_EXECUTABLES = frozenset({"open", "osascript", "ps", "wine"})
    SENSITIVE_OPTIONS = frozenset(
        {"--api-key", "--password", "--secret", "--token", "/d"}
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
        for value in argv:
            text = str(value)
            if hide_next:
                redacted.append("<redacted>")
                hide_next = False
                continue
            redacted.append(text)
            hide_next = text.lower() in cls.SENSITIVE_OPTIONS
        return json.dumps(redacted, ensure_ascii=False)

    @classmethod
    def _sensitive_values(cls, argv):
        return tuple(
            str(argv[index + 1])
            for index, value in enumerate(argv[:-1])
            if str(value).lower() in cls.SENSITIVE_OPTIONS and str(argv[index + 1])
        )

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
        if Path(command[0]).name == "ps":
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
        self.sleep = sleep
        self.poll_seconds = poll_seconds
        self.transition_stable_seconds = transition_stable_seconds
        self.timeout_seconds = timeout_seconds
        self.notify = notify
        self.logger = logger or logging.getLogger("ostriv_macos.launcher")
        self._opened = False
        self._notified = False

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
    def _task_processes(output) -> Dict[str, set]:
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        processes: Dict[str, set] = {}
        try:
            rows = csv.reader(io.StringIO(str(output)[: MAX_LOG_EVIDENCE_BYTES]))
            for row in rows:
                if len(row) < 2:
                    continue
                image = row[0].lstrip("\ufeff").strip().lower()
                pid_text = row[1].strip()
                if not image or not pid_text.isdecimal():
                    continue
                pid = int(pid_text)
                if not 0 < pid < 2**31:
                    continue
                processes.setdefault(image, set()).add(pid)
        except csv.Error:
            return {}
        return processes

    def _renderer_running(self, helper_pids) -> bool:
        selected = sorted(set(helper_pids))
        if not selected or len(selected) > MAX_RENDERER_HELPER_PIDS:
            return False
        command = [
            "/bin/ps",
            "-ww",
            "-o",
            "pid=,command=",
            "-p",
            ",".join(str(pid) for pid in selected),
        ]
        try:
            result = self.runner.run(command, timeout=5.0)
        except (ExternalCommandError, OSError, ValueError):
            return False
        if result.returncode != 0:
            return False
        output = result.stdout
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        selected_set = set(selected)
        for line in str(output)[:MAX_LOG_EVIDENCE_BYTES].splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2 or not fields[0].isdecimal():
                continue
            if int(fields[0]) not in selected_set:
                continue
            if "--type=renderer" in fields[1].split():
                return True
        return False

    def probe(self) -> SteamSignals:
        if self._probe is not None:
            signals = self._probe()
        else:
            if self.config is None or self.runner is None:
                raise TypeError("SteamController requires a probe or config and runner")

            task_command = self._wine_command(
                "--no-update",
                "--no-lock",
                "tasklist",
                "/fo",
                "csv",
                "/nh",
            )
            tasks = self.runner.run(task_command, timeout=10.0)
            processes = (
                self._task_processes(tasks.stdout) if tasks.returncode == 0 else {}
            )
            process = bool(processes.get("steam.exe"))
            renderer = self._renderer_running(
                processes.get("steamwebhelper.exe", set())
            )
            registry_command = self._wine_command(
                "--no-update",
                "--no-lock",
                "reg",
                "query",
                r"HKCU\Software\Valve\Steam\ActiveProcess",
            )
            registry = self.runner.run(registry_command, timeout=10.0)
            output = registry.stdout
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            active_user = False
            for line in str(output).splitlines():
                parts = line.split()
                if (
                    len(parts) == 3
                    and parts[0] == "ActiveUser"
                    and parts[2].startswith("0x")
                ):
                    try:
                        active_user = int(parts[2], 16) != 0
                    except ValueError:
                        active_user = False
            signals = SteamSignals(process, active_user, renderer)
        self.logger.info(
            "steam probe process=%s active_user=%s renderer=%s ready=%s",
            signals.process,
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
            if not signals.process and not self._opened:
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
        logger.info("launcher boundary=recovery status=OK")
        logger.info("launcher boundary=steam_readiness status=start")
        actual_steam.ensure_ready()
        logger.info("launcher boundary=steam_readiness status=OK")
        (install_handlers or install_signal_handlers)(actual_profile)
        logger.info("launcher boundary=profile_switch status=start")
        actual_profile.switch()
        logger.info("launcher boundary=profile_switch status=OK")

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
        if (
            final_state in ("steam_api", "graphics_context", "ambiguous_log")
            or returncode != 0
        ):
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
                if actual_profile is not None:
                    actual_profile.restore_once()
            except Exception:
                final_state = "failed"
                if logger is not None:
                    logger.exception("display profile restoration failed")
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
