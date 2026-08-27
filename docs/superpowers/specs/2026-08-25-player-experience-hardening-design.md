# Player Experience Hardening Design

**Status:** Approved in conversation on 2026-08-25

## Purpose

Make Ostriv on Apple Silicon Macs dependable for ordinary players without asking them to
understand Git, Git LFS, Wine registry commands, driver files, Steam startup timing, or raw
diagnostic procedures.

The supported player journey is:

1. Download the project release ZIP.
2. Extract it.
3. Run `python3 patch.py` and choose Install, Reinstall, or Restore.
4. Open **Ostriv (patched)** for subsequent play sessions.

The Python script remains the player-facing installer. A native macOS installer is not part of
this work.

## Success criteria

- The player download contains the real driver DLLs and requires no Git, Homebrew, Git LFS,
  pip packages, or developer tools beyond an available Python 3.9+ interpreter.
- The installer never reports success for an incomplete payload or a partially applied patch.
- Default, symlinked, registered, and externally stored CrossOver bottles are discoverable.
- One launcher click starts Steam when needed, waits until it is ready, and starts Ostriv.
- Repeated launcher clicks cannot race each other.
- Known Steam initialization failures receive one controlled automatic retry.
- The original display profile is restored after normal exit, handled failure, termination, or
  recovery on the next launch.
- Player-facing terminal output is brief, clean, clear, actionable, and non-repetitive.
- Detailed diagnostics are written locally without requiring a player to run raw test commands.
- Automated tests cover installer, launcher, recovery, and release-artifact failure modes.

## Global constraints

- Keep `python3 patch.py` as the only documented player installation command.
- Keep runtime code standard-library-only; developer and CI tooling may use separate tooling.
- Do not download executable payloads at installer runtime.
- Do not collect telemetry or upload diagnostics.
- Do not modify Ostriv executables or Steam files.
- Preserve genuine DLLs and unrelated user settings. Journal required graphics-setting changes
  and restore the player's original values on Restore.
- Do not claim Intel Mac support.
- Do not change or depend on Steam overlay behavior; continue launching outside Steam's Play
  button.

## Architecture

`patch.py` becomes a thin entrypoint that delegates to an internal `ostriv_macos` package. The
player still runs one script, while installer responsibilities become independently testable.

| Component | Responsibility |
|---|---|
| `patch.py` | Start the CLI, map expected failures to concise output, and provide the fatal-error boundary |
| `ostriv_macos/discovery.py` | Locate CrossOver, enumerate registered/default/external bottles, and find Ostriv installations |
| `ostriv_macos/payload.py` | Load the payload manifest and validate DLL type, size, and digest before mutation |
| `ostriv_macos/installer.py` | Plan and transactionally apply Install, Reinstall, and Restore |
| `ostriv_macos/launcher.py` | Install/remove the launcher runtime, configuration, application wrapper, and icon |
| `ostriv_macos/launcher_runtime.py` | Enforce one active launch, orchestrate Steam readiness, run Ostriv, and recover the display profile |
| `ostriv_macos/diagnostics.py` | Write structured local logs and render concise player-facing outcomes |
| `scripts/build-release.py` | Assemble and verify the self-contained player ZIP |
| `tests/` | Exercise pure units, fake CrossOver integration, failure recovery, and the final artifact |

The launcher runtime is stored as a normal source file instead of a large string inside
`patch.py`. Installation copies that runtime plus a small JSON configuration file into the
bottle. Values such as the bottle identifier, Wine executable, game path, and log path are data,
not interpolated Python source.

Only responsibilities touched by this work move into the package. Unrelated driver-building and
technical documentation remain where they are.

## Release artifact

Each tagged release publishes a named player ZIP assembled by `scripts/build-release.py`. It
contains:

- `patch.py` and the complete `ostriv_macos` package;
- the actual prebuilt DLLs;
- `assets/settings.data` and launcher assets;
- `payload-manifest.json` with exact file sizes and SHA-256 digests;
- the player README and license notices.

The automatically generated GitHub source ZIP is not the documented player download. Release
notes and the README link directly to the named player asset. Git clone instructions move to a
developer section.

