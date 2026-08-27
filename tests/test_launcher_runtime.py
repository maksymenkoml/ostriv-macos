import unittest
from dataclasses import dataclass
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import plistlib
import stat
import subprocess
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
    def __init__(
        self,
        process=True,
        registry=b"ActiveUser    REG_DWORD    0x1\n",
        renderer=True,
        root="/tmp",
    ):
        self.process = process
        self.registry = registry
        self.renderer = renderer
        self.root = root
        self.calls = []

    def run(self, argv, timeout=None):
        argv = list(argv)
        self.calls.append((argv, timeout))
        if argv == [
            "/usr/bin/pgrep",
            "-f",
            "steam[.]exe|steamwebhelper[.]exe",
        ]:
            pids = []
            if self.process:
                pids.append("9417")
            if self.renderer:
                pids.append("9418")
            return FakeResult(0 if pids else 1, "\n".join(pids))
        if argv[:1] == ["/bin/ps"]:
            rows = []
            if self.process:
                rows.append("9417 00:10 C:\\Steam\\steam.exe")
            if self.renderer:
                rows.append(
                    "9418 00:09 C:\\Steam\\steamwebhelper.exe --type=renderer"
                )
            return FakeResult(0, "\n".join(rows))
        if argv[:1] == ["/usr/sbin/lsof"]:
            rows = []
            for pid in ("9417", "9418"):
                if pid in argv[-4]:
                    rows.append(
                        "p{}\nfcwd\n"
                        "n{}/Bottles/Ostriv/drive_c/Program Files (x86)/Steam".format(
                            pid, self.root
                        )
                    )
            return FakeResult(0, "\n".join(rows))
        return FakeResult(0, self.registry)


class BottleScopedHostRunner:
    def __init__(self, cwd, cwd_error=None):
        self.cwd = cwd
        self.cwd_error = cwd_error
        self.calls = []

    def run(self, argv, timeout=None):
        argv = list(argv)
        self.calls.append((argv, timeout))
        if argv == [
            "/usr/bin/pgrep",
            "-f",
            "steam[.]exe|steamwebhelper[.]exe",
        ]:
            return FakeResult(0, "89520\n89528\n89570\n")
        if argv[:1] == ["/bin/ps"]:
            return FakeResult(
                0,
                "89520 00:10 C:\\Steam\\steam.exe\n"
                "89528 00:09 C:\\Steam\\steamwebhelper.exe --type=utility\n"
                "89570 00:08 C:\\Steam\\steamwebhelper.exe --type=renderer\n",
            )
        if argv[:1] == ["/usr/sbin/lsof"]:
            if self.cwd_error is not None:
                raise self.cwd_error
            return FakeResult(
                0,
                "p89520\nfcwd\nn{}\n"
                "p89570\nfcwd\nn{}\n".format(self.cwd, self.cwd),
            )
        if "reg" in argv:
            return FakeResult(0, b"ActiveUser REG_DWORD 0x1\n")
        raise AssertionError(argv)


def make_config(root="/tmp"):
    return LauncherConfig(
        schema=1,
        bottle_name="Ostriv",
        bottle_argument="Ostriv",
        scope="managed",
        bottle_realpath=root + "/Bottles/Ostriv",
        bottle_tag="CrossOver-selected-id/",
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


def write_active_user(config, active=True):
    registry = Path(config.bottle_realpath) / "user.reg"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        "[Software\\\\Valve\\\\Steam\\\\ActiveProcess] 1\n"
        '"ActiveUser"=dword:{:08x}\n'.format(1 if active else 0),
        encoding="utf-8",
    )


