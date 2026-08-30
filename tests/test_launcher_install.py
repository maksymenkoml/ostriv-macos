import base64
import bz2
import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ostriv_macos.launcher as launcher_module
from ostriv_macos.diagnostics import CommandResult, PatchError
from ostriv_macos.discovery import Bottle, CrossOverInstall, GameInstallation
from ostriv_macos.installer import InstallJournal, Installer, Transaction, UndoRecord
from ostriv_macos.launcher import LauncherInstaller
from ostriv_macos.launcher import _extract_menu_helper


def digest(data):
    return hashlib.sha256(data).hexdigest()


def newc_archive(entries):
    archive = bytearray()
    inode = 1
    for name, mode, data, links in [
        *entries,
        ("TRAILER!!!", 0, b"", 1),
    ]:
        encoded_name = name.encode("utf-8") + b"\0"
        values = (
            inode,
            mode,
            0,
            0,
            links,
            0,
            len(data),
            0,
            0,
            0,
            0,
            len(encoded_name),
            0,
        )
        archive.extend(b"070701" + b"".join("{:08x}".format(value).encode("ascii") for value in values))
        archive.extend(encoded_name)
        archive.extend(b"\0" * ((-len(archive)) % 4))
        archive.extend(data)
        archive.extend(b"\0" * ((-len(archive)) % 4))
        inode += 1
    return bz2.compress(bytes(archive))


def odc_archive(entries):
    archive = bytearray()
    inode = 1
    for name, mode, data, links in [
        *entries,
        ("TRAILER!!!", 0, b"", 1),
    ]:
        encoded_name = name.encode("utf-8") + b"\0"
        header = (
            "070707"
            "{dev:06o}{ino:06o}{mode:06o}{uid:06o}{gid:06o}{links:06o}"
            "{rdev:06o}{mtime:011o}{namesize:06o}{size:011o}"
        ).format(
            dev=0,
            ino=inode,
            mode=mode,
            uid=0,
            gid=0,
            links=links,
            rdev=0,
            mtime=0,
            namesize=len(encoded_name),
            size=len(data),
        )
        archive.extend(header.encode("ascii"))
        archive.extend(encoded_name)
        archive.extend(data)
        inode += 1
    return bz2.compress(bytes(archive))


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.environments = []
        self.cxmenu_returncode = 0
        self.cxmenu_purge_returncode = 0
        self.cxmenu_failures = 0
        self.processes = ""
        self.defaults_returncode = 0

    def run(self, argv, timeout=None, environment=None):
        argv = list(argv)
        self.calls.append((argv, timeout))
        self.environments.append(environment)
        if Path(argv[0]).name == "cxmenu":
            if "--create" in argv and self.cxmenu_failures:
                self.cxmenu_failures -= 1
                return CommandResult(7, "", "cxmenu failed once")
            returncode = (
                self.cxmenu_purge_returncode
                if "--purge" in argv
                else self.cxmenu_returncode
            )
            return CommandResult(returncode, "", "cxmenu failed")
        if Path(argv[0]).name == "ps":
            return CommandResult(0, self.processes, "")
        if Path(argv[0]).name == "defaults":
            return CommandResult(self.defaults_returncode, "", "defaults failed")
        raise AssertionError(argv)


