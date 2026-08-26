"""Standalone runtime copied next to the installed CrossOver launcher.

This module intentionally imports only the Python standard library: the installed copy must
keep working after the release directory that supplied it has gone away.
"""

import atexit
import ctypes
import json
import os
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


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

    @classmethod
    def load(cls, path: Path) -> "LauncherConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema") != 1:
            raise RuntimeError("Unsupported launcher configuration")
        return cls(**data)


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

    def __init__(self, backend, marker: Path, srgb_path: str):
        self.backend = backend
        self.marker = Path(marker)
        self.srgb_path = srgb_path
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

    def switch(self) -> None:
        self.original = self.backend.get()
        atomic_json(self.marker, {"original": self.original})
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
            self.restored = True
        finally:
            if not self.restored:
                self._restoring = False


def install_signal_handlers(guard: ProfileGuard) -> None:
    """Restore profiles during normal exit and preserve SIGINT/SIGTERM semantics."""
    atexit.register(guard.restore_once)
    previous = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    handling = False

    def cleanup_then_resignal(signum, _frame):
        nonlocal handling
        if handling:
            return
        handling = True
        try:
            guard.restore_once()
        finally:
            signal.signal(signum, previous[signum])
            os.kill(os.getpid(), signum)

    for signum in previous:
        signal.signal(signum, cleanup_then_resignal)


def run_game(config: LauncherConfig, runner):
    """Run the configured game command through the caller's tolerant runner."""
    return runner.run(config.game_command)
