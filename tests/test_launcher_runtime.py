import unittest
from dataclasses import dataclass
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import plistlib
from unittest.mock import patch

import ostriv_macos.launcher_runtime as runtime

from ostriv_macos.launcher_runtime import (
    LauncherConfig,
    LauncherRuntimeError,
    ProcessLock,
    SteamController,
    SteamSignals,
    classify_launch,
    main,
    read_new_log,
    run_launcher,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@dataclass(frozen=True)
class FakeResult:
    returncode: int
    stdout: object = ""
    stderr: object = ""


class ProbeRunner:
    def __init__(self, process=True, registry=b"ActiveUser    REG_DWORD    0x1\n", renderer=True):
        self.process = process
        self.registry = registry
        self.renderer = renderer
        self.calls = []

    def run(self, argv, timeout=None):
        argv = list(argv)
        self.calls.append((argv, timeout))
        if argv[:2] == ["pgrep", "-f"] and "steamwebhelper" not in argv[-1]:
            return FakeResult(0 if self.process else 1)
        if argv[:2] == ["pgrep", "-f"]:
            return FakeResult(0 if self.renderer else 1)
        return FakeResult(0, self.registry)


def make_config(root="/tmp"):
    return LauncherConfig(
        schema=1,
        bottle_name="Ostriv",
        bottle_argument="Ostriv",
        scope="managed",
        wine="/Applications/CrossOver.app/wine",
        game_command=["wine", "C:/Ostriv/ostriv.exe"],
        steam_apps_root=root + "/CrossOver Apps",
        steam_links=["Steam.lnk"],
        game_log=root + "/ostriv.log",
        launcher_log=root + "/launcher.log",
        lock_path=root + "/launcher.lock",
        recovery_marker=root + "/profile.json",
        messages={"error": "Unable to start Ostriv."},
    )


def write_config(config, path):
    Path(path).write_text(json.dumps(config.__dict__), encoding="utf-8")


class SteamControllerTests(unittest.TestCase):
    def test_probe_requires_process_active_user_and_renderer(self):
        """Ignoring any one Steam signal would report a half-started client as ready."""
        expected = [
            (False, True, True, False),
            (True, False, True, False),
            (True, True, False, False),
            (True, True, True, True),
        ]
        for process, active_user, renderer, ready in expected:
            with self.subTest(
                process=process, active_user=active_user, renderer=renderer
            ):
                registry = (
                    b"ActiveUser    REG_DWORD    0x1\n"
                    if active_user
                    else b"ActiveUser    REG_DWORD    0x0\n"
                )
                runner = ProbeRunner(process, registry, renderer)
                controller = SteamController(config=make_config(), runner=runner)

                signals = controller.probe()

                self.assertEqual(
                    SteamSignals(process, active_user, renderer), signals
                )
                self.assertEqual(ready, signals.ready)

    def test_probe_tolerates_invalid_utf8_registry_output(self):
        """Strict registry decoding would prevent a ready client from being detected."""
        runner = ProbeRunner(registry=b"\xff ignored\nActiveUser REG_DWORD 0x1\n")

        signals = SteamController(config=make_config(), runner=runner).probe()

        self.assertTrue(signals.active_user)

    def test_transitioning_client_must_stay_ready_for_15_seconds(self):
        """Returning after the first ready probe would preserve the cold-start race."""
        clock = FakeClock()
        signals = [
            SteamSignals(False, False, False),
            SteamSignals(True, True, False),
        ] + [SteamSignals(True, True, True)] * 9
        opened = []
        controller = SteamController(
            probe=lambda: signals.pop(0),
            open_steam=lambda: opened.append(True),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            poll_seconds=2.0,
            transition_stable_seconds=15.0,
            timeout_seconds=300.0,
        )

        controller.ensure_ready()

        self.assertEqual([True], opened)
        self.assertGreaterEqual(clock.now, 15.0)

    def test_transition_readiness_timer_resets_after_any_signal_drops(self):
        """Counting readiness across a gap would launch before 15 continuous seconds."""
        clock = FakeClock()
        signals = [
            SteamSignals(True, True, False),
            SteamSignals(True, True, True),
            SteamSignals(True, True, True),
            SteamSignals(True, True, False),
        ] + [SteamSignals(True, True, True)] * 9
        controller = SteamController(
            probe=lambda: signals.pop(0),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        controller.ensure_ready()

        self.assertGreaterEqual(clock.now, 24.0)

    def test_warm_client_uses_two_probes_without_opening(self):
        """Treating one warm probe as sufficient would launch during a transient ready state."""
        clock = FakeClock()
        opened = []
        controller = SteamController(
            probe=lambda: SteamSignals(True, True, True),
            open_steam=lambda: opened.append(True),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        controller.ensure_ready()

        self.assertEqual([], opened)
        self.assertEqual(2.0, clock.now)

    def test_starting_or_logged_out_client_is_not_opened_again(self):
        """Opening Steam while its process exists would create duplicate startup attempts."""
        for first in (
            SteamSignals(True, False, False),
            SteamSignals(True, True, False),
        ):
            with self.subTest(first=first):
                clock = FakeClock()
                opened = []
                notified = []
                signals = [first] + [SteamSignals(True, True, True)] * 9
                controller = SteamController(
                    probe=lambda: signals.pop(0),
                    open_steam=lambda: opened.append(True),
                    notify=lambda: notified.append(True),
                    monotonic=clock.monotonic,
                    sleep=clock.sleep,
                )

                controller.ensure_ready()

                self.assertEqual([], opened)
                self.assertEqual([True], notified)

    def test_timeout_notifies_once_and_does_not_open_logged_out_client(self):
        """A stalled login must neither spam notifications nor launch another Steam."""
        clock = FakeClock()
        opened = []
        notified = []
        controller = SteamController(
            probe=lambda: SteamSignals(True, False, True),
            open_steam=lambda: opened.append(True),
            notify=lambda: notified.append(True),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            timeout_seconds=6.0,
        )

        with self.assertRaises(LauncherRuntimeError) as caught:
            controller.ensure_ready()

        self.assertEqual([], opened)
        self.assertEqual([True], notified)
        self.assertEqual(6.0, clock.now)
        self.assertEqual("steam_login", caught.exception.message_key)

    def test_timeout_override_does_not_repeat_open_or_notification(self):
        """The targeted retry gate must not restart Steam or repeat its wait message."""
        clock = FakeClock()
        opened = []
        notified = []
        controller = SteamController(
            probe=lambda: SteamSignals(False, False, False),
            open_steam=lambda: opened.append(True),
            notify=lambda: notified.append(True),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        for _attempt in range(2):
            with self.assertRaises(LauncherRuntimeError) as caught:
                controller.ensure_ready(timeout_seconds=4.0)
            self.assertEqual("steam_timeout", caught.exception.message_key)

        self.assertEqual([True], opened)
        self.assertEqual([True], notified)
        self.assertEqual(8.0, clock.now)

    def test_absent_client_opens_matching_crossover_steam_app_once(self):
        """Using a bare Wine child or opening twice would recreate Steam's broken half-state."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            app = Path(config.steam_apps_root) / "Bottle" / "Steam.app"
            contents = app / "Contents"
            contents.mkdir(parents=True)
            with (contents / "Info.plist").open("wb") as stream:
                plistlib.dump(
                    {
                        "CXHelperAppBottleName": config.bottle_name,
                        "CrossOverHelperCommand": '"C:/Steam/Steam.lnk"',
                    },
                    stream,
                )
            calls = []

            class OpenRunner:
                def run(self, argv, timeout=None):
                    calls.append((list(argv), timeout))
                    return FakeResult(0)

            clock = FakeClock()
            signals = [SteamSignals(False, False, False)] + [
                SteamSignals(True, True, True)
            ] * 9
            controller = SteamController(
                config=config,
                runner=OpenRunner(),
                probe=lambda: signals.pop(0),
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            )

            controller.ensure_ready()

            self.assertEqual([(["open", str(app)], 10.0)], calls)


class ProcessLockTests(unittest.TestCase):
    def test_second_launcher_is_rejected_until_first_lock_closes(self):
        """Allowing two holders would let double-click launches race profile and game state."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "launcher.lock"
            first = ProcessLock(path)
            second = ProcessLock(path)

            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.close()
            self.assertTrue(second.acquire())
            second.close()

    def test_stale_lock_path_is_harmless_and_is_not_deleted(self):
        """Treating file existence as ownership would permanently block after a hard exit."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.lock"
            path.write_text("stale", encoding="utf-8")
            lock = ProcessLock(path)

            self.assertTrue(lock.acquire())
            lock.close()

            self.assertTrue(path.exists())


class LaunchLogTests(unittest.TestCase):
    def test_only_fresh_appended_log_content_is_classified(self):
        """Reading the full log would retry forever because of an old Steam failure."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ostriv.log"
            stale = b"SteamAPI_Init() failed\n"
            path.write_bytes(stale)
            offset = path.stat().st_size
            with path.open("ab") as stream:
                stream.write(b"unrelated fresh failure\n")

            text = read_new_log(path, offset)

            self.assertEqual("unrelated fresh failure\n", text)
            self.assertEqual("other", classify_launch(text))

    def test_fresh_markers_and_invalid_utf8_are_tolerated(self):
        """Strict decoding would hide a fresh failure marker behind unrelated bad bytes."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ostriv.log"
            path.write_bytes(b"old\n")
            offset = path.stat().st_size
            with path.open("ab") as stream:
                stream.write(b"\xff SteamAPI_Init() failed\n")

            text = read_new_log(path, offset)

            self.assertIn("\ufffd", text)
            self.assertEqual("steam_api", classify_launch(text))
            self.assertEqual(
                "graphics_context", classify_launch("windows_createWindow FAILED")
            )


class FakeLock:
    def __init__(self, events, acquired=True):
        self.events = events
        self.acquired = acquired

    def acquire(self):
        self.events.append("lock")
        return self.acquired

    def close(self):
        self.events.append("unlock")


class FakeProfile:
    def __init__(self, events):
        self.events = events

    def recover(self):
        self.events.append("recover")

    def switch(self):
        self.events.append("switch")

    def restore_once(self):
        self.events.append("restore")


class FakeSteam:
    def __init__(self, events, failure=None):
        self.events = events
        self.failure = failure

    def ensure_ready(self, timeout_seconds=None):
        self.events.append(("steam", timeout_seconds))
        if self.failure is not None:
            raise self.failure


class FakeGameRunner:
    def __init__(self, events, log_path, additions):
        self.events = events
        self.log_path = Path(log_path)
        self.additions = list(additions)
        self.calls = 0

    def run(self, argv, timeout=None):
        self.calls += 1
        self.events.append(("launch", self.calls, list(argv)))
        addition = self.additions.pop(0)
        with self.log_path.open("ab") as stream:
            stream.write(addition)
        return FakeResult(0)


class FakeLogger:
    def __init__(self, events):
        self.events = events

    def info(self, message, *args):
        if message == "launcher final state: %s":
            self.events.append(("final", args[0]))

    def exception(self, _message, *_args):
        self.events.append("diagnostic")


class LauncherOrchestrationTests(unittest.TestCase):
    def test_lock_failure_shows_one_message_and_short_circuits_every_adapter(self):
        """Doing work after a failed lock would let a double click mutate shared state."""
        events = []
        dialogs = []
        config = make_config()
        config.messages["already_running"] = "Ostriv is already starting or running."

        code = run_launcher(
            config,
            lock=FakeLock(events, acquired=False),
            log_factory=lambda _path: self.fail("launcher log created after lock failure"),
            runner=self.fail,
            steam=self.fail,
            profile=self.fail,
            dialog=dialogs.append,
        )

        self.assertEqual(0, code)
        self.assertEqual(["lock"], events)
        self.assertEqual(["Ostriv is already starting or running."], dialogs)

    def test_successful_launch_follows_exact_order_and_restores_before_unlock(self):
        """Reordering recovery, switching, restoration, or unlock can strand the profile."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            Path(config.game_log).write_bytes(b"old log\n")
            events = []
            logger = FakeLogger(events)
            runner = FakeGameRunner(events, config.game_log, [b"normal exit\n"])

            code = run_launcher(
                config,
                lock=FakeLock(events),
                log_factory=lambda _path: events.append("log") or logger,
                runner=runner,
                steam=FakeSteam(events),
                profile=FakeProfile(events),
                dialog=lambda _message: self.fail("unexpected dialog"),
                install_handlers=lambda _profile: events.append("handlers"),
            )

            self.assertEqual(0, code)
            self.assertEqual(
                [
                    "lock",
                    "log",
                    "recover",
                    ("steam", None),
                    "handlers",
                    "switch",
                    ("launch", 1, config.game_command),
                    "restore",
                    ("final", "other"),
                    "unlock",
                ],
                events,
            )

    def test_readiness_failure_still_attempts_profile_restore_before_unlock(self):
        """A pre-switch Steam failure must still repair any recovery marker encountered."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            events = []
            failure = LauncherRuntimeError("steam_timeout")

            with self.assertRaises(LauncherRuntimeError):
                run_launcher(
                    config,
                    lock=FakeLock(events),
                    log_factory=lambda _path: events.append("log") or FakeLogger(events),
                    runner=self.fail,
                    steam=FakeSteam(events, failure=failure),
                    profile=FakeProfile(events),
                    dialog=lambda _message: self.fail("main owns failure dialogs"),
                    install_handlers=lambda _profile: events.append("handlers"),
                )

            self.assertEqual(
                [
                    "lock",
                    "log",
                    "recover",
                    ("steam", None),
                    "diagnostic",
                    "restore",
                    ("final", "failed"),
                    "unlock",
                ],
                events,
            )

    def test_only_fresh_steam_api_failure_gets_one_retry(self):
        """Missing the new marker or retrying it repeatedly would preserve a player dead end."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            Path(config.game_log).write_bytes(b"stale graphics text\n")
            events = []
            runner = FakeGameRunner(
                events,
                config.game_log,
                [b"SteamAPI_Init() failed\n", b"normal retry exit\n"],
            )

            code = run_launcher(
                config,
                lock=FakeLock(events),
                log_factory=lambda _path: FakeLogger(events),
                runner=runner,
                steam=FakeSteam(events),
                profile=FakeProfile(events),
                dialog=lambda _message: self.fail("unexpected dialog"),
                install_handlers=lambda _profile: None,
            )

            self.assertEqual(0, code)
            self.assertEqual(2, runner.calls)
            self.assertEqual(
                [("steam", None), ("steam", 30.0)],
                [event for event in events if isinstance(event, tuple) and event[0] == "steam"],
            )
            self.assertIn(("final", "other"), events)

    def test_graphics_and_unrelated_failures_never_retry(self):
        """Broad automatic retries would duplicate launches for failures Steam cannot fix."""
        for addition in (b"windows_createWindow FAILED\n", b"unrelated failure\n"):
            with self.subTest(addition=addition), TemporaryDirectory() as directory:
                config = make_config(directory)
                events = []
                runner = FakeGameRunner(events, config.game_log, [addition])

                code = run_launcher(
                    config,
                    lock=FakeLock(events),
                    log_factory=lambda _path: FakeLogger(events),
                    runner=runner,
                    steam=FakeSteam(events),
                    profile=FakeProfile(events),
                    dialog=lambda _message: self.fail("unexpected dialog"),
                    install_handlers=lambda _profile: None,
                )

                self.assertEqual(0, code)
                self.assertEqual(1, runner.calls)
                self.assertEqual(1, events.count("restore"))

    def test_game_runner_failure_restores_profile_and_releases_lock(self):
        """An external launch exception after switching must not strand the display profile."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            events = []

            class FailingRunner:
                def run(self, _argv, timeout=None):
                    events.append("launch")
                    raise OSError("game launch failed")

            with self.assertRaisesRegex(OSError, "game launch failed"):
                run_launcher(
                    config,
                    lock=FakeLock(events),
                    log_factory=lambda _path: FakeLogger(events),
                    runner=FailingRunner(),
                    steam=FakeSteam(events),
                    profile=FakeProfile(events),
                    dialog=lambda _message: self.fail("main owns failure dialogs"),
                    install_handlers=lambda _profile: None,
                )

            self.assertEqual(1, events.count("restore"))
            self.assertEqual("unlock", events[-1])


class LauncherMainTests(unittest.TestCase):
    def test_main_creates_log_before_lazy_platform_adapters(self):
        """Constructing a failing macOS/process adapter first would lose its diagnostics."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            config_path = Path(directory) / "launcher.json"
            write_config(config, config_path)
            events = []
            logger = FakeLogger(events)
            runner = FakeGameRunner(events, config.game_log, [b"normal exit\n"])

            with patch.object(
                runtime, "ProcessLock", side_effect=lambda _path: FakeLock(events)
            ), patch.object(
                runtime,
                "_create_launcher_log",
                side_effect=lambda _path: events.append("log") or logger,
            ), patch.object(
                runtime,
                "ExternalProcessRunner",
                side_effect=lambda: events.append("runner-adapter") or runner,
            ), patch.object(
                runtime,
                "ColorSyncProfileBackend",
                side_effect=lambda: events.append("color-adapter") or object(),
            ), patch.object(
                runtime,
                "ProfileGuard",
                side_effect=lambda *_args: events.append("profile-adapter")
                or FakeProfile(events),
            ), patch.object(
                runtime,
                "SteamController",
                side_effect=lambda **_kwargs: events.append("steam-adapter")
                or FakeSteam(events),
            ), patch.object(
                runtime,
                "install_signal_handlers",
                side_effect=lambda _profile: events.append("handlers"),
            ), patch.object(
                runtime,
                "_display_dialog",
                side_effect=lambda _message: self.fail("unexpected dialog"),
            ):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main([str(config_path)])

            self.assertEqual(0, code)
            self.assertEqual("", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())
            self.assertLess(events.index("log"), events.index("runner-adapter"))
            self.assertLess(events.index("log"), events.index("color-adapter"))
            self.assertLess(events.index("log"), events.index("steam-adapter"))

    def test_main_maps_handled_failure_to_one_configured_dialog_and_log(self):
        """Leaking a traceback or repeating guidance would violate the player boundary."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            config.messages.update(
                {
                    "steam_timeout": "Steam is still starting. Try again shortly.",
                    "error": "Unable to start Ostriv.",
                }
            )
            config_path = Path(directory) / "launcher.json"
            write_config(config, config_path)
            events = []
            dialogs = []
            failure = LauncherRuntimeError(
                "steam_timeout", "raw registry and process diagnostics"
            )

            with patch.object(
                runtime, "ProcessLock", side_effect=lambda _path: FakeLock(events)
            ), patch.object(
                runtime, "ExternalProcessRunner", return_value=self.fail
            ), patch.object(
                runtime, "ColorSyncProfileBackend", return_value=object()
            ), patch.object(
                runtime,
                "ProfileGuard",
                side_effect=lambda *_args: FakeProfile(events),
            ), patch.object(
                runtime,
                "SteamController",
                side_effect=lambda **_kwargs: FakeSteam(events, failure=failure),
            ), patch.object(runtime, "_display_dialog", side_effect=dialogs.append):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main([str(config_path)])

            self.assertNotEqual(0, code)
            self.assertEqual(
                ["Steam is still starting. Try again shortly."], dialogs
            )
            self.assertEqual("", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())
            self.assertIn(
                "raw registry and process diagnostics",
                Path(config.launcher_log).read_text(encoding="utf-8"),
            )


class LauncherConfigValidationTests(unittest.TestCase):
    def test_load_rejects_non_mapping_root_and_wrong_field_types(self):
        """Weak JSON typing would let main reach unsafe path and message operations."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.json"
            valid = make_config(directory).__dict__
            invalid_payloads = [
                [],
                {**valid, "schema": True},
                {**valid, "bottle_name": 7},
                {**valid, "game_command": "wine ostriv.exe"},
                {**valid, "game_command": ["wine", 7]},
                {**valid, "steam_links": ["Steam.lnk", None]},
                {**valid, "messages": ["Unable to start"]},
                {**valid, "messages": {"error": 7}},
            ]

            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(
                        RuntimeError, "Invalid launcher configuration"
                    ):
                        LauncherConfig.load(path)


if __name__ == "__main__":
    unittest.main()