class FakeExtractor:
    def __init__(self):
        self.fail = False

    def __call__(self, template, destination):
        if self.fail:
            raise OSError("fixture extraction failure")
        # CrossOver's real Menu Helper archive records a private root mode.
        destination.chmod(0o700)
        executable = destination / "Contents/MacOS/Menu Helper"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"menu-helper-executable")
        executable.chmod(0o755)
        resources = destination / "Contents/Resources"
        resources.mkdir(parents=True)
        with (destination / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump(
                {
                    "CFBundleExecutable": "Menu Helper",
                    "CFBundleIconFile": "CrossOverHelper.icns",
                },
                stream,
            )


class LauncherFixture:
    def __init__(self, scope="private"):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.release = self.root / "Release Source"
        self.release.mkdir()
        source_runtime = Path(__file__).parents[1] / "ostriv_macos/launcher_runtime.py"
        self.runtime_source = self.release / "launcher_runtime.py"
        shutil.copyfile(source_runtime, self.runtime_source)

        self.crossover_app = self.root / "CrossOver 26.app"
        self.shared_support = self.crossover_app / "Contents/SharedSupport/CrossOver"
        self.bin_dir = self.shared_support / "bin"
        self.bin_dir.mkdir(parents=True)
        for name in ("wine", "cxmenu"):
            tool = self.bin_dir / name
            tool.write_bytes(b"#!/bin/sh\n")
            tool.chmod(0o755)
        resources = self.crossover_app / "Contents/Resources"
        resources.mkdir(parents=True)
        self.template = resources / "Menu Helper.cpbz2"
        self.template.write_bytes(b"fixture archive")
        (resources / "exeIcon.icns").write_bytes(b"menu-helper-default-icon")

        self.bottle_root = self.root / "External Bottles/Bottle With Spaces"
        self.game_dir = self.bottle_root / "drive_c/Program Files/Ostriv"
        self.game_dir.mkdir(parents=True)
        (self.game_dir / "ostriv.exe").write_bytes(b"game")
        (self.bottle_root / "cxbottle.conf").write_text(
            '"BottleID" = "fixture-bottle-id"\n', encoding="utf-8"
        )
        crossover = CrossOverInstall(self.crossover_app, self.shared_support, "26.2")
        self.bottle = Bottle("Bottle With Spaces", self.bottle_root, scope, crossover)
        self.installation = GameInstallation(self.bottle, self.game_dir, "0.5.9.58")

        self.destination = self.root / "Applications With Spaces/CrossOver"
        self.destination.mkdir(parents=True)
        self.game_launcher = self.destination / "Games/Ostriv.app"
        icon = self.game_launcher / "Contents/Resources/CrossOverHelper.icns"
        icon.parent.mkdir(parents=True)
        icon.write_bytes(b"ostriv-icon")
        with (self.game_launcher / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump(
                {
                    "CXHelperAppBottleName": self.bottle.name,
                    "CrossOverHelperCommand": '"C:/Program Files/Ostriv/Ostriv.lnk"',
                },
                stream,
            )

        self.runner = FakeRunner()
        self.extractor = FakeExtractor()
        self.installer = LauncherInstaller(
            package_root=self.release,
            launcher_destination=self.destination,
            runner=self.runner,
            runtime_source=self.runtime_source,
            extractor=self.extractor,
        )
        self.journal = InstallJournal(self.bottle_root / "launcher-test-journal.json")
        self.transaction = Transaction(
            self.journal, {"restore_launcher": self._restore_snapshots}
        )
        self.transaction.start("launcher-test")

    def cleanup(self):
        self.temp.cleanup()

    @property
    def app(self):
        return self.destination / "Ostriv (patched).app"

    @property
    def runtime(self):
        return self.bottle_root / "play-ostriv-patched.py"

    @property
    def config(self):
        return self.bottle_root / "launcher-config.json"

    def create_legacy_launcher(self):
        executable = self.app / "Contents/MacOS/Menu Helper"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"legacy executable")
        executable.chmod(0o755)
        with (self.app / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump(
                {
                    "CFBundleName": "Ostriv (patched)",
                    "CFBundleDisplayName": "Ostriv (patched)",
                    "CFBundleIdentifier": "com.codeweavers.CrossOverHelper.{}.{}".format(
                        hashlib.md5(self.bottle.name.encode("utf-8")).hexdigest().upper(),
                        hashlib.md5(b"Ostriv (patched)").hexdigest().upper(),
                    ),
                    "CrossOverHelperCommand": "exec /usr/bin/env python3 {}".format(
                        self.runtime
                    ),
                    "CXHelperAppBottleName": self.bottle.name,
                    "CXHelperAppBottleTag": "CrossOver-fixture-bottle-id/",
                },
                stream,
            )
        self.runtime.write_text(
            '#!/usr/bin/env python3\n"""Generated by ostriv-macos patch.py"""\n',
            encoding="utf-8",
        )

    def create_absolute_bottle_catalog(self):
        catalog = self.bottle_root / "desktopdata/cxmenu/cxmenu_macosx.plist"
        catalog.parent.mkdir(parents=True, exist_ok=True)
        with catalog.open("wb") as stream:
            plistlib.dump(
                {
                    "CrossOver-fixture-bottle-id/": {
                        "Children": {},
                        "Description": str(self.bottle_root.resolve()),
                    }
                },
                stream,
            )

    def apply_crossover_generated_launcher_refresh(self):
        self.app.chmod(0o755)
        plist_path = self.app / "Contents/Info.plist"
        properties = plistlib.loads(plist_path.read_bytes())
        properties.pop("CFBundleDisplayName")
        properties.update(
            {
                "CFBundleDocumentTypes": [
                    {
                        "CFBundleTypeRole": "Viewer",
                        "LSItemContentTypes": [
                            "com.codeweavers.CrossOverHelper.MenuDummyType"
                        ],
                    }
                ],
                "CFBundleIconFile": "CrossOverHelper",
                "CrossOverHelperMenuPath": "StartMenu/Ostriv (patched)",
                "CXHelperAppVersion": 43,
                "CXOriginalMenuName": "Ostriv (patched)",
                "UTImportedTypeDeclarations": [
                    {
                        "UTTypeIdentifier": (
                            "com.codeweavers.CrossOverHelper.MenuDummyType"
                        )
                    }
                ],
            }
        )
        plist_path.write_bytes(plistlib.dumps(properties))
        (self.app / "Contents/Resources/CrossOverHelper.icns").write_bytes(
            b"menu-helper-default-icon"
        )

    def downgrade_to_legacy_safe_area_state(self, state):
        """Recreate the exact launcher/state shape written before notch support."""
        legacy = dict(state)
        plist_path = self.app / "Contents/Info.plist"
        properties = plistlib.loads(plist_path.read_bytes())
        properties.pop("NSPrefersDisplaySafeAreaCompatibilityMode", None)
        plist_path.write_bytes(plistlib.dumps(properties))
        plist_sha256 = digest(plist_path.read_bytes())
        legacy["plist_fields"] = [
            "CFBundleName",
            "CFBundleDisplayName",
            "CFBundleIdentifier",
            "CrossOverHelperCommand",
            "CXHelperAppBottleName",
            "CXHelperAppBottleTag",
        ]
        legacy["plist_sha256"] = plist_sha256
        legacy["app_inventory"] = launcher_module._inventory(self.app)
        legacy["artifacts"] = [dict(item) for item in legacy["artifacts"]]
        legacy["artifacts"][1]["sha256"] = plist_sha256
        return legacy

    @staticmethod
    def _restore_snapshots(record):
        for snapshot in record.data.get("snapshots", []):
            path = Path(snapshot["path"])
            if snapshot.get("present"):
                data = base64.b64decode(snapshot["content"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(snapshot.get("mode", 0o644))
            elif path.is_file() and snapshot.get("remove_sha256"):
                if digest(path.read_bytes()) == snapshot["remove_sha256"]:
                    path.unlink()


class LauncherInstallerTests(unittest.TestCase):
    def test_app_symlink_is_rejected_without_touching_victim_or_starting_journal(self):
        """Resolving the final app leaf lets install replace files outside the launcher root."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        victim = fixture.root / "victim app"
        victim.mkdir()
        marker = victim / "unknown.txt"
        marker.write_text("keep", encoding="utf-8")
        fixture.app.symlink_to(victim, target_is_directory=True)
        journal = fixture.bottle_root / "preflight-must-not-start.json"

        with self.assertRaises(PatchError) as caught:
            fixture.installer.preflight(fixture.installation)

        self.assertEqual("install.launcher_ownership", caught.exception.code)
        self.assertEqual("keep", marker.read_text(encoding="utf-8"))
        self.assertTrue(fixture.app.is_symlink())
        self.assertFalse(journal.exists())
        self.assertFalse(fixture.app.with_name(fixture.app.name + ".pending").exists())

    def test_all_reserved_launcher_leaves_reject_symlinks(self):
        """Pending and backup aliases are mutation targets just as dangerous as the app leaf."""
        suffixes = (
            "Ostriv (patched).app.pending",
            ".Ostriv (patched).app.ostriv-macos.previous",
            ".Ostriv (patched).app.ostriv-macos.replaced",
        )
        for suffix in suffixes:
            with self.subTest(path=suffix):
                fixture = LauncherFixture()
                self.addCleanup(fixture.cleanup)
                victim = fixture.root / "external victim"
                victim.mkdir()
                marker = victim / "marker"
                marker.write_text("keep", encoding="utf-8")
                reserved = fixture.destination / suffix
                reserved.symlink_to(victim, target_is_directory=True)

                with self.assertRaises(PatchError) as caught:
                    fixture.installer.preflight(fixture.installation)

                self.assertEqual("install.launcher_ownership", caught.exception.code)
                self.assertTrue(reserved.is_symlink())
                self.assertEqual("keep", marker.read_text(encoding="utf-8"))

    def test_runtime_and_config_symlinks_survive_late_menu_failure_lexically(self):
        """Rollback must not follow or replace runtime/config symlink leaves."""
        for leaf_name in ("runtime", "config"):
            with self.subTest(leaf=leaf_name):
                fixture = LauncherFixture()
                self.addCleanup(fixture.cleanup)
                leaf = getattr(fixture, leaf_name)
                victim = fixture.root / (leaf_name + "-outside")
                victim.write_bytes((leaf_name + "-victim").encode("ascii"))
                before = victim.read_bytes()
                leaf.symlink_to(victim)
                fixture.runner.cxmenu_returncode = 7

                with self.assertRaises(PatchError) as caught:
                    fixture.installer.install(
                        fixture.transaction, fixture.installation
                    )

                self.assertEqual("install.launcher_ownership", caught.exception.code)
                self.assertTrue(leaf.is_symlink())
                self.assertEqual(victim.resolve(), leaf.resolve())
                self.assertEqual(before, victim.read_bytes())
                self.assertFalse(
                    leaf.with_name("." + leaf.name + ".pending").exists()
                )
                self.assertEqual([], fixture.runner.calls)

    def test_runtime_and_config_pending_symlink_leaves_are_reserved(self):
        """Dangling pending aliases must be rejected with lstat before staging."""
        for destination_name in ("runtime", "config"):
            with self.subTest(leaf=destination_name):
                fixture = LauncherFixture()
                self.addCleanup(fixture.cleanup)
                destination = getattr(fixture, destination_name)
                pending = destination.with_name("." + destination.name + ".pending")
                pending.symlink_to(fixture.root / "missing-external-target")

                with self.assertRaises(PatchError) as caught:
                    fixture.installer.preflight(fixture.installation)

                self.assertEqual("install.launcher_ownership", caught.exception.code)
                self.assertTrue(pending.is_symlink())

    def test_preexisting_unowned_recovery_leaves_are_rejected_unchanged(self):
        """Install must not adopt or delete an unrecorded marker or lock."""
        for name in (".ostriv-launcher.lock", ".ostriv-profile-recovery.json"):
            with self.subTest(name=name):
                fixture = LauncherFixture()
                self.addCleanup(fixture.cleanup)
                path = fixture.bottle_root / name
                path.write_bytes(b"unowned recovery data")

                with self.assertRaises(PatchError) as caught:
                    fixture.installer.preflight(fixture.installation)

                self.assertEqual("install.launcher_ownership", caught.exception.code)
                self.assertEqual(b"unowned recovery data", path.read_bytes())
                self.assertEqual([], fixture.runner.calls)

    def test_production_transaction_recovers_owned_pending_tree_after_restart(self):
        """A killed extraction must be recoverable before launcher.install can bind handlers."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        production = Installer(
            fixture.release,
            fixture.installer,
            launcher_destination=fixture.destination,
        )
        transaction = production.transaction_for(fixture.installation)
        transaction.start("install")
        app = fixture.installer._app_path()
        pending = app.with_name(app.name + ".pending")
        transaction.journal.begin(
            "extract launcher template",
            UndoRecord(
                "restore_launcher",
                {
                    "snapshots": [],
                    "owned_root": str(pending),
                    "owned_directories": [str(pending)],
                    "remove_owned_tree": True,
                },
            ),
        )
        pending.mkdir()
        (pending / "partially-extracted").write_text("owned", encoding="utf-8")

        production = Installer(
            fixture.release,
            fixture.installer,
            launcher_destination=fixture.destination,
        )
        production.transaction_for(fixture.installation).recover_incomplete()

        self.assertFalse(pending.exists())

    def test_production_restart_recovers_app_backup_after_completed_renames(self):
        """A crash after app swaps must restore the recorded prior bundle in a new process."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        legacy_empty = fixture.app / "Contents/Resources/restart-empty"
        legacy_empty.mkdir(parents=True)
        legacy_link = fixture.app / "Contents/Resources/restart-link"
        legacy_link.symlink_to("missing-target")
        app = fixture.installer._app_path()
        pending = app.with_name(app.name + ".pending")
        backup = app.with_name("." + app.name + ".ostriv-macos.previous")
        pending.mkdir()
        (pending / "replacement").write_text("new", encoding="utf-8")
        old_inventory = launcher_module._inventory(app)
        replacement_inventory = launcher_module._inventory(pending)
        production = Installer(
            fixture.release,
            fixture.installer,
            launcher_destination=fixture.destination,
        )
        transaction = production.transaction_for(fixture.installation)
        transaction.start("install")
        transaction.journal.begin(
            "extract launcher template",
            UndoRecord(
                "restore_launcher",
                {
                    "snapshots": [],
                    "owned_root": str(pending),
                    "owned_directories": [str(pending)],
                    "remove_owned_tree": True,
                },
            ),
        )
        transaction.journal.begin(
            "replace launcher app",
            UndoRecord(
                "restore_launcher",
                {
                    "snapshots": [],
                    "owned_root": str(backup),
                    "owned_directories": [str(backup)],
                    "moved_tree": {
                        "source": str(backup),
                        "destination": str(app),
                        "source_inventory": old_inventory,
                        "replacement_inventory": replacement_inventory,
                    },
                },
            ),
        )
        os.replace(app, backup)
        os.replace(pending, app)

        production.transaction_for(fixture.installation).recover_incomplete()

        self.assertTrue((app / "Contents/Resources/restart-empty").is_dir())
        self.assertTrue((app / "Contents/Resources/restart-link").is_symlink())
        self.assertFalse(backup.exists())
        self.assertFalse(pending.exists())

    def test_recovery_rejects_corrupt_owned_directory_claim(self):
        """Journal corruption must not turn an empty arbitrary directory into owned data."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        arbitrary = fixture.root / "user empty directory"
        arbitrary.mkdir()
        production = Installer(
            fixture.release,
            fixture.installer,
            launcher_destination=fixture.destination,
        )
        transaction = production.transaction_for(fixture.installation)
        transaction.start("install")
        transaction.journal.begin(
            "corrupt launcher record",
            UndoRecord(
                "restore_launcher",
                {
                    "snapshots": [],
                    "owned_root": str(arbitrary),
                    "owned_directories": [str(arbitrary)],
                },
            ),
        )

        with self.assertRaises(PatchError) as caught:
            production.transaction_for(fixture.installation).recover_incomplete()

        self.assertEqual("install.rollback_failed", caught.exception.code)
        self.assertTrue(arbitrary.is_dir())
        self.assertFalse(
            production.transaction_for(fixture.installation).journal.data["complete"]
        )

    def test_recovery_validates_entire_captured_tree_before_touching_current_app(self):
        """Corrupt rich-tree entries must not escape root or delete the installed tree first."""
        for label in (
            "traversal",
            "absolute",
            "duplicate",
            "symlink-parent",
            "file-parent",
            "symlink-metadata",
            "unknown-type",
            "nul-path",
            "surrogate-path",
            "nul-symlink-target",
            "surrogate-symlink-target",
        ):
            with self.subTest(case=label):
                fixture = LauncherFixture()
                self.addCleanup(fixture.cleanup)
                state = fixture.installer.install(
                    fixture.transaction, fixture.installation
                )
                fixture.transaction.journal.commit()
                captured = launcher_module._captured_tree(
                    fixture.installer._app_path()
                )
                victim = fixture.installer._app_path().parent / "outside-victim"
                victim.write_text("keep", encoding="utf-8")
                additions = {
                    "traversal": [
                        {"relative_path": "../outside-victim", "type": "file"}
                    ],
                    "absolute": [
                        {"relative_path": str(victim), "type": "file"}
                    ],
                    "symlink-parent": [
                        {
                            "relative_path": "redirect",
                            "type": "symlink",
                            "target": "../outside-victim",
                        },
                        {"relative_path": "redirect/child", "type": "file"},
                    ],
                    "file-parent": [
                        {"relative_path": "collision", "type": "file"},
                        {"relative_path": "collision/child", "type": "file"},
                    ],
                    "symlink-metadata": [
                        {"relative_path": "missing-target", "type": "symlink"}
                    ],
                    "unknown-type": [
                        {"relative_path": "device", "type": "device"}
                    ],
                    "nul-path": [
                        {"relative_path": "bad\0path", "type": "file"}
                    ],
                    "surrogate-path": [
                        {"relative_path": "bad\udcffpath", "type": "file"}
                    ],
                    "nul-symlink-target": [
                        {
                            "relative_path": "unsafe-target",
                            "type": "symlink",
                            "target": "bad\0target",
                        }
                    ],
                    "surrogate-symlink-target": [
                        {
                            "relative_path": "unsafe-target",
                            "type": "symlink",
                            "target": "bad\udcfftarget",
                        }
                    ],
                }.get(label, [])
                if label == "duplicate":
                    captured["entries"].append(dict(captured["entries"][0]))
                else:
                    for addition in additions:
                        item = dict(addition)
                        if item["type"] == "file":
                            item.update(
                                {
                                    "content": base64.b64encode(b"corrupt").decode(
                                        "ascii"
                                    ),
                                    "sha256": digest(b"corrupt"),
                                    "mode": 0o644,
                                }
                            )
                        captured["entries"].append(item)
                before = {
                    path.relative_to(fixture.app).as_posix(): path.read_bytes()
                    for path in fixture.app.rglob("*")
                    if path.is_file()
                }
                production = Installer(
                    fixture.release,
                    fixture.installer,
                    launcher_destination=fixture.destination,
                )
                transaction = production.transaction_for(fixture.installation)
                transaction.start("restore")
                corrupt_journal = dict(transaction.journal.data)
                corrupt_journal["records"] = [
                    {
                        "name": "corrupt restore tree",
                        "status": "pending",
                        "undo": {
                            "kind": "restore_launcher",
                            "data": {
                                "snapshots": [],
                                "restore_trees": [captured],
                            },
                        },
                    }
                ]
                transaction.journal.path.write_text(
                    json.dumps(corrupt_journal, ensure_ascii=True),
                    encoding="utf-8",
                )

                with self.assertRaises(PatchError) as caught:
                    production.transaction_for(
                        fixture.installation
                    ).recover_incomplete()

                self.assertEqual("install.rollback_failed", caught.exception.code)
                self.assertEqual("keep", victim.read_text(encoding="utf-8"))
                self.assertEqual(
                    before,
                    {
                        path.relative_to(fixture.app).as_posix(): path.read_bytes()
                        for path in fixture.app.rglob("*")
                        if path.is_file()
                    },
                )
                self.assertFalse(
                    production.transaction_for(fixture.installation).journal.data[
                        "complete"
                    ]
                )

    def test_recovery_purge_failure_is_typed_and_keeps_journal(self):
        """Silently accepting cxmenu failure makes interrupted registration unrecoverable."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        production = Installer(
            fixture.release,
            fixture.installer,
            launcher_destination=fixture.destination,
        )
        transaction = production.transaction_for(fixture.installation)
        transaction.start("install")
        transaction.journal.begin(
            "register launcher menu",
            UndoRecord("restore_launcher", {"snapshots": [], "purge_menu": True}),
        )
        fixture.runner.cxmenu_purge_returncode = 7

        with self.assertRaises(PatchError) as caught:
            production.transaction_for(fixture.installation).recover_incomplete()

        self.assertEqual("install.rollback_failed", caught.exception.code)
        self.assertIn("cxmenu purge failed", caught.exception.detail)
        self.assertFalse(
            production.transaction_for(fixture.installation).journal.data["complete"]
        )

    def test_menu_helper_extractor_rejects_traversal_before_writing(self):
        """An archive must not escape the owned pending tree or partially populate it."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        destination = fixture.root / "pending"
        destination.mkdir()
        outside = fixture.root / "escaped"
        fixture.template.write_bytes(
            newc_archive(
                [
                    ("Contents/Info.plist", 0o100644, b"safe", 1),
                    ("../escaped", 0o100644, b"victim", 1),
                ]
            )
        )

        with self.assertRaises(OSError):
            _extract_menu_helper(fixture.template, destination)

        self.assertFalse(outside.exists())
        self.assertEqual([], list(destination.iterdir()))

    def test_menu_helper_extractor_rejects_links_and_special_files(self):
        """Links and device-like records can redirect or broaden extraction ownership."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        unsafe_entries = (
            ("absolute", "/tmp/ostriv-extractor-victim", 0o100644, b"x", 1),
            ("symlink", "Contents/link", 0o120777, b"../../victim", 1),
            ("hardlink", "Contents/file", 0o100644, b"x", 2),
            ("device", "Contents/device", 0o020600, b"", 1),
        )
        for label, name, mode, data, links in unsafe_entries:
            with self.subTest(label=label):
                destination = fixture.root / ("pending-" + label)
                destination.mkdir()
                fixture.template.write_bytes(newc_archive([(name, mode, data, links)]))

                with self.assertRaises(OSError):
                    _extract_menu_helper(fixture.template, destination)

                self.assertEqual([], list(destination.iterdir()))

    def test_menu_helper_extractor_materializes_only_validated_regular_tree(self):
        """The controlled extractor must retain the template's bytes and executable mode."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        destination = fixture.root / "pending-valid"
        destination.mkdir()
        fixture.template.write_bytes(
            newc_archive(
                [
                    ("Contents", 0o040755, b"", 2),
                    ("Contents/MacOS", 0o040755, b"", 2),
                    ("Contents/MacOS/Menu Helper", 0o100755, b"helper-bytes", 1),
                ]
            )
        )

        _extract_menu_helper(fixture.template, destination)

        executable = destination / "Contents/MacOS/Menu Helper"
        self.assertEqual(b"helper-bytes", executable.read_bytes())
        self.assertEqual(0o755, executable.stat().st_mode & 0o777)

    def test_menu_helper_extractor_supports_portable_ascii_cpio(self):
        """CrossOver archives may use the portable old ASCII cpio representation."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        destination = fixture.root / "pending-odc"
        destination.mkdir()
        fixture.template.write_bytes(
            odc_archive(
                [
                    ("Contents", 0o040755, b"", 2),
                    ("Contents/Menu Helper", 0o100755, b"portable-helper", 1),
                ]
            )
        )

        _extract_menu_helper(fixture.template, destination)

        self.assertEqual(
            b"portable-helper", (destination / "Contents/Menu Helper").read_bytes()
        )

    def test_menu_helper_extractor_accepts_archive_root_directory(self):
        """CrossOver 26.3 prefixes its valid portable archive with a dot root."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        destination = fixture.root / "pending-odc-root"
        destination.mkdir()
        fixture.template.write_bytes(
            odc_archive(
                [
                    (".", 0o040755, b"", 2),
                    ("./Contents", 0o040755, b"", 2),
                    ("./Contents/MacOS", 0o040755, b"", 2),
                    (
                        "./Contents/MacOS/Menu Helper",
                        0o100755,
                        b"crossover-26.3-helper",
                        1,
                    ),
                ]
            )
        )

        _extract_menu_helper(fixture.template, destination)

        executable = destination / "Contents/MacOS/Menu Helper"
        self.assertEqual(b"crossover-26.3-helper", executable.read_bytes())
        self.assertEqual(0o755, executable.stat().st_mode & 0o777)

    def test_app_renames_are_each_followed_by_destination_directory_fsync(self):
        """A durable journal cannot recover a rename the filesystem never persisted."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        app = fixture.installer._app_path()
        pending = app.with_name(app.name + ".pending")
        backup = app.with_name("." + app.name + ".ostriv-macos.previous")
        events = []
        original_replace = os.replace
        original_sync = launcher_module._sync_directory

        def replace(source, destination):
            source_path = Path(source)
            destination_path = Path(destination)
            if source_path in (app, pending):
                events.append(("replace", source_path, destination_path))
            return original_replace(source, destination)

        def sync(directory):
            if Path(directory) == app.parent:
                events.append(("sync", Path(directory)))
            return original_sync(directory)

        with patch.object(launcher_module.os, "replace", side_effect=replace), patch.object(
            launcher_module, "_sync_directory", side_effect=sync
        ):
            fixture.installer.install(fixture.transaction, fixture.installation)

        self.assertEqual(
            [
                ("replace", app, backup),
                ("sync", app.parent),
                ("replace", pending, app),
                ("sync", app.parent),
            ],
            events,
        )

    def test_failure_after_each_app_rename_fsync_restores_previous_bundle(self):
        """Either durability boundary may fail after its rename has already mutated disk."""
        for fail_at in (1, 2):
            with self.subTest(fsync=fail_at):
                fixture = LauncherFixture()
                self.addCleanup(fixture.cleanup)
                fixture.create_legacy_launcher()
                old_executable = (fixture.app / "Contents/MacOS/Menu Helper").read_bytes()
                app_parent = fixture.installer._app_path().parent
                original_sync = launcher_module._sync_directory
                app_syncs = [0]

                def fail_selected(directory):
                    if Path(directory) == app_parent:
                        app_syncs[0] += 1
                        if app_syncs[0] == fail_at:
                            raise OSError("injected app-directory fsync failure")
                    return original_sync(directory)

                with patch.object(
                    launcher_module, "_sync_directory", side_effect=fail_selected
                ):
                    with self.assertRaises(PatchError) as caught:
                        fixture.installer.install(
                            fixture.transaction, fixture.installation
                        )

                self.assertEqual("install.launcher_materialize", caught.exception.code)
                self.assertEqual(
                    old_executable,
                    (fixture.app / "Contents/MacOS/Menu Helper").read_bytes(),
                )
                self.assertFalse(
                    fixture.app.with_name(
                        "." + fixture.app.name + ".ostriv-macos.previous"
                    ).exists()
                )

    def test_private_external_bottle_uses_distinct_wine_and_cxmenu_identities(self):
        """Giving cxmenu an absolute bottle poisons CrossOver's launcher catalog identity."""
        fixture = LauncherFixture(scope="private")
        self.addCleanup(fixture.cleanup)

        state = fixture.installer.install(fixture.transaction, fixture.installation)

        config = json.loads(fixture.config.read_text(encoding="utf-8"))
        self.assertEqual(str(fixture.bottle_root.resolve()), config["bottle_argument"])
        self.assertEqual("private", config["scope"])
        self.assertEqual(
            [
                str(fixture.bin_dir / "wine"),
                "--bottle",
                str(fixture.bottle_root.resolve()),
                "--check",
                "--wait-children",
                "--start",
                "C:/Program Files/Ostriv/ostriv.exe",
            ],
            config["game_command"],
        )
        plist = plistlib.loads((fixture.app / "Contents/Info.plist").read_bytes())
        expected_command = (
            "exec /usr/bin/env python3 "
            "'{}' '{}'".format(fixture.runtime.resolve(), fixture.config.resolve())
        )
        self.assertEqual(expected_command, plist["CrossOverHelperCommand"])
        cxmenu = fixture.runner.calls[-1][0]
        self.assertEqual(fixture.bottle.name, cxmenu[cxmenu.index("--bottle") + 1])
        self.assertEqual(
            ["--scope", "private"],
            cxmenu[cxmenu.index("--scope") : cxmenu.index("--scope") + 2],
        )
        self.assertEqual(
            {"CX_BOTTLE_PATH": str(fixture.bottle_root.resolve().parent)},
            fixture.runner.environments[-1],
        )
        self.assertIn("--icon", cxmenu)
        self.assertEqual(
            str(
                (
                    fixture.game_launcher
                    / "Contents/Resources/CrossOverHelper.icns"
                ).resolve()
            ),
            cxmenu[cxmenu.index("--icon") + 1],
        )
        self.assertEqual(expected_command, state["command"])

    def test_affected_catalog_requires_crossover_to_be_closed_before_repair(self):
        """Repairing preferences while CrossOver is open lets it restore stale cache data."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_absolute_bottle_catalog()
        fixture.runner.processes = str(
            fixture.crossover_app.resolve() / "Contents/MacOS/CrossOver"
        )

        with self.assertRaises(PatchError) as caught:
            fixture.installer.install(fixture.transaction, fixture.installation)

        self.assertEqual("install.crossover_running", caught.exception.code)
        self.assertEqual([], fixture.transaction.journal.data["records"])

    def test_affected_catalog_resets_only_its_cached_bottle_before_registration(self):
        """Keeping the stale cached tree prevents CrossOver from recreating missing apps."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_absolute_bottle_catalog()

        fixture.installer.install(fixture.transaction, fixture.installation)

        commands = [call for call, _timeout in fixture.runner.calls]
        defaults_calls = [
            command for command in commands if Path(command[0]).name == "defaults"
        ]
        self.assertEqual(
            [
                [
                    "/usr/bin/defaults",
                    "write",
                    "com.codeweavers.CrossOver",
                    "MostRecentCXFBMenuPlist",
                    "-dict-add",
                    "CrossOver-fixture-bottle-id/",
                    "{}",
                ]
            ],
            defaults_calls,
        )
        defaults_index = commands.index(defaults_calls[0])
        create_index = next(
            index for index, command in enumerate(commands) if "--create" in command
        )
        self.assertLess(defaults_index, create_index)

    def test_catalog_cache_reset_failure_stops_before_menu_registration(self):
        """Ignoring defaults failure reports success while CrossOver keeps stale launchers."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_absolute_bottle_catalog()
        fixture.runner.defaults_returncode = 7

        with self.assertRaises(PatchError) as caught:
            fixture.installer.install(fixture.transaction, fixture.installation)

        self.assertEqual("install.launcher_cache", caught.exception.code)
        self.assertFalse(
            any("--create" in command for command, _timeout in fixture.runner.calls)
        )
        self.assertFalse(fixture.app.exists())

    def test_managed_bottle_keeps_name_and_managed_scope(self):
        """Dropping managed scope can bind Wine and cxmenu to a same-named private bottle."""
        fixture = LauncherFixture(scope="managed")
        self.addCleanup(fixture.cleanup)

        fixture.installer.install(fixture.transaction, fixture.installation)

        config = json.loads(fixture.config.read_text(encoding="utf-8"))
        self.assertEqual(fixture.bottle.name, config["bottle_argument"])
        self.assertEqual("managed", config["scope"])
        self.assertEqual(
            ["--bottle", fixture.bottle.name, "--scope", "managed"],
            fixture.runner.calls[-1][0][1:5],
        )
        self.assertEqual(
            ["--scope", "managed"],
            config["game_command"][3:5],
        )

    def test_missing_menu_helper_is_typed_and_mutates_nothing(self):
        """Starting without the CrossOver template would leave a partial launcher install."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.template.unlink()

        with self.assertRaises(PatchError) as caught:
            fixture.installer.install(fixture.transaction, fixture.installation)

        self.assertEqual("install.launcher_template", caught.exception.code)
        self.assertFalse(fixture.runtime.exists())
        self.assertFalse(fixture.config.exists())
        self.assertFalse(fixture.app.exists())
        self.assertEqual([], fixture.runner.calls)

    def test_cxmenu_failure_is_typed_and_rolls_back_legacy_launcher(self):
        """Ignoring cxmenu failure reports success with no registered CrossOver menu entry."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        empty = fixture.app / "Contents/Resources/rollback-empty"
        empty.mkdir(parents=True)
        link = fixture.app / "Contents/Resources/rollback-link"
        link.symlink_to("missing-target")
        old_app = {
            path.relative_to(fixture.app).as_posix(): path.read_bytes()
            for path in fixture.app.rglob("*")
            if path.is_file()
        }
        old_runtime = fixture.runtime.read_bytes()
        fixture.runner.cxmenu_returncode = 7

        with self.assertRaises(PatchError) as caught:
            fixture.installer.install(fixture.transaction, fixture.installation)

        self.assertEqual("install.launcher_menu", caught.exception.code)
        self.assertEqual(old_runtime, fixture.runtime.read_bytes())
        self.assertEqual(
            old_app,
            {
                path.relative_to(fixture.app).as_posix(): path.read_bytes()
                for path in fixture.app.rglob("*")
                if path.is_file()
            },
        )
        self.assertTrue(empty.is_dir())
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path("missing-target"), link.readlink())

    def test_task5_undo_handler_removes_only_empty_owned_staging_directories(self):
        """Leaving empty pending/backup bundles makes a recovered install permanently conflict."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        fixture.runner.cxmenu_returncode = 7
        core = Installer(
            fixture.release,
            fixture.installer,
            launcher_destination=fixture.destination,
        )
        transaction = core.transaction_for(fixture.installation)
        transaction.start("launcher-core-undo-test")

        with self.assertRaises(PatchError):
            fixture.installer.install(transaction, fixture.installation)

        self.assertFalse(fixture.app.with_name(fixture.app.name + ".pending").exists())
        self.assertFalse(
            fixture.app.with_name("." + fixture.app.name + ".ostriv-macos.previous").exists()
        )

    def test_materializes_verified_plist_runtime_config_and_icon(self):
        """A launcher with drifted identity, runtime, config, or icon is deleted or fails later."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)

        state = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.installer.verify(fixture.installation, state)

        self.assertEqual(fixture.runtime_source.read_bytes(), fixture.runtime.read_bytes())
        self.assertEqual(b"ostriv-icon", (fixture.app / "Contents/Resources/CrossOverHelper.icns").read_bytes())
        info = plistlib.loads((fixture.app / "Contents/Info.plist").read_bytes())
        bundle_id = "com.codeweavers.CrossOverHelper.{}.{}".format(
            hashlib.md5(fixture.bottle.name.encode("utf-8")).hexdigest().upper(),
            hashlib.md5("Ostriv (patched)".encode("utf-8")).hexdigest().upper(),
        )
        self.assertEqual(
            {
                "CFBundleName": "Ostriv (patched)",
                "CFBundleDisplayName": "Ostriv (patched)",
                "CFBundleIdentifier": bundle_id,
                "CrossOverHelperCommand": state["command"],
                "CXHelperAppBottleName": fixture.bottle.name,
                "CXHelperAppBottleTag": "CrossOver-fixture-bottle-id/",
                "NSPrefersDisplaySafeAreaCompatibilityMode": True,
            },
            {key: info[key] for key in state["plist_fields"]},
        )
        config = json.loads(fixture.config.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "schema",
                "bottle_name",
                "bottle_argument",
                "scope",
                "bottle_realpath",
                "bottle_tag",
                "profile_owner_token",
                "wine",
                "game_command",
                "steam_apps_root",
                "steam_links",
                "game_log",
                "launcher_log",
                "lock_path",
                "recovery_marker",
                "messages",
            },
            set(config),
        )
        self.assertEqual(digest(fixture.runtime.read_bytes()), state["runtime_sha256"])
        self.assertEqual(digest(fixture.config.read_bytes()), state["config_sha256"])

    def test_verify_rejects_plist_tampering(self):
        """Checking only that an app exists misses a foreign or corrupted helper bundle."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = fixture.installer.install(fixture.transaction, fixture.installation)
        plist_path = fixture.app / "Contents/Info.plist"
        info = plistlib.loads(plist_path.read_bytes())
        info["CXHelperAppBottleName"] = "Another Bottle"
        plist_path.write_bytes(plistlib.dumps(info))

        with self.assertRaises(PatchError) as caught:
            fixture.installer.verify(fixture.installation, state)

        self.assertEqual("install.launcher_verify", caught.exception.code)
        self.assertIn("CXHelperAppBottleName", caught.exception.detail)

    def test_verify_rejects_missing_disabled_or_malformed_safe_area_preference(self):
        """A current launcher must not silently return to notch-obscured fullscreen."""
        missing = object()
        for value in (missing, False, 1, "true"):
            with self.subTest(value=value):
                fixture = LauncherFixture()
                self.addCleanup(fixture.cleanup)
                state = fixture.installer.install(
                    fixture.transaction, fixture.installation
                )
                plist_path = fixture.app / "Contents/Info.plist"
                info = plistlib.loads(plist_path.read_bytes())
                if value is missing:
                    info.pop("NSPrefersDisplaySafeAreaCompatibilityMode")
                else:
                    info["NSPrefersDisplaySafeAreaCompatibilityMode"] = value
                plist_path.write_bytes(plistlib.dumps(info))

                with self.assertRaises(PatchError) as caught:
                    fixture.installer.verify(fixture.installation, state)

                self.assertEqual("install.launcher_verify", caught.exception.code)
                self.assertIn(
                    "NSPrefersDisplaySafeAreaCompatibilityMode",
                    caught.exception.detail,
                )

    def test_verify_recomputes_identity_when_state_and_plist_are_tampered_together(self):
        """Persisted state cannot authenticate the plist that the same state describes."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = dict(fixture.installer.install(fixture.transaction, fixture.installation))
        plist_path = fixture.app / "Contents/Info.plist"
        info = plistlib.loads(plist_path.read_bytes())
        info["CrossOverHelperCommand"] = "exec /tmp/foreign-launcher"
        plist_path.write_bytes(plistlib.dumps(info))
        state["command"] = info["CrossOverHelperCommand"]
        state["plist_sha256"] = digest(plist_path.read_bytes())

        with self.assertRaises(PatchError) as caught:
            fixture.installer.verify(fixture.installation, state)

        self.assertEqual("install.launcher_verify", caught.exception.code)
        self.assertIn("CrossOverHelperCommand", caught.exception.detail)

    def test_verify_rejects_paired_safe_area_inventory_downgrade(self):
        """Current state cannot relabel a notch-unsafe plist as a legacy launcher."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        current = fixture.installer.install(
            fixture.transaction, fixture.installation
        )
        downgraded = fixture.downgrade_to_legacy_safe_area_state(current)

        with self.assertRaises(PatchError) as caught:
            fixture.installer.verify(fixture.installation, downgraded)

        self.assertEqual("install.launcher_verify", caught.exception.code)
        self.assertIn("plist field inventory", caught.exception.detail)

    def test_reinstall_rejects_paired_unlisted_plist_tampering_before_mutation(self):
        """A rewritten state digest cannot authorize launch-critical template fields."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = dict(fixture.installer.install(fixture.transaction, fixture.installation))
        fixture.transaction.journal.commit()
        plist_path = fixture.app / "Contents/Info.plist"
        properties = plistlib.loads(plist_path.read_bytes())
        properties["CFBundleExecutable"] = "Foreign Helper"
        properties["CFBundleIconFile"] = "Foreign.icns"
        plist_path.write_bytes(plistlib.dumps(properties))
        state["plist_sha256"] = digest(plist_path.read_bytes())
        (fixture.bottle_root / "ostriv-macos-state.json").write_text(
            json.dumps({"launcher_artifacts": state}), encoding="utf-8"
        )
        before = {
            path.relative_to(fixture.app).as_posix(): path.read_bytes()
            for path in fixture.app.rglob("*")
            if path.is_file()
        }
        journal = InstallJournal(fixture.bottle_root / "paired-plist-reinstall.json")
        transaction = Transaction(
            journal, {"restore_launcher": fixture._restore_snapshots}
        )
        transaction.start("reinstall")
        calls_before = list(fixture.runner.calls)

        with self.assertRaises(PatchError) as caught:
            fixture.installer.install(transaction, fixture.installation)

        self.assertEqual("install.launcher_verify", caught.exception.code)
        self.assertIn("CFBundleExecutable", caught.exception.detail)
        self.assertEqual([], transaction.journal.data["records"])
        self.assertEqual(calls_before, fixture.runner.calls)
        self.assertEqual(
            before,
            {
                path.relative_to(fixture.app).as_posix(): path.read_bytes()
                for path in fixture.app.rglob("*")
                if path.is_file()
            },
        )

    def test_reinstall_accepts_exact_crossover_generated_launcher_refresh(self):
        """CrossOver's post-restart helper normalization must not break Reinstall."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        first = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.transaction.journal.commit()
        state_path = fixture.bottle_root / "ostriv-macos-state.json"
        state_path.write_text(
            json.dumps({"launcher_artifacts": dict(first)}), encoding="utf-8"
        )
        fixture.apply_crossover_generated_launcher_refresh()
        refreshed = plistlib.loads(
            (fixture.app / "Contents/Info.plist").read_bytes()
        )
        self.assertIs(
            refreshed["NSPrefersDisplaySafeAreaCompatibilityMode"], True
        )
        second_journal = InstallJournal(fixture.bottle_root / "refresh-reinstall.json")
        second_transaction = Transaction(
            second_journal, {"restore_launcher": fixture._restore_snapshots}
        )
        second_transaction.start("reinstall")

        try:
            second = fixture.installer.install(
                second_transaction, fixture.installation
            )
        except PatchError as error:
            self.fail("trusted CrossOver refresh was rejected: {}".format(error.detail))

        fixture.installer.verify(fixture.installation, second)
        self.assertFalse(
            fixture.app.with_name(
                "." + fixture.app.name + ".ostriv-macos.replaced"
            ).exists()
        )

    def test_reinstall_upgrades_launcher_without_safe_area_preference(self):
        """Pre-v0.1.4 ownership must remain valid long enough to upgrade."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        first = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.transaction.journal.commit()
        legacy = fixture.downgrade_to_legacy_safe_area_state(first)
        (fixture.bottle_root / "ostriv-macos-state.json").write_text(
            json.dumps({"launcher_artifacts": legacy}), encoding="utf-8"
        )
        journal = InstallJournal(fixture.bottle_root / "safe-area-upgrade.json")
        transaction = Transaction(
            journal, {"restore_launcher": fixture._restore_snapshots}
        )
        transaction.start("reinstall")

        upgraded = fixture.installer.install(transaction, fixture.installation)

        info = plistlib.loads((fixture.app / "Contents/Info.plist").read_bytes())
        self.assertIs(info["NSPrefersDisplaySafeAreaCompatibilityMode"], True)
        self.assertIn(
            "NSPrefersDisplaySafeAreaCompatibilityMode",
            upgraded["plist_fields"],
        )

    def test_reinstall_upgrades_refreshed_launcher_without_safe_area_preference(self):
        """CrossOver's rewrite must not strand a valid pre-v0.1.4 app."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        first = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.transaction.journal.commit()
        legacy = fixture.downgrade_to_legacy_safe_area_state(first)
        (fixture.bottle_root / "ostriv-macos-state.json").write_text(
            json.dumps({"launcher_artifacts": legacy}), encoding="utf-8"
        )
        fixture.apply_crossover_generated_launcher_refresh()
        journal = InstallJournal(
            fixture.bottle_root / "refreshed-safe-area-upgrade.json"
        )
        transaction = Transaction(
            journal, {"restore_launcher": fixture._restore_snapshots}
        )
        transaction.start("reinstall")

        upgraded = fixture.installer.install(transaction, fixture.installation)

        info = plistlib.loads((fixture.app / "Contents/Info.plist").read_bytes())
        self.assertIs(info["NSPrefersDisplaySafeAreaCompatibilityMode"], True)
        fixture.installer.verify(fixture.installation, upgraded)

    def test_restore_removes_exact_crossover_generated_launcher_refresh(self):
        """Restore must remove the trusted post-restart helper, not leave a dead app."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.transaction.journal.commit()
        fixture.apply_crossover_generated_launcher_refresh()

        fixture.installer.restore(fixture.installation, state)

        self.assertFalse(fixture.app.exists())

    def test_restore_removes_refreshed_legacy_safe_area_launcher(self):
        """A refreshed pre-v0.1.4 app remains owned and fully removable."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.transaction.journal.commit()
        legacy = fixture.downgrade_to_legacy_safe_area_state(state)
        fixture.apply_crossover_generated_launcher_refresh()

        fixture.installer.restore(fixture.installation, legacy)

        self.assertFalse(fixture.app.exists())

    def test_restore_preserves_foreign_changes_to_generated_launcher(self):
        """CrossOver's plist rewrite must not broaden Restore ownership."""
        for change in (
            "icon",
            "executable",
            "executable matching newer template",
            "root mode",
        ):
            with self.subTest(change=change):
                fixture = LauncherFixture()
                self.addCleanup(fixture.cleanup)
                state = fixture.installer.install(
                    fixture.transaction, fixture.installation
                )
                fixture.transaction.journal.commit()
                fixture.apply_crossover_generated_launcher_refresh()
                if change == "icon":
                    changed_path = (
                        fixture.app / "Contents/Resources/CrossOverHelper.icns"
                    )
                    changed_path.write_bytes(b"foreign-icon")
                elif change in ("executable", "executable matching newer template"):
                    changed_path = fixture.app / "Contents/MacOS/Menu Helper"
                    changed_path.write_bytes(b"foreign-executable")
                    if change == "executable matching newer template":
                        original_extractor = fixture.installer.extractor

                        def newer_extractor(template, destination):
                            original_extractor(template, destination)
                            (
                                destination / "Contents/MacOS/Menu Helper"
                            ).write_bytes(b"foreign-executable")

                        fixture.installer.extractor = newer_extractor
                else:
                    changed_path = fixture.app
                    changed_path.chmod(0o777)

                fixture.installer.restore(fixture.installation, state)

                self.assertTrue(changed_path.exists())
                if change == "icon":
                    self.assertEqual(b"foreign-icon", changed_path.read_bytes())
                elif change in ("executable", "executable matching newer template"):
                    self.assertEqual(b"foreign-executable", changed_path.read_bytes())
                else:
                    self.assertEqual(0o777, stat.S_IMODE(changed_path.stat().st_mode))

    def test_incomplete_reinstall_recovers_crossover_generated_backup(self):
        """A durable rollback must recognize CrossOver's trusted helper refresh."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.transaction.journal.commit()
        (fixture.bottle_root / "ostriv-macos-state.json").write_text(
            json.dumps({"launcher_artifacts": dict(state)}), encoding="utf-8"
        )
        fixture.apply_crossover_generated_launcher_refresh()
        app = fixture.installer._app_path()
        generated_plist = (app / "Contents/Info.plist").read_bytes()
        backup = app.with_name(
            "." + app.name + ".ostriv-macos.replaced"
        )
        os.replace(app, backup)
        shutil.copytree(backup, app)
        replacement_inventory = launcher_module._inventory(app)
        record = UndoRecord(
            "restore_launcher",
            {
                "snapshots": [],
                "owned_root": str(backup),
                "owned_directories": [str(backup)],
                "remove_owned_tree": False,
                "moved_tree": {
                    "source": str(backup),
                    "destination": str(app),
                    "source_inventory": list(state["app_inventory"]),
                    "replacement_inventory": replacement_inventory,
                },
            },
        )
        handler = fixture.installer.undo_handler(
            fixture.installation, fixture._restore_snapshots
        )

        handler(record)

        self.assertFalse(backup.exists())
        self.assertEqual(generated_plist, (app / "Contents/Info.plist").read_bytes())

    def test_incomplete_reinstall_rejects_foreign_generated_backup_changes(self):
        """Recovery trusts recorded content, never current CrossOver template content."""
        for change in ("icon", "executable matching newer template"):
            with self.subTest(change=change):
                fixture = LauncherFixture()
                self.addCleanup(fixture.cleanup)
                state = fixture.installer.install(
                    fixture.transaction, fixture.installation
                )
                fixture.transaction.journal.commit()
                (fixture.bottle_root / "ostriv-macos-state.json").write_text(
                    json.dumps({"launcher_artifacts": dict(state)}), encoding="utf-8"
                )
                fixture.apply_crossover_generated_launcher_refresh()
                app = fixture.installer._app_path()
                backup = app.with_name("." + app.name + ".ostriv-macos.replaced")
                os.replace(app, backup)
                shutil.copytree(backup, app)
                if change == "icon":
                    foreign_path = (
                        backup / "Contents/Resources/CrossOverHelper.icns"
                    )
                    foreign_content = b"foreign-icon"
                else:
                    foreign_path = backup / "Contents/MacOS/Menu Helper"
                    foreign_content = b"foreign-executable"
                    original_extractor = fixture.installer.extractor

                    def newer_extractor(template, destination):
                        original_extractor(template, destination)
                        (destination / "Contents/MacOS/Menu Helper").write_bytes(
                            foreign_content
                        )

                    fixture.installer.extractor = newer_extractor
                foreign_path.write_bytes(foreign_content)
                record = UndoRecord(
                    "restore_launcher",
                    {
                        "snapshots": [],
                        "owned_root": str(backup),
                        "owned_directories": [str(backup)],
                        "remove_owned_tree": False,
                        "moved_tree": {
                            "source": str(backup),
                            "destination": str(app),
                            "source_inventory": list(state["app_inventory"]),
                            "replacement_inventory": launcher_module._inventory(app),
                        },
                    },
                )
                handler = fixture.installer.undo_handler(
                    fixture.installation, fixture._restore_snapshots
                )

                with self.assertRaises(PatchError) as caught:
                    handler(record)

                self.assertEqual(
                    "restore.launcher_ownership", caught.exception.code
                )
                self.assertEqual(foreign_content, foreign_path.read_bytes())
                self.assertTrue(app.exists())

    def test_crossover_generated_refresh_rejects_unknown_icon(self):
        """A trusted plist rewrite must not authorize unrelated bundle content."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.apply_crossover_generated_launcher_refresh()
        (fixture.app / "Contents/Resources/CrossOverHelper.icns").write_bytes(
            b"foreign-icon"
        )

        with self.assertRaises(PatchError) as caught:
            fixture.installer.verify(fixture.installation, state)

        self.assertEqual("install.launcher_verify", caught.exception.code)
        self.assertEqual("Launcher icon digest does not match", caught.exception.detail)

    def test_crossover_generated_refresh_rejects_unknown_root_mode(self):
        """CrossOver's known rewrite allows only its observed bundle root modes."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.apply_crossover_generated_launcher_refresh()
        fixture.app.chmod(0o777)

        with self.assertRaises(PatchError) as caught:
            fixture.installer.verify(fixture.installation, state)

        self.assertEqual("install.launcher_verify", caught.exception.code)
        self.assertEqual(
            "Launcher bundle inventory does not match", caught.exception.detail
        )

    def test_new_launcher_verifies_before_legacy_is_removed(self):
        """A failed pending app must not destroy the working legacy launcher."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        fixture.extractor.fail = True

        with self.assertRaises(PatchError):
            fixture.installer.install(fixture.transaction, fixture.installation)

        self.assertTrue(fixture.app.exists())
        self.assertEqual(b"legacy executable", (fixture.app / "Contents/MacOS/Menu Helper").read_bytes())
        self.assertIn(b"Generated by ostriv-macos patch.py", fixture.runtime.read_bytes())

    def test_success_replaces_legacy_only_after_pending_verification_and_restore_recovers_it(self):
        """Deleting the old app instead of recording it makes Restore destructive."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()

        state = fixture.installer.install(fixture.transaction, fixture.installation)

        self.assertEqual(b"menu-helper-executable", (fixture.app / "Contents/MacOS/Menu Helper").read_bytes())
        self.assertEqual(fixture.runtime_source.read_bytes(), fixture.runtime.read_bytes())
        self.assertTrue(Path(state["previous_app"]).exists())
        fixture.installer.restore(fixture.installation, state)
        self.assertEqual(b"legacy executable", (fixture.app / "Contents/MacOS/Menu Helper").read_bytes())
        self.assertIn(b"Generated by ostriv-macos patch.py", fixture.runtime.read_bytes())
        purge = fixture.runner.calls[-1][0]
        self.assertEqual(["--purge", "--filter", "StartMenu/Ostriv (patched)"], purge[-3:])

    def test_restore_preserves_previous_empty_directories_and_dangling_symlinks(self):
        """A file-only previous-app inventory cannot reproduce the bundle's exact types."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        empty = fixture.app / "Contents/Resources/empty-owned-directory"
        empty.mkdir(parents=True)
        empty.chmod(0o711)
        link = fixture.app / "Contents/Resources/dangling-owned-link"
        link.symlink_to("missing-target")

        state = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.installer.restore(fixture.installation, state)

        self.assertTrue(empty.is_dir())
        self.assertEqual(0o711, empty.stat().st_mode & 0o777)
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path("missing-target"), link.readlink())
        self.assertFalse(Path(state["previous_app"]).exists())

    def test_restore_recreates_restrictive_previous_directory_modes_deepest_first(self):
        """Recorded 0555 parents cannot be applied before their children are restored."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        nested = fixture.app / "Contents/Resources/restrictive/nested"
        nested.mkdir(parents=True)
        marker = nested / "marker.txt"
        marker.write_text("legacy", encoding="utf-8")
        restrictive = (
            (nested, 0o555),
            (nested.parent, 0o500),
            (fixture.app / "Contents", 0o555),
            (fixture.app, 0o555),
        )
        for path, mode in restrictive:
            path.chmod(mode)

        state = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.installer.restore(fixture.installation, state)

        self.assertEqual("legacy", marker.read_text(encoding="utf-8"))
        for path, mode in restrictive:
            self.assertEqual(mode, stat.S_IMODE(path.stat().st_mode), path)

    def test_launcher_transaction_rollback_recreates_restrictive_tree_exactly(self):
        """Rollback materializes children before restoring restrictive directory modes."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.transaction.journal.commit()
        app = fixture.installer._app_path()
        nested = app / "Contents/Resources"
        restrictive = ((nested, 0o500), (app, 0o555))
        for path, mode in restrictive:
            path.chmod(mode)
        before = launcher_module._captured_tree(app)
        journal = InstallJournal(fixture.bottle_root / "restrictive-rollback.json")
        handler = fixture.installer.undo_handler(
            fixture.installation, fixture._restore_snapshots
        )
        transaction = Transaction(journal, {"restore_launcher": handler})
        transaction.start("restrictive-rollback")

        def remove_then_fail():
            for path in sorted(
                [candidate for candidate in app.rglob("*") if candidate.is_dir()],
                key=lambda candidate: len(candidate.parts),
                reverse=True,
            ):
                path.chmod(0o700)
            app.chmod(0o700)
            shutil.rmtree(app)
            raise OSError("injected failure after launcher removal")

        with self.assertRaisesRegex(OSError, "injected failure"):
            transaction.step(
                "remove restrictive launcher",
                UndoRecord("restore_launcher", {"restore_trees": [before]}),
                remove_then_fail,
            )

        self.assertEqual(before, launcher_module._captured_tree(app))
        self.assertEqual(state["runtime_sha256"], digest(fixture.runtime.read_bytes()))

    def test_failed_restore_recreates_installed_snapshot_and_menu(self):
        """Restore rollback must include the previous bundle and inverse menu mutation."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        legacy_empty = fixture.app / "Contents/Resources/legacy-empty"
        legacy_empty.mkdir(parents=True)
        legacy_link = fixture.app / "Contents/Resources/legacy-link"
        legacy_link.symlink_to("missing-target")
        state = fixture.installer.install(fixture.transaction, fixture.installation)
        installed_runtime = fixture.runtime.read_bytes()
        installed_config = fixture.config.read_bytes()
        installed_executable = (fixture.app / "Contents/MacOS/Menu Helper").read_bytes()
        fixture.transaction.journal.commit()
        journal = InstallJournal(fixture.bottle_root / "restore-failure.json")
        handler = fixture.installer.undo_handler(
            fixture.installation, fixture._restore_snapshots
        )
        transaction = Transaction(journal, {"restore_launcher": handler})
        transaction.start("restore")
        undo = fixture.installer.restore_undo_data(fixture.installation, state)

        def fail_after_restore():
            fixture.installer.restore(fixture.installation, state)
            raise OSError("injected failure after restore mutation")

        with self.assertRaises(OSError):
            transaction.step(
                "restore launcher",
                UndoRecord("restore_launcher", undo),
                fail_after_restore,
            )

        self.assertEqual(installed_runtime, fixture.runtime.read_bytes())
        self.assertEqual(installed_config, fixture.config.read_bytes())
        self.assertEqual(
            installed_executable,
            (fixture.app / "Contents/MacOS/Menu Helper").read_bytes(),
        )
        previous = Path(state["previous_app"])
        self.assertTrue((previous / "Contents/Resources/legacy-empty").is_dir())
        self.assertTrue((previous / "Contents/Resources/legacy-link").is_symlink())
        recreate = fixture.runner.calls[-1][0]
        self.assertEqual(
            ["--create", "StartMenu/Ostriv (patched)", "--type", "raw"],
            recreate[recreate.index("--create") : recreate.index("--create") + 4],
        )
        self.assertEqual(
            str(
                (
                    fixture.game_launcher
                    / "Contents/Resources/CrossOverHelper.icns"
                ).resolve()
            ),
            recreate[recreate.index("--icon") + 1],
        )
        self.assertEqual(
            {"CX_BOTTLE_PATH": str(fixture.bottle_root.resolve().parent)},
            fixture.runner.environments[-1],
        )

    def test_restore_filesystem_error_is_typed_after_exact_transaction_rollback(self):
        """Raw filesystem errors must not bypass restore rollback or report success."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        state = fixture.installer.install(fixture.transaction, fixture.installation)
        installed_runtime = fixture.runtime.read_bytes()
        installed_executable = (fixture.app / "Contents/MacOS/Menu Helper").read_bytes()
        fixture.transaction.journal.commit()
        journal = InstallJournal(fixture.bottle_root / "typed-restore-failure.json")
        transaction = Transaction(
            journal,
            {
                "restore_launcher": fixture.installer.undo_handler(
                    fixture.installation, fixture._restore_snapshots
                )
            },
        )
        transaction.start("restore")
        undo = fixture.installer.restore_undo_data(fixture.installation, state)
        original_atomic_write = launcher_module._atomic_write
        failures = [OSError("injected atomic restore failure")]

        def fail_once(path, data, mode=0o644):
            if failures:
                raise failures.pop()
            return original_atomic_write(path, data, mode)

        with patch.object(
            launcher_module,
            "_atomic_write",
            side_effect=fail_once,
        ):
            with self.assertRaises(PatchError) as caught:
                transaction.step(
                    "restore launcher",
                    UndoRecord("restore_launcher", undo),
                    lambda: fixture.installer.restore(fixture.installation, state),
                )

        self.assertEqual("restore.launcher_filesystem", caught.exception.code)
        self.assertEqual(installed_runtime, fixture.runtime.read_bytes())
        self.assertEqual(
            installed_executable,
            (fixture.app / "Contents/MacOS/Menu Helper").read_bytes(),
        )

    def test_restore_preparation_capture_errors_are_typed_without_mutation(self):
        """Undo capture happens before the action wrapper and must never leak raw OSError."""
        for helper_name in ("_captured_tree", "_snapshot"):
            with self.subTest(helper=helper_name):
                fixture = LauncherFixture()
                self.addCleanup(fixture.cleanup)
                state = fixture.installer.install(
                    fixture.transaction, fixture.installation
                )
                before_app = {
                    path.relative_to(fixture.app).as_posix(): path.read_bytes()
                    for path in fixture.app.rglob("*")
                    if path.is_file()
                }
                before_runtime = fixture.runtime.read_bytes()
                calls_before = list(fixture.runner.calls)

                with patch.object(
                    launcher_module,
                    helper_name,
                    side_effect=OSError("injected undo capture failure"),
                ):
                    with self.assertRaises(PatchError) as caught:
                        fixture.installer.restore_undo_data(
                            fixture.installation, state
                        )

                self.assertEqual("restore.launcher_prepare", caught.exception.code)
                self.assertEqual(calls_before, fixture.runner.calls)
                self.assertEqual(before_runtime, fixture.runtime.read_bytes())
                self.assertEqual(
                    before_app,
                    {
                        path.relative_to(fixture.app).as_posix(): path.read_bytes()
                        for path in fixture.app.rglob("*")
                        if path.is_file()
                    },
                )

    def test_production_restore_preparation_failure_starts_no_journal(self):
        """The production boundary must finish launcher capture before transaction.start."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = fixture.installer.install(fixture.transaction, fixture.installation)
        production = Installer(
            fixture.release,
            fixture.installer,
            launcher_destination=fixture.destination,
        )
        journal_path = production.journal_path(fixture.installation)
        self.assertFalse(journal_path.exists())

        with patch.object(
            launcher_module,
            "_captured_tree",
            side_effect=OSError("injected production capture failure"),
        ):
            with self.assertRaises(PatchError) as caught:
                production._launcher_restore_undo(fixture.installation, state)

        self.assertEqual("restore.launcher_prepare", caught.exception.code)
        self.assertFalse(journal_path.exists())

    def test_reinstall_preserves_the_original_legacy_restore_target(self):
        """Backing up the first hardened app on reinstall loses the genuine legacy launcher."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        first = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.transaction.journal.commit()
        (fixture.bottle_root / "ostriv-macos-state.json").write_text(
            json.dumps({"launcher_artifacts": dict(first)}), encoding="utf-8"
        )
        second_journal = InstallJournal(fixture.bottle_root / "launcher-reinstall.json")
        second_transaction = Transaction(
            second_journal, {"restore_launcher": fixture._restore_snapshots}
        )
        second_transaction.start("launcher-reinstall")

        second = fixture.installer.install(second_transaction, fixture.installation)
        fixture.installer.restore(fixture.installation, second)

        self.assertEqual(b"legacy executable", (fixture.app / "Contents/MacOS/Menu Helper").read_bytes())
        self.assertIn(b"Generated by ostriv-macos patch.py", fixture.runtime.read_bytes())

    def test_reinstall_failure_after_rich_replaced_tree_removal_restores_exactly(self):
        """File-only removal undo loses owned empty directories and symlink types."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        base_extractor = fixture.extractor

        def rich_extractor(template, destination):
            base_extractor(template, destination)
            empty = destination / "Contents/Resources/owned-empty"
            empty.mkdir()
            empty.chmod(0o711)
            (destination / "Contents/Resources/owned-link").symlink_to(
                "missing-owned-target"
            )

        fixture.installer.extractor = rich_extractor
        first = fixture.installer.install(fixture.transaction, fixture.installation)
        fixture.transaction.journal.commit()
        state_path = fixture.bottle_root / "ostriv-macos-state.json"
        state_bytes = json.dumps({"launcher_artifacts": dict(first)}).encode("utf-8")
        state_path.write_bytes(state_bytes)
        before = launcher_module._captured_tree(fixture.installer._app_path())
        second_journal = InstallJournal(fixture.bottle_root / "rich-reinstall.json")
        second_transaction = Transaction(
            second_journal, {"restore_launcher": fixture._restore_snapshots}
        )
        second_transaction.start("reinstall")
        fixture.runner.cxmenu_failures = 1

        with self.assertRaises(PatchError) as caught:
            fixture.installer.install(
                second_transaction, fixture.installation
            )

        self.assertEqual("install.launcher_menu", caught.exception.code)
        self.assertEqual(
            before, launcher_module._captured_tree(fixture.installer._app_path())
        )
        self.assertEqual(state_bytes, state_path.read_bytes())
        self.assertIn("--create", fixture.runner.calls[-1][0])
        self.assertFalse(
            fixture.app.with_name(
                "." + fixture.app.name + ".ostriv-macos.replaced"
            ).exists()
        )

    def test_restore_removes_only_owned_inventory_and_leaves_unknown_files(self):
        """Recursive launcher cleanup can erase user files that were never installed by us."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = fixture.installer.install(fixture.transaction, fixture.installation)
        unknown = fixture.app / "Contents/Resources/user-note.txt"
        unknown.write_text("keep", encoding="utf-8")

        fixture.installer.restore(fixture.installation, state)

        self.assertEqual("keep", unknown.read_text(encoding="utf-8"))
        self.assertFalse(fixture.runtime.exists())
        self.assertFalse(fixture.config.exists())
        self.assertFalse((fixture.app / "Contents/MacOS/Menu Helper").exists())

    def test_restore_rejects_outside_launcher_paths_without_touching_them(self):
        """Trusting duplicated paths in state lets a corrupt record claim unrelated files."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        state = dict(fixture.installer.install(fixture.transaction, fixture.installation))
        victim = fixture.root / "unrelated.py"
        victim.write_bytes(fixture.runtime.read_bytes())
        state["runtime"] = str(victim)

        with self.assertRaises(PatchError) as caught:
            fixture.installer.restore(fixture.installation, state)

        self.assertEqual("restore.launcher_ownership", caught.exception.code)
        self.assertTrue(victim.exists())

    def test_legacy_restore_purges_only_the_patched_menu_entry(self):
        """Skipping purge leaves the obsolete launcher in CrossOver after legacy Restore."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        state = {
            "legacy": True,
            "artifacts": [
                {"path": str(fixture.app)},
                {"path": str(fixture.runtime)},
            ],
        }

        fixture.installer.restore(fixture.installation, state)

        purge = fixture.runner.calls[-1][0]
        self.assertEqual(
            ["--purge", "--filter", "StartMenu/Ostriv (patched)"], purge[-3:]
        )
        self.assertFalse(fixture.app.exists())
        self.assertTrue(fixture.runtime.exists())

    def test_legacy_restore_removes_known_entries_but_preserves_unknown_files_and_symlinks(self):
        """Recursive legacy cleanup erases user data and follows ownership beyond known entries."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        unknown = fixture.app / "Contents/Resources/user-note.txt"
        unknown.parent.mkdir(parents=True, exist_ok=True)
        unknown.write_text("keep", encoding="utf-8")
        victim = fixture.root / "outside.txt"
        victim.write_text("outside", encoding="utf-8")
        link = fixture.app / "Contents/Resources/user-link"
        link.symlink_to(victim)
        state = {
            "legacy": True,
            "artifacts": [
                {"path": str(fixture.app)},
                {"path": str(fixture.runtime)},
            ],
        }

        fixture.installer.restore(fixture.installation, state)

        self.assertEqual("keep", unknown.read_text(encoding="utf-8"))
        self.assertTrue(link.is_symlink())
        self.assertEqual(victim, link.readlink())
        self.assertEqual("outside", victim.read_text(encoding="utf-8"))
        self.assertFalse((fixture.app / "Contents/Info.plist").exists())
        self.assertFalse((fixture.app / "Contents/MacOS/Menu Helper").exists())
        self.assertTrue(fixture.app.exists())

    def test_legacy_restore_never_follows_symlinked_bundle_parents(self):
        """Leaf checks are unsafe when Contents or a nested parent redirects externally."""
        for parent_name in ("Contents", "Contents/Resources"):
            with self.subTest(parent=parent_name):
                fixture = LauncherFixture()
                self.addCleanup(fixture.cleanup)
                fixture.create_legacy_launcher()
                external = fixture.root / "external bundle victim"
                if parent_name == "Contents":
                    shutil.copytree(fixture.app / "Contents", external)
                    shutil.rmtree(fixture.app / "Contents")
                    (fixture.app / "Contents").symlink_to(
                        external, target_is_directory=True
                    )
                    victim = external / "Info.plist"
                else:
                    external.mkdir()
                    victim = external / "CrossOverHelper.icns"
                    victim.write_bytes(b"external-icon")
                    (fixture.app / "Contents/Resources").symlink_to(
                        external, target_is_directory=True
                    )
                before = victim.read_bytes()
                state = {
                    "legacy": True,
                    "artifacts": [
                        {"path": str(fixture.app)},
                        {"path": str(fixture.runtime)},
                    ],
                }

                fixture.installer.restore(fixture.installation, state)

                self.assertEqual(before, victim.read_bytes())
                self.assertTrue(
                    (fixture.app / parent_name).is_symlink(), parent_name
                )

    def test_legacy_restore_preserves_modified_runtime_containing_old_marker(self):
        """A marker substring cannot authenticate arbitrary or user-modified Python code."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_legacy_launcher()
        modified = fixture.runtime.read_bytes() + b"print('unrelated modification')\n"
        fixture.runtime.write_bytes(modified)
        state = {
            "legacy": True,
            "artifacts": [
                {"path": str(fixture.app)},
                {"path": str(fixture.runtime)},
            ],
        }

        fixture.installer.restore(fixture.installation, state)

        self.assertEqual(modified, fixture.runtime.read_bytes())

    def test_installed_runtime_has_no_project_import_and_survives_source_move(self):
        """Importing package code from the release directory makes the installed app non-standalone."""
        fixture = LauncherFixture()
        self.addCleanup(fixture.cleanup)
        fixture.installer.install(fixture.transaction, fixture.installation)
        installed_source = fixture.runtime.read_text(encoding="utf-8")
        self.assertNotRegex(installed_source, r"(?m)^\s*(?:from|import)\s+ostriv_macos\b")
        moved_release = fixture.release.with_name("Release Source moved away")
        fixture.release.rename(moved_release)

        spec = importlib.util.spec_from_file_location("installed_ostriv_launcher", fixture.runtime)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = module.LauncherConfig.load(fixture.config)
        events = []

        class Lock:
            def acquire(self):
                events.append("lock")
                return True

            def close(self):
                events.append("unlock")

        class Log:
            def info(self, *args):
                events.append("log")

            def exception(self, *args):
                raise AssertionError(args)

        class Steam:
            def ensure_ready(self, retry=False):
                events.append(("steam", retry))

        class Profile:
            def recover(self):
                events.append("recover")

            def switch(self):
                events.append("switch")

            def restore_once(self):
                events.append("restore")

        class Runner:
            def run(self, argv):
                events.append(("game", list(argv)))
                return type("Result", (), {"returncode": 0})()

        result = module.run_launcher(
            config,
            lock=Lock(),
            log_factory=lambda _path: Log(),
            runner=Runner(),
            steam=Steam(),
            profile=Profile(),
            dialog=lambda message: events.append(("dialog", message)),
            install_handlers=lambda profile: events.append("handlers"),
        )
        self.assertEqual(0, result)
        self.assertIn(("game", config.game_command), events)
        self.assertEqual("unlock", events[-1])


if __name__ == "__main__":
    unittest.main()
