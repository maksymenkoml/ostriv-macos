import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from ostriv_macos.discovery import (
    Bottle,
    CrossOverInstall,
    configured_bottle_roots,
    discover_bottles,
    discover_games,
    find_crossover_apps,
    resolve_explicit_game,
)
from ostriv_macos.diagnostics import PatchError


def make_bottle(root: Path, name: str) -> Path:
    bottle = root / name
    (bottle / "drive_c").mkdir(parents=True)
    (bottle / "cxbottle.conf").write_text("[Bottle]\n", encoding="utf-8")
    (bottle / "system.reg").write_text("REGEDIT4\n", encoding="utf-8")
    return bottle


def make_crossover(root: Path, name: str = "CrossOver.app") -> Path:
    app = root / name
    wine = app / "Contents/SharedSupport/CrossOver/bin/wine"
    wine.parent.mkdir(parents=True, exist_ok=True)
    wine.write_text("#!/bin/sh\n", encoding="utf-8")
    wine.chmod(0o755)
    with (app / "Contents/Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleShortVersionString": "26.3"}, stream)
    return app


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def crossover(self):
        app = make_crossover(self.home / "Applications")
        return CrossOverInstall(
            app,
            app / "Contents/SharedSupport/CrossOver",
            "26.3",
        )

    def managed_root(self):
        return self.home / "No System Bottles"

    def test_bottle_path_supports_colon_separated_external_roots(self):
        first = self.home / "Bottles"
        second = self.home / "External Bottles"
        conf = self.home / "Library/Application Support/CrossOver/CrossOver.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text(
            "[CrossOver]\nBottlePath={}:{}\n".format(first, second),
            encoding="utf-8",
        )
        roots = configured_bottle_roots(self.home, {})
        self.assertIn(first.resolve(), roots)
        self.assertIn(second.resolve(), roots)

    def test_environment_roots_include_private_and_managed_paths(self):
        private = self.home / "External Bottles"
        managed = self.home / "Managed Bottles"
        roots = configured_bottle_roots(
            self.home,
            {
                "CX_BOTTLE_PATH": str(private),
                "CX_MANAGED_BOTTLE_PATH": str(managed),
            },
        )
        self.assertIn(private.resolve(), roots)
        self.assertIn(managed.resolve(), roots)

    def test_symlinked_bottle_keeps_name_and_resolves_real_root(self):
        default = self.home / "Library/Application Support/CrossOver/Bottles"
        external = make_bottle(self.home / "Volumes/Games", "Sniper 5")
        default.mkdir(parents=True)
        (default / "Sniper 5").symlink_to(external, target_is_directory=True)
        bottles = discover_bottles(self.crossover(), self.home, {}, self.managed_root())
        self.assertEqual("Sniper 5", bottles[0].name)
        self.assertEqual(external.resolve(), bottles[0].root)

    def test_helper_plist_adds_valid_absolute_private_bottle(self):
        external = make_bottle(self.home / "Volumes/Игри", "Ostriv Space")
        helper = self.home / "Applications/CrossOver/Helpers/Info.plist"
        helper.parent.mkdir(parents=True)
        with helper.open("wb") as stream:
            plistlib.dump(
                {
                    "CXHelperAppBottleName": str(external),
                    "CXHelperAppBottleTag": "private",
                    "CrossOverHelperCommand": "run",
                },
                stream,
            )
        bottles = discover_bottles(self.crossover(), self.home, {}, self.managed_root())
        self.assertEqual(1, len(bottles))
        self.assertEqual(external.resolve(), bottles[0].root)
        self.assertEqual("private", bottles[0].scope)

    def test_managed_bottle_uses_name_and_scope_argument(self):
        managed = self.home / "Managed Bottles"
        make_bottle(managed, "Managed Space")
        bottles = discover_bottles(
            self.crossover(),
            self.home,
            {"CX_MANAGED_BOTTLE_PATH": str(managed)},
            self.managed_root(),
        )
        bottle = next(item for item in bottles if item.name == "Managed Space")
        self.assertEqual("Managed Space", bottle.command_bottle())
        self.assertEqual(["--scope", "managed"], bottle.scope_args())

    def test_managed_bottle_wins_over_private_symlink_alias(self):
        private = self.home / "Private Bottles"
        managed = self.home / "Managed Bottles"
        actual = make_bottle(managed, "Managed Bottle")
        private.mkdir()
        (private / "Private Alias").symlink_to(actual, target_is_directory=True)
        bottles = discover_bottles(
            self.crossover(),
            self.home,
            {
                "CX_BOTTLE_PATH": str(private),
                "CX_MANAGED_BOTTLE_PATH": str(managed),
            },
            self.managed_root(),
        )
        self.assertEqual(1, len(bottles))
        self.assertEqual("Managed Bottle", bottles[0].name)
        self.assertEqual("managed", bottles[0].scope)
        self.assertEqual("Managed Bottle", bottles[0].command_bottle())
        self.assertEqual(["--scope", "managed"], bottles[0].scope_args())

    def test_private_bottle_uses_resolved_absolute_path_without_scope(self):
        root = make_bottle(self.home / "External Bottles", "Ostriv Україна")
        bottle = Bottle("Ostriv Україна", root.resolve(), "private", self.crossover())
        self.assertEqual(str(root.resolve()), bottle.command_bottle())
        self.assertEqual([], bottle.scope_args())

    def test_duplicate_real_bottles_are_returned_once_deterministically(self):
        root = self.home / "External Bottles"
        external = make_bottle(root, "Ostriv")
        linked = self.home / "Linked Bottles"
        linked.mkdir()
        (linked / "Ostriv").symlink_to(external, target_is_directory=True)
        bottles = discover_bottles(
            self.crossover(),
            self.home,
            {"CX_BOTTLE_PATH": os.pathsep.join((str(root), str(linked)))},
            self.managed_root(),
        )
        self.assertEqual([external.resolve()], [item.root for item in bottles])

    def test_bottle_missing_required_registry_file_is_ignored(self):
        root = self.home / "Broken Bottles"
        bottle = root / "Incomplete"
        (bottle / "drive_c").mkdir(parents=True)
        (bottle / "cxbottle.conf").write_text("[Bottle]\n", encoding="utf-8")
        bottles = discover_bottles(
            self.crossover(),
            self.home,
            {"CX_BOTTLE_PATH": str(root)},
            self.managed_root(),
        )
        self.assertEqual([], bottles)

    def test_find_crossover_apps_checks_user_and_system_and_avoids_runner_when_disabled(self):
        user_app = make_crossover(self.home / "Applications")
        system_root = self.home / "System Applications"
        system_app = make_crossover(system_root)

        class ForbiddenRunner:
            def run(self, *args, **kwargs):
                raise AssertionError("runner must not be called")

        apps = find_crossover_apps(
            home=self.home,
            env={},
            runner=ForbiddenRunner(),
            system_app=system_app,
            allow_subprocess=False,
        )
        self.assertEqual([user_app.resolve(), system_app.resolve()], [item.app for item in apps])
        self.assertEqual(["26.3", "26.3"], [item.version for item in apps])

    def test_find_crossover_apps_discovers_spotlight_path_with_spaces_and_deduplicates(self):
        fixture_root = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(fixture_root.cleanup)
        app = make_crossover(Path(fixture_root.name) / "Moved Apps", "CrossOver.app")

        class ListingRunner:
            def run(self, argv, timeout=None):
                class Result:
                    def __init__(self, stdout):
                        self.stdout = stdout

                if argv[0] == "mdfind":
                    return Result(str(app) + "\n")
                return Result("path: {}\n".format(app))

        apps = find_crossover_apps(
            home=self.home,
            env={},
            runner=ListingRunner(),
            system_app=self.home / "Missing/CrossOver.app",
        )
        self.assertEqual([app.resolve()], [item.app for item in apps])

    def test_find_crossover_apps_discovers_preview_app_from_spotlight(self):
        fixture_root = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(fixture_root.cleanup)
        app = make_crossover(
            Path(fixture_root.name) / "Applications", "CrossOver Preview.app"
        )

        class ListingRunner:
            def run(self, argv, timeout=None):
                class Result:
                    stdout = str(app) + "\n" if argv[0] == "mdfind" else ""

                return Result()

        apps = find_crossover_apps(
            home=self.home,
            env={},
            runner=ListingRunner(),
            system_app=self.home / "Missing/CrossOver.app",
        )

        self.assertEqual([app.resolve()], [item.app for item in apps])

    def test_find_crossover_apps_finds_preview_in_conventional_locations(self):
        """A Preview-only install must be found without touching Spotlight."""
        app = make_crossover(self.home / "Applications", "CrossOver Preview.app")

        apps = find_crossover_apps(
            home=self.home,
            env={},
            allow_subprocess=False,
            system_app=self.home / "Missing/CrossOver.app",
        )

        self.assertEqual([app.resolve()], [item.app for item in apps])

    def test_find_crossover_apps_finds_preview_by_name_query(self):
        """Preview must be reachable when only the -name query can see it."""
        fixture_root = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(fixture_root.cleanup)
        app = make_crossover(
            Path(fixture_root.name) / "Applications", "CrossOver Preview.app"
        )

        class ListingRunner:
            def run(self, argv, timeout=None):
                class Result:
                    stdout = ""

                if argv[:2] == ["mdfind", "-name"] and "Preview" in argv[2]:
                    Result.stdout = str(app) + "\n"
                return Result()

        apps = find_crossover_apps(
            home=self.home,
            env={},
            runner=ListingRunner(),
            system_app=self.home / "Missing/CrossOver.app",
        )

        self.assertEqual([app.resolve()], [item.app for item in apps])

    def test_find_crossover_apps_finds_preview_in_launch_services_dump(self):
        """Preview must survive the lsregister fallback when Spotlight is blind."""
        fixture_root = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(fixture_root.cleanup)
        app = make_crossover(
            Path(fixture_root.name) / "Registered", "CrossOver Preview.app"
        )

        class ListingRunner:
            def run(self, argv, timeout=None):
                class Result:
                    stdout = ""

                if Path(argv[0]).name == "lsregister":
                    Result.stdout = "path: {}\n".format(app)
                return Result()

        apps = find_crossover_apps(
            home=self.home,
            env={},
            runner=ListingRunner(),
            system_app=self.home / "Missing/CrossOver.app",
        )

        self.assertEqual([app.resolve()], [item.app for item in apps])

    def test_find_crossover_apps_skips_launch_services_dump_when_fast_paths_work(self):
        """A known CrossOver app must not trigger a multi-megabyte registry dump."""
        user_app = make_crossover(self.home / "Applications")

        class RecordingRunner:
            def __init__(self):
                self.calls = []

            def run(self, argv, timeout=None):
                self.calls.append((argv, timeout))

                class Result:
                    stdout = ""

                return Result()

        runner = RecordingRunner()
        apps = find_crossover_apps(
            home=self.home,
            env={},
            runner=runner,
            system_app=self.home / "Missing/CrossOver.app",
        )

        self.assertEqual([user_app.resolve()], [item.app for item in apps])
        self.assertEqual(
            ["mdfind", "mdfind", "mdfind"],
            [Path(call[0][0]).name for call in runner.calls],
        )

    def test_find_crossover_apps_keeps_fallback_when_spotlight_result_is_unusable(self):
        """A stale Spotlight hit must not hide a usable Launch Services bundle."""
        fixture_root = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(fixture_root.cleanup)
        root = Path(fixture_root.name)
        stale = root / "Stale/CrossOver.app"
        stale.mkdir(parents=True)
        usable = make_crossover(root / "Registered")

        class ListingRunner:
            def run(self, argv, timeout=None):
                class Result:
                    stdout = ""

                if Path(argv[0]).name == "mdfind":
                    Result.stdout = str(stale) + "\n"
                else:
                    Result.stdout = "path: {}\n".format(usable)
                return Result()

        apps = find_crossover_apps(
            home=self.home,
            env={},
            runner=ListingRunner(),
            system_app=self.home / "Missing/CrossOver.app",
        )

        self.assertEqual([usable.resolve()], [item.app for item in apps])

    def test_find_crossover_apps_ignores_runner_timeouts(self):
        class TimeoutRunner:
            def run(self, argv, timeout=None):
                raise subprocess.TimeoutExpired(argv, timeout)

        apps = find_crossover_apps(
            home=self.home,
            env={},
            runner=TimeoutRunner(),
            system_app=self.home / "Missing/CrossOver.app",
        )
        self.assertEqual([], apps)

    def test_find_crossover_apps_ignores_typed_command_timeouts(self):
        """Optional discovery fallbacks must not turn a timeout into install failure."""

        class TimeoutRunner:
            def run(self, argv, timeout=None):
                raise PatchError(
                    "command.timeout",
                    "CrossOver took too long to respond.",
                    "timeout={}".format(timeout),
                )

        apps = find_crossover_apps(
            home=self.home,
            env={},
            runner=TimeoutRunner(),
            system_app=self.home / "Missing/CrossOver.app",
        )
        self.assertEqual([], apps)

    def test_discover_games_finds_all_games_per_bottle_and_parses_version(self):
        bottle_root = make_bottle(self.home / "Bottles", "Game Bottle")
        game = bottle_root / "drive_c/Steam/steamapps/common/Ostriv"
        game.mkdir(parents=True)
        (game / "ostriv.exe").write_bytes(b"MZ")
        duplicate = bottle_root / "drive_c/Other/Ostriv"
        duplicate.mkdir(parents=True)
        (duplicate / "OSTRIV.EXE").write_bytes(b"MZ")
        log = bottle_root / "drive_c/users/crossover/Saved Games/Ostriv/log.txt"
        log.parent.mkdir(parents=True)
        log.write_text("Alpha (0.5.9.58 Jun 4 2026)\n", encoding="utf-8")
        bottle = Bottle("Game Bottle", bottle_root.resolve(), "private", self.crossover())
        games = discover_games([bottle])
        self.assertEqual([duplicate.resolve(), game.resolve()], [item.game_dir for item in games])
        self.assertEqual(["0.5.9.58", "0.5.9.58"], [item.version for item in games])

    def test_discover_games_returns_no_results_without_ostriv(self):
        root = make_bottle(self.home / "Bottles", "Empty")
        bottle = Bottle("Empty", root.resolve(), "private", self.crossover())
        self.assertEqual([], discover_games([bottle]))

    def test_discover_games_returns_an_installation_for_each_bottle(self):
        bottles = []
        for name in ("First Game", "Друга Гра"):
            root = make_bottle(self.home / "Bottles", name)
            game = root / "drive_c/Games/Ostriv"
            game.mkdir(parents=True)
            (game / "ostriv.exe").write_bytes(b"MZ")
            bottles.append(Bottle(name, root.resolve(), "private", self.crossover()))
        games = discover_games(bottles)
        self.assertEqual(["First Game", "Друга Гра"], [game.bottle.name for game in games])

    def test_explicit_external_game_walks_up_to_bottle_root(self):
        bottle = make_bottle(self.home / "Volumes/T7/GAMES/Bottles", "Sniper 5")
        game = bottle / "drive_c/Steam/steamapps/common/Ostriv"
        game.mkdir(parents=True)
        (game / "ostriv.exe").write_bytes(b"MZ")
        result = resolve_explicit_game(game, [self.crossover()])
        self.assertEqual(bottle.resolve(), result.bottle.root)
        self.assertEqual("Sniper 5", result.bottle.name)
        self.assertEqual("private", result.bottle.scope)

    def test_explicit_managed_game_preserves_discovered_bottle_identity(self):
        """An explicit path inside inventory must keep managed scope, name, and owner."""
        first_app = make_crossover(self.home / "First", "CrossOver.app")
        second_app = make_crossover(self.home / "Second", "CrossOver.app")
        first = CrossOverInstall(
            first_app, first_app / "Contents/SharedSupport/CrossOver", "26.3"
        )
        second = CrossOverInstall(
            second_app, second_app / "Contents/SharedSupport/CrossOver", "26.3"
        )
        root = make_bottle(self.home / "Managed", "Shared Name")
        game = root / "drive_c/Steam/steamapps/common/Ostriv"
        game.mkdir(parents=True)
        (game / "ostriv.exe").write_bytes(b"MZ")
        managed = Bottle("Managed Ostriv", root.resolve(), "managed", second)

        result = resolve_explicit_game(game, [first, second], [managed])

        self.assertEqual(managed, result.bottle)
        self.assertEqual("Managed Ostriv", result.bottle.command_bottle())
        self.assertEqual(["--scope", "managed"], result.bottle.scope_args())
        self.assertEqual(second.app, result.bottle.crossover.app)

    def test_explicit_inventory_selects_the_crossover_that_discovered_the_root(self):
        """The first CrossOver in app order must not steal another app's bottle."""
        first_app = make_crossover(self.home / "First Owner", "CrossOver.app")
        second_app = make_crossover(self.home / "Actual Owner", "CrossOver.app")
        first = CrossOverInstall(
            first_app, first_app / "Contents/SharedSupport/CrossOver", "26.3"
        )
        second = CrossOverInstall(
            second_app, second_app / "Contents/SharedSupport/CrossOver", "26.3"
        )
        root = make_bottle(self.home / "Private Roots", "Actual Bottle")
        game = root / "drive_c/Games/Ostriv"
        game.mkdir(parents=True)
        (game / "ostriv.exe").write_bytes(b"MZ")
        discovered = Bottle("Actual Bottle", root.resolve(), "private", second)

        result = resolve_explicit_game(game, [first, second], [discovered])

        self.assertEqual(discovered, result.bottle)

    def test_explicit_game_outside_bottle_raises_patch_error(self):
        game = self.home / "Downloads/Ostriv"
        game.mkdir(parents=True)
        (game / "ostriv.exe").write_bytes(b"MZ")
        with self.assertRaises(PatchError) as raised:
            resolve_explicit_game(game, [self.crossover()])
        self.assertEqual("discovery.explicit_not_in_bottle", raised.exception.code)

    def test_explicit_writable_directory_inside_bottle_is_not_an_ostriv_installation(self):
        bottle = make_bottle(self.home / "Volumes/T7/GAMES/Bottles", "External")
        arbitrary = bottle / "drive_c/users/crossover/Documents"
        arbitrary.mkdir(parents=True)
        with self.assertRaises(PatchError) as raised:
            resolve_explicit_game(arbitrary, [self.crossover()])
        self.assertEqual("discovery.explicit_not_ostriv", raised.exception.code)

    def test_explicit_ostriv_named_file_outside_drive_c_is_rejected(self):
        bottle = make_bottle(self.home / "Volumes/T7/GAMES/Bottles", "External")
        arbitrary = bottle / "metadata"
        arbitrary.mkdir()
        (arbitrary / "ostriv.exe").write_bytes(b"MZ")
        with self.assertRaises(PatchError) as raised:
            resolve_explicit_game(arbitrary, [self.crossover()])
        self.assertEqual("discovery.explicit_not_ostriv", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
