# Player Experience Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (- [ ]) syntax for tracking.

**Goal:** Ship a self-contained player ZIP whose Python installer and generated launcher provide
a dependable, concise, one-command install and one-click play experience.

**Architecture:** Keep patch.py as the single player entrypoint while moving discovery, payload
validation, transactional installation, launcher materialization, runtime orchestration, and
diagnostics into focused standard-library-only modules. Preserve live-tested CrossOver and
ColorSync code paths, add deterministic fakes around external processes, and publish only a
validated named GitHub release asset.

**Tech Stack:** Python 3.9+ standard library, CrossOver 25/26 command-line tools, macOS ColorSync
through ctypes, unittest, GitHub Actions, Git LFS during development/CI only, GitHub CLI for
release publication.

**Spec:** docs/superpowers/specs/2026-08-25-player-experience-hardening-design.md

## Global Constraints

- Keep python3 patch.py as the only documented player installation command.
- Keep runtime code standard-library-only; developer and CI tooling may use separate tooling.
- Do not download executable payloads at installer runtime.
- Do not collect telemetry or upload diagnostics.
- Do not modify Ostriv executables or Steam files.
- Preserve genuine DLLs and unrelated user settings. Journal required graphics-setting changes
  and restore the player's original values on Restore.
- Do not claim Intel Mac support.
- Do not change or depend on Steam overlay behavior; continue launching outside Steam's Play
  button.
- Default terminal output must remain brief, clean, clear, actionable, and non-repetitive.
- Treat payload, registry, bottle environment, settings, and launcher failures as fatal required
  operations; never print success after a required warning.

---

## Phase 0: Documentation Discovery and Allowed APIs

This phase is complete. Executors must reread the referenced source before editing the
corresponding component.

### Repository sources

- Approved behavior: docs/superpowers/specs/2026-08-25-player-experience-hardening-design.md
- Current CrossOver discovery/version logic: patch.py:36-124
- Driver environment contract: patch.py:126-146
- Game discovery and fixed-root defect: patch.py:164-222
- Driver/config mutations: patch.py:238-312
- Current embedded launcher runtime: patch.py:375-576
- CrossOver menu helper materialization: patch.py:577-713
- Settings, Windows paths, and Restore inventory: patch.py:716-844
- Existing interactive menu and entrypoint: patch.py:851-1040
- Verified Steam cold-start constraints: docs/plan-launcher-cold-steam-start.md:38-69
- Driver constraints and dead ends: docs/technical.md
- Player documentation: README.md
- Git LFS payload declarations: .gitattributes and prebuilt/README.md
- Developer-only Mesa build: scripts/build-driver.sh; do not change it in this plan

### CrossOver sources

- Installed command help:
  - /Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine --help
  - /Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/cxmenu --help
  - /Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/cxbottle --help
- Bundled bottle resolver:
  /Applications/CrossOver.app/Contents/SharedSupport/CrossOver/lib/perl/CXBottle.pm:41-195
- CrossOver app and helper metadata:
  /Applications/CrossOver.app/Contents/Info.plist and generated helper Info.plist files
- Menu helper template:
  /Applications/CrossOver.app/Contents/Resources/Menu Helper.cpbz2

### Official release sources

- GitHub LFS archives:
  https://docs.github.com/en/enterprise-cloud@latest/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/managing-git-lfs-objects-in-archives-of-your-repository
- Git LFS pointer format:
  https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-git-large-file-storage
- Official checkout action and lfs input:
  https://github.com/actions/checkout
- Official Python setup action and version input:
  https://github.com/actions/setup-python
- GitHub CLI release creation:
  https://cli.github.com/manual/gh_release_create
- GITHUB_TOKEN permissions:
  https://docs.github.com/en/actions/tutorials/authenticate-with-github_token

### Allowed API surface

- Wine registry:
  wine --bottle BOTTLE [--scope managed] --no-update --no-lock reg query|add|delete
- Game launch:
  wine --bottle BOTTLE [--scope managed] --check --wait-children --start FILE
- Menu registration:
  cxmenu --bottle BOTTLE [--scope managed] --create StartMenu/... --type raw
  --command COMMAND --description DESCRIPTION --install
- Known-candidate validation: cxbottle --bottle BOTTLE [--scope managed] --status or --get-uuid
- Private external bottles: pass an absolute bottle root; managed bottles require a name and
  --scope managed.
- Bottle roots: CrossOver.conf [CrossOver] BottlePath, CX_BOTTLE_PATH,
  CX_MANAGED_BOTTLE_PATH, private/managed defaults, symlinked roots, and generated helper plists.
- Atomic local state: tempfile.NamedTemporaryFile in the destination directory followed by
  os.replace.
- Process lock: fcntl.flock with LOCK_EX | LOCK_NB while retaining the open file descriptor.
- Cleanup: atexit.register plus SIGINT/SIGTERM handlers calling one idempotent restore function.
- External command decoding: capture bytes, then decode with UTF-8 and errors="replace", or use
  encoding="utf-8", errors="replace" directly.
- Release: actions/checkout@v7 with lfs: true and actions/setup-python@v7 with an explicit
  Python version, then gh release create TAG ASSET --verify-tag --generate-notes with GH_TOKEN
  and contents: write.

### Anti-pattern guards

- Do not invent cxbottle --list or wine --list-bottles; neither exists.
- Do not treat the default private bottle directory as the only bottle authority.
- Do not derive an external bottle name through relpath against the default root.
- Do not pass absolute paths with --scope managed.
- Do not make opengl32=native global; retain the ostriv.exe AppDefaults override.
- Do not put SteamAppId or SteamGameId in bottle environment variables.
- Do not launch through Steam Play, a Steam URL, bare Wine, open -g, or open -b on our launcher.
- Do not gate readiness on ActiveUser alone or on the Wine-side Steam PID changing.
- Do not reset ColorSync to a generic default; restore the exact custom profile, including None.
- Do not interpolate configuration values into generated Python source.
- Do not remove the old launcher before the replacement verifies.
- Do not rely on GitHub's automatic source ZIP or checkout without lfs: true.
- Do not publish before extracting and validating the completed player ZIP.

---

## Planned File Structure

