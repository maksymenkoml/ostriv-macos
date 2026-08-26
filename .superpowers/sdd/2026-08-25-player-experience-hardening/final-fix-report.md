# Final player-safety fix report

Date: 2026-08-26

Base: `ca95c8ac5fca74f71e0731b8168f8c00dbf85b4e`

Tracked implementation range: `ca95c8ac5fca74f71e0731b8168f8c00dbf85b4e..HEAD`

Final commit: `fix: close final player safety gaps`

## Outcome

All nine Important final-review findings are closed. The final focused safety matrix passes 194
tests, the complete suite passes 264 tests, the release is reproducible byte-for-byte, and the
final player ZIP passes bounded extraction plus fresh Git-less normal- and Python-only-PATH
preflight/diagnosis. No live Install, Reinstall, Restore, bottle mutation, Steam/Ostriv launch,
network access, or display-profile change was used during verification.

## Finding dispositions and TDD evidence

### 1. Launcher runtime/config symlink rollback safety — closed

Production disposition:

- Launcher snapshots now keep the lexical parent and leaf and record `file`, `symlink`, or
  `absent` explicitly instead of resolving the leaf through a target.
- Preflight uses `lstat`/`lexists` for the app, runtime, config, recovery leaves, and every
  pending/backup leaf. Runtime/config symlinks and occupied pending leaves are rejected before a
  transaction mutation.
- The launcher undo handler restores an allowed symlink snapshot lexically and never treats its
  target as an owned path.

RED command:

`python3 -m unittest tests.test_launcher_install.LauncherInstallerTests.test_runtime_and_config_symlinks_survive_late_menu_failure_lexically tests.test_launcher_install.LauncherInstallerTests.test_runtime_and_config_pending_symlink_leaves_are_reserved -v`

RED result: existing runtime/config aliases reached the later menu path and were mutated, while
dangling pending aliases were accepted. The expected ownership failures and unchanged lexical
leaves failed.

GREEN evidence: both named regressions pass; the expanded reserved-leaf matrix and complete
launcher-install module pass.

### 2. Symlinked settings ancestor escape — closed

Production disposition:

- Install and Restore validate every existing settings ancestor as a real non-symlink directory.
- Settings reads, atomic creation, replacement, unlink, backup, rollback, and legacy Restore use
  descriptor-relative operations with no-follow directory and leaf opens.
- Missing directory chains are created below the canonical bottle descriptor and durably synced;
  lexical and canonical ownership remain rooted in the selected bottle.

RED command:

`python3 -m unittest tests.test_installer.InstallerTests.test_install_rejects_missing_settings_below_symlinked_ancestor_before_mutation tests.test_installer.InstallerTests.test_restore_rejects_symlinked_settings_ancestor_before_legacy_mutation -v`

RED result: Install followed `Saved Games` and created the template outside the bottle; Restore did
not fail at the pre-mutation boundary. The outside-tree and journal/state assertions failed.

GREEN evidence: both RED regressions pass. The additional owned-state regression
`test_owned_restore_rejects_missing_settings_behind_symlinked_ancestor` proves an installed patch,
state file, and absent journal remain unchanged while the outside tree stays empty.

### 3. Restore coordination with launcher profile marker/lock — closed

Production disposition:

- Install creates a 256-bit ownership token, durably reserves the per-bottle launcher lock, reserves
  the marker path, and records the paths/digests/token in launcher state and artifact inventory.
- Install rejects unowned preexisting regular or symlink marker/lock leaves unchanged.
- Restore validates state and both leaves, acquires the runtime's same advisory lock, lazily loads
  the macOS profile backend only for a valid marker, restores the exact `str`/`None` profile, and
  removes only the still-owned marker and lock.
- Runtime lock opens are no-follow and require a single-link regular file.
- A hard kill after the journaled lock unlink is recoverable: an exact pending Restore journal
  snapshot, token, and digest can exclusively and durably recreate that lock before reacquisition
  and rollback. No unproved lock is recreated.

Initial RED command:

`python3 -m unittest tests.test_installer.InstallerTests.test_restore_recovers_exact_profile_and_removes_owned_marker_and_lock tests.test_installer.InstallerTests.test_restore_preserves_invalid_or_unowned_launcher_recovery_artifacts -v`

