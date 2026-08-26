# Task 5 report — transactional installer and Restore

## Status

DONE

Commit: `d3bc9ee feat: make install and restore transactional`

## Implemented

- Added the complete dependency-injected installer layer to `ostriv_macos/installer.py` while
  preserving the reviewed Task 4 transaction interfaces.
- Added exact bottle environment values, scoped Wine registry access, one retry for each failed
  required registry mutation, and query verification after successful registry writes/deletes.
- Added preflight checks for payload validity, destination/config/settings writeability,
  `system.reg`, Wine, `cxbottle`, `cxmenu`, Menu Helper, launcher destination, and bottle status.
  Managed bottles use name plus managed scope; external private bottles use their absolute root
  without managed scope.
- Added journaled driver staging, genuine-file backups, game-scoped app-id file handling,
  executable-scoped registry override, targeted environment mutation, safe graphics mutation,
  launcher-port orchestration, post-install verification, and atomic ownership-state writing.
- Added schema-1 `InstallState` with project version, resolved bottle/game identity, owned and
  backup files, prior registry value, config/settings original and installed digests/backups,
  launcher artifacts, and stable verification completion time.
- Added concrete idempotent undo handlers for `remove_path`, `restore_file`, `restore_registry`,
  `restore_config`, `restore_settings`, and `restore_launcher`. Handlers require matching ownership
  metadata and known digests, constrain claims to the selected bottle/game/launcher roots, and
  preserve unknown replacements.
- Added journal-driven Restore with reverse replay, installed-state rollback on Restore failure,
  verification before ownership-state removal, incomplete-operation recovery, and conservative
  legacy migration for recognizable `.bak` files, driver files, app-id file, diagnostic logs,
  exact known environment values, settings, registry override, and launcher artifacts.
- Lower layers remain silent. Runtime source contains neither forbidden bottle-wide Steam variable
  identifiers nor the legacy global bottle-root constant.

## TDD evidence

### Initial RED

Command:

```text
python3 -m unittest tests.test_installer -v
```

Outcome before production changes: exit 1. `tests.test_installer` failed to import because
`ostriv_macos.installer` did not provide the new installer contract (`ImportError: cannot import
name 'BOTTLE_ENV'`). This was the expected missing-feature failure.

### Focused RED/GREEN cycles added during implementation and self-review

- Initial fake-bottle integration implementation reached 17/17 installer tests passing.
- The new dynamic Restore failure matrix then failed for injected mutations 2 through 10 because
  restored originals were not accepted as the safe current value during rollback. Snapshot undo
  metadata was tightened; the focused Restore matrix then passed.
- Registry Restore tests exposed `install.registry` leaking from a failed prior-value restore and
  missing legacy launcher delegation. Both focused tests failed first, then passed with the
  restore-specific error boundary and journaled legacy launcher restore.
- The interrupted-install restart test failed first with `install.ownership_conflict` after an
  applied ownership-state write. Reloading ownership state after incomplete-journal recovery made
  the test pass.
- The out-of-root ownership-state test failed first because Restore accepted the injected path.
  Semantic absolute-path/root validation made it pass without touching the victim.
- The collateral config/settings verification test failed first because safe targeted values alone
  still passed. Comparing full installed digests made both subcases pass.

### Final GREEN

Command:

```text
python3 -m unittest tests.test_transaction tests.test_installer -v
```

Outcome: exit 0, 39 tests run, all passed, no warnings or stray output.

The installer suite dynamically measured the successful install and Restore transaction record
counts and injected a typed failure after every actual journaled mutation; it does not cap either
matrix at seven.

## Full verification

```text
python3 -m unittest discover -s tests -v
```

Exit 0: 74 tests run, all passed, output pristine.

```text
python3 -m py_compile ostriv_macos/installer.py tests/test_installer.py
```

Exit 0 with no output.

```text
rg -n 'SteamAppId|SteamGameId|BOTTLES_ROOT' ostriv_macos/installer.py
rg -n -F 'print(' ostriv_macos/installer.py
```

Both exited 1 with no matches, which is the expected clean scan.

```text
git diff --check
```

Exit 0 with no output.

## Files changed

- `ostriv_macos/installer.py`
- `tests/test_installer.py`

## Self-review

- Completeness: checked every Task 5 brief step and all five controller rulings. Install order is
  unchanged apart from refreshing ownership state after recovery and before starting the new
  transaction, which is required for crash correctness.