class SteamControllerTests(unittest.TestCase):
    def test_probe_uses_fast_bottle_scoped_host_signals_and_registry_file(self):
        """A healthy client must not pay for Wine commands on every poll."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            write_active_user(config)
            bottle = Path(config.bottle_realpath)
            calls = []

            class NativeRunner:
                def run(self, argv, timeout=None):
                    argv = list(argv)
                    calls.append(argv)
                    if argv == [
                        "/usr/bin/pgrep",
                        "-f",
                        "steam[.]exe|steamwebhelper[.]exe",
                    ]:
                        return FakeResult(0, "9417\n9418\n")
                    if argv[:1] == ["/bin/ps"]:
                        return FakeResult(
                            0,
                            "9417 00:10 C:\\Steam\\steam.exe\n"
                            "9418 00:09 C:\\Steam\\steamwebhelper.exe --type=renderer\n",
                        )
                    if argv[:1] == ["/usr/sbin/lsof"]:
                        cwd = bottle / "drive_c/Program Files (x86)/Steam"
                        return FakeResult(
                            0,
                            "p9417\nfcwd\nn{}\n"
                            "p9418\nfcwd\nn{}\n".format(cwd, cwd),
                        )
                    raise AssertionError(argv)

            signals = SteamController(config=config, runner=NativeRunner()).probe()

        self.assertEqual(SteamSignals(True, True, True), signals)
        self.assertEqual(3, len(calls))
        self.assertFalse(any("wine" in Path(call[0]).name for call in calls))

    def test_logged_out_registry_file_does_not_fall_back_to_slow_wine_query(self):
        """A valid zero ActiveUser is definitive, not a reason to run Wine."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            write_active_user(config, active=False)
            runner = ProbeRunner(root=directory)

            signals = SteamController(config=config, runner=runner).probe()

        self.assertEqual(SteamSignals(True, False, True), signals)
        self.assertFalse(
            any("reg" in call for call, _timeout in runner.calls),
        )

    def test_stale_nonzero_registry_file_requires_live_wine_confirmation(self):
        """Persisted login state from an older Steam process is not current evidence."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            write_active_user(config)
            registry_path = Path(config.bottle_realpath) / "user.reg"
            os.utime(registry_path, (1, 1))
            runner = ProbeRunner(
                registry=b"ActiveUser REG_DWORD 0x0\n",
                root=directory,
            )

            signals = SteamController(config=config, runner=runner).probe()

        self.assertEqual(SteamSignals(True, False, True), signals)
        self.assertTrue(any("reg" in call for call, _timeout in runner.calls))

    def test_failed_wine_registry_query_cannot_supply_active_user(self):
        """Valid-looking partial stdout from a failed query is not login evidence."""

        class FailedRegistryRunner(ProbeRunner):
            def run(self, argv, timeout=None):
                if "reg" in argv:
                    return FakeResult(1, b"ActiveUser REG_DWORD 0x1\n")
                return super().run(argv, timeout)

        signals = SteamController(
            config=make_config(), runner=FailedRegistryRunner()
        ).probe()

        self.assertEqual(SteamSignals(True, False, True), signals)

    def test_live_registry_confirmation_is_cached_for_unchanged_process_and_file(self):
        """A stale snapshot gets one live check, not Wine polling on every probe."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            write_active_user(config)
            registry_path = Path(config.bottle_realpath) / "user.reg"
            os.utime(registry_path, (1, 1))
            runner = ProbeRunner(root=directory)
            controller = SteamController(config=config, runner=runner)

            first = controller.probe()
            second = controller.probe()

        self.assertTrue(first.ready)
        self.assertTrue(second.ready)
        registry_calls = [
            call for call, _timeout in runner.calls if "reg" in call
        ]
        self.assertEqual(1, len(registry_calls))

    def test_logged_out_live_confirmation_is_rechecked_on_the_next_probe(self):
        """Signing in after a logged-out query must become visible during the wait."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            write_active_user(config)
            registry_path = Path(config.bottle_realpath) / "user.reg"
            os.utime(registry_path, (1, 1))

            class SigningInRunner(ProbeRunner):
                def __init__(self):
                    super().__init__(root=directory)
                    self.registry_results = iter(
                        (
                            b"ActiveUser REG_DWORD 0x0\n",
                            b"ActiveUser REG_DWORD 0x1\n",
                        )
                    )

                def run(self, argv, timeout=None):
                    if "reg" in argv:
                        self.calls.append((list(argv), timeout))
                        return FakeResult(0, next(self.registry_results))
                    return super().run(argv, timeout)

            controller = SteamController(config=config, runner=SigningInRunner())

            first = controller.probe()
            second = controller.probe()

        self.assertFalse(first.active_user)
        self.assertTrue(second.active_user)

    def test_live_confirmation_expires_while_process_and_file_are_unchanged(self):
        """A cached login result must not mask a later sign-out indefinitely."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            write_active_user(config)
            registry_path = Path(config.bottle_realpath) / "user.reg"
            os.utime(registry_path, (1, 1))
            clock = FakeClock()

            class SigningOutRunner(ProbeRunner):
                def __init__(self):
                    super().__init__(root=directory)
                    self.registry_results = iter(
                        (
                            b"ActiveUser REG_DWORD 0x1\n",
                            b"ActiveUser REG_DWORD 0x0\n",
                        )
                    )

                def run(self, argv, timeout=None):
                    if "reg" in argv:
                        self.calls.append((list(argv), timeout))
                        return FakeResult(0, next(self.registry_results))
                    return super().run(argv, timeout)

            controller = SteamController(
                config=config,
                runner=SigningOutRunner(),
                monotonic=clock.monotonic,
            )

            first = controller.probe()
            clock.sleep(60.0)
            second = controller.probe()

        self.assertTrue(first.active_user)
        self.assertFalse(second.active_user)

    def test_live_confirmation_is_not_reused_for_a_new_process_generation(self):
        """PID reuse must not carry login state from an older Steam process."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            write_active_user(config)
            registry_path = Path(config.bottle_realpath) / "user.reg"
            os.utime(registry_path, (1, 1))
            observations = iter((100.0, 200.0))

            class RestartedSteamRunner(ProbeRunner):
                def __init__(self):
                    super().__init__(root=directory)
                    self.registry_results = iter(
                        (
                            b"ActiveUser REG_DWORD 0x1\n",
                            b"ActiveUser REG_DWORD 0x0\n",
                        )
                    )

                def run(self, argv, timeout=None):
                    if "reg" in argv:
                        self.calls.append((list(argv), timeout))
                        return FakeResult(0, next(self.registry_results))
                    return super().run(argv, timeout)

            controller = SteamController(
                config=config,
                runner=RestartedSteamRunner(),
                wall_time=lambda: next(observations),
            )

            first = controller.probe()
            second = controller.probe()

        self.assertTrue(first.active_user)
        self.assertFalse(second.active_user)

    def test_probe_records_each_readiness_signal_in_the_launcher_file_log(self):
        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "launcher.log"
            logger = runtime._create_launcher_log(log_path)
            controller = SteamController(
                config=make_config(directory),
                runner=ProbeRunner(process=True, renderer=False, root=directory),
            )
            controller.logger = logger

            signals = controller.probe()

            self.assertEqual(SteamSignals(True, True, False), signals)
            self.assertIn(
                "steam probe process=True process_known=True active_user=True "
                "renderer=False ready=False",
                log_path.read_text(encoding="utf-8"),
            )

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

                expected_active_user = active_user if process else False
                self.assertEqual(
                    SteamSignals(process, expected_active_user, renderer), signals
                )
                self.assertEqual(ready, signals.ready)

    def test_probe_tolerates_invalid_utf8_registry_output(self):
        """Strict registry decoding would prevent a ready client from being detected."""
        runner = ProbeRunner(registry=b"\xff ignored\nActiveUser REG_DWORD 0x1\n")

        signals = SteamController(config=make_config(), runner=runner).probe()

        self.assertTrue(signals.active_user)

    def test_transient_host_probe_failure_does_not_abort_cold_start(self):
        """One failed host probe must not abort the five-minute readiness window."""
        clock = FakeClock()

        class TimeoutOnceRunner(ProbeRunner):
            def __init__(self):
                super().__init__()
                self.timed_out = False

            def run(self, argv, timeout=None):
                if list(argv)[:1] == ["/usr/bin/pgrep"] and not self.timed_out:
                    self.timed_out = True
                    raise runtime.ExternalCommandError("host process probe failed")
                return super().run(argv, timeout)

        opened = []
        controller = SteamController(
            config=make_config(),
            runner=TimeoutOnceRunner(),
            open_steam=lambda: opened.append(True),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            poll_seconds=1.0,
            transition_stable_seconds=0.0,
        )

        controller.ensure_ready()

        self.assertEqual(1.0, clock.now)
        self.assertEqual([], opened)

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

    def test_retry_mode_requires_30_continuous_ready_seconds_without_warm_shortcut(self):
        """Reusing warm readiness would retry after two seconds instead of a stable window."""
        clock = FakeClock()
        controller = SteamController(
            probe=lambda: SteamSignals(True, True, True),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        controller.ensure_ready(retry=True)

        self.assertEqual(30.0, clock.now)

    def test_retry_mode_resets_30_second_timer_after_signal_drop(self):
        """Counting ready time across a drop would retry against a transitioning client."""
        clock = FakeClock()

        def probe():
            if clock.now == 10.0:
                return SteamSignals(True, True, False)
            return SteamSignals(True, True, True)

        controller = SteamController(
            probe=probe,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        controller.ensure_ready(retry=True)

        self.assertEqual(42.0, clock.now)

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

    def test_warm_client_disappearance_on_second_probe_opens_once(self):
        """Checking absence only on the first probe would strand a disappearing warm client."""
        clock = FakeClock()
        opened = []
        notified = []
        signals = [
            SteamSignals(True, True, True),
            SteamSignals(False, False, False),
        ] + [SteamSignals(True, True, True)] * 9
        controller = SteamController(
            probe=lambda: signals.pop(0),
            open_steam=lambda: opened.append(clock.now),
            notify=lambda: notified.append(clock.now),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        controller.ensure_ready()

        self.assertEqual([2.0], opened)
        self.assertEqual([2.0], notified)

    def test_starting_client_that_disappears_later_opens_only_once(self):
        """A later process loss must consume the one allowed open without repeated starts."""
        clock = FakeClock()
        opened = []
        notified = []
        signals = [
            SteamSignals(True, False, False),
            SteamSignals(False, False, False),
            SteamSignals(False, False, False),
        ] + [SteamSignals(True, True, True)] * 9
        controller = SteamController(
            probe=lambda: signals.pop(0),
            open_steam=lambda: opened.append(clock.now),
            notify=lambda: notified.append(clock.now),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        controller.ensure_ready()

        self.assertEqual([2.0], opened)
        self.assertEqual([0.0], notified)

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

    def test_non_divisible_timeout_caps_last_sleep_at_absolute_deadline(self):
        """Sleeping a full poll interval would overrun a five-second readiness deadline."""
        clock = FakeClock()
        controller = SteamController(
            probe=lambda: SteamSignals(True, False, True),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            poll_seconds=2.0,
        )

        with self.assertRaises(LauncherRuntimeError):
            controller.ensure_ready(timeout_seconds=5.0)

        self.assertEqual(5.0, clock.now)

    def test_slow_probe_cannot_return_ready_after_deadline(self):
        """Readiness observed only after a slow probe crosses the deadline must be rejected."""
        clock = FakeClock()

        def slow_ready_probe():
            clock.now += 3.0
            return SteamSignals(True, True, True)

        controller = SteamController(
            probe=slow_ready_probe,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            poll_seconds=1.0,
        )

        with self.assertRaises(LauncherRuntimeError):
            controller.ensure_ready(timeout_seconds=5.0)

        self.assertEqual(7.0, clock.now)

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
                        "CXHelperAppBottleTag": config.bottle_tag,
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

    def test_steam_app_requires_matching_bottle_name_and_tag(self):
        """A same-named app owned by another bottle must never be opened."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            object.__setattr__(config, "bottle_tag", "CrossOver-selected-id/")
            config.steam_links.clear()
            root = Path(config.steam_apps_root)
            wrong = root / "Wrong Steam.app"
            contents = wrong / "Contents"
            contents.mkdir(parents=True)
            with (contents / "Info.plist").open("wb") as stream:
                plistlib.dump(
                    {
                        "CXHelperAppBottleName": config.bottle_name,
                        "CXHelperAppBottleTag": "CrossOver-other-id/",
                        "CrossOverHelperCommand": '"C:/Steam/Steam.lnk"',
                    },
                    stream,
                )
            calls = []

            class OpenRunner:
                def run(self, argv, timeout=None):
                    calls.append((list(argv), timeout))
                    return FakeResult(0)

            controller = SteamController(config=config, runner=OpenRunner())

            self.assertFalse(controller._open_configured_steam())
            self.assertEqual([], calls)

    def test_probe_never_uses_crossover_windows_pid_namespace(self):
        """CrossOver's Windows PIDs must never be treated as macOS process IDs."""
        config = make_config("/private/tmp/selected")
        runner = BottleScopedHostRunner(
            "/private/tmp/selected/Bottles/Ostriv/drive_c/"
            "Program Files (x86)/Steam"
        )
        signals = SteamController(
            config=config, runner=runner
        ).probe()

        self.assertEqual(SteamSignals(True, True, True), signals)
        self.assertFalse(
            any("tasklist" in call for call, _timeout in runner.calls),
            "the native probe must never consume CrossOver's Windows PIDs",
        )

    def test_process_image_requires_windows_executable_position_and_boundary(self):
        self.assertEqual(
            "steam.exe",
            SteamController._process_image("C:\\Program Files\\Steam\\steam.exe -silent"),
        )
        self.assertEqual(
            "",
            SteamController._process_image("C:\\Steam\\steam.exe.backup -silent"),
        )
        self.assertEqual(
            "",
            SteamController._process_image("--note=C:\\Steam\\steam.exe"),
        )

    def test_elapsed_time_parser_is_bounded_and_accepts_macos_ps_formats(self):
        self.assertEqual(452, SteamController._elapsed_seconds("07:32"))
        self.assertEqual(93784, SteamController._elapsed_seconds("1-02:03:04"))
        self.assertIsNone(SteamController._elapsed_seconds("9" * 5000))
        self.assertIsNone(SteamController._elapsed_seconds("25:00:00"))

    def test_steam_and_renderer_are_scoped_to_the_bottle_independently(self):
        selected = "/private/tmp/selected/Bottles/Ostriv/drive_c/Steam"
        other = "/private/tmp/other/Bottles/Ostriv/drive_c/Steam"

        class SplitOwnershipRunner(ProbeRunner):
            def __init__(self, steam_cwd, renderer_cwd):
                super().__init__(root="/private/tmp/selected")
                self.steam_cwd = steam_cwd
                self.renderer_cwd = renderer_cwd

            def run(self, argv, timeout=None):
                if list(argv)[:1] == ["/usr/sbin/lsof"]:
                    return FakeResult(
                        0,
                        "p9417\nfcwd\nn{}\n"
                        "p9418\nfcwd\nn{}\n".format(
                            self.steam_cwd, self.renderer_cwd
                        ),
                    )
                return super().run(argv, timeout)

        config = make_config("/private/tmp/selected")
        for steam_cwd, renderer_cwd, expected in (
            (selected, other, SteamSignals(True, True, False)),
            (other, selected, SteamSignals(False, False, True)),
        ):
            with self.subTest(steam_cwd=steam_cwd, renderer_cwd=renderer_cwd):
                signals = SteamController(
                    config=config,
                    runner=SplitOwnershipRunner(steam_cwd, renderer_cwd),
                ).probe()
                self.assertEqual(expected, signals)

    def test_renderer_from_another_bottle_cannot_satisfy_readiness(self):
        """A host renderer is ready only when its cwd belongs to the selected bottle."""
        config = make_config("/private/tmp/selected")
        runner = BottleScopedHostRunner(
            "/private/tmp/other/Bottles/Ostriv/drive_c/Program Files (x86)/Steam"
        )
        signals = SteamController(
            config=config, runner=runner
        ).probe()

        self.assertEqual(SteamSignals(False, False, False), signals)

    def test_bottle_cwd_probe_failure_is_a_safe_false_negative(self):
        """An unavailable cwd probe must not crash or trust an unscoped renderer."""
        runner = BottleScopedHostRunner(
            "/tmp/Bottles/Ostriv",
            cwd_error=runtime.ExternalCommandError("lsof unavailable"),
        )

        result = SteamController(config=make_config(), runner=runner).probe()

        self.assertEqual(SteamSignals(False, False, False, False), result)

    def test_incomplete_cwd_evidence_is_unknown_instead_of_known_absence(self):
        """Missing CWD rows must not make a running Steam client look absent."""
        selected = (
            "/private/tmp/selected/Bottles/Ostriv/drive_c/"
            "Program Files (x86)/Steam"
        )

        class IncompleteCwdRunner(ProbeRunner):
            def __init__(self, output):
                super().__init__(root="/private/tmp/selected")
                self.output = output

            def run(self, argv, timeout=None):
                if list(argv)[:1] == ["/usr/sbin/lsof"]:
                    return FakeResult(0, self.output)
                return super().run(argv, timeout)

        config = make_config("/private/tmp/selected")
        for output in (
            "",
            "p9417\nfcwd\nn{}\n".format(selected),
            "p9417\nn{}\np9418\nfcwd\nn{}\n".format(selected, selected),
        ):
            with self.subTest(output=output):
                signals = SteamController(
                    config=config,
                    runner=IncompleteCwdRunner(output),
                ).probe()

                self.assertEqual(
                    SteamSignals(False, False, False, False), signals
                )

    def test_host_pid_parser_rejects_non_ascii_and_pathological_decimals(self):
        """Untrusted host PID text must not reach Python's bounded integer parser."""
        pathological = "9" * 5000

        class PathologicalCandidateRunner(ProbeRunner):
            def run(self, argv, timeout=None):
                if list(argv)[:1] == ["/usr/bin/pgrep"]:
                    return FakeResult(0, "\uff11\uff12\uff13\n{}\n".format(pathological))
                return super().run(argv, timeout)

        signals = SteamController(
            config=make_config(), runner=PathologicalCandidateRunner()
        ).probe()

        self.assertEqual(SteamSignals(False, False, False, False), signals)

    def test_ps_pid_parser_treats_pathological_decimal_as_safe_false(self):
        """A huge ps PID field cannot crash selected-helper role correlation."""
        config = make_config("/private/tmp/selected")
        pathological = "9" * 5000

        class PathologicalPsRunner(ProbeRunner):
            def run(self, argv, timeout=None):
                if list(argv)[:1] == ["/bin/ps"]:
                    return FakeResult(
                        0,
                        pathological
                        + " /selected/steamwebhelper.exe --type=renderer\n",
                    )
                return super().run(argv, timeout)

        signals = SteamController(
            config=config, runner=PathologicalPsRunner(root="/private/tmp/selected")
        ).probe()

        self.assertEqual(SteamSignals(False, False, False, False), signals)

    def test_non_renderer_helper_never_passes_selected_bottle_readiness_gate(self):
        """A helper without the renderer role must not satisfy the 15-second gate."""
        config = make_config("/private/tmp/selected")
        clock = FakeClock()
        calls = []

        class NonRendererRunner:
            def run(self, argv, timeout=None):
                argv = list(argv)
                calls.append(argv)
                if argv[:1] == ["/usr/bin/pgrep"]:
                    return FakeResult(0, "9417\n9418\n")
                if argv[:1] == ["/bin/ps"]:
                    return FakeResult(
                        0,
                        "9417 00:10 C:\\Steam\\steam.exe\n"
                        "9418 00:09 C:\\Steam\\steamwebhelper.exe --type=gpu-process\n",
                    )
                if argv[:1] == ["/usr/sbin/lsof"]:
                    return FakeResult(
                        0,
                        "p9417\nfcwd\n"
                        "n/private/tmp/selected/Bottles/Ostriv/drive_c/"
                        "Program Files (x86)/Steam\n",
                    )
                return FakeResult(0, b"ActiveUser REG_DWORD 0x1\n")

        controller = SteamController(
            config=config,
            runner=NonRendererRunner(),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            poll_seconds=2.0,
            timeout_seconds=18.0,
        )

        with self.assertRaises(LauncherRuntimeError) as caught:
            controller.ensure_ready()

        self.assertEqual("steam_timeout", caught.exception.message_key)
        self.assertEqual(18.0, clock.now)
        lsof_call = next(call for call in calls if call[:1] == ["/usr/sbin/lsof"])
        self.assertEqual(
            "9417",
            lsof_call[lsof_call.index("-p") + 1],
            "a non-renderer must not reach bottle ownership probing",
        )

    def test_renderer_detail_query_failure_is_a_safe_false_negative(self):
        """Missing host detail evidence must never infer a renderer from its image name."""
        config = make_config("/private/tmp/selected")

        class FailedDetailRunner(ProbeRunner):
            def run(self, argv, timeout=None):
                if list(argv)[:1] == ["/bin/ps"]:
                    return FakeResult(1, "", "ps unavailable")
                return super().run(argv, timeout)

        signals = SteamController(
            config=config, runner=FailedDetailRunner(root="/private/tmp/selected")
        ).probe()

        self.assertEqual(SteamSignals(False, False, False, False), signals)


