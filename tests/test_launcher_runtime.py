import unittest
from dataclasses import dataclass
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import plistlib
import re
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


class SteamControllerTests(unittest.TestCase):
    def test_probe_records_each_readiness_signal_in_the_launcher_file_log(self):
        with TemporaryDirectory() as directory:
            log_path = Path(directory) / "launcher.log"
            logger = runtime._create_launcher_log(log_path)
            controller = SteamController(
                config=make_config(directory),
                runner=ProbeRunner(process=True, renderer=False),
            )
            controller.logger = logger

            signals = controller.probe()

            self.assertEqual(SteamSignals(True, True, False), signals)
            self.assertIn(
                "steam probe process=True active_user=True renderer=False ready=False",
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

    def test_process_and_renderer_probes_include_selected_canonical_bottle_scope(self):
        """Global Steam processes cannot satisfy a different selected bottle."""
        config = make_config("/private/tmp/selected")
        object.__setattr__(config, "bottle_realpath", "/Bottles/selected-id")
        calls = []

        class ScopedRunner:
            def run(self, argv, timeout=None):
                argv = list(argv)
                calls.append(argv)
                if argv[:2] == ["pgrep", "-f"]:
                    matched_selected = re.escape(config.bottle_realpath) in argv[-1]
                    return FakeResult(1 if matched_selected else 0)
                return FakeResult(0, b"ActiveUser    REG_DWORD    0x1\n")

        signals = SteamController(config=config, runner=ScopedRunner()).probe()

        self.assertEqual(SteamSignals(False, True, False), signals)
        pgrep_patterns = [argv[-1] for argv in calls if argv[:2] == ["pgrep", "-f"]]
        self.assertEqual(2, len(pgrep_patterns))
        self.assertTrue(
            all(re.escape(config.bottle_realpath) in pattern for pattern in pgrep_patterns)
        )


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
            lock = ProcessLock(Path(directory) / "launcher.lock")
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
                return_value=type("Status", (), {"st_mode": 0o100600, "st_nlink": 1})(),
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