- Safety: every delete/replace path is digest-gated and root-constrained; corrupt state cannot claim
  an arbitrary absolute path; pre-existing matching files are not silently claimed; unrelated
  config/settings bytes and modes are preserved.
- Mutation check: tests catch wrong payload copies/hashes, wrong registry scope/value/retry count,
  missing preflight tools/files, environment value drift, config/settings collateral damage,
  missing rollback, unsafe unknown-file deletion, corrupt/out-of-root state, and legacy overreach.
- Simplification pass: kept helpers local to the assigned module, consolidated snapshot replay for
  concrete undo kinds, and avoided future launcher imports or Task 8 production behavior.

## Concerns

- The production launcher is intentionally not exercised here; Task 8 must implement
  `LauncherPort` and supply verified runtime/config/plist/icon state. Task 5 uses only the
  deterministic fake required by the brief.
- No real CrossOver process or bottle was mutated during tests. Tool invocation, scope, retries,
  non-UTF-8 diagnostics, state, and filesystem behavior were exercised against the fake bottle.
- The CLI/nonzero process outcome boundary belongs to Task 9; Task 5 supplies typed `PatchError`
  failures and never returns a success result after required-operation failure.

---

## Fix round 1/5 — Critical and Important review findings

Commit: `c5a417f fix: harden transactional installer recovery`

### Addressed findings

1. Driver and backup copies now stage to sibling temporary files, copy and preserve metadata,
   flush/fsync the staged file, atomically replace the destination, fsync the parent directory,
   and clean the temporary file on every failure path. The injected failure occurs at the real
   driver `os.replace` boundary and proves the genuine destination remains byte-for-byte intact.
2. Ownership-state validation now uses exact driver/app-id/settings and launcher artifact
   inventories plus exact sibling backup relationships. An unrelated in-root `ostriv.exe` claim
   is rejected as `restore.state_corrupt` before mutation.
3. Changed-payload reinstall keeps the persistent genuine-backup digest while a per-operation
   snapshot restores the previously installed payload on rollback. Successful Restore returns the
   original genuine DLL.
4. Registry query retries true command/query failures, recognizes only explicit missing-value
   output as absence, parses the complete data column (including spaces), and makes delete
   verification failures fatal rather than treating them as absence.
5. Preflight enforces the complete exact five-entry payload inventory before transaction creation.
6. Template settings installation records every newly created settings parent. Install rollback
   and successful Restore remove only those exact, empty, owned directories.
7. Legacy environment cleanup parses configuration sections and removes exact known values only
   from `[EnvironmentVariables]`.

### Strict TDD evidence

#### Finding 1 RED/GREEN

RED command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_driver_copy_failure_before_replace_preserves_genuine_destination -v
```

RED outcome: exit 1; `AssertionError: OSError not raised`, proving driver installation performed no
atomic replace boundary for the injected in-window failure.

GREEN: the same command exited 0; 1 test passed. The failure is now injected before the driver
replace, the genuine DLL and entire fixture snapshot are unchanged, and no sibling staging file
remains.

#### Finding 2 RED/GREEN

RED command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_state_cannot_claim_unrelated_file_inside_game_directory -v
```

RED outcome: exit 1; `AssertionError: PatchError not raised`; the broad root check accepted an
`ostriv.exe` ownership claim.

GREEN: the same command exited 0; 1 test passed, with typed corrupt-state rejection and exact tree
preservation.

#### Finding 3 RED/GREEN

RED command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_reinstall_with_changed_payload_keeps_genuine_restore_backup -v
```

RED outcome: exit 1; Restore raised `restore.verify` because the genuine `opengl32.dll` backup no
longer matched the overwritten old-payload digest in state.

GREEN: the same command exited 0; 1 test passed, including successful restoration of the genuine
DLL after installing a changed payload.

#### Finding 4 RED/GREEN

RED command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_registry_preserves_prior_value_containing_spaces tests.test_installer.InstallerTests.test_registry_query_failure_is_typed_and_rolls_back tests.test_installer.InstallerTests.test_delete_verification_query_failure_does_not_pass_as_absent -v
```

RED outcome: exit 1; all 3 tests failed. A spaced value was truncated to `spaces`, a registry query
failure did not raise, and failed delete verification passed as absent.

GREEN: the same command exited 0; 3 tests passed.

#### Finding 5 RED/GREEN