class ExternalProcessRunnerTests(unittest.TestCase):
    @patch("subprocess.run")
    def test_file_log_bounds_decoded_output_and_redacts_echoed_sensitive_values(
        self, run
    ):
        run.return_value = FakeResult(
            9,
            b"private-value result\xff\n" + b"x" * 5000,
            b"launcher failure\x8e\n",
        )
        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "launcher.log"
            runner = runtime.ExternalProcessRunner(
                logger=runtime._create_launcher_log(log_path)
            )

            result = runner.run(
                ["wine", "reg", "add", "/d", "private-value", "/f"], timeout=2
            )

            text = log_path.read_text(encoding="utf-8")
        self.assertEqual(9, result.returncode)
        self.assertIn("private-value", result.stdout)
        self.assertIn("command result returncode=9", text)
        self.assertIn("result�", text)
        self.assertIn("launcher failure�", text)
        self.assertIn("<truncated", text)
        self.assertNotIn("private-value", text)
        self.assertLess(len(text), 6000)

    @patch("subprocess.run")
    def test_lsof_cwd_output_is_omitted_from_the_launcher_log(self, run):
        """Bottle paths from host process inspection are private diagnostics."""
        private_path = "/Users/player/Library/CrossOver/Private Bottle"
        run.return_value = FakeResult(0, ("n" + private_path + "\n").encode(), b"")
        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "launcher.log"
            runner = runtime.ExternalProcessRunner(
                logger=runtime._create_launcher_log(log_path)
            )

            result = runner.run(
                ["/usr/sbin/lsof", "-w", "-b", "-a", "-p", "9418", "-Fn"],
                timeout=5,
            )

            text = log_path.read_text(encoding="utf-8")
        self.assertIn(private_path, result.stdout)
        self.assertIn("<process details omitted>", text)
        self.assertNotIn(private_path, text)

    @patch("subprocess.run")
    def test_private_bottle_and_open_paths_are_redacted_from_log(self, run):
        private_bottle = "/Users/player/Library/CrossOver/Bottles/Private"
        private_app = "/Users/player/Applications/CrossOver/Steam.app"
        run.return_value = FakeResult(0, b"", b"")
        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "launcher.log"
            runner = runtime.ExternalProcessRunner(
                logger=runtime._create_launcher_log(log_path)
            )
            runner.run(
                ["wine", "--bottle", private_bottle, "reg", "query", "HKCU\\Key"],
                timeout=5,
            )
            runner.run(["open", private_app], timeout=5)

            text = log_path.read_text(encoding="utf-8")

        self.assertNotIn(private_bottle, text)
        self.assertNotIn(private_app, text)
        self.assertIn("<redacted>", text)

    @patch("subprocess.run")
    def test_process_detail_query_never_logs_captured_command_lines(self, run):
        """Host process arguments stay internal even when readiness needs their role."""
        run.return_value = FakeResult(
            0,
            b"418 steamwebhelper --type=renderer --token private-value\n",
            b"private-value warning\n",
        )
        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "launcher.log"
            runner = runtime.ExternalProcessRunner(
                logger=runtime._create_launcher_log(log_path)
            )

            result = runner.run(
                ["/bin/ps", "-ww", "-o", "pid=,command=", "-p", "418"],
                timeout=5.0,
            )

            text = log_path.read_text(encoding="utf-8")
        self.assertIn("private-value", result.stdout)
        self.assertNotIn("private-value", text)
        self.assertIn("<process details omitted>", text)


