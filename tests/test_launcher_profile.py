import json
import io
import os
import signal
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ostriv_macos import launcher_runtime as runtime
from ostriv_macos.launcher_runtime import (
    DisplayModeGuard,
    InactiveDisplayGuard,
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


class SignalDispatcher:
    """A small signal-system boundary fake that dispatches the current disposition."""

    def __init__(self, sigint, sigterm):
        self.handlers = {signal.SIGINT: sigint, signal.SIGTERM: sigterm}
        self.kills = []

    def getsignal(self, signum):
        return self.handlers[signum]

    def setsignal(self, signum, handler):
        previous = self.handlers[signum]
        self.handlers[signum] = handler
        return previous

    def kill(self, pid, signum):
        self.kills.append((pid, signum))
        self.dispatch(signum)

    def dispatch(self, signum):
        handler = self.handlers[signum]
        if callable(handler):
            handler(signum, None)


class FakeDisplayModes:
    """Display-mode boundary fake: a notched panel exposes a 16:10 twin."""

    def __init__(self, current, safe=None, set_result=True):
        self.current = current
        self.safe = safe
        self.set_calls = []
        self.set_result = set_result

    def get(self):
        return self.current

    def safe_mode(self):
        return self.safe

    def set(self, mode):
        self.set_calls.append(mode)
        if self.set_result:
            self.current = mode
        return self.set_result


NOTCHED = {"width": 3456, "height": 2234}
SAFE = {"width": 3456, "height": 2160}


class DisplayModeGuardTests(unittest.TestCase):
    def test_exit_restores_exact_original_mode(self):
        """Losing the original height would leave the Mac in the 16:10 mode."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "display.json"
            backend = FakeDisplayModes(NOTCHED, SAFE)
            guard = DisplayModeGuard(backend, marker)

            guard.switch()
            guard.restore_once()
            guard.restore_once()

            self.assertEqual([SAFE, NOTCHED], backend.set_calls)
            self.assertFalse(marker.exists())

    def test_display_without_safe_mode_is_left_alone(self):
        """An external monitor has no camera housing and must not be touched."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "display.json"
            backend = FakeDisplayModes({"width": 2560, "height": 1440}, None)
            guard = DisplayModeGuard(backend, marker)

            guard.switch()
            guard.restore_once()

            self.assertEqual([], backend.set_calls)
            self.assertFalse(marker.exists())

    def test_next_launch_recovers_mode_after_a_crash(self):
        """Without recovery a killed launcher would strand the 16:10 mode."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "display.json"
            marker.write_text(json.dumps({"original": NOTCHED}), encoding="utf-8")
            backend = FakeDisplayModes(SAFE, None)

            DisplayModeGuard(backend, marker).recover()

            self.assertEqual([NOTCHED], backend.set_calls)
            self.assertFalse(marker.exists())

    def test_failed_switch_keeps_recovery_marker_for_next_launch(self):
        """Removing the marker after a failed switch would lose the saved mode."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "display.json"
            backend = FakeDisplayModes(NOTCHED, SAFE, set_result=False)
            guard = DisplayModeGuard(backend, marker)

            with self.assertRaisesRegex(RuntimeError, "Could not switch display mode"):
                guard.switch()

            self.assertTrue(marker.exists())
            self.assertEqual({"original": NOTCHED}, json.loads(marker.read_text()))

    def test_corrupt_marker_raises_without_deleting_it(self):
        """Deleting a malformed marker would lose the only recovery state."""
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "display.json"
            marker.write_text("not json", encoding="utf-8")
            backend = FakeDisplayModes(SAFE, None)

            with self.assertRaisesRegex(RuntimeError, "recovery marker"):
                DisplayModeGuard(backend, marker).recover()

            self.assertTrue(marker.exists())


class DefaultDisplayGuardTests(unittest.TestCase):
    def test_off_macos_the_guard_is_inactive(self):
        """Building the CoreGraphics backend off macOS aborts the whole launch."""
        with patch.object(runtime.sys, "platform", "linux"):
            guard = runtime._default_display_guard(object())

        self.assertIsInstance(guard, InactiveDisplayGuard)

    def test_on_macos_the_real_guard_is_built(self):
        """Losing this branch would silently drop the notch fix on every Mac."""
        class Config:
            launcher_log = "/tmp/ostriv-macos/Steam-test.log"
            profile_owner_token = "token"

        with patch.object(runtime.sys, "platform", "darwin"), patch.object(
            runtime, "CoreGraphicsDisplayBackend", return_value=object()
        ):
            guard = runtime._default_display_guard(Config())

        self.assertIsInstance(guard, DisplayModeGuard)
        self.assertEqual(
            Path("/tmp/ostriv-macos/Steam-test.display-recovery.json"), guard.marker
        )


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
            "bottle_realpath": "/tmp/Bottles/Ostriv",
            "bottle_tag": "CrossOver-fixture-id/",
            "profile_owner_token": "0" * 64,
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
    def test_sigint_restores_both_dispositions_before_resignalling(self):
        """Leaving SIGTERM wrapped after SIGINT can swallow a later real SIGTERM."""
        previous_calls = []

        def prior_int(signum, _frame):
            previous_calls.append(signum)

        def prior_term(signum, _frame):
            previous_calls.append(signum)

        dispatcher = SignalDispatcher(prior_int, prior_term)
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "profile.json"
            backend = FakeProfiles("/Profiles/P3.icc")
            guard = ProfileGuard(backend, marker, "/Profiles/sRGB.icc")
            guard.switch()
            with patch("ostriv_macos.launcher_runtime.atexit.register"), patch(
                "ostriv_macos.launcher_runtime.signal.getsignal", side_effect=dispatcher.getsignal
            ), patch(
                "ostriv_macos.launcher_runtime.signal.signal", side_effect=dispatcher.setsignal
            ), patch("ostriv_macos.launcher_runtime.os.kill", side_effect=dispatcher.kill):
                install_signal_handlers(guard)
                dispatcher.dispatch(signal.SIGINT)
                self.assertIs(prior_int, dispatcher.handlers[signal.SIGINT])
                self.assertIs(prior_term, dispatcher.handlers[signal.SIGTERM])
                dispatcher.dispatch(signal.SIGTERM)

        self.assertEqual(["/Profiles/sRGB.icc", "/Profiles/P3.icc"], backend.set_calls)
        self.assertEqual([signal.SIGINT, signal.SIGTERM], previous_calls)
        self.assertEqual([(os.getpid(), signal.SIGINT)], dispatcher.kills)

    def test_sigterm_with_ignored_prior_does_not_leave_a_wrapped_signal(self):
        """An ignored re-signal must not leave either disposition permanently wrapped."""
        dispatcher = SignalDispatcher(signal.SIG_IGN, signal.SIG_IGN)
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "profile.json"
            backend = FakeProfiles("/Profiles/P3.icc")
            guard = ProfileGuard(backend, marker, "/Profiles/sRGB.icc")
            guard.switch()
            with patch("ostriv_macos.launcher_runtime.atexit.register"), patch(
                "ostriv_macos.launcher_runtime.signal.getsignal", side_effect=dispatcher.getsignal
            ), patch(
                "ostriv_macos.launcher_runtime.signal.signal", side_effect=dispatcher.setsignal
            ), patch("ostriv_macos.launcher_runtime.os.kill", side_effect=dispatcher.kill):
                install_signal_handlers(guard)
                dispatcher.dispatch(signal.SIGTERM)
                self.assertIs(signal.SIG_IGN, dispatcher.handlers[signal.SIGINT])
                self.assertIs(signal.SIG_IGN, dispatcher.handlers[signal.SIGTERM])
                dispatcher.dispatch(signal.SIGINT)

        self.assertEqual(["/Profiles/sRGB.icc", "/Profiles/P3.icc"], backend.set_calls)
        self.assertEqual([(os.getpid(), signal.SIGTERM)], dispatcher.kills)


class AtexitCleanupTests(unittest.TestCase):
    def test_atexit_cleanup_swallows_restore_error_without_output(self):
        """Registering restore_once directly emits an atexit traceback on final cleanup failure."""
        registered = []
        dispatcher = SignalDispatcher(signal.SIG_DFL, signal.SIG_DFL)
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "profile.json"
            backend = FakeProfiles("/Profiles/P3.icc")
            guard = ProfileGuard(backend, marker, "/Profiles/sRGB.icc")
            guard.switch()
            backend.set_result = False
            with patch("ostriv_macos.launcher_runtime.atexit.register", side_effect=registered.append), patch(
                "ostriv_macos.launcher_runtime.signal.getsignal", side_effect=dispatcher.getsignal
            ), patch(
                "ostriv_macos.launcher_runtime.signal.signal", side_effect=dispatcher.setsignal
            ):
                install_signal_handlers(guard)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                registered[0]()

            self.assertEqual("", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())
            self.assertTrue(marker.exists())
            self.assertFalse(guard.restored)