RED command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_missing_required_payload_inventory_fails_before_transaction -v
```

RED outcome: exit 1; the missing driver failed only later as `install.payload`, while missing
settings produced no exception.

GREEN: the same command exited 0; both missing-entry subtests passed with
`install.payload_inventory`, zero transactions, no journal, and an exact original snapshot.

#### Finding 6 RED/GREEN

RED command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_missing_settings_directory_is_removed_on_install_rollback -v
```

RED outcome: exit 1; snapshot equality showed the newly created `Ostriv` settings directory was
left behind.

GREEN: the same command exited 0; 1 test passed with exact tree restoration.

#### Finding 7 RED/GREEN

RED command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_legacy_environment_cleanup_is_limited_to_environment_section -v
```

RED outcome: exit 1; the identical key/value in `[UnrelatedSection]` was missing after cleanup.

GREEN command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_legacy_environment_cleanup_is_limited_to_environment_section tests.test_installer.InstallerTests.test_legacy_restore_migrates_only_recognizable_owned_artifacts -v
```

GREEN outcome: exit 0; 2 tests passed.

### Fix-round verification

```text
python3 -m unittest tests.test_transaction tests.test_installer -v
```

Exit 0: 48 tests passed, output pristine.

```text
python3 -m unittest discover -s tests -v
```

Exit 0: 83 tests passed, output pristine.

```text
python3 -m py_compile ostriv_macos/installer.py tests/test_installer.py
```

Exit 0 with no output.

```text
rg -n 'SteamAppId|SteamGameId|BOTTLES_ROOT' ostriv_macos/installer.py
rg -n -F 'print(' ostriv_macos/installer.py
```

Both exited 1 with no matches, the expected clean result.

```text
git diff --check
```

Exit 0 with no output.

### Fix-round self-review

- Atomic staging is used for both persistent originals and installed driver destinations; failure
  before replace leaves the destination untouched, and failure after replace remains recoverable
  because the destination has a complete known digest.
- Exact state inventory validation rejects arbitrary in-root paths and validates target/backup
  pairs before any Restore transaction begins.
- Reinstall separates persistent original ownership metadata from operation-local rollback state.
- Registry failures retain phase-specific typed codes, and only explicit missing-value results can
  produce `None`.
- Newly owned directories are removed only when exact, recorded, allowed, and empty.
- No deferred Minor or future launcher/CLI implementation was included.

### Fix-round concerns

None. All 2 Critical and 5 Important findings are addressed; the deferred Minor remains outside
this round as instructed.

## Review fix round 2

Changed files in this round:

- `ostriv_macos/installer.py`
- `tests/test_installer.py`
- `.superpowers/sdd/2026-08-25-player-experience-hardening/task-5-report.md`

### Finding 1 RED/GREEN

RED command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_incomplete_copy_journal_removes_owned_staging_file tests.test_installer.InstallerTests.test_successful_driver_copy_has_ordered_staging_cleanup_and_no_artifact -v
```

RED outcome: exit 1; 2 tests failed because no durable `remove_staging` record existed before the
driver mutation, so neither interrupted recovery nor successful ordering could identify an owned
staging path.

GREEN command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_incomplete_copy_journal_removes_owned_staging_file tests.test_installer.InstallerTests.test_successful_driver_copy_has_ordered_staging_cleanup_and_no_artifact tests.test_installer.InstallerTests.test_driver_copy_failure_before_replace_preserves_genuine_destination -v
```

GREEN outcome: exit 0; 3 tests passed. An incomplete journal containing a partial sibling staging
file recovered idempotently, removed the staging artifact, and preserved the genuine driver bytes;
successful and caught-failure paths also left no staging artifact.

### Finding 2 RED/GREEN

RED command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_registry_missing_requires_exact_wine_registry_diagnostic tests.test_installer.InstallerTests.test_registry_delete_rejects_ambiguous_not_found_output -v
```

RED outcome: exit 1; 5 subtest/test failures showed that generic bottle, dependency, and backend
messages containing `not found` or `unable to find` were accepted as registry absence, including
during delete verification.

GREEN command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_registry_missing_requires_exact_wine_registry_diagnostic tests.test_installer.InstallerTests.test_registry_delete_rejects_ambiguous_not_found_output tests.test_installer.InstallerTests.test_delete_verification_query_failure_does_not_pass_as_absent -v
```

GREEN outcome: exit 0; 3 tests passed. Only full-line Wine `reg:` missing-key/value diagnostics
produce absence; ambiguous nonzero results retry once and then raise the phase-specific typed
registry error.