class ProcessLockTests(unittest.TestCase):
    @unittest.skipIf(not hasattr(os, "O_NOFOLLOW"), "requires no-follow file opens")
    def test_lock_open_never_follows_a_substituted_symlink(self):
        """A lock alias must not create or lock an external target."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "outside.lock"
            alias = root / "launcher.lock"
            alias.symlink_to(target)

            with self.assertRaises(OSError):
                ProcessLock(alias).acquire()

            self.assertTrue(alias.is_symlink())
            self.assertFalse(target.exists())

    def test_lock_rejects_path_replaced_after_open_without_deleting_replacement(self):
        """A held descriptor must still name the current single-link lock inode."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.lock"
            path.write_bytes(b"owned\n")
            lock = ProcessLock(path)

            def substitute_after_lock(_descriptor, _operation):
                path.unlink()
                path.write_bytes(b"unowned replacement\n")

            try:
                with patch.object(
                    runtime.fcntl, "flock", side_effect=substitute_after_lock
                ):
                    with self.assertRaisesRegex(OSError, "lock path changed"):
                        lock.acquire()
            finally:
                lock.close()

            self.assertEqual(b"unowned replacement\n", path.read_bytes())

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

    def test_held_lock_proof_rejects_changed_content_and_mode(self):
        """Recovery may preserve an inode only while its full ownership proof holds."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.lock"
            expected = b"owned-token\n"
            path.write_bytes(expected)
            path.chmod(0o600)
            lock = ProcessLock(path)
            self.assertTrue(lock.acquire())
            try:
                path.write_bytes(b"changed-token\n")
                with self.assertRaisesRegex(OSError, "content changed"):
                    lock.validate_current_path(expected, 0o600)
                self.assertEqual(b"changed-token\n", path.read_bytes())

                path.write_bytes(expected)
                path.chmod(0o644)
                with self.assertRaisesRegex(OSError, "mode changed"):
                    lock.validate_current_path(expected, 0o600)
                self.assertEqual(0o644, stat.S_IMODE(path.stat().st_mode))
            finally:
                lock.close()

    def test_stale_lock_path_is_harmless_and_is_not_deleted(self):
        """Treating file existence as ownership would permanently block after a hard exit."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.lock"
            path.write_text("stale", encoding="utf-8")
            lock = ProcessLock(path)

            self.assertTrue(lock.acquire())
            lock.close()

            self.assertTrue(path.exists())

    def test_unexpected_flock_error_closes_local_descriptor(self):
        """An unexpected flock failure must not leak the just-opened descriptor."""
        with TemporaryDirectory() as directory:
            lock = ProcessLock(Path(directory) / "launcher.lock")
            closed = []
            with patch.object(runtime.os, "open", return_value=71), patch.object(
                runtime.os,
                "fstat",
                return_value=type("Status", (), {"st_mode": 0o100600, "st_nlink": 1})(),
            ), patch.object(
                runtime.fcntl, "flock", side_effect=OSError("flock failed")
            ), patch.object(runtime.os, "close", side_effect=closed.append):
                with self.assertRaisesRegex(OSError, "flock failed"):
                    lock.acquire()

            self.assertEqual([71], closed)
            self.assertIsNone(lock.fd)

    def test_repeat_acquire_is_idempotent_without_opening_another_descriptor(self):
        """A repeat acquire must not overwrite and leak the held lock descriptor."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.lock"
            path.write_bytes(b"")
            status = path.stat()
            lock = ProcessLock(path)
            opened = []
            flocked = []
            closed = []

            def open_once(*_args):
                if opened:
                    raise AssertionError("repeat acquire opened another descriptor")
                opened.append(72)
                return 72

            def flock_once(descriptor, _flags):
                if flocked:
                    raise AssertionError("repeat acquire called flock again")
                flocked.append(descriptor)

            with patch.object(runtime.os, "open", side_effect=open_once), patch.object(
                runtime.os,
                "fstat",
                return_value=status,
            ), patch.object(
                runtime.fcntl, "flock", side_effect=flock_once
            ), patch.object(runtime.os, "close", side_effect=closed.append):
                self.assertTrue(lock.acquire())
                self.assertTrue(lock.acquire())
                lock.close()

            self.assertEqual([72], opened)
            self.assertEqual([72], flocked)
            self.assertEqual([72], closed)


class LaunchLogTests(unittest.TestCase):
    def test_generation_token_reads_only_bounded_content_evidence(self):
        """Capturing an old large log must not hash its unbounded full contents."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ostriv.log"
            path.write_bytes(b"x" * (runtime.LOG_TOKEN_TAIL_BYTES * 4))
            real_open = Path.open
            bytes_read = [0]

            class CountingReader:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    self.stream.__enter__()
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def read(self, size=-1):
                    data = self.stream.read(size)
                    bytes_read[0] += len(data)
                    return data

                def __getattr__(self, name):
                    return getattr(self.stream, name)

            def counted_open(candidate, *args, **kwargs):
                stream = real_open(candidate, *args, **kwargs)
                return CountingReader(stream) if Path(candidate) == path else stream

            with patch.object(Path, "open", new=counted_open):
                runtime.capture_log_generation(path)

            self.assertLessEqual(bytes_read[0], runtime.LOG_TOKEN_TAIL_BYTES * 2)

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
            self.assertEqual("clean_exit", classify_launch("done exiting."))

    def test_generation_reader_handles_append_truncate_recreate_and_in_place_overwrite(self):
        """Ostriv may append, truncate, recreate, or overwrite its per-run log."""
        cases = ("append", "truncate", "recreate", "overwrite")
        for case in cases:
            with self.subTest(case=case), TemporaryDirectory() as directory:
                path = Path(directory) / "ostriv.log"
                stale = b"stale SteamAPI_Init() failed\n"
                path.write_bytes(stale)
                before = path.stat()
                token = runtime.capture_log_generation(path)
                fresh = b"windows_createWindow FAILED\n"
                if case == "append":
                    with path.open("ab") as stream:
                        stream.write(fresh)
                elif case == "truncate":
                    path.write_bytes(fresh)
                elif case == "recreate":
                    path.unlink()
                    path.write_bytes(fresh)
                else:
                    replacement = fresh.ljust(len(stale), b"!")[: len(stale)]
                    with path.open("r+b") as stream:
                        stream.seek(0)
                        stream.write(replacement)
                        stream.flush()
                    os.utime(
                        path,
                        ns=(before.st_atime_ns, before.st_mtime_ns),
                    )

                text = read_new_log(path, token)

                self.assertNotIn("stale SteamAPI", text)
                self.assertEqual("graphics_context", classify_launch(text))

    def test_generation_reader_returns_empty_for_untouched_stale_marker(self):
        """Metadata and content evidence must not reclassify an untouched old failure."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ostriv.log"
            path.write_bytes(b"SteamAPI_Init() failed\n")
            token = runtime.capture_log_generation(path)

            self.assertEqual("", read_new_log(path, token))


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

    def ensure_ready(self, timeout_seconds=None, retry=False):
        self.events.append(("steam", timeout_seconds, retry))
        if self.failure is not None:
            raise self.failure


class FakeGameRunner:
    def __init__(self, events, log_path, additions, returncodes=None):
        self.events = events
        self.log_path = Path(log_path)
        self.additions = list(additions)
        self.returncodes = list(returncodes or [0] * len(self.additions))
        self.calls = 0

    def run(self, argv, timeout=None):
        self.calls += 1
        self.events.append(("launch", self.calls, list(argv)))
        addition = self.additions.pop(0)
        with self.log_path.open("ab") as stream:
            stream.write(addition)
        return FakeResult(self.returncodes.pop(0))


class MiddleOverwriteRunner:
    """Represent Ostriv overwriting only an unsampled part of a large run log."""

    def __init__(self, events, log_path):
        self.events = events
        self.log_path = Path(log_path)
        self.calls = 0

    def run(self, argv, timeout=None):
        self.calls += 1
        self.events.append(("launch", self.calls, list(argv)))
        before = self.log_path.stat()
        with self.log_path.open("r+b") as stream:
            stream.seek(runtime.LOG_TOKEN_TAIL_BYTES * 2)
            stream.write(b"SteamAPI_Init() failed")
            stream.flush()
        os.utime(
            self.log_path,
            ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
        )
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
    def test_launcher_file_log_records_readiness_profile_and_game_boundaries(self):
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            Path(config.game_log).write_bytes(b"old log\n")
            events = []

            code = run_launcher(
                config,
                lock=FakeLock(events),
                runner=FakeGameRunner(events, config.game_log, [b"normal exit\n"]),
                steam=FakeSteam(events),
                profile=FakeProfile(events),
                dialog=lambda _message: self.fail("unexpected dialog"),
                install_handlers=lambda _profile: events.append("handlers"),
            )

            self.assertEqual(0, code)
            text = Path(config.launcher_log).read_text(encoding="utf-8")
            boundaries = (
                "launcher boundary=recovery status=start",
                "launcher boundary=recovery status=OK",
                "launcher boundary=steam_readiness status=start",
                "launcher boundary=steam_readiness status=OK",
                "launcher boundary=profile_switch status=start",
                "launcher boundary=profile_switch status=OK",
                "launcher boundary=game_launch status=start attempt=1",
                "launcher boundary=game_launch status=finished attempt=1 returncode=0",
                "launcher classification=other attempt=1",
                "launcher final state: other",
            )
            for item in boundaries:
                self.assertIn(item, text)
            positions = [text.index(item) for item in boundaries]
            self.assertEqual(sorted(positions), positions)

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
                    ("steam", None, False),
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
                    ("steam", None, False),
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
                [("steam", None, False), ("steam", None, True)],
                [event for event in events if isinstance(event, tuple) and event[0] == "steam"],
            )
            self.assertIn(("final", "other"), events)

    def test_graphics_is_terminal_and_unrelated_zero_result_does_not_retry(self):
        """Broad automatic retries would duplicate launches for failures Steam cannot fix."""
        for addition in (b"windows_createWindow FAILED\n", b"unrelated failure\n"):
            with self.subTest(addition=addition), TemporaryDirectory() as directory:
                config = make_config(directory)
                events = []
                runner = FakeGameRunner(events, config.game_log, [addition])

                arguments = dict(
                    lock=FakeLock(events),
                    log_factory=lambda _path: FakeLogger(events),
                    runner=runner,
                    steam=FakeSteam(events),
                    profile=FakeProfile(events),
                    dialog=lambda _message: self.fail("unexpected dialog"),
                    install_handlers=lambda _profile: None,
                )
                if b"windows_createWindow" in addition:
                    with self.assertRaises(LauncherRuntimeError):
                        run_launcher(config, **arguments)
                else:
                    self.assertEqual(0, run_launcher(config, **arguments))
                self.assertEqual(1, runner.calls)
                self.assertEqual(1, events.count("restore"))

    def test_clean_game_exit_ignores_crossover_wrapper_status_one(self):
        """CrossOver returns one after a normal playable session and clean game shutdown."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            events = []
            runner = FakeGameRunner(
                events,
                config.game_log,
                [b"uiMainMenu\ndone exiting.\n"],
                returncodes=[1],
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
            self.assertIn(("final", "clean_exit"), events)
            self.assertEqual(1, events.count("restore"))
            self.assertEqual("unlock", events[-1])

    def test_terminal_steam_graphics_and_nonzero_results_are_typed_and_clean_up(self):
        """A completed Wine command is not success when fresh evidence says launch failed."""
        cases = (
            (
                "second SteamAPI",
                [b"SteamAPI_Init() failed\n", b"SteamAPI_Init() failed\n"],
                [0, 0],
                2,
            ),
            ("graphics", [b"windows_createWindow FAILED\n"], [0], 1),
            ("nonzero", [b"normal log\n"], [9], 1),
            (
                "nonzero graphics",
                [b"windows_createWindow FAILED\n"],
                [9],
                1,
            ),
        )
        for label, additions, returncodes, attempts in cases:
            with self.subTest(label=label), TemporaryDirectory() as directory:
                config = make_config(directory)
                events = []
                runner = FakeGameRunner(
                    events, config.game_log, additions, returncodes=returncodes
                )

                with self.assertRaises(LauncherRuntimeError) as caught:
                    run_launcher(
                        config,
                        lock=FakeLock(events),
                        log_factory=lambda _path: FakeLogger(events),
                        runner=runner,
                        steam=FakeSteam(events),
                        profile=FakeProfile(events),
                        dialog=lambda _message: self.fail("main owns failure dialogs"),
                        install_handlers=lambda _profile: None,
                    )

                self.assertEqual("game_failed", caught.exception.message_key)
                self.assertLessEqual(len(caught.exception.detail), 256)
                self.assertEqual(attempts, runner.calls)
                self.assertEqual(1, events.count("restore"))
                self.assertEqual("unlock", events[-1])

    def test_large_middle_only_overwrite_is_typed_terminal_and_cleans_up(self):
        """Changed metadata without sampled evidence must never become launch success."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            Path(config.game_log).write_bytes(
                b"h" * runtime.LOG_TOKEN_TAIL_BYTES
                + b"m" * (runtime.LOG_TOKEN_TAIL_BYTES * 2)
                + b"t" * runtime.LOG_TOKEN_TAIL_BYTES
            )
            events = []
            runner = MiddleOverwriteRunner(events, config.game_log)

            with self.assertRaises(LauncherRuntimeError) as caught:
                run_launcher(
                    config,
                    lock=FakeLock(events),
                    log_factory=lambda _path: FakeLogger(events),
                    runner=runner,
                    steam=FakeSteam(events),
                    profile=FakeProfile(events),
                    dialog=lambda _message: self.fail("main owns failure dialogs"),
                    install_handlers=lambda _profile: None,
                )

            self.assertEqual("game_failed", caught.exception.message_key)
            self.assertLessEqual(len(caught.exception.detail), 256)
            self.assertEqual(1, runner.calls)
            self.assertEqual(1, events.count("restore"))
            self.assertEqual("unlock", events[-1])

    def test_first_steam_marker_is_classified_before_nonzero_result_and_retries_once(self):
        """Only attempt one's fresh Steam marker may override nonzero into one retry."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            events = []
            runner = FakeGameRunner(
                events,
                config.game_log,
                [b"SteamAPI_Init() failed\n", b"normal retry exit\n"],
                returncodes=[7, 0],
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

    def test_final_state_logging_base_exception_cannot_skip_unlock(self):
        """A logger BaseException after restoration must still release the process lock."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            events = []
            runner = FakeGameRunner(events, config.game_log, [b"normal exit\n"])

            class ExplodingLogger(FakeLogger):
                def info(self, message, *args):
                    if message == "launcher final state: %s":
                        events.append("final-log")
                        raise KeyboardInterrupt()

            with self.assertRaises(KeyboardInterrupt):
                run_launcher(
                    config,
                    lock=FakeLock(events),
                    log_factory=lambda _path: ExplodingLogger(events),
                    runner=runner,
                    steam=FakeSteam(events),
                    profile=FakeProfile(events),
                    dialog=lambda _message: self.fail("unexpected dialog"),
                    install_handlers=lambda _profile: None,
                )

            self.assertEqual(1, events.count("restore"))
            self.assertEqual("unlock", events[-1])


class LauncherMainTests(unittest.TestCase):
    def test_main_maps_ambiguous_large_log_generation_to_one_game_action(self):
        """A bounded-but-changed game log must fail silently except for one action."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            action = "Ostriv could not start. Quit and reopen CrossOver, then try again."
            config.messages["game_failed"] = action
            Path(config.game_log).write_bytes(
                b"h" * runtime.LOG_TOKEN_TAIL_BYTES
                + b"m" * (runtime.LOG_TOKEN_TAIL_BYTES * 2)
                + b"t" * runtime.LOG_TOKEN_TAIL_BYTES
            )
            config_path = Path(directory) / "launcher.json"
            write_config(config, config_path)
            events = []
            dialogs = []
            runner = MiddleOverwriteRunner(events, config.game_log)

            with patch.object(
                runtime, "ProcessLock", side_effect=lambda _path: FakeLock(events)
            ), patch.object(
                runtime, "ExternalProcessRunner", return_value=runner
            ), patch.object(
                runtime, "ColorSyncProfileBackend", return_value=object()
            ), patch.object(
                runtime,
                "ProfileGuard",
                side_effect=lambda *_args: FakeProfile(events),
            ), patch.object(
                runtime,
                "SteamController",
                side_effect=lambda **_kwargs: FakeSteam(events),
            ), patch.object(
                runtime, "install_signal_handlers", side_effect=lambda _profile: None
            ), patch.object(
                runtime, "_display_dialog", side_effect=dialogs.append
            ):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main([str(config_path)])

            self.assertEqual(1, code)
            self.assertEqual([action], dialogs)
            self.assertEqual("", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())
            self.assertEqual(1, runner.calls)
            self.assertEqual(1, events.count("restore"))
            self.assertEqual("unlock", events[-1])

    def test_expected_command_timeout_log_is_bounded_redacted_and_dialog_stays_exact(self):
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            config.game_command[:] = [
                "wine",
                "reg",
                "add",
                "/d",
                "private-value",
                "/f",
            ]
            config_path = Path(directory) / "launcher.json"
            write_config(config, config_path)
            events = []
            dialogs = []

            def time_out(argv, **kwargs):
                raise subprocess.TimeoutExpired(
                    list(argv),
                    kwargs.get("timeout"),
                    output=b"private-value output\xff\n" + b"x" * 3000,
                    stderr=b"launcher timeout\x8e\n",
                )

            with patch.object(
                runtime, "ProcessLock", side_effect=lambda _path: FakeLock(events)
            ), patch.object(
                runtime,
                "ProfileGuard",
                side_effect=lambda *_args: FakeProfile(events),
            ), patch.object(
                runtime, "ColorSyncProfileBackend", return_value=object()
            ), patch.object(
                runtime,
                "SteamController",
                side_effect=lambda **_kwargs: FakeSteam(events),
            ), patch.object(
                runtime, "install_signal_handlers", side_effect=lambda _profile: None
            ), patch.object(
                runtime, "_display_dialog", side_effect=dialogs.append
            ), patch("subprocess.run", side_effect=time_out):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main([str(config_path)])

            self.assertEqual(1, code)
            self.assertEqual(["Unable to start Ostriv."], dialogs)
            self.assertEqual("", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())
            text = Path(config.launcher_log).read_text(encoding="utf-8")
            self.assertIn("command failure status=timeout", text)
            self.assertIn("argv=[\"wine\", \"reg\", \"add\", \"/d\", \"<redacted>\", \"/f\"]", text)
            self.assertIn("output='<redacted> output�", text)
            self.assertIn("<truncated", text)
            self.assertNotIn("private-value", text)
            self.assertNotIn("x" * 2049, text)

    def assert_bootstrap_failure_is_generic(self, argv):
        dialogs = []
        with patch.object(runtime, "_display_dialog", side_effect=dialogs.append), patch.object(
            runtime,
            "_create_launcher_log",
            side_effect=lambda _path: self.fail("bootstrap failure created launcher log"),
        ), patch.object(
            runtime,
            "ExternalProcessRunner",
            side_effect=lambda: self.fail("bootstrap failure constructed process adapter"),
        ), patch.object(
            runtime,
            "ColorSyncProfileBackend",
            side_effect=lambda: self.fail("bootstrap failure constructed profile adapter"),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(argv)

        self.assertNotEqual(0, code)
        self.assertEqual(["Unable to start Ostriv."], dialogs)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_wrong_argument_count_shows_one_generic_fallback_dialog(self):
        """A wrapper invocation mistake must not fail silently or reveal diagnostics."""
        self.assert_bootstrap_failure_is_generic([])

    def test_missing_config_shows_one_generic_fallback_dialog(self):
        """A missing configuration cannot supply copy, so main must use the safe fallback."""
        with TemporaryDirectory() as directory:
            self.assert_bootstrap_failure_is_generic(
                [str(Path(directory) / "missing.json")]
            )

    def test_malformed_config_shows_one_generic_fallback_dialog(self):
        """Malformed JSON must not fail silently or expose its parser diagnostic."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.json"
            path.write_text("not json", encoding="utf-8")
            self.assert_bootstrap_failure_is_generic([str(path)])

    def test_invalid_schema_and_types_show_one_generic_fallback_dialog(self):
        """Invalid configuration data must use one generic message before adapters exist."""
        with TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.json"
            valid = make_config(directory).__dict__
            for payload in (
                {**valid, "schema": 2},
                {**valid, "launcher_log": 7},
            ):
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    self.assert_bootstrap_failure_is_generic([str(path)])

    def test_default_contention_dialog_does_not_construct_runtime_adapters(self):
        """The one contention dialog must not instantiate the normal process/runtime stack."""
        with TemporaryDirectory() as directory:
            config = make_config(directory)
            config.messages["already_running"] = "Ostriv is already starting or running."
            config_path = Path(directory) / "launcher.json"
            write_config(config, config_path)
            events = []
            dialogs = []

            def show_dialog(argv, **_kwargs):
                dialogs.append(list(argv)[-1])
                return FakeResult(0)

            with patch.object(
                runtime, "ProcessLock", side_effect=lambda _path: FakeLock(events, False)
            ), patch.object(
                runtime,
                "_create_launcher_log",
                side_effect=lambda _path: self.fail("contention created the launcher log"),
            ), patch.object(
                runtime,
                "ExternalProcessRunner",
                side_effect=lambda: self.fail("contention constructed process adapter"),
            ), patch.object(
                runtime,
                "ColorSyncProfileBackend",
                side_effect=lambda: self.fail("contention constructed profile adapter"),
            ), patch.object(
                runtime,
                "SteamController",
                side_effect=lambda **_kwargs: self.fail("contention constructed Steam"),
            ), patch("subprocess.run", side_effect=show_dialog):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main([str(config_path)])

            self.assertEqual(0, code)
            self.assertEqual(["lock"], events)
            self.assertEqual(["Ostriv is already starting or running."], dialogs)
            self.assertEqual("", stdout.getvalue())
            self.assertEqual("", stderr.getvalue())

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