Initial RED result: construction failed because the integration had no profile backend boundary;
Restore neither coordinated the launcher lock nor safely consumed owned recovery state.

Audit RED commands and results:

- `python3 -m unittest tests.test_installer.InstallerTests.test_restore_restart_recovers_journaled_lock_unlink_before_reacquiring -v` — retry failed with
  `restore.launcher_recovery` because lock acquisition preceded recovery of its journaled unlink.
- `python3 -m unittest tests.test_launcher_runtime.ProcessLockTests.test_lock_open_never_follows_a_substituted_symlink -v` — the lock followed the alias and created the external target.

GREEN evidence: exact profile recovery and owned-artifact removal pass; invalid marker owner and
invalid lock content preserve marker/lock/state and perform no profile call; hard-kill restart,
unowned Install leaves, and no-follow runtime lock tests all pass.

### 4. Crash durability for nested writes/restores — closed

Production disposition:

- Atomic byte/JSON writes durably publish newly created parent chains and fsync the destination
  directory after replacement.
- Durable replace/unlink/rmdir helpers cover all installer mutation callers, including legacy
  app-id, diagnostic, and settings removal. Moves sync both source and destination directories.
- Descriptor-relative settings operations fsync the containing directory after create, replace,
  or unlink. Atomic copy syncs the staging source directory and destination directory.
- Restore state/journal ordering remains recoverable when a directory sync reports failure.

Initial RED command:

`python3 -m unittest tests.test_transaction.TransactionTests.test_atomic_bytes_syncs_each_new_parent_and_the_published_leaf_directory tests.test_transaction.TransactionTests.test_directory_sync_failure_rolls_back_atomic_player_write_recoverably tests.test_installer.InstallerTests.test_restore_directory_sync_failure_keeps_installed_state_recoverable -v`

RED result: nested parent sync calls were absent and the injected post-replace/Restore directory
sync boundaries were never reached.

Audit RED command:

`python3 -m unittest tests.test_installer.InstallerTests.test_legacy_unlink_directory_sync_failure_restores_removed_app_id -v`

Audit RED result: the legacy unlink completed without reaching the containing-directory sync.

GREEN evidence: all four regressions pass. The Restore injection identifies the actual
descriptor-relative settings directory by device/inode, so it tests the production mutation
boundary rather than a call ordinal.

### 5. Terminal game launch failures — closed

Production disposition:

- Fresh log evidence is classified before command status. Only attempt one's fresh SteamAPI marker
  enters the approved retry.
- A second SteamAPI marker, graphics-context marker, or any nonzero/unknown command result raises
  bounded typed `game_failed` detail.
- The installed configuration supplies one concise player action:
  `Ostriv could not start. Quit and reopen CrossOver, then try again.`
- The existing `finally` boundary restores the exact profile and releases the lock; `main` owns the
  single configured dialog and keeps stdout/stderr empty.

RED command:

`python3 -m unittest tests.test_launcher_runtime.LauncherOrchestrationTests.test_terminal_steam_graphics_and_nonzero_results_are_typed_and_clean_up tests.test_launcher_runtime.LauncherOrchestrationTests.test_first_steam_marker_is_classified_before_nonzero_result_and_retries_once -v`

RED result: terminal SteamAPI, graphics, and nonzero results returned success rather than raising
`game_failed`.

GREEN evidence: second-attempt SteamAPI, graphics, nonzero with/without markers, first-attempt retry
precedence, profile restoration, lock release, configured-dialog mapping, and silent main tests pass.

### 6. Overwritten Ostriv log detection — closed

Production disposition:

- The launcher captures a bounded generation token containing device/inode/size/mtime plus at most
  128 KiB of head/tail content evidence.
- Fresh reads distinguish untouched, append-only, truncate, recreate, and in-place overwrite paths.
  Returned evidence is capped at 256 KiB and decoded tolerantly.
- An untouched stale marker returns empty and cannot trigger another retry.

Initial RED command:

`python3 -m unittest tests.test_launcher_runtime.LaunchLogTests.test_generation_reader_handles_append_truncate_recreate_and_in_place_overwrite tests.test_launcher_runtime.LaunchLogTests.test_generation_reader_returns_empty_for_untouched_stale_marker -v`