### Round-2 verification

```text
python3 -m unittest tests.test_transaction tests.test_installer -v
```

Exit 0: 52 tests passed, output pristine.

```text
python3 -m unittest discover -s tests -v
```

Exit 0: 87 tests passed, output pristine.

```text
python3 -m py_compile ostriv_macos/installer.py tests/test_installer.py
```

Exit 0 with no output.

```text
rg -n 'SteamAppId|SteamGameId|BOTTLES_ROOT' ostriv_macos/installer.py
rg -n -F 'print(' ostriv_macos/installer.py
```

Both exited 1 with no matches, the expected clean result.

```text
git diff --check
```

Exit 0 with no output.

### Round-2 self-review

- Every atomic file-copy staging path is selected without claiming an existing path, created only
  after its exact removal record is durable, and constrained to an allowed destination or its
  validated backup relationship.
- The staging removal handler is idempotent for pending/applied recovery, rejects paths outside the
  exact sibling naming and destination inventory, and does not remove symlinks or directories.
- Rollback order is target restore/removal first, then staging cleanup; a partial pre-replace copy
  therefore cannot truncate or replace a genuine destination.
- Registry absence recognition is anchored to Wine's structured `reg:` missing-key/value output;
  unrelated dependency, bottle, and backend failures remain typed failures after one retry.
- No deferred Minor or future launcher/CLI implementation was included.

### Round-2 concerns

None. Both remaining findings are addressed; 2 addressed and 0 open for this round.

## Review fix round 3

Changed files in this round:

- `ostriv_macos/installer.py`
- `tests/test_installer.py`
- `.superpowers/sdd/2026-08-25-player-experience-hardening/task-5-report.md`

### Staging trust boundary RED/GREEN

RED command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_symlink_driver_destination_is_rejected_before_journaling tests.test_installer.InstallerTests.test_staging_symlink_substitution_never_follows_or_deletes_symlink tests.test_installer.InstallerTests.test_incomplete_copy_journal_removes_owned_staging_file -v
```

RED outcome: exit 1; 1 failure and 2 errors. A symlink driver leaf reached a journaled copy and
resolved staging beside its external target, a substituted staging symlink was followed rather
than rejected, and recovery records carried no created-file identity.

GREEN: the same command exited 0; 3 tests passed. Unsafe mutable destination symlinks fail
preflight with no transaction or external temp, staging substitution leaves the external victim
and genuine driver byte-exact, the unknown symlink is not deleted, and incomplete-journal recovery
still removes a partial staging file whose recorded device/inode matches.

### Round-3 verification

```text
python3 -m unittest tests.test_transaction tests.test_installer -v
```

Exit 0: 54 tests passed, output pristine.

```text
python3 -m unittest discover -s tests -v
```

Exit 0: 89 tests passed, output pristine.

```text
python3 -m py_compile ostriv_macos/installer.py tests/test_installer.py
```

Exit 0 with no output.

```text
rg -n 'SteamAppId|SteamGameId|BOTTLES_ROOT' ostriv_macos/installer.py
rg -n -F 'print(' ostriv_macos/installer.py
```

Both exited 1 with no matches, the expected clean result.

```text
git diff --check
```

Exit 0 with no output.

### Round-3 self-review

- Preflight checks every mutable driver/app-id/config/settings leaf for symlinks before constructing
  a transaction, and `_install_file()` retains a defensive direct-call rejection.
- Staging preserves the lexical destination leaf while resolving only its parent; exact target and
  backup inventories therefore cannot escape through leaf resolution.
- Each high-entropy sibling stage is created with exclusive/no-follow flags, fsynced, and records
  its device/inode before any payload bytes are copied.
- Reopen, pre-truncate, pre-replace, post-replace, and undo cleanup all require the recorded regular
  single-link identity; symlinks, directories, hard links, and substituted inodes are not followed
  or removed.
- No deferred Minor or future launcher/CLI implementation was included.

### Round-3 concerns

None. The one staging trust-boundary finding is addressed; 1 addressed and 0 open for this round.

## Review fix round 4

Changed files in this round:

- `ostriv_macos/installer.py`
- `tests/test_installer.py`
- `.superpowers/sdd/2026-08-25-player-experience-hardening/task-5-report.md`

### Durable private-stage design

- Each copy now uses a high-entropy `0700` staging directory rather than a mutable sibling file.
  The initial pending journal record names the exact stage directory, its fixed `payload` leaf,
  the destination, and the exact cleanup-handoff directory before filesystem creation begins.
- Exclusive/no-follow creation captures both directory and file device/inode identities. A new
  pending-record checkpoint atomically persists those identities and fsyncs the journal before
  the installer fsyncs the staging file, staging directory, or destination parent. Therefore the
  strict kill point after stage fsync but before `mark_applied` has a pending yet fully identifying
  durable undo record.
- Copying remains descriptor-relative and no-follow. The payload descriptor and private-directory
  descriptor are identity-checked, the payload is fsynced, and descriptor-relative `os.replace`
  moves the owned inode to the destination before the destination directory is fsynced.
- Cleanup never validates a leaf in the mutable destination namespace and then unlinks that path.
  It first atomically hands off the entire stage directory to its journaled recovery name with
  macOS `renamex_np(RENAME_EXCL)`, which has been available since macOS 10.12 and cannot overwrite
  a pre-existing entry. Both rename parents are then fsynced; recovery accepts either side of an
  interrupted handoff.
- Only after the handoff does cleanup open the private directory with `O_DIRECTORY|O_NOFOLLOW`,
  validate its recorded identity, require an exact empty-or-single-payload inventory, and validate
  the payload descriptor identity. Identity mismatch, symlink substitution, or added content is
  preserved in quarantine and is never unlinked. Owned content is removed descriptor-relative,
  with the private directory and cleanup parent fsynced. Normal success, caught rollback, and
  restart recovery leave no owned stage directory or file.

### Strict TDD evidence

RED command (the cleanup test had its pre-GREEN name at this point):

```text
python3 -m unittest tests.test_installer.InstallerTests.test_hard_termination_after_staging_fsync_recovers_the_owned_stage tests.test_installer.InstallerTests.test_staging_cleanup_does_not_delete_a_leaf_substituted_after_check -v
```

RED outcome: exit 1; both tests failed for the intended production defects. Restart recovery left
the stage present after termination between its fsync and `mark_applied`, and cleanup deleted the
unknown regular file substituted after the successful `lstat` identity check.

The atomic-handoff form of the second test was also run against `9e2758a`; it failed because the
old cleanup never performed a directory handoff (`substituted` remained empty). Existing recovery
and successful-copy tests also failed their new private-directory assertions, confirming the old
sibling-file design could not satisfy the new invariant.

GREEN command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_hard_termination_after_staging_fsync_recovers_the_owned_stage tests.test_installer.InstallerTests.test_staging_cleanup_preserves_directory_substituted_at_atomic_handoff -v
```