The build performs a clean staging step, validates the staged payload, creates the ZIP, extracts
it into a second clean directory, and runs the installer preflight against the extracted result.
Publication is blocked if a DLL is an LFS pointer, is missing, lacks a PE `MZ` header, has the
wrong size or digest, or if any required runtime or asset file is absent.

## Discovery and selection

Discovery follows CrossOver's registered bottle information rather than treating
`~/Library/Application Support/CrossOver/Bottles` as the complete world. The implementation
also scans the default root and follows bottle symlinks. An explicit game path remains available
as a fallback, but it must resolve the actual containing bottle root and bottle identity instead
of deriving `..` from a path outside the default root.

The installer verifies that every candidate contains `ostriv.exe`, a bottle configuration, and
the required CrossOver command surface. Zero candidates produce one actionable error. One is
selected automatically. Multiple candidates use the existing keyboard menu and show only bottle
name, Ostriv version when known, and a shortened location.

## Payload preflight

All payload files are validated before the first destination mutation. Validation checks:

1. existence and regular-file type;
2. absence of the Git LFS pointer prefix;
3. PE `MZ` header for DLLs;
4. exact size from `payload-manifest.json`;
5. exact SHA-256 digest from the manifest.

Any failure stops installation and identifies the release download as incomplete or corrupt. The
player is told to download the release ZIP again; Git LFS instructions are not part of the player
recovery path.

## Transactional installation

Before applying changes, the installer constructs a complete operation plan and verifies source
files, destination permissions, bottle configuration, registry tooling, settings location, and
launcher destination.

Operations are journaled. Existing genuine files are backed up once, new files are staged on the
destination filesystem, and replacements use atomic rename where possible. Required registry and
configuration changes are applied and verified rather than treated as best-effort warnings. If a
required step fails, completed steps roll back in reverse order and the command exits nonzero.

Install and Reinstall converge on the same final state. Restore uses the ownership journal and
backups to remove only project-owned artifacts, restore genuine originals, restore bottle
configuration, and remove the generated launcher. Restore is safe to repeat.

A post-install verification confirms payload digests at the destination, registry override,
bottle environment, safe graphics settings, launcher runtime/configuration, and application
wrapper before printing success.

## Launcher state machine

The launcher performs the following states in order:

1. **Lock:** acquire a per-bottle non-blocking lock. A second invocation displays
   “Ostriv is already starting or running” and exits successfully without starting another
   process.
2. **Log:** create the launcher log before calling CrossOver, Wine, Steam, or ColorSync.
3. **Recover:** if a previous run left a display-profile recovery marker, restore that profile
   before doing anything else.
4. **Probe Steam:** collect process, login, and UI/helper readiness signals. Being merely present
   is never treated as ready.
5. **Start Steam:** open the matching CrossOver Steam application once when Steam is absent.
6. **Wait:** if Steam was started or is transitioning, require all readiness signals to remain
   stable for 15 seconds. An already warm client must pass two consecutive probes two seconds
   apart. The total readiness timeout is five minutes.
7. **Explain:** while waiting, show at most one macOS notification. If login is required or the
   timeout expires, do not launch Ostriv; show one concise dialog with the required action.
8. **Switch profile:** record the original display profile in a recovery marker, then switch to
   sRGB.
9. **Launch:** record the current end offset of the Ostriv log, then run the game once.
10. **Classify:** inspect only log content written by this launch. If the process exits and the new
    content contains `SteamAPI_Init() failed`, continue probing Steam for 30 seconds and retry the
    game exactly once. No other error receives an automatic retry.
11. **Restore:** restore the original display profile in `finally`, `atexit`, SIGINT, and SIGTERM
    paths, then remove the recovery marker. A hard kill is repaired by the next launch.
12. **Unlock:** record the final state and release the lock.

Subprocess text decoding uses replacement for invalid byte sequences anywhere CrossOver, Wine,
registry, Spotlight, or launcher output is consumed. Parsing relies only on stable ASCII keys.

## Player-facing output contract

Terminal output is a product surface, not a debug stream. The default output follows these rules:

- Print the product title once.
- Print at most one line per stage: discovery, package check, installation, verification.
- Use short menu labels (`Install`, `Reinstall`, `Restore`) and show each explanation once.
- Do not print a large decorative banner.
- Use one stable vocabulary: `OK`, `FAILED`, and `WARNING`; do not mix synonyms such as
  “complete,” “done,” and “installed” for the same outcome.