Initial RED result: `capture_log_generation` did not exist and the old suffix-offset model could not
represent overwrite/recreate generations.

Audit RED command:

`python3 -m unittest tests.test_launcher_runtime.LaunchLogTests.test_generation_token_reads_only_bounded_content_evidence -v`

Audit RED result: token capture read 327,680 bytes for a 262,144-byte fixture, exceeding the
131,072-byte evidence cap.

GREEN evidence: `LaunchLogTests` passes 5/5, including append, truncate, recreate, same-size
coarse-mtime overwrite, untouched stale evidence, invalid UTF-8, and bounded token reads.

### 7. Selected-bottle Steam app/readiness scope — closed

Production disposition:

- Launcher configuration persists canonical bottle realpath and exact CrossOver BottleID tag.
- Steam helper selection requires both bottle display name and tag.
- Steam process and renderer probes include the escaped selected canonical bottle scope; the Wine
  ActiveUser query already uses the selected bottle argument and managed scope.
- Cold/warm/retry timers and the one-open/one-notification behavior are unchanged.

RED command:

`python3 -m unittest tests.test_launcher_runtime.SteamControllerTests.test_steam_app_requires_matching_bottle_name_and_tag tests.test_launcher_runtime.SteamControllerTests.test_process_and_renderer_probes_include_selected_canonical_bottle_scope -v`

RED result: same-named wrong-tag apps were eligible and global process/renderer results could mark
another bottle ready.

GREEN evidence: wrong-tag app selection is false with zero open calls; other-bottle process and
renderer signals remain false even when the global fake would return ready. All readiness timing
tests pass unchanged.

### 8. Explicit managed path identity — closed

Production disposition:

- Explicit resolution accepts the discovered Bottle inventory and intersects the enclosing
  canonical bottle root before synthesizing any identity.
- A match preserves discovered name, root, managed/private scope, owning CrossOver, command bottle,
  tag source, and managed scope arguments. Only unmatched validated external roots synthesize a
  private absolute identity.
- Diagnose and production CLI discovery build/pass the inventory for explicit selection.

RED command:

`python3 -m unittest tests.test_discovery.DiscoveryTests.test_explicit_managed_game_preserves_discovered_bottle_identity tests.test_discovery.DiscoveryTests.test_explicit_inventory_selects_the_crossover_that_discovered_the_root -v`

RED result: the resolver rejected the inventory argument and always synthesized a private bottle
owned by the first CrossOver.

GREEN evidence: both discovery intersections pass, full discovery passes 21/21, and
`test_explicit_managed_path_keeps_managed_cli_command_identity` proves the CLI retains
`--scope managed`.

### 9. Restrictive launcher directory modes — closed

Production disposition:

- Captured trees and moved previous apps are materialized with temporary owner read/write/execute
  permissions.
- Children, files, and symlinks are fully restored and their directories synced before recorded
  modes are applied deepest-first, including the root. Final directory descriptors are fsynced.
- Inventory removal temporarily opens restrictive owned directories before child deletion.

RED command:

`python3 -m unittest tests.test_launcher_install.LauncherInstallerTests.test_restore_recreates_restrictive_previous_directory_modes_deepest_first tests.test_launcher_install.LauncherInstallerTests.test_launcher_transaction_rollback_recreates_restrictive_tree_exactly -v`

RED result: normal Restore and rollback raised `PermissionError` while trying to populate children
below recorded 0555/0500 parents.

GREEN evidence: both paths restore exact nested bytes, symlinks, empty directories, and final root
and nested modes.

## Changed tracked files

- `.superpowers/sdd/2026-08-25-player-experience-hardening/final-fix-report.md`
- `ostriv_macos/cli.py`
- `ostriv_macos/discovery.py`
- `ostriv_macos/installer.py`
- `ostriv_macos/launcher.py`
- `ostriv_macos/launcher_runtime.py`
- `tests/test_cli.py`
- `tests/test_discovery.py`
- `tests/test_installer.py`
- `tests/test_launcher_install.py`
- `tests/test_launcher_profile.py`
- `tests/test_launcher_runtime.py`
- `tests/test_transaction.py`