GREEN outcome: exit 0; 2 tests passed. Recovery removed the fsynced owned stage from a pending
identity-bearing record, while handoff-time substitution preserved exactly one unknown file with
its bytes intact and left the genuine driver unchanged.

### Round-4 verification

```text
python3 -m unittest tests.test_transaction tests.test_installer -v
```

Exit 0: 56 tests passed, output pristine.

```text
python3 -m unittest discover -s tests -v
```

Exit 0: 91 tests passed, output pristine.

```text
python3 -m py_compile ostriv_macos/installer.py tests/test_installer.py
```

Exit 0 with no output.

```text
rg -n 'SteamAppId|SteamGameId|BOTTLES_ROOT' ostriv_macos/installer.py
rg -n -F 'print(' ostriv_macos/installer.py
```

Both exited 1 with no matches, the expected clean result.

```text
git diff --check
```

Exit 0 with no output.

### Round-4 self-review

- Mutation check: removing the pending identity checkpoint leaks the strict kill-window stage;
  returning to pre-handoff path validation deletes the substituted unknown entry; weakening
  directory/file identity checks lets the existing symlink and incomplete-recovery tests fail.
- All copy staging variants (driver backups, driver destinations, config backups, and settings
  backups) use the same private-directory invariant. No launcher/CLI or deferred Minor behavior
  was added.
- The implementation uses only Python standard-library interfaces plus the macOS libc
  `renamex_np` entry point through `ctypes`; its 10.12 availability predates the macOS 12 minimum.

### Round-4 concerns

None. Both remaining staging-safety defects are addressed; 2 addressed and 0 open for this round.

## Review fix round 5

Changed files in this round:

- `ostriv_macos/installer.py`
- `tests/test_installer.py`
- `.superpowers/sdd/2026-08-25-player-experience-hardening/task-5-report.md`

### Atomic payload-capture design