| File | Responsibility |
|---|---|
| patch.py | Python-version guard and thin call into ostriv_macos.cli |
| ostriv_macos/__init__.py | Package version and public package marker |
| ostriv_macos/diagnostics.py | Typed failures, tolerant command runner, logs, and concise output |
| ostriv_macos/payload.py | Manifest loading and pre-mutation payload validation |
| ostriv_macos/discovery.py | CrossOver, bottle, and Ostriv discovery |
| ostriv_macos/installer.py | Persistent journal, transaction runner, Install/Reinstall/Restore |
| ostriv_macos/launcher_runtime.py | Standalone copied launcher runtime and state machine |
| ostriv_macos/launcher.py | Runtime/config/app materialization and legacy launcher migration |
| ostriv_macos/cli.py | Menus, orchestration, --diagnose, and internal --preflight |
| payload-manifest.json | Exact size/SHA-256/type contract for bundled payload |
| scripts/build-release.py | Allowlisted clean-stage, ZIP, extract, and smoke validation |
| .github/workflows/ci.yml | Pure and macOS-specific test lanes |
| .github/workflows/release.yml | Tag-gated validated player-asset publication |
| tests/*.py | Unit, integration, snapshots, artifact, and regression tests |

---

## Phase 1: Foundations, Payload Integrity, and Discovery

**What to implement:** Copy stable helpers from patch.py into testable modules, then extend only
the defective boundaries. This phase introduces no player-visible behavior change until the CLI
is rewired in Task 9.

**Documentation references:** Approved spec sections Architecture, Payload preflight, Discovery
and selection, Player-facing output contract, and Diagnostics; repository and CrossOver sources
listed in Phase 0.

**Verification checklist:** All phase tests pass with Python 3.9-compatible syntax; no runtime
dependency outside the standard library; invalid byte streams never raise UnicodeDecodeError;
payload validation performs no destination writes; external bottles resolve to a real root.

**Anti-pattern guards:** Do not move the interactive entrypoint yet, mutate bottles from discovery,
or make payload recovery mention Git LFS to players.

### Task 1: Package Foundation, Tolerant Commands, and Concise Output

**Files:**
- Create: ostriv_macos/__init__.py
- Create: ostriv_macos/diagnostics.py
- Create: tests/__init__.py
- Create: tests/test_diagnostics.py

**Interfaces:**
- Produces: PatchError(code, player_message, detail), CommandResult(returncode, stdout, stderr),
  CommandRunner.run(argv, timeout=None), configure_logger(path), and PlayerOutput.
- Consumes: Python 3.9 standard library only.

- [ ] **Step 1: Write failing tests for decoding, typed errors, and output de-duplication**

~~~python
# tests/test_diagnostics.py
import io
import tempfile
import unittest
from pathlib import Path

from ostriv_macos.diagnostics import (
    PatchError,
    PlayerOutput,
    decode_output,
)


class DiagnosticsTests(unittest.TestCase):
    def test_decode_output_replaces_invalid_utf8(self):
        self.assertEqual("OK\ufffd", decode_output(b"OK\x8e"))

    def test_patch_error_keeps_player_copy_separate_from_detail(self):
        error = PatchError(
            "payload.invalid",
            "The download is incomplete. Download the release ZIP again.",
            "opengl32.dll is a Git LFS pointer",
        )
        self.assertEqual("payload.invalid", error.code)
        self.assertNotIn("Git LFS", error.player_message)
        self.assertIn("Git LFS", error.detail)

    def test_stage_is_printed_once(self):
        stream = io.StringIO()
        output = PlayerOutput(stream, color=False)
        output.title()
        output.stage("Package", "OK")
        output.stage("Package", "OK")
        self.assertEqual(
            "Ostriv for macOS\nPackage: OK\n",
            stream.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
~~~

- [ ] **Step 2: Run the focused test and verify the missing module failure**

Run: python3 -m unittest tests.test_diagnostics -v

Expected: FAIL with ModuleNotFoundError for ostriv_macos.

- [ ] **Step 3: Add the package and minimal diagnostics implementation**

~~~python
# ostriv_macos/__init__.py
__version__ = "0.1.0"
~~~

~~~python
# ostriv_macos/diagnostics.py
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Optional, Sequence


class PatchError(Exception):
    def __init__(self, code: str, player_message: str, detail: str = ""):
        super().__init__(detail or player_message)
        self.code = code
        self.player_message = player_message
        self.detail = detail


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def decode_output(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


class CommandRunner:
    def run(
        self,
        argv: Sequence[str],
        timeout: Optional[float] = None,
    ) -> CommandResult:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        return CommandResult(
            result.returncode,
            decode_output(result.stdout),
            decode_output(result.stderr),
        )


def configure_logger(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ostriv_macos")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(
        str(path),
        encoding="utf-8",
        errors="backslashreplace",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


class PlayerOutput:
    def __init__(self, stream: IO[str] = sys.stdout, color: Optional[bool] = None):
        self.stream = stream
        self.color = stream.isatty() if color is None else color
        self._title_printed = False
        self._stages = set()

    def _line(self, text: str = "") -> None:
        print(text, file=self.stream)

    def title(self) -> None:
        if not self._title_printed:
            self._line("Ostriv for macOS")
            self._title_printed = True

    def stage(self, label: str, status: str, detail: str = "") -> None:
        if label in self._stages:
            return
        self._stages.add(label)
        suffix = " \u00b7 " + detail if detail else ""
        self._line("{}: {}{}".format(label, status, suffix))
~~~

- [ ] **Step 4: Add logger and CommandRunner tests using a mocked CompletedProcess**

Test that CommandRunner receives an argument list with shell=False behavior, captures stderr, and
returns replacement characters for invalid bytes. Test that configure_logger writes the detailed
PatchError text while PlayerOutput receives only player_message.

- [ ] **Step 5: Run the foundation tests**

Run: python3 -m unittest tests.test_diagnostics -v

Expected: all diagnostics tests PASS.

- [ ] **Step 6: Commit the foundation**

~~~bash
git add ostriv_macos/__init__.py ostriv_macos/diagnostics.py tests
git commit -m "refactor: add diagnostics and output foundation"
~~~

### Task 2: Manifest and Complete Payload Preflight

**Files:**
- Create: payload-manifest.json
- Create: ostriv_macos/payload.py
- Create: tests/test_payload.py

**Interfaces:**
- Consumes: PatchError from Task 1.
- Produces: PayloadEntry(relative_path, size, sha256, pe), load_manifest(path), and
  validate_payload(root, entries).

- [ ] **Step 1: Write failing table-driven payload tests**

~~~python
# tests/test_payload.py
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ostriv_macos.diagnostics import PatchError
from ostriv_macos.payload import load_manifest, validate_payload


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(self, payload: bytes) -> Path:
        target = self.root / "prebuilt" / "opengl32.dll"
        target.parent.mkdir()
        target.write_bytes(payload)
        manifest = {
            "schema": 1,
            "files": [{
                "path": "prebuilt/opengl32.dll",
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "pe": True,
            }],
        }
        path = self.root / "payload-manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_valid_pe_payload_passes(self):
        path = self.write_manifest(b"MZ" + b"\0" * 64)
        validate_payload(self.root, load_manifest(path))

    def test_lfs_pointer_is_rejected_before_header_or_hash(self):
        pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 10\n"
        path = self.write_manifest(pointer)
        with self.assertRaises(PatchError) as caught:
            validate_payload(self.root, load_manifest(path))
        self.assertEqual("payload.lfs_pointer", caught.exception.code)

    def test_hash_mismatch_is_rejected(self):
        path = self.write_manifest(b"MZ" + b"\0" * 64)
        (self.root / "prebuilt" / "opengl32.dll").write_bytes(
            b"MZ" + b"\0" * 63 + b"\1"
        )
        with self.assertRaises(PatchError) as caught:
            validate_payload(self.root, load_manifest(path))
        self.assertEqual("payload.hash_mismatch", caught.exception.code)

    def test_size_mismatch_is_rejected(self):
        path = self.write_manifest(b"MZ" + b"\0" * 64)
        (self.root / "prebuilt" / "opengl32.dll").write_bytes(b"MZshort")
        with self.assertRaises(PatchError) as caught:
            validate_payload(self.root, load_manifest(path))
        self.assertEqual("payload.size_mismatch", caught.exception.code)
~~~

- [ ] **Step 2: Run payload tests and verify the missing module failure**

Run: python3 -m unittest tests.test_payload -v

Expected: FAIL with ModuleNotFoundError for ostriv_macos.payload.

- [ ] **Step 3: Add the exact checked-in manifest**

~~~json
{
  "schema": 1,
  "files": [
    {
      "path": "prebuilt/dxil.dll",
      "size": 1503072,
      "sha256": "cbcfe883a09fd0ca1f98abdf3a9553b560895e3283a136da82a8381253a169df",
      "pe": true
    },
    {
      "path": "prebuilt/libgallium_wgl.dll",
      "size": 45716642,
      "sha256": "816817bc20216fa07840dbba86ef25ec1715f6899df635f1f1aec9af209cf8b3",
      "pe": true
    },
    {
      "path": "prebuilt/libwinpthread-1.dll",
      "size": 343646,
      "sha256": "1bb16e85f19c34629364de7407b3531201e787d803df0db6e46d01d2e8a277ac",
      "pe": true
    },
    {
      "path": "prebuilt/opengl32.dll",
      "size": 539467,
      "sha256": "3b5c1e578c8b41dab765fcc90e5981917a8ea22105dee67d410633b3f5af2c3f",
      "pe": true
    },
    {
      "path": "assets/settings.data",
      "size": 1332,
      "sha256": "08bdbb1bb0aacdbc2d4aff6d0a22d653af90516ab151cd4e3ec60847e2efd8e1",
      "pe": false
    }
  ]
}
~~~

- [ ] **Step 4: Implement streaming validation in the required order**

~~~python
# ostriv_macos/payload.py
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Tuple

from .diagnostics import PatchError

LFS_PREFIX = b"version https://git-lfs.github.com/spec/v1"


@dataclass(frozen=True)
class PayloadEntry:
    relative_path: str
    size: int
    sha256: str
    pe: bool


def load_manifest(path: Path) -> Tuple[PayloadEntry, ...]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data["files"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise PatchError(
            "payload.manifest",
            "The download is incomplete. Download the release ZIP again.",
            "Unreadable payload manifest: {}".format(error),
        ) from error
    if data.get("schema") != 1 or not isinstance(items, list):
        raise PatchError(
            "payload.manifest",
            "The download is incomplete. Download the release ZIP again.",
            "Unsupported payload manifest schema",
        )
    entries = []
    seen = set()
    for item in items:
        try:
            relative = PurePosixPath(str(item["path"]))
            digest = str(item["sha256"]).lower()
            size = int(item["size"])
        except (KeyError, TypeError, ValueError) as error:
            raise PatchError(
                "payload.manifest",
                "The download is incomplete. Download the release ZIP again.",
                "Invalid payload manifest entry: {}".format(error),
            ) from error
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or relative.as_posix() in seen
            or size < 0
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise PatchError(
                "payload.manifest",
                "The download is incomplete. Download the release ZIP again.",
                "Invalid payload manifest entry: {}".format(item),
            )
        seen.add(relative.as_posix())
        entries.append(
            PayloadEntry(relative.as_posix(), size, digest, bool(item.get("pe")))
        )
    return tuple(entries)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_payload(root: Path, entries: Iterable[PayloadEntry]) -> None:
    for entry in entries:
        path = root / entry.relative_path
        if not path.is_file():
            code = "payload.missing"
            detail = "{} is missing".format(entry.relative_path)
        else:
            with path.open("rb") as stream:
                header = stream.read(128)
            if header.startswith(LFS_PREFIX):
                code = "payload.lfs_pointer"
                detail = "{} is a Git LFS pointer".format(entry.relative_path)
            elif entry.pe and not header.startswith(b"MZ"):
                code = "payload.not_pe"
                detail = "{} is not a PE DLL".format(entry.relative_path)
            elif path.stat().st_size != entry.size:
                code = "payload.size_mismatch"
                detail = "{} has the wrong size".format(entry.relative_path)
            elif _digest(path) != entry.sha256:
                code = "payload.hash_mismatch"
                detail = "{} has the wrong SHA-256".format(entry.relative_path)
            else:
                continue
        raise PatchError(
            code,
            "The download is incomplete. Download the release ZIP again.",
            detail,
        )
~~~

- [ ] **Step 5: Expand tests to missing, non-PE, size mismatch, digest mismatch, malformed JSON,
unsupported schema, duplicate paths, and absolute/parent-traversal paths**

Use separate fixture bytes for each branch and assert both the exact error code and the single
player recovery sentence.

- [ ] **Step 6: Run payload tests and the validator against the real checkout**

Run: python3 -m unittest tests.test_payload -v

Run:
python3 -c "from pathlib import Path; from ostriv_macos.payload import load_manifest,validate_payload; p=Path('.'); validate_payload(p,load_manifest(p/'payload-manifest.json'))"

Expected: tests PASS and real checkout validation exits 0 with no output.

- [ ] **Step 7: Commit payload validation**

~~~bash
git add payload-manifest.json ostriv_macos/payload.py tests/test_payload.py
git commit -m "feat: reject incomplete player payloads"
~~~

### Task 3: CrossOver, External Bottle, and Ostriv Discovery

**Files:**
- Create: ostriv_macos/discovery.py
- Create: tests/test_discovery.py

**Interfaces:**
- Consumes: CommandRunner and PatchError from Task 1.
- Produces: CrossOverInstall, Bottle, GameInstallation, find_crossover_apps(),
  configured_bottle_roots(), discover_bottles(), discover_games(), and
  resolve_explicit_game().

- [ ] **Step 1: Write failing discovery fixtures for every supported root source**

~~~python
# tests/test_discovery.py
import os
import plistlib
import tempfile
import unittest
from pathlib import Path

from ostriv_macos.discovery import (
    CrossOverInstall,
    configured_bottle_roots,
    discover_bottles,
    resolve_explicit_game,
)


def make_bottle(root: Path, name: str) -> Path:
    bottle = root / name
    (bottle / "drive_c").mkdir(parents=True)
    (bottle / "cxbottle.conf").write_text("[Bottle]\n", encoding="utf-8")
    (bottle / "system.reg").write_text("REGEDIT4\n", encoding="utf-8")
    return bottle


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

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

    def test_symlinked_bottle_keeps_name_and_resolves_real_root(self):
        default = self.home / "Library/Application Support/CrossOver/Bottles"
        external = make_bottle(self.home / "Volumes/Games", "Sniper 5")
        default.mkdir(parents=True)
        (default / "Sniper 5").symlink_to(external, target_is_directory=True)
        crossover = CrossOverInstall(
            self.home / "Applications/CrossOver.app",
            self.home / "Applications/CrossOver.app/Contents/SharedSupport/CrossOver",
            "26.3",
        )
        bottles = discover_bottles(crossover, self.home, {})
        self.assertEqual("Sniper 5", bottles[0].name)
        self.assertEqual(external.resolve(), bottles[0].root)

    def test_explicit_external_game_walks_up_to_bottle_root(self):
        bottle = make_bottle(self.home / "Volumes/T7/GAMES/Bottles", "Sniper 5")
        game = bottle / "drive_c/Steam/steamapps/common/Ostriv"
        game.mkdir(parents=True)
        (game / "ostriv.exe").write_bytes(b"MZ")
        crossover = CrossOverInstall(
            self.home / "Applications/CrossOver.app",
            self.home / "Applications/CrossOver.app/Contents/SharedSupport/CrossOver",
            "26.3",
        )
        result = resolve_explicit_game(game, [crossover])
        self.assertEqual(bottle.resolve(), result.bottle.root)
        self.assertEqual("Sniper 5", result.bottle.name)
~~~

- [ ] **Step 2: Run discovery tests and verify the missing module failure**

Run: python3 -m unittest tests.test_discovery -v

Expected: FAIL with ModuleNotFoundError for ostriv_macos.discovery.

- [ ] **Step 3: Copy current CrossOver app discovery and define resolved models**

Copy patch.py:36-92 and 103-110 into discovery.py, preserving OSTRIV_CROSSOVER_APP,
~/Applications, /Applications, Spotlight, LaunchServices, and version plist behavior. Replace
globals with explicit home/env/runner parameters. Give find_crossover_apps() an
`allow_subprocess: bool = True` argument: false checks configured/fixed filesystem locations only
and never invokes Spotlight or LaunchServices, which Task 9 uses for read-only diagnosis. Add
these Python 3.9-compatible models:

~~~python
@dataclass(frozen=True)
class CrossOverInstall:
    app: Path
    shared_support: Path
    version: Optional[str]


@dataclass(frozen=True)
class Bottle:
    name: str
    root: Path
    scope: str
    crossover: CrossOverInstall

    def command_bottle(self) -> str:
        return self.name if self.scope == "managed" else str(self.root)

    def scope_args(self) -> List[str]:
        return ["--scope", "managed"] if self.scope == "managed" else []


@dataclass(frozen=True)
class GameInstallation:
    bottle: Bottle
    game_dir: Path
    version: Optional[str]
~~~

- [ ] **Step 4: Implement root collection from the bundled resolver contract**

Use configparser.ConfigParser(interpolation=None) for CrossOver.conf, split BottlePath and
environment roots on os.pathsep, require absolute roots, add private and managed defaults, and
deduplicate with Path.resolve(). Scan roots with os.scandir so symlink entries are retained before
canonicalization. A valid bottle requires cxbottle.conf, system.reg, and drive_c.

Also scan ~/Applications/CrossOver recursively for helper Info.plist files. Read
CXHelperAppBottleName, CXHelperAppBottleTag, and CrossOverHelperCommand with plistlib; accept an
absolute private bottle path only after the same three-file validation.

- [ ] **Step 5: Copy game search and replace fixed-root bottle derivation**

Copy the recursive ostriv.exe search from patch.py:179-193. discover_games() accepts Bottle
objects and never reconstructs their paths. resolve_explicit_game() walks parents from the given
game directory until it finds a root containing cxbottle.conf, system.reg, and drive_c; it raises
discovery.explicit_not_in_bottle if no such root exists.

- [ ] **Step 6: Add tests for user/system CrossOver apps, environment roots, helper plists,
managed scope, spaces, Unicode, duplicates, zero results, multiple games, and
allow_subprocess=False never calling the runner**

Assert managed Bottle.command_bottle() returns the name plus --scope managed, while private
external bottles return their absolute root with no managed scope.

- [ ] **Step 7: Run discovery tests**

Run: python3 -m unittest tests.test_discovery -v

Expected: all discovery tests PASS without invoking installed CrossOver.

- [ ] **Step 8: Commit discovery**

~~~bash
git add ostriv_macos/discovery.py tests/test_discovery.py
git commit -m "feat: discover registered and external CrossOver bottles"
~~~

---

## Phase 2: Transactional Install, Reinstall, and Restore

**What to implement:** Copy the current driver, registry, environment, settings, and Restore
operations into an explicit transaction with a persistent ownership/recovery journal. Preserve
current values and backup compatibility while making every required step verifiable and
rollback-safe.

**Documentation references:** patch.py:126-146, 205-312, 716-844; approved spec sections
Transactional installation and Migration and compatibility.

**Verification checklist:** Failure injected after every mutation restores byte-for-byte prior
state; Install and Reinstall converge; Restore is repeatable; external bottles never route through
the fixed default root; success is impossible until post-install verification passes.

**Anti-pattern guards:** Do not back up one of this project's own DLLs as an original, replace
files before complete payload validation, leave registry/config changes after rollback, or treat
required failures as warnings.

### Task 4: Persistent Journal and Idempotent Transaction Engine

**Files:**
- Create: tests/test_transaction.py
- Modify: ostriv_macos/installer.py (create)

**Interfaces:**
- Consumes: PatchError and configure_logger from Task 1.
- Produces: UndoRecord(kind, data), InstallJournal(path), Transaction(journal, handlers),
  atomic_write_json(path, data), start(operation), and recover_incomplete().

- [ ] **Step 1: Write failing tests for reverse rollback and interrupted-journal recovery**

~~~python
# tests/test_transaction.py
import tempfile
import unittest
from pathlib import Path

from ostriv_macos.installer import InstallJournal, Transaction, UndoRecord


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.json"
        self.events = []
        self.handlers = {
            "event": lambda record: self.events.append(record.data["undo"]),
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_failure_rolls_back_applied_steps_in_reverse_order(self):
        transaction = Transaction(InstallJournal(self.path), self.handlers)
        transaction.start("install")
        transaction.step(
            "first",
            UndoRecord("event", {"undo": "undo-first"}),
            lambda: self.events.append("first"),
        )
        transaction.step(
            "second",
            UndoRecord("event", {"undo": "undo-second"}),
            lambda: self.events.append("second"),
        )
        with self.assertRaises(RuntimeError):
            transaction.step(
                "third",
                UndoRecord("event", {"undo": "undo-third"}),
                lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            )
        transaction.rollback()
        self.assertEqual(
            ["first", "second", "undo-third", "undo-second", "undo-first"],
            self.events,
        )

    def test_pending_record_is_recovered_idempotently(self):
        journal = InstallJournal(self.path)
        journal.start("install")
        journal.begin("copy", UndoRecord("event", {"undo": "restore"}))
        Transaction(journal, self.handlers).recover_incomplete()
        Transaction(journal, self.handlers).recover_incomplete()
        self.assertEqual(["restore"], self.events)

    def test_completed_operation_is_not_replayed_by_the_next_operation(self):
        first = Transaction(InstallJournal(self.path), self.handlers)
        first.start("install")
        first.step("copy", UndoRecord("event", {"undo": "old"}), lambda: None)
        first.journal.commit()
        second = Transaction(InstallJournal(self.path), self.handlers)
        second.recover_incomplete()
        second.start("reinstall")
        self.assertEqual([], self.events)
        self.assertEqual([], second.journal.data["records"])
~~~

- [ ] **Step 2: Run the transaction tests and verify the missing interface failure**

Run: python3 -m unittest tests.test_transaction -v

Expected: FAIL because installer.py or the transaction interfaces do not exist.

- [ ] **Step 3: Implement atomic journal persistence and record states**

~~~python
# core interfaces in ostriv_macos/installer.py
@dataclass(frozen=True)
class UndoRecord:
    kind: str
    data: Dict[str, object]


def atomic_write_json(path: Path, data: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            delete=False,
        ) as stream:
            temp_name = stream.name
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, str(path))
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


class InstallJournal:
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> Dict[str, object]:
        if not self.path.exists():
            return {"schema": 1, "complete": True, "records": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        atomic_write_json(self.path, self.data)

    def start(self, operation: str) -> None:
        if self.data["records"] and not self.data.get("complete"):
            raise PatchError(
                "install.recovery_required",
                "A previous installation needs recovery.",
                "Cannot replace an incomplete journal",
            )
        self.data = {
            "schema": 1,
            "operation": operation,
            "complete": False,
            "records": [],
        }
        self._save()

    def begin(self, name: str, undo: UndoRecord) -> int:
        records = self.data["records"]
        records.append({
            "name": name,
            "status": "pending",
            "undo": {"kind": undo.kind, "data": undo.data},
        })
        self._save()
        return len(records) - 1

    def mark_applied(self, index: int) -> None:
        self.data["records"][index]["status"] = "applied"
        self._save()

    def mark_rolled_back(self, index: int) -> None:
        self.data["records"][index]["status"] = "rolled_back"
        self._save()

    def commit(self) -> None:
        self.data["complete"] = True
        self._save()
~~~

Pending and applied undo records must be safe to execute more than once. This is required because
an interrupted atomic replacement may have completed before the journal reached applied.

- [ ] **Step 4: Implement transaction execution and recovery**

~~~python
class Transaction:
    def __init__(
        self,
        journal: InstallJournal,
        handlers: Mapping[str, Callable[[UndoRecord], None]],
    ):
        self.journal = journal
        self.handlers = handlers

    def start(self, operation: str) -> None:
        self.journal.start(operation)

    def _undo(self, record_data: Dict[str, object]) -> None:
        record = UndoRecord(record_data["kind"], record_data["data"])
        self.handlers[record.kind](record)

    def step(
        self,
        name: str,
        undo: UndoRecord,
        action: Callable[[], None],
    ) -> None:
        index = self.journal.begin(name, undo)
        try:
            action()
            self.journal.mark_applied(index)
        except BaseException:
            self._undo({"kind": undo.kind, "data": undo.data})
            self.journal.mark_rolled_back(index)
            raise

    def rollback(self) -> None:
        for index in range(len(self.journal.data["records"]) - 1, -1, -1):
            item = self.journal.data["records"][index]
            if item["status"] in ("pending", "applied"):
                self._undo(item["undo"])
                self.journal.mark_rolled_back(index)
        self.journal.commit()

    def recover_incomplete(self) -> None:
        if not self.journal.data.get("complete"):
            self.rollback()
~~~

- [ ] **Step 5: Add tests for atomic JSON replacement, corrupt journals, completed-operation
rotation, repeated rollback, and handler failure logging**

A corrupt journal raises PatchError with code install.journal_corrupt and does not start a new
install over unknown state. Handler failures retain the record and raise install.rollback_failed.

- [ ] **Step 6: Run transaction tests**

Run: python3 -m unittest tests.test_transaction -v

Expected: all transaction tests PASS.

- [ ] **Step 7: Commit the transaction engine**

~~~bash
git add ostriv_macos/installer.py tests/test_transaction.py
git commit -m "feat: add recoverable install transactions"
~~~

### Task 5: Journaled Driver, Registry, Environment, Settings, and Restore Operations

**Files:**
- Modify: ostriv_macos/installer.py
- Create: tests/test_installer.py

**Interfaces:**
- Consumes: Bottle and GameInstallation from Task 3, validated PayloadEntry values from Task 2,
  and Transaction from Task 4.
- Produces: InstallState, WineRegistry, the LauncherPort protocol, Installer.install(),
  Installer.restore(), and Installer.verify(). Task 8's LauncherInstaller implements
  LauncherPort; Task 5 tests use a deterministic fake.

- [ ] **Step 1: Write a fake-bottle integration test and failure-injection matrix**

~~~python
# tests/test_installer.py
import tempfile
import unittest
from pathlib import Path

from ostriv_macos.installer import Installer


class InstallerTests(unittest.TestCase):
    def test_failure_after_each_required_step_restores_original_tree(self):
        for fail_after in range(1, 8):
            with self.subTest(fail_after=fail_after):
                fixture = FakeBottleFixture()
                before = fixture.snapshot()
                installer = fixture.installer(fail_after=fail_after)
                with self.assertRaises(Exception):
                    installer.install(fixture.installation, fixture.payload)
                self.assertEqual(before, fixture.snapshot())

    def test_install_reinstall_restore_are_idempotent(self):
        fixture = FakeBottleFixture()
        installer = fixture.installer()
        installer.install(fixture.installation, fixture.payload)
        installed_once = fixture.snapshot()
        installer.install(fixture.installation, fixture.payload)
        self.assertEqual(installed_once, fixture.snapshot())
        installer.restore(fixture.installation)
        restored_once = fixture.snapshot()
        installer.restore(fixture.installation)
        self.assertEqual(restored_once, fixture.snapshot())
~~~

Define FakeBottleFixture in the same test file. It creates cxbottle.conf, system.reg, drive_c,
ostriv.exe, genuine destination DLL content, settings.data, and fake registry state in a temporary
directory. snapshot() returns relative path, bytes, mode, and fake registry values so equality is
byte-for-byte.

- [ ] **Step 2: Run installer tests and verify the missing Installer failure**

Run: python3 -m unittest tests.test_installer -v

Expected: FAIL because Installer is not implemented.

- [ ] **Step 3: Copy and adapt current mutation contracts**

Copy these exact sources into installer.py and make every path derive from GameInstallation.bottle
or game_dir rather than BOTTLES_ROOT:

- BOTTLE_ENV from patch.py:126-146 without changing values.
- Wine command lookup/wrapper from patch.py:205-222.
- driver backup rules from patch.py:229-255.
- game-scoped steam_appid.txt from patch.py:256-266.
- ostriv.exe-only registry override from patch.py:268-285.
- cxbottle.conf environment mutation from patch.py:286-312.
- settings mutation from patch.py:716-750.
- legacy Restore inventory from patch.py:768-844.

Use this registry interface so previous values can be journaled:

~~~python
class WineRegistry:
    def __init__(self, wine: Path, bottle: Bottle, runner: CommandRunner):
        self.wine = wine
        self.bottle = bottle
        self.runner = runner

    def _base(self) -> List[str]:
        return (
            [str(self.wine), "--bottle", self.bottle.command_bottle()]
            + self.bottle.scope_args()
            + ["--no-update", "--no-lock", "reg"]
        )

    def query(self, key: str, value: str) -> Optional[str]:
        result = self.runner.run(self._base() + ["query", key, "/v", value])
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[0] == value:
                return fields[-1]
        return None

    def set(self, key: str, value: str, data: str) -> None:
        result = self.runner.run(
            self._base() + ["add", key, "/v", value, "/d", data, "/f"]
        )
        if result.returncode != 0:
            raise PatchError("install.registry", "Installation failed.", result.stderr)

    def delete(self, key: str, value: str) -> None:
        self.runner.run(self._base() + ["delete", key, "/v", value, "/f"])
~~~

Define LauncherPort in installer.py with install(transaction, installation),
verify(installation, launcher_state), and restore(installation, launcher_state) methods. Keep the
protocol independent of ostriv_macos.launcher to avoid a circular import. FakeBottleFixture
provides a FakeLauncherPort that records those calls and returns a launcher-state mapping; Task 8
supplies the production implementation through dependency injection.

- [ ] **Step 4: Define ownership state and concrete undo record kinds**

InstallState schema 1 records project version, bottle realpath, game realpath, owned files,
backup files, prior registry value, original cxbottle.conf backup/digest, original settings
backup/digest, launcher artifacts, and completed verification time. Store it as
ostriv-macos-state.json in the resolved bottle root.

Implement idempotent undo handlers for remove_path, restore_file, restore_registry,
restore_config, restore_settings, and restore_launcher. A handler must check current state before
acting and must never delete a path absent from its matching ownership record.

- [ ] **Step 5: Implement the exact install operation order**

~~~python
def install(
    self,
    installation: GameInstallation,
    payload: Sequence[PayloadEntry],
) -> InstallState:
    validate_payload(self.package_root, payload)
    self.preflight(installation, payload)
    transaction = self.transaction_for(installation)
    transaction.recover_incomplete()
    transaction.start("install")
    try:
        self.stage_driver_files(transaction, installation, payload)
        self.write_app_id(transaction, installation, "773790")
        self.set_native_override(transaction, installation)
        self.set_bottle_environment(transaction, installation)
        self.set_safe_graphics(transaction, installation)
        launcher_state = self.launcher.install(transaction, installation)
        state = self.verify(installation, payload, launcher_state)
        self.write_install_state(transaction, installation, state)
        transaction.journal.commit()
        return state
    except BaseException:
        transaction.rollback()
        raise
~~~

preflight verifies all payloads, destination parent writeability, cxbottle.conf, system.reg,
settings location, Wine/cxmenu/menu-helper availability, and launcher destination before the
first transaction record. It also validates the selected bottle with
`cxbottle --bottle BOTTLE [--scope managed] --status`; a private external bottle passes its
absolute root and no managed scope.

- [ ] **Step 6: Implement post-install verification and journal-driven Restore**

verify() checks destination payload hashes, steam_appid.txt, exact scoped registry value, every
BOTTLE_ENV value, unrelated config/settings preservation, safe required settings, launcher
runtime/config digests, and the generated app plist/icon. Restore first recovers an incomplete
journal, starts a fresh `restore` operation, and replays ownership state in reverse through
journaled mutations. A Restore failure rolls back to the installed state; successful verification
commits the restore journal and removes the ownership state last. If no state file exists, migrate
current .bak files and legacy launcher names from patch.py:768-844 without claiming unknown files.

- [ ] **Step 7: Expand tests to registry retries, non-UTF-8 output, genuine DLL preservation,
unrelated settings preservation, corrupt state, missing tools, external paths, and legacy Restore**

Each required failure must assert a PatchError code, nonzero CLI outcome contract, exact rollback,
and no success result.

- [ ] **Step 8: Run transaction and installer suites**

Run: python3 -m unittest tests.test_transaction tests.test_installer -v

Expected: all tests PASS.

- [ ] **Step 9: Commit transactional installer operations**

~~~bash
git add ostriv_macos/installer.py tests/test_installer.py
git commit -m "feat: make install and restore transactional"
~~~

---

## Phase 3: Recoverable One-Click Launcher

**What to implement:** Extract the live-tested embedded launcher into a standalone copied runtime,
then add exact-profile recovery, process locking, Steam state orchestration, targeted retry, and
verified app materialization.

**Documentation references:** patch.py:364-713; docs/plan-launcher-cold-steam-start.md:38-69;
docs/technical.md display-profile and dead-end sections; approved spec Launcher state machine.

**Verification checklist:** The copied runtime has no package-relative imports; a second click
cannot start a second game; cold and warm readiness use the approved timing; only a fresh
SteamAPI_Init failure retries; every handled path restores the exact profile; app replacement is
verified before legacy removal.

**Anti-pattern guards:** Do not use O_EXCL files as locks, ActiveUser alone as readiness, raw
profile reset, source interpolation, open -g/open -b, bare Wine launch, or early legacy removal.

### Task 6: Extract Launcher Runtime, JSON Configuration, and Profile Recovery

**Files:**
- Create: ostriv_macos/launcher_runtime.py
- Create: tests/test_launcher_profile.py

**Interfaces:**
- Produces: LauncherConfig.load(path), atomic_json(path, data), ColorSyncProfileBackend,
  ProfileGuard, install_signal_handlers(guard), and run_game(config, runner).
- Constraint: launcher_runtime.py is standalone standard-library code because it is copied into
  the bottle and must work after the downloaded release directory is moved or deleted.

- [ ] **Step 1: Write failing profile recovery tests with a fake backend**

~~~python
# tests/test_launcher_profile.py
import tempfile
import unittest
from pathlib import Path

from ostriv_macos.launcher_runtime import ProfileGuard


class FakeProfiles:
    def __init__(self, current):
        self.current = current
        self.set_calls = []

    def get(self):
        return self.current

    def set(self, value):
        self.set_calls.append(value)
        self.current = value
        return True


class ProfileGuardTests(unittest.TestCase):
    def test_exit_restores_exact_original_profile(self):
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
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "profile.json"
            marker.write_text('{"original": null}', encoding="utf-8")
            backend = FakeProfiles("/Profiles/sRGB.icc")
            ProfileGuard(backend, marker, "/Profiles/sRGB.icc").recover()
            self.assertEqual([None], backend.set_calls)
~~~

- [ ] **Step 2: Run profile tests and verify the missing runtime failure**

Run: python3 -m unittest tests.test_launcher_profile -v

Expected: FAIL with ModuleNotFoundError for launcher_runtime.

- [ ] **Step 3: Extract the current ColorSync bridge exactly before extending it**

Copy patch.py:404-471 into launcher_runtime.py. Preserve:

- CGDisplayCreateUUIDFromDisplayID loaded from ColorSync, not CoreGraphics.
- custom profile lookup from slot "1" with PROFILE_ID fallback.
- restoration by reapplying the saved path, including None.

Wrap the copied functions behind ColorSyncProfileBackend.get() and .set(value) so tests use the
fake backend. Load ColorSync/CoreFoundation lazily in ColorSyncProfileBackend.__init__, never at
module import, so Linux tests and the release preflight can import launcher_runtime.py without
macOS frameworks.

- [ ] **Step 4: Add atomic configuration and an idempotent ProfileGuard**

~~~python
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
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != 1:
            raise RuntimeError("Unsupported launcher configuration")
        return cls(**data)


class ProfileGuard:
    def __init__(self, backend, marker: Path, srgb_path: str):
        self.backend = backend
        self.marker = marker
        self.srgb_path = srgb_path
        self.original = None
        self.switched = False
        self.restored = False

    def recover(self) -> None:
        if self.marker.exists():
            data = json.loads(self.marker.read_text(encoding="utf-8"))
            self.backend.set(data["original"])
            self.marker.unlink()

    def switch(self) -> None:
        self.original = self.backend.get()
        atomic_json(self.marker, {"original": self.original})
        if not self.backend.set(self.srgb_path):
            raise RuntimeError("Could not switch display profile")
        self.switched = True

    def restore_once(self) -> None:
        if self.restored:
            return
        self.restored = True
        if self.switched or self.marker.exists():
            self.backend.set(self.original)
        if self.marker.exists():
            self.marker.unlink()
~~~

- [ ] **Step 5: Register normal and signal cleanup**

Register restore_once with atexit. SIGINT and SIGTERM handlers call restore_once, restore the prior
handler/default, and re-signal the current process so exit semantics remain correct. Keep
restore_once reentrant because finally, signal, and atexit may all call it.

- [ ] **Step 6: Add tests for custom/None profiles, failed switch, corrupt marker, repeated
cleanup, SIGINT/SIGTERM handler calls, and launcher config schema**

- [ ] **Step 7: Run launcher profile tests**

Run: python3 -m unittest tests.test_launcher_profile -v

Expected: all profile/config tests PASS.

- [ ] **Step 8: Commit the extracted runtime foundation**

~~~bash
git add ostriv_macos/launcher_runtime.py tests/test_launcher_profile.py
git commit -m "refactor: extract recoverable launcher runtime"
~~~

### Task 7: Single-Instance Lock, Steam State Machine, Log Classification, and Retry

**Files:**
- Modify: ostriv_macos/launcher_runtime.py
- Create: tests/test_launcher_runtime.py

**Interfaces:**
- Consumes: LauncherConfig and ProfileGuard from Task 6.
- Produces: ProcessLock, SteamSignals, SteamController.probe(), SteamController.ensure_ready(),
  read_new_log(), classify_launch(), and run_launcher().

- [ ] **Step 1: Write a deterministic fake-clock state-machine test**

~~~python
# tests/test_launcher_runtime.py
import unittest

from ostriv_macos.launcher_runtime import SteamController, SteamSignals


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SteamControllerTests(unittest.TestCase):
    def test_transitioning_client_must_stay_ready_for_15_seconds(self):
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

    def test_warm_client_uses_two_probes_without_opening(self):
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
~~~

- [ ] **Step 2: Run runtime tests and verify the missing interfaces**

Run: python3 -m unittest tests.test_launcher_runtime -v

Expected: FAIL because SteamController and SteamSignals do not exist.

- [ ] **Step 3: Copy current Steam probes and remove the early-return race**

Copy patch.py:474-558 into methods that receive LauncherConfig and a tolerant runner. Preserve
steam.exe, ActiveUser, and steamwebhelper renderer probes. Remove the current
if steam_running(): return path. Readiness is all three signals, never presence alone.

~~~python
@dataclass(frozen=True)
class SteamSignals:
    process: bool
    active_user: bool
    renderer: bool

    @property
    def ready(self) -> bool:
        return self.process and self.active_user and self.renderer
~~~

ensure_ready() probes twice two seconds apart for a warm-ready client. Otherwise it opens Steam
once if absent, sends at most one waiting notification, requires 15 continuous ready seconds, and
raises a typed runtime failure after 300 seconds without launching the game.

- [ ] **Step 4: Add an OS-released advisory lock**

~~~python
class ProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(
            str(self.path),
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
            0o600,
        )
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            return False

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
~~~

Do not delete the lock file and do not use O_EXCL. The kernel releases flock when the process
dies; a leftover path is harmless.

- [ ] **Step 5: Implement fresh-log classification and exactly one targeted retry**

~~~python
def read_new_log(path: Path, offset: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        stream.seek(min(offset, path.stat().st_size))
        return stream.read().decode("utf-8", errors="replace")


def classify_launch(text: str) -> str:
    if "SteamAPI_Init() failed" in text:
        return "steam_api"
    if "windows_createWindow FAILED" in text:
        return "graphics_context"
    return "other"
~~~

run_launcher() records the pre-launch log size, runs the game once, and classifies only appended
content. On steam_api, it continues readiness probes for 30 seconds and retries exactly once with
a new offset. graphics_context and other never retry.

Implement the complete orchestration order from the approved spec: acquire lock; create the
launcher log; recover a stale profile marker; ensure Steam readiness; switch profile; launch and
classify; restore in finally; record the final state; release the lock. Register atexit and signal
recovery before switching. A failed lock shows the configured already-running dialog once and
returns success without opening Steam, switching profiles, or starting the game.

Add `main(argv=None) -> int` and an `if __name__ == "__main__"` guard to the standalone runtime.
main loads one JSON config argument, creates the log before constructing the lazy ColorSync and
external-process adapters, and maps handled failures to one configured dialog plus a nonzero exit
code. It writes details to the launcher log and never writes raw diagnostic output to the player.

- [ ] **Step 6: Add tests for stopped, starting, warm, logged-out, timeout, non-UTF-8 registry,
double-click lock, stale lock path, fresh/stale log markers, one retry, and unrelated failure**

Use fake monotonic/sleep/run/open/notify/dialog/profile dependencies. No test may launch installed
Steam or CrossOver.

- [ ] **Step 7: Run all launcher runtime tests**

Run:
python3 -m unittest tests.test_launcher_profile tests.test_launcher_runtime -v

Expected: all launcher runtime tests PASS.

- [ ] **Step 8: Commit the state machine**

~~~bash
git add ostriv_macos/launcher_runtime.py tests/test_launcher_runtime.py
git commit -m "feat: make patched launcher one-click and race-free"
~~~

### Task 8: Verified Launcher App Materialization and Legacy Migration

**Files:**
- Create: ostriv_macos/launcher.py
- Create: tests/test_launcher_install.py

**Interfaces:**
- Consumes: Bottle/GameInstallation, Transaction, and launcher_runtime.py.
- Produces: LauncherState, LauncherInstaller.install(), LauncherInstaller.restore(), and
  LauncherInstaller.verify().

- [ ] **Step 1: Write failing launcher-install tests**

Test a private external bottle with spaces, a managed bottle, missing Menu Helper.cpbz2, cxmenu
failure, icon copying, plist verification, legacy launcher preservation on new-app failure, and
legacy cleanup only after successful replacement.

~~~python
def test_new_launcher_verifies_before_legacy_is_removed(self):
    fixture = LauncherFixture(materialization_fails=True)
    fixture.create_legacy_launcher()
    with self.assertRaises(PatchError):
        fixture.installer.install(fixture.transaction, fixture.installation)
    self.assertTrue(fixture.legacy_launcher.exists())
~~~

- [ ] **Step 2: Run the focused test and verify the missing launcher module failure**

Run: python3 -m unittest tests.test_launcher_install -v

Expected: FAIL with ModuleNotFoundError for ostriv_macos.launcher.

- [ ] **Step 3: Copy launcher identity and materialization code**

Copy patch.py:364-370 and 577-713. Preserve:

- Ostriv (patched), StartMenu/Ostriv (patched), and the existing application location.
- Menu Helper.cpbz2 extraction and Info.plist editing.
- uppercase MD5 bundle identifier:

~~~python
bundle_id = "com.codeweavers.CrossOverHelper.{}.{}".format(
    hashlib.md5(bottle.name.encode("utf-8")).hexdigest().upper(),
    hashlib.md5("Ostriv (patched)".encode("utf-8")).hexdigest().upper(),
)
~~~

- CFBundleName, CFBundleDisplayName, CFBundleIdentifier, CrossOverHelperCommand,
  CXHelperAppBottleName, and CXHelperAppBottleTag.
- the game icon lookup/copy behavior from patch.py:644-668.

- [ ] **Step 4: Replace source templating with a runtime/config pair**

Copy launcher_runtime.py byte-for-byte into the resolved bottle root. Write launcher-config.json
atomically with schema 1 and every LauncherConfig field. Put shared dialog strings in the config
messages mapping so CLI/installer code owns wording and runtime dialogs do not drift.

CrossOverHelperCommand invokes:

~~~text
exec /usr/bin/env python3 "<resolved bottle root>/play-ostriv-patched.py" \
  "<resolved bottle root>/launcher-config.json"
~~~

Build that raw command with shell-safe quoting of each path; do not interpolate values into Python
source:

~~~python
command = "exec /usr/bin/env python3 {} {}".format(
    shlex.quote(str(runtime_path)),
    shlex.quote(str(config_path)),
)
~~~

- [ ] **Step 5: Stage, verify, and atomically swap the app**

Build the replacement as Ostriv (patched).app.pending in the destination directory. Verify the
executable, six plist fields, runtime/config existence and digests, icon, and bottle identity.
Only then move the old app to the transaction backup and os.replace the pending app. Register the
cxmenu entry with the supported --create/--type raw/--command/--install form. Any cxmenu or verify
failure rolls back through Task 4.

- [ ] **Step 6: Implement Restore and legacy migration**

Track the app, runtime, config, cxmenu entry, icon, and previous app in LauncherState. Remove
legacy play-ostriv-patched.py/template artifacts only after replacement verification. Restore
purges only StartMenu/Ostriv (patched), removes owned runtime/config/app files, and restores a
recorded previous app.

- [ ] **Step 7: Run launcher materialization and transaction tests**

Load the installed `play-ostriv-patched.py` from its copied path with importlib and execute its
run_launcher entrypoint against fake Wine/Steam/clock/profile/dialog dependencies. Assert it has
no package-relative import and behaves the same after the source release directory is renamed.

Run:
python3 -m unittest tests.test_launcher_install tests.test_transaction -v

Expected: all tests PASS.

- [ ] **Step 8: Commit launcher materialization**

~~~bash
git add ostriv_macos/launcher.py tests/test_launcher_install.py
git commit -m "feat: install a verified standalone launcher"
~~~

---

## Phase 4: Thin CLI, Player Copy, and Verified Release Asset

**What to implement:** Rewire the approved modular internals through one concise CLI, add
read-only diagnosis/preflight paths, then build and publish an allowlisted player ZIP only after
unpacked-artifact verification.

**Documentation references:** approved spec Player-facing output contract, Diagnostics, Release
artifact, and Automated verification; official GitHub sources in Phase 0; current menu code at
patch.py:851-1040.

**Verification checklist:** Player snapshots contain one title, one line per stage, one outcome,
one next-step block, and one log path; expected errors never show tracebacks; --diagnose and
--preflight make no mutations; the extracted named ZIP validates with no Git installation.

**Anti-pattern guards:** Do not leave business logic in patch.py, document Git clone as the player
path, run an interactive installer in CI, include the repository wholesale, or publish an
unvalidated asset.

### Task 9: Thin Entrypoint, Concise CLI, Diagnose, and Preflight

**Files:**
- Create: ostriv_macos/cli.py
- Create: tests/test_cli.py
- Modify: patch.py

**Interfaces:**
- Consumes: discovery, payload, Installer, LauncherInstaller, PatchError, logger, and PlayerOutput.
- Produces: cli.main(argv=None) -> int, diagnose(context) -> DiagnosticSummary, and
  preflight(package_root) -> int.

- [ ] **Step 1: Write exact terminal snapshot tests**

~~~python
# tests/test_cli.py
import io
import unittest

from ostriv_macos.cli import main


class CliTests(unittest.TestCase):
    def test_success_is_brief_and_non_repetitive(self):
        stream = io.StringIO()
        code = main(
            [],
            services=FakeServices.success(),
            stdin=io.StringIO("\n"),
            stdout=stream,
        )
        self.assertEqual(0, code)
        self.assertEqual(
            "Ostriv for macOS\n"
            "Found: CrossOver 26.2 \u00b7 Ostriv 0.5.9.60 \u00b7 My Bottle\n"
            "Package: OK\n"
            "Installation: OK\n"
            "\n"
            "Ready. Quit and reopen CrossOver once, then open Ostriv (patched).\n"
            "Log: ~/Library/Logs/ostriv-macos/install.log\n",
            stream.getvalue(),
        )

    def test_expected_failure_has_one_action_and_no_traceback(self):
        stream = io.StringIO()
        code = main(
            [],
            services=FakeServices.payload_failure(),
            stdin=io.StringIO(),
            stdout=stream,
        )
        text = stream.getvalue()
        self.assertEqual(2, code)
        self.assertEqual(1, text.count("Download the release ZIP again."))
        self.assertEqual(1, text.count("Log:"))
        self.assertNotIn("Traceback", text)
        self.assertNotIn("Git LFS", text)

    def test_diagnose_is_read_only(self):
        services = FakeServices.success()
        code = main(["--diagnose"], services=services, stdout=io.StringIO())
        self.assertEqual(0, code)
        self.assertEqual([], services.mutations)
~~~

The FakeServices fixture provides deterministic discovery, payload, installer, launcher, and
diagnostic results without invoking CrossOver.

- [ ] **Step 2: Run CLI tests and verify the missing module failure**

Run: python3 -m unittest tests.test_cli -v

Expected: FAIL with ModuleNotFoundError for ostriv_macos.cli.

- [ ] **Step 3: Copy the current keyboard menu and shorten its labels**

Copy select() from patch.py:873-930. Preserve arrow-key and non-TTY behavior. Present only
Install, Reinstall, and Restore as labels; print the explanation once before selection rather
than embedding repeated prose in every option.

- [ ] **Step 4: Implement CLI argument and outcome boundaries**

~~~python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("game_path", nargs="?")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--preflight", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    services=None,
    stdin=sys.stdin,
    stdout=sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    package_root = Path(__file__).resolve().parent.parent
    log_path = Path.home() / "Library/Logs/ostriv-macos/install.log"
    read_only = args.preflight or args.diagnose
    if read_only:
        logger = logging.getLogger("ostriv_macos.read_only")
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
    else:
        logger = configure_logger(log_path)
    output = PlayerOutput(stdout)
    if not args.preflight:
        output.title()
    try:
        active = services or build_services(package_root, logger, stdin, output)
        if args.preflight:
            active.validate_package()
            return 0
        if args.diagnose:
            active.print_diagnosis(args.game_path)
            return 0
        return run_interactive(active, args.game_path, output, log_path)
    except PatchError as error:
        logger.error("%s: %s", error.code, error.detail or error.player_message)
        output.failure(error.player_message, None if args.preflight else log_path)
        return 2
    except Exception:
        logger.exception("unexpected installer failure")
        output.failure(
            "Something went wrong. Try Reinstall once.",
            None if args.preflight else log_path,
        )
        return 3
~~~

PlayerOutput.success() and failure() own the blank line, final sentence, and single Log line.
failure() accepts an optional log path and omits the Log line for the internal --preflight path.
No lower-level component prints to stdout. --preflight and --diagnose must reach their read-only
branches before configure_logger creates a directory or file; their service construction must
likewise use read-only adapters only.

- [ ] **Step 5: Implement read-only diagnosis**

DiagnosticSummary contains Python version, CrossOver app/version, resolved bottle roots,
discovered games, payload result, install state, launcher state, and shortened log paths.
Diagnosis calls discovery/validation/read methods only. Tests mock mutation methods to raise if
called and assert the command still returns 0. Its discovery path reads application plists,
CrossOver.conf, helper plists, bottle markers, manifests, journals, and logs directly; it must not
invoke Spotlight, cxbottle, wine, cxmenu, open, or any other subprocess, because --diagnose is
strictly process-free as well as mutation-free.

--preflight validates the manifest and payload plus the presence of patch.py, every package source
module, assets/settings.data, README.md, and LICENSE. It neither imports macOS frameworks nor
performs CrossOver discovery, writes logs, or starts subprocesses.

- [ ] **Step 6: Replace patch.py with a Python 3.9 guard and thin delegation**

~~~python
#!/usr/bin/env python3
import sys


if sys.version_info < (3, 9):
    print("Ostriv for macOS")
    print("Python 3.9 or newer is required.")
    raise SystemExit(2)

from ostriv_macos.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
~~~

- [ ] **Step 7: Add integration snapshots for zero/one/multiple games, Install/Reinstall/Restore,
external explicit path, corrupt journal, launcher failure, and unexpected exception**

Assert title/stage/outcome/log line counts, nonzero failure codes, no duplicated guidance, and no
raw command output or traceback.

- [ ] **Step 8: Run all CLI and existing module tests**

Run: python3 -m unittest discover -s tests -v

Expected: all tests PASS.

- [ ] **Step 9: Commit CLI integration**

~~~bash
git add patch.py ostriv_macos/cli.py tests/test_cli.py
git commit -m "feat: provide a concise player installer"
~~~

### Task 10: Player ZIP Builder, CI, Release Workflow, and README

**Files:**
- Create: scripts/build-release.py
- Create: tests/test_release.py
- Create: .github/workflows/ci.yml
- Create: .github/workflows/release.yml
- Modify: .gitignore
- Modify: README.md
- Modify: prebuilt/README.md

**Interfaces:**
- Consumes: payload-manifest.json and ostriv_macos.payload validation.
- Produces: build_release(output: Path) and a constant asset named ostriv-macos-player.zip.

- [ ] **Step 1: Write a failing release-artifact test**

~~~python
# tests/test_release.py
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_release import build_release, safe_extract


class ReleaseTests(unittest.TestCase):
    def test_built_zip_is_allowlisted_and_preflights_after_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "ostriv-macos-player.zip"
            build_release(archive)
            unpacked = root / "unpacked"
            with zipfile.ZipFile(archive) as bundle:
                safe_extract(bundle, unpacked)
                names = set(bundle.namelist())
            self.assertIn("patch.py", names)
            self.assertIn("prebuilt/opengl32.dll", names)
            self.assertNotIn(".git/config", names)
            self.assertFalse(any(name.startswith(".build/") for name in names))
            result = subprocess.run(
                ["python3", str(unpacked / "patch.py"), "--preflight"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
~~~

- [ ] **Step 2: Run the release test and verify the missing builder failure**

Run: python3 -m unittest tests.test_release -v

Expected: FAIL because scripts.build_release does not exist.

- [ ] **Step 3: Implement an explicit player-file allowlist**

~~~python
# top of scripts/build-release.py
import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from ostriv_macos.payload import load_manifest, validate_payload


PLAYER_PATHS = (
    Path("patch.py"),
    Path("ostriv_macos"),
    Path("payload-manifest.json"),
    Path("prebuilt/opengl32.dll"),
    Path("prebuilt/libgallium_wgl.dll"),
    Path("prebuilt/dxil.dll"),
    Path("prebuilt/libwinpthread-1.dll"),
    Path("prebuilt/README.md"),
    Path("assets/settings.data"),
    Path("README.md"),
    Path("LICENSE"),
)


def copy_player_paths(source: Path, stage: Path) -> None:
    for relative in PLAYER_PATHS:
        src = source / relative
        dst = stage / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(
                src,
                dst,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        else:
            shutil.copy2(src, dst)
~~~

Do not use zip -r on the repository. The release contains only PLAYER_PATHS.
The repository-root sys.path bootstrap must precede the ostriv_macos import so both direct
`python3 scripts/build-release.py` execution and test imports resolve the same checked-in package.

- [ ] **Step 4: Build, extract safely, and run the same preflight**

~~~python
def safe_extract(bundle: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in bundle.infolist():
        target = (root / member.filename).resolve()
        if target != root and root not in target.parents:
            raise ValueError("unsafe ZIP member: {}".format(member.filename))
    bundle.extractall(root)


def build_release(output: Path) -> None:
    source = REPOSITORY_ROOT
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        stage = temporary / "stage"
        unpacked = temporary / "unpacked"
        stage.mkdir()
        copy_player_paths(source, stage)
        entries = load_manifest(stage / "payload-manifest.json")
        validate_payload(stage, entries)
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(stage).as_posix())
        with zipfile.ZipFile(output) as bundle:
            safe_extract(bundle, unpacked)
        validate_payload(
            unpacked,
            load_manifest(unpacked / "payload-manifest.json"),
        )
        result = subprocess.run(
            [sys.executable, str(unpacked / "patch.py"), "--preflight"],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("unpacked player ZIP failed preflight")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist/ostriv-macos-player.zip"),
    )
    args = parser.parse_args(argv)
    build_release(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
~~~

- [ ] **Step 5: Add CI with explicit Python versions and hydrated LFS**

~~~yaml
# .github/workflows/ci.yml
name: Test

on:
  push:
  pull_request:

permissions:
  contents: read

jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
        python: ["3.9", "3.13"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v7
        with:
          lfs: true
      - uses: actions/setup-python@v7
        with:
          python-version: ${{ matrix.python }}
      - run: python -m unittest discover -s tests -v
      - if: runner.os == 'Linux' && matrix.python == '3.13'
        run: python scripts/build-release.py --output dist/ostriv-macos-player.zip
~~~

- [ ] **Step 6: Add tag-gated publication after Linux and macOS verification**

~~~yaml
# .github/workflows/release.yml
name: Publish player ZIP

on:
  push:
    tags: ["v*"]

permissions:
  contents: write

jobs:
  verify:
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v7
        with:
          lfs: true
          fetch-depth: 0
      - uses: actions/setup-python@v7
        with:
          python-version: "3.9"
      - run: python -m unittest discover -s tests -v

  release:
    needs: verify
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          lfs: true
          fetch-depth: 0
      - uses: actions/setup-python@v7
        with:
          python-version: "3.9"
      - run: python scripts/build-release.py --output dist/ostriv-macos-player.zip
      - name: Publish verified player ZIP
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh release create "$GITHUB_REF_NAME" \
            "dist/ostriv-macos-player.zip#Ostriv for macOS (Apple Silicon)" \
            --verify-tag \
            --generate-notes
~~~

Do not use deprecated actions/create-release or actions/upload-release-asset.

- [ ] **Step 7: Rewrite the README player path and keep developer instructions separate**

The first screen of README.md must contain:

1. A Download latest player ZIP link to
   https://github.com/maksymenkoml/ostriv-macos/releases/latest/download/ostriv-macos-player.zip
2. Extract the ZIP.
3. Run python3 patch.py.
4. Open Ostriv (patched).

Keep the warning against Steam Play once. Move git clone, Git LFS, driver rebuilding, and
contributor commands under Development. Keep one troubleshooting table mapping Package: FAILED,
CrossOver/game not found, Steam timeout, graphics-context failure, and unexpected failure to one
action plus the log path. Update prebuilt/README.md to state that players receive hydrated DLLs in
the release asset and LFS applies only to repository contributors.

Add `dist/` to .gitignore so local verification artifacts never dirty the tracked tree.

- [ ] **Step 8: Test the builder, scan the unpacked ZIP, and validate workflow syntax**

Run: python3 -m unittest tests.test_release -v

Run: python3 scripts/build-release.py --output dist/ostriv-macos-player.zip

Run:
python3 -c "import zipfile; z=zipfile.ZipFile('dist/ostriv-macos-player.zip'); assert all('.git/' not in n and not n.startswith('.build/') for n in z.namelist())"

Run:
ruby -e 'require "yaml"; ARGV.each { |path| YAML.parse_file(path) }' .github/workflows/ci.yml .github/workflows/release.yml

Expected: tests PASS; builder, archive scan, and YAML parse all exit 0.

- [ ] **Step 9: Commit release automation and player documentation**

~~~bash
git add scripts/build-release.py tests/test_release.py .github/workflows .gitignore README.md prebuilt/README.md
git commit -m "build: publish a verified player download"
~~~

---

## Phase 5: Final Integration and Regression Verification

**What to implement:** Close documentation and migration gaps, run the complete verification
matrix, and prove the checked-in artifact path matches the approved player journey.

**Documentation references:** every source in Phase 0 and every requirement in the approved spec.

**Verification checklist:** All tests pass on supported Python syntax; no strict external-output
decoding remains; no fixed-root or embedded-template ownership remains; full install rollback and
launcher state matrices pass; the unpacked release preflights; documentation has one player path.

**Anti-pattern guards:** Do not weaken or skip a test to make the matrix green, perform a live
Steam launch as part of automated verification, publish a tag in this task, or claim real-world
compatibility beyond the documented CrossOver 25/26 evidence.

### Task 11: Full Matrix, Anti-Pattern Scan, and Documentation Closure

**Files:**
- Modify: CLAUDE.md
- Modify: docs/technical.md
- Modify: docs/plan-launcher-cold-steam-start.md
- Modify: any implementation/test file only when a verification failure identifies a defect

**Interfaces:**
- Consumes: all previous tasks.
- Produces: a clean, documented, fully verified implementation ready for code review.

- [ ] **Step 1: Update developer commands and architecture documentation**

CLAUDE.md must list:

~~~bash
python3 -m unittest discover -s tests -v
python3 scripts/build-release.py --output dist/ostriv-macos-player.zip
python3 patch.py --preflight
python3 patch.py --diagnose
~~~

docs/technical.md must describe module ownership, payload manifest, resolved Bottle model,
transaction journal/state file, copied runtime/config, lock/recovery marker, and local logs.
docs/plan-launcher-cold-steam-start.md must mark the old early return and heuristic-only behavior
as superseded by the stable readiness state machine and targeted retry.

- [ ] **Step 2: Run syntax and full unit/integration verification**

Run: python3 -m compileall -q patch.py ostriv_macos scripts tests

Run: python3 -m unittest discover -s tests -v

Expected: compile exits 0 and the test summary reports zero failures and zero errors.

- [ ] **Step 3: Run the complete release-artifact verification**

Run: python3 scripts/build-release.py --output dist/ostriv-macos-player.zip

Extract the asset into a fresh temporary directory, then run from that directory:

~~~bash
python3 patch.py --preflight
python3 patch.py --diagnose
~~~

Expected: preflight exits 0 without CrossOver mutation; diagnose prints a concise read-only
summary and one log-path line.

- [ ] **Step 4: Run anti-pattern scans**

Run:
rg --pcre2 -n 'text=True(?![^\n]*errors=)' patch.py ostriv_macos

Expected: no matches.

Run:
rg -n 'BOTTLES_ROOT|LAUNCHER_SCRIPT\s*=' patch.py ostriv_macos

Expected: no matches.

Run:
rg -n 'cxbottle.+--list|wine.+--list-bottles|SteamAppId|SteamGameId|open.+-g|open.+-b' patch.py ostriv_macos

Expected: no matches except an explicitly commented regression guard in documentation, never
runtime code.

Run: git diff --check

Expected: exits 0 with no output.

- [ ] **Step 5: Review terminal snapshots line by line**

Confirm every success/failure snapshot has:

- one product title;
- no more than one line for each stage;
- one final outcome;
- one actionable instruction block;
- one Log line;
- no repeated sentence;
- no raw command output, copied-filename list, retry chatter, or traceback.

Any mismatch is a test failure; update implementation and snapshot together only when the approved
spec requires the new wording.

- [ ] **Step 6: Verify spec coverage**

Map every Success criteria and Global constraints bullet in the design spec to at least one named
test. Add a test before changing code for any uncovered requirement. Record the mapping as a
comment table at the end of tests/test_cli.py so future changes keep the coverage visible.

- [ ] **Step 7: Commit final documentation and verification fixes**

~~~bash
git add CLAUDE.md docs patch.py ostriv_macos scripts tests .github README.md prebuilt/README.md payload-manifest.json
git commit -m "docs: finalize hardened player workflow"
~~~

- [ ] **Step 8: Run final clean-tree verification**

Run: python3 -m unittest discover -s tests -v

Run: python3 scripts/build-release.py --output dist/ostriv-macos-player.zip

Run: git status --short

Expected: tests and build exit 0. Only the ignored/generated dist artifact may exist; tracked
working tree is clean.

---

## Execution Order and Review Gates

1. Phase 1 establishes pure foundations and must pass review before bottle mutation work.
2. Phase 2 must prove failure-injection rollback before any launcher extraction is accepted.
3. Phase 3 must pass fake-process and profile-recovery review before CLI integration.
4. Phase 4 must prove the unpacked asset before workflows/documentation are accepted.
5. Phase 5 is evidence gathering and closure, not a feature-expansion phase.

Each task receives its own specification-compliance review and code-quality review before the next
task begins. Never combine commits from separate tasks merely because they touch the same file.