`dist/ostriv-macos-player.zip` is deliberately ignored and not committed.

## Verification results

- Starting HEAD: exact `ca95c8ac5fca74f71e0731b8168f8c00dbf85b4e`; baseline full suite:
  238 tests, OK.
- `python3 -m compileall -q patch.py ostriv_macos scripts tests`: exit 0, no output.
- Focused production-boundary matrix (`installer`, `launcher_install`, `launcher_profile`,
  `launcher_runtime`, `discovery`, `transaction`): 194 tests, OK in 2.546s.
- Exact CLI/dialog matrix (`tests.test_cli` plus `LauncherMainTests`): 43 tests, OK.
- Release/documentation/extraction/workflow matrix (`tests.test_release -v`): 14 tests, OK.
- Final `python3 -m unittest discover -s tests -v`: 264 tests, OK in 5.350s.
- Release builder run twice from the final source: both exits 0; `cmp -s` exits 0, proving complete
  byte-for-byte equality.
- Production `safe_extract` into a nonexistent fresh Git-less destination: exit 0; the bounded and
  hostile archive matrix also passes all four `SafeExtractTests`.
- Fresh extracted normal PATH: `patch.py --preflight` exit 0 and silent;
  `patch.py --diagnose` exit 0 with one concise read-only summary.
- Fresh extracted Python-only PATH (only a `python3` link available): preflight exit 0 and silent;
  diagnose exit 0 with the same concise read-only summary.
- Workflow YAML parse with Ruby for `.github/workflows/ci.yml` and
  `.github/workflows/release.yml`: exit 0, no output.
- Archive scan rejects `.git/` and `.build/`: exit 0.
- Anti-pattern scan `text=True` without tolerant decoding: exit 1, no matches.
- Anti-pattern scan fixed-root/embedded launcher constants: exit 1, no matches.
- Anti-pattern scan forbidden bottle-list, Steam identity, and background/bundle open forms: exit 1,
  no matches.
- Production network/import scan (`requests|urllib|curl|wget|git clone|SteamAppId|SteamGameId`):
  exit 1, no matches.
- `git diff --check`: exit 0, no output.
- Final tracked `git status --short` after commit: no output. The ignored ZIP remains at
  `dist/ostriv-macos-player.zip`.

## Final artifact

- Path: `dist/ostriv-macos-player.zip`
- Size: 12,470,657 bytes
- SHA-256: `fee8a21ff663133669b2a73a02b9aba0a1801ec7dc57079b6b1d7bd14b10383d`
- Entries: exactly 18

Inventory:

1. `LICENSE`
2. `README.md`
3. `assets/settings.data`
4. `ostriv_macos/__init__.py`
5. `ostriv_macos/cli.py`
6. `ostriv_macos/diagnostics.py`
7. `ostriv_macos/discovery.py`
8. `ostriv_macos/installer.py`
9. `ostriv_macos/launcher.py`
10. `ostriv_macos/launcher_runtime.py`
11. `ostriv_macos/payload.py`
12. `patch.py`
13. `payload-manifest.json`
14. `prebuilt/README.md`
15. `prebuilt/dxil.dll`
16. `prebuilt/libgallium_wgl.dll`
17. `prebuilt/libwinpthread-1.dll`
18. `prebuilt/opengl32.dll`

## Deviations and residual risks

No requested verification or production outcome was skipped. The rejected stage-line finding and
deferred Minors were intentionally left untouched.

No live CrossOver mutation or launcher/game/profile operation was exercised, by design; extracted
diagnosis performed only its required read-only discovery. Mutating/runtime tests use temporary
bottles, fake external processes, and an injected profile backend. A log larger than the bounded
generation-token budget is represented by head/tail evidence; a same-inode overwrite confined
entirely to an unsampled middle region with unchanged metadata is inherently indistinguishable
without violating that bound. A nonzero launch result remains terminal, but a zero result with a
failure marker only in that pathological region could lack marker evidence. Selected-bottle process
matching was verified against command patterns/fakes rather than a live CrossOver process table;
absence of the canonical scope produces a safe readiness false negative instead of accepting
another bottle.