- Cleanup no longer validates the fixed `payload` entry and then unlinks that same mutable name.
  After the directory handoff and recorded directory-identity check, it first uses macOS
  `renameatx_np(RENAME_EXCL)` with the cleanup directory descriptor as both parent descriptors to
  atomically capture the current entry under `.payload-capture-<128-bit-stage-token>`. Only the
  captured entry is then opened without following symlinks, checked against the journaled
  device/inode/link-count identity, and removed.
- The capture name is derived from the already journaled high-entropy stage token. This keeps the
  recovery namespace exclusive and unpredictable without changing the journal schema, and it lets
  an incomplete round-4 journal use the stronger cleanup path after upgrade.
- The capture rename is fsynced before the trust decision. The captured unlink is fsynced before
  directory removal, and the existing cleanup-parent fsync remains last, retaining round 4's
  durability ordering.
- A pre-existing cleanup handoff together with the project-owned source is now a hard recovery
  failure, not a successful no-op. Unknown cleanup content is preserved, the ownership state is
  not committed, and the incomplete journal keeps the exact source identity available for a later
  recovery attempt. Once the external collision is removed, recovery hands off and cleans the
  owned source normally.

### Strict TDD evidence

Payload validation/removal seam RED command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_staging_cleanup_preserves_payload_substituted_after_validation -v
```

RED outcome: exit 1; the focused test substituted `payload` immediately after
`_owned_file_status()` returned successfully, and the old path-based unlink deleted the unknown
replacement (`AssertionError: 1 != 0` preserved copies).

GREEN: the same command exited 0; 1 test passed. The substitution happens only after the owned
entry has been atomically captured under its exclusive high-entropy name, so the unknown bytes at
the former `payload` name remain present and the genuine driver remains unchanged.

Occupied-handoff RED command:

```text
python3 -m unittest tests.test_installer.InstallerTests.test_occupied_staging_handoff_keeps_recoverable_transaction_state -v
```

RED outcome: exit 1; `AssertionError: PatchError not raised` proved install could return success
while both the owned source stage and an unknown cleanup handoff directory remained.

GREEN: the same command exited 0; 1 test passed. Install now fails as
`install.rollback_failed`, writes no ownership state, preserves the unknown bytes and owned source,
and leaves an incomplete journal. After the test removes only its injected unknown collision,
`recover_incomplete()` removes the exact owned stage, preserves the genuine destination, and
completes the journal.

### Round-5 verification

```text
python3 -m unittest tests.test_transaction tests.test_installer -v
```

Exit 0: 58 tests passed, output pristine.

```text
python3 -m unittest discover -s tests -v
```

Exit 0: 93 tests passed, output pristine.

```text
python3 -m py_compile ostriv_macos/installer.py tests/test_installer.py
```

Exit 0 with no output.

```text
rg -n 'SteamAppId|SteamGameId|BOTTLES_ROOT' ostriv_macos/installer.py
rg -n -F 'print(' ostriv_macos/installer.py
```

Both exited 1 with no matches, the expected clean result.

```text
git diff --check
```

Exit 0 with no output.

### Round-5 platform limitation and residual assumption

macOS 12 provides `renameatx_np(RENAME_EXCL)` but no supported API that conditionally unlinks a
directory entry by an already-open file descriptor or expected inode. Consequently, after atomic
capture and descriptor validation, the final unlink still resolves the captured high-entropy name.
The design assumes no same-UID adversarial process discovers the random `0700` cleanup directory
and its capture name and deliberately replaces that entry in the narrow interval between final
`fstat` and `unlink`. Accidental collisions, fixed-name substitution at the former seam, symlinks,
directory replacement, hard links, and occupied handoff names are handled conservatively. A
malicious same-UID actor with continuous filesystem access can still win the final name-lookup
race; the standard macOS 12 interfaces cannot remove that last race without a privileged helper or
a different trust boundary.

### Round-5 self-review

- Mutation check: restoring check-then-unlink at the fixed `payload` name fails the new substitution
  test; returning on a simultaneous source/handoff pair fails the occupied-handoff test by allowing
  a successful state commit.
- The descriptor-relative capture uses only standard library plus the same macOS libc family as
  round 4. It does not change install order, ownership-state schema, driver inventory, launcher/CLI
  behavior, or the deferred Minor finding.
- Both assigned findings are addressed: 2 addressed, 0 open. The unavoidable same-UID limitation
  above is retained as an explicit concern rather than treated as solved.
