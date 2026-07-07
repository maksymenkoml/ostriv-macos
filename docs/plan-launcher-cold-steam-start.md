# Plan — Bug B: cold Steam start never launches the game

> Parked plan for a **separate PR** (not the per-user CrossOver-detection PR). No code from
> this document has been implemented yet.

## Symptom

When Steam is **not already running**, double-clicking the generated **Ostriv (patched)**
launcher starts Steam but the game window never appears. Relaunching the launcher *after*
Steam has fully loaded works. Reproduces on the reference machine (M5 Max, CrossOver 26.2).

## Where the code is

The generated bottle-side launcher script (`play-ostriv-patched.py`, produced from the
`LAUNCHER_SCRIPT` string template in `patch.py`). Relevant functions:

- `start_steam()` — `patch.py` ~L432-459
- `main()` — `patch.py` ~L462-470
- `create_play_launcher()` — `patch.py` ~L550-565 (writes the template via `.format(...)`)
- `materialize_launcher_app()` — `patch.py` ~L498-537 (builds the launcher `.app` and its
  MD5-scheme `CFBundleIdentifier`)

## Two candidate root causes (both hardened — log wasn't captured)

The user saw no window and did not capture `log.txt`, so the signature can't single out one
cause. Both fixes below are **additive** (they add focus-restore and a stronger readiness gate;
they remove nothing). Apply both, then confirm which one mattered via the log.

### Cause 1 — foreground-focus steal (leading hypothesis)

`start_steam()` runs `open <steam app>` (L442), which brings **Steam** to the macOS
foreground. When `GAME_CMD` runs shortly after (L467), the frontmost GUI app is Steam's Menu
Helper, not Ostriv's. Per CLAUDE.md's hard rule, winemac with no foreground GUI app posts quit
and the game self-exits at the menu. On relaunch, `steam_running()` is already true, so
`start_steam()` returns immediately at L438 without any `open` — Ostriv's helper stays frontmost
and the game launches. This is fully consistent with "relaunch works."

**Fix:**
- Open Steam without stealing focus: `open -g <app>` (background) at L442.
- Immediately before `subprocess.run(GAME_CMD)`, re-activate Ostriv's own launcher so winemac
  sees a foreground GUI app:
  `subprocess.run(["open", "-b", LAUNCHER_BUNDLE_ID])` followed by a short settle (~1-2s).
- `LAUNCHER_BUNDLE_ID` is the same MD5-scheme identifier built in `materialize_launcher_app`
  (`com.codeweavers.CrossOverHelper.<MD5(bottle)>.<MD5(menu name)>`, uppercase — L527-529).
  Interpolate it into the `LAUNCHER_SCRIPT` template from `create_play_launcher` so it always
  matches the generated `.app`.

### Cause 2 — Steam not ready

The wait loop (L454-459) breaks as soon as Steam's `ActiveProcess\ActiveUser` registry value is
nonzero, but on a **cold start** (client self-update, login window) Steam may write that key
before it can service `SteamAPI_Init`; the fixed `time.sleep(5)` settle (L459) is then too
short and the game exits during init.

**Fix:** after the `ActiveUser` break, strengthen the readiness gate — e.g. extend the settle
and/or re-verify `steam_running()` remains true for a couple of consecutive polls — instead of
proceeding on the first `ActiveUser`-nonzero read alone.

## Files to modify

- `patch.py` — the `LAUNCHER_SCRIPT` template (`start_steam`/`main`, ~L432-470) and
  `create_play_launcher` (~L550-565) to interpolate `LAUNCHER_BUNDLE_ID`.

No revert or deletion of existing behavior — both changes are additive.

## Verification

1. Reinstall so the launcher is regenerated; confirm the bottle-side `play-ostriv-patched.py`
   contains `LAUNCHER_BUNDLE_ID`, the `open -g` for Steam, and the pre-`GAME_CMD` re-activate
   call.
2. **Key repro:** fully quit Steam, then double-click **Ostriv (patched)** *once*. The game
   window should appear on this first launch — no second relaunch. Confirm Ostriv's window is
   frontmost after Steam finishes loading.
3. Read `<bottle>/drive_c/users/crossover/Saved Games/Ostriv/log.txt`: healthy =
   `4.3 (Core Profile) Mesa` → `uiMainMenu`. If it recurs, the log discriminates the cause
   (reached-menu-then-quit = focus steal; early exit at init = Steam readiness).
4. Regression: with Steam already running, the launcher still works (unchanged path).
