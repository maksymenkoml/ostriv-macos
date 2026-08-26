import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ostriv_macos.launcher_runtime import (
    LauncherConfig,
    ProfileGuard,
    atomic_json,
    install_signal_handlers,
)


class FakeProfiles:
    def __init__(self, current, set_result=True):
        self.current = current
        self.set_calls = []
        self.set_result = set_result

    def get(self):
        return self.current

    def set(self, value):
        self.set_calls.append(value)
        if self.set_result:
            self.current = value
        return self.set_result


class ProfileGuardTests(unittest.TestCase):
    def test_exit_restores_exact_original_profile(self):
        """Changing the saved P3 path to None must make this recovery fail."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "profile.json"
            backend = FakeProfiles("/Profiles/P3.icc")
            guard = ProfileGuard(backend, marker, "/Profiles/sRGB.icc")

            guard.switch()
            guard.restore_once()
            guard.restore_once()

            self.assertEqual(
                ["/Profiles/sRGB.icc", "/Profiles/P3.icc"],
                backend.set_calls,
            )
            self.assertFalse(marker.exists())

    def test_next_launch_recovers_factory_default_none(self):
        """Dropping None support would restore the wrong display-profile state."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "profile.json"
            marker.write_text('{"original": null}', encoding="utf-8")
            backend = FakeProfiles("/Profiles/sRGB.icc")

            ProfileGuard(backend, marker, "/Profiles/sRGB.icc").recover()

            self.assertEqual([None], backend.set_calls)
            self.assertFalse(marker.exists())

    def test_failed_switch_keeps_recovery_marker_for_next_launch(self):
        """Removing the marker after a failed switch would lose the saved profile."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "profile.json"
            backend = FakeProfiles("/Profiles/P3.icc", set_result=False)
            guard = ProfileGuard(backend, marker, "/Profiles/sRGB.icc")

            with self.assertRaisesRegex(RuntimeError, "Could not switch display profile"):
                guard.switch()

            self.assertTrue(marker.exists())
            self.assertEqual({"original": "/Profiles/P3.icc"}, json.loads(marker.read_text()))
            recovery_backend = FakeProfiles("/Profiles/sRGB.icc")
            ProfileGuard(recovery_backend, marker, "/Profiles/sRGB.icc").recover()
            self.assertEqual(["/Profiles/P3.icc"], recovery_backend.set_calls)

    def test_corrupt_marker_raises_without_deleting_it(self):
        """Deleting malformed markers would silently lose the only recovery state."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "profile.json"
            marker.write_text("not json", encoding="utf-8")
            backend = FakeProfiles("/Profiles/sRGB.icc")

            with self.assertRaisesRegex(RuntimeError, "Invalid profile recovery marker"):
                ProfileGuard(backend, marker, "/Profiles/sRGB.icc").recover()

            self.assertTrue(marker.exists())
            self.assertEqual([], backend.set_calls)

    def test_restore_failure_leaves_marker_for_a_later_recovery(self):
        """Marking cleanup complete after a failed restoration would strand the profile."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "profile.json"
            backend = FakeProfiles("/Profiles/P3.icc")
            guard = ProfileGuard(backend, marker, "/Profiles/sRGB.icc")
            guard.switch()
            backend.set_result = False

            with self.assertRaisesRegex(RuntimeError, "Could not restore display profile"):
                guard.restore_once()

            self.assertTrue(marker.exists())
            self.assertFalse(guard.restored)

    def test_atomic_json_replaces_a_file_from_its_parent_directory(self):
        """Writing a temporary file elsewhere would make replacement non-atomic across volumes."""
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "profile.json"
            target.parent.mkdir()
            replace = os.replace
            replaced = []

            def record_replace(source, destination):
                replaced.append((Path(source), Path(destination)))
                return replace(source, destination)

            with patch("ostriv_macos.launcher_runtime.os.replace", side_effect=record_replace):
                atomic_json(target, {"original": "/Profiles/P3.icc"})

            self.assertEqual({"original": "/Profiles/P3.icc"}, json.loads(target.read_text()))
            self.assertEqual(1, len(replaced))
            self.assertEqual(target.parent, replaced[0][0].parent)
            self.assertEqual(target, replaced[0][1])


class LauncherConfigTests(unittest.TestCase):
    def test_load_accepts_only_schema_one(self):
        """Accepting a different schema would run a launcher against incompatible data."""
        payload = {
            "schema": 1,
            "bottle_name": "Ostriv",
            "bottle_argument": "Ostriv",
            "scope": "managed",
            "wine": "/Applications/CrossOver.app/wine",
            "game_command": ["wine", "C:/Ostriv/ostriv.exe"],
            "steam_apps_root": "/tmp/steamapps",
            "steam_links": ["Steam.lnk"],
            "game_log": "/tmp/game.log",
            "launcher_log": "/tmp/launcher.log",
            "lock_path": "/tmp/lock",
            "recovery_marker": "/tmp/profile.json",
            "messages": {"error": "Unable to start Ostriv."},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            config = LauncherConfig.load(path)
            self.assertEqual("Ostriv", config.bottle_name)

            payload["schema"] = 2
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Unsupported launcher configuration"):
                LauncherConfig.load(path)


class SignalHandlerTests(unittest.TestCase):
    def test_sigint_cleanup_restores_previous_handler_before_resignalling(self):
        """Leaving the cleanup handler installed would recursively handle the re-signal."""
        guard = type("Guard", (), {"calls": 0, "restore_once": lambda self: setattr(self, "calls", self.calls + 1)})()
        previous = signal.SIG_DFL
        registered = {}

        def fake_signal(signum, handler):
            old = registered.get(signum, previous)
            registered[signum] = handler
            return old

        with patch("ostriv_macos.launcher_runtime.signal.getsignal", return_value=previous), patch(
            "ostriv_macos.launcher_runtime.signal.signal", side_effect=fake_signal
        ) as set_signal, patch("ostriv_macos.launcher_runtime.os.kill") as kill:
            install_signal_handlers(guard)
            registered[signal.SIGINT](signal.SIGINT, None)

        self.assertEqual(1, guard.calls)
        self.assertEqual(previous, registered[signal.SIGINT])
        self.assertEqual([(signal.SIGINT, previous)], [call.args for call in set_signal.call_args_list if call.args[0] == signal.SIGINT][-1:])
        kill.assert_called_once_with(os.getpid(), signal.SIGINT)

    def test_sigterm_cleanup_is_idempotent_with_repeated_delivery(self):
        """A second signal must not restore the display profile twice."""
        guard = type("Guard", (), {"calls": 0, "restore_once": lambda self: setattr(self, "calls", self.calls + 1)})()
        registered = {}

        def fake_signal(signum, handler):
            old = registered.get(signum, signal.SIG_DFL)
            registered[signum] = handler
            return old

        with patch("ostriv_macos.launcher_runtime.signal.getsignal", return_value=signal.SIG_DFL), patch(
            "ostriv_macos.launcher_runtime.signal.signal", side_effect=fake_signal
        ), patch("ostriv_macos.launcher_runtime.os.kill"):
            install_signal_handlers(guard)
            handler = registered[signal.SIGTERM]
            handler(signal.SIGTERM, None)
            handler(signal.SIGTERM, None)

        self.assertEqual(1, guard.calls)
