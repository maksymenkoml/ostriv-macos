# Task 6 report — launcher runtime/profile recovery

## Evidence

- **RED:** `python3 -m unittest tests.test_launcher_profile -v` before
  `ostriv_macos/launcher_runtime.py` existed failed as expected with
  `ModuleNotFoundError: No module named 'ostriv_macos.launcher_runtime'`.
- **GREEN:** `python3 -m unittest tests.test_launcher_profile -v` passed **9 tests**.
- **Full suite:** `python3 -m unittest discover -s tests -v` passed **102 tests**.
- **Compile:** `python3 -m py_compile ostriv_macos/launcher_runtime.py
  tests/test_launcher_profile.py` passed.
- **Diff whitespace check:** `git diff --check` passed after intent-to-add staging.

## Changed files

- `ostriv_macos/launcher_runtime.py` — standalone stdlib launcher foundation:
  durable same-directory JSON replacement, schema-1 configuration, lazy ColorSync bridge,
  recoverable idempotent profile guard, normal/signal cleanup, and the minimal game runner seam.
- `tests/test_launcher_profile.py` — fake-backend recovery/config/atomic-write/signal tests.

## ColorSync bridge fidelity

- `CGDisplayCreateUUIDFromDisplayID` is loaded from **ColorSync.framework**, while the main
  display ID remains from CoreGraphics.
- Reading uses custom-profile slot **`"1"`** first and `kColorSyncDeviceDefaultProfileID` as the
  fallback.
- Both saved path strings and `None` are applied unchanged on restoration; `None` intentionally
  removes the custom profile and returns to the factory default.
- CoreFoundation, CoreGraphics, and ColorSync frameworks are loaded only in
  `ColorSyncProfileBackend.__init__`, so importing or preflighting the copied runtime does not
  require macOS frameworks.

## Self-review

- Recovery markers are written through a sibling temporary file, file-synced, atomically
  replaced, then directory-synced.
- Malformed markers raise a typed `RuntimeError` and remain in place; failed switching and failed
  restoration retain the original marker for a later launch to recover.
- `restore_once` has both complete and in-progress guards, so `finally`, signal, and `atexit`
  cleanup cannot perform duplicate restoration.
- SIGINT/SIGTERM restore the previous handler before re-signalling the current process. The
  in-handler guard prevents recursive cleanup if the old closure is invoked again.
- The runtime has no project/package-relative imports and performs no direct printing.

## Concerns

None found in the Task 6 scope. The actual macOS framework calls are deliberately not exercised
in this portable test suite; their copied bridge constants and call sequence were checked against
`patch.py` and are lazy by construction.