- Print the final outcome once.
- Print next steps once, only after success.
- Never repeat the same warning or instruction in the stage output and summary.
- Never print raw command output, Python tracebacks, retry chatter, full manifests, or every copied
  filename by default.
- Consolidate optional warnings into one short block after the outcome.
- Put the actionable sentence before the log path.
- Shorten home-directory paths with `~` and avoid exposing implementation-only paths.
- Use color only for an interactive terminal and keep the text understandable without color.
- Return a nonzero exit code for every failed required operation.

The normal success shape is:

```text
Ostriv for macOS
Found: CrossOver 26.2 · Ostriv 0.5.9.60 · My Bottle
Package: OK
Installation: OK

Ready. Quit and reopen CrossOver once, then open Ostriv (patched).
Log: ~/Library/Logs/ostriv-macos/install.log
```

The normal failure shape is:

```text
Ostriv for macOS
Package: FAILED

The download is incomplete. Download the release ZIP again.
Log: ~/Library/Logs/ostriv-macos/install.log
```

Developer detail remains available in the log rather than through a verbose player workflow. The
launcher uses the same message catalog for its macOS dialogs so terminal and dialog wording do not
drift.

## Diagnostics

Installer logs live under `~/Library/Logs/ostriv-macos/install.log`. Launcher logs use a stable,
filesystem-safe bottle identifier under the same directory. Logs include timestamps, stage/state
transitions, resolved versions and paths, validation results, commands without shell expansion,
return codes, decoded output, rollback actions, recovery actions, and relevant Ostriv log markers.

Expected failures become typed internal errors with one player message and one detailed log entry.
Unexpected exceptions are caught at the entrypoint: the terminal receives a short unexpected-error
message and log path, while the traceback is recorded only in the log.

`python3 patch.py --diagnose` is read-only. It prints a compact summary of Python, CrossOver,
bottles, Ostriv installations, payload validity, installation state, launcher state, and log paths.
It does not mutate files, start processes, access the network, or upload data.

## Automated verification

Tests use temporary directories, dependency injection, and fake CrossOver/Wine/Steam commands so
installer and launcher behavior is deterministic. The matrix covers:

- valid payloads, LFS pointers, truncation, invalid PE files, size mismatch, and digest mismatch;
- CrossOver in user, system, and custom locations;
- default, symlinked, external, missing, and multiple bottles;
- paths and bottle names containing spaces and non-ASCII characters;
- Install/Reinstall/Restore repetition and convergence;
- injected failure after each mutation and exact reverse rollback;
- non-UTF-8 subprocess and registry output;
- Steam stopped, starting, warm, logged out, stalled, and timed out;
- concurrent launch attempts, stale locks, and hard-termination recovery markers;
- exact one-time retry for a new Steam API failure and no retry for unrelated failures;
- display-profile restoration on every handled exit path;
- generated launcher execution against fake external processes;
- concise terminal snapshots that reject duplicate lines, raw tracebacks, and repeated guidance;
- release ZIP extraction followed by payload and CLI smoke checks.

Pure tests run on every change. macOS CI runs macOS-specific discovery, ColorSync boundary tests,
launcher materialization, and release assembly. A tagged release cannot publish unless the unpacked
player artifact passes its validation and smoke suite. Community members are not required to run
raw test procedures as a release gate.

## Migration and compatibility

Reinstall replaces the legacy generated launcher script with the packaged launcher runtime and
configuration while preserving the existing **Ostriv (patched)** application name and icon. It
removes obsolete launcher pieces only after the new launcher passes verification. Existing driver
backups remain authoritative for Restore.

The first hardened release retains compatibility with the currently demonstrated Apple Silicon,
CrossOver 25/26, and Ostriv Alpha 5 layouts, while treating unknown layouts as diagnosable failures
rather than silently applying partial configuration.

## Out of scope

- Native graphical installer or settings application
- Runtime driver downloads or automatic updater
- Telemetry, crash uploads, or remote log collection
- Intel Mac support
- Modifying Steam, enabling its overlay, or launching through Steam's Play button
- Rebuilding Mesa as part of player installation
