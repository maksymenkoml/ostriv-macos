# Bug B: cold Steam start never launches the game

> **Fixed and verified live** — on a cold start (Steam not running) the game now launches on
> the first try to a playable main menu (user-confirmed). The initial fix described below was
> later superseded by the hardened launcher's stable readiness state machine and targeted retry.

## Symptom

With Steam **not** already running, double-clicking **Ostriv (patched)** started Steam but the
game never appeared (or appeared and crashed). Relaunching *after* Steam had fully loaded
worked. Reproduced on the reference machine (M5 Max, CrossOver 26.2).

## Root causes (both confirmed by testing)

### 1. `start_steam()` wait loop never broke → ~5-minute stall

The loop only broke when the Steam `ActiveProcess` registry **pid differed** from the pid read
before launch:

```python
old_pid, _ = steam_state()
...
if pid and pid != old_pid and active_user:  # never true
```

That pid is Steam's *wine-side* pid, which wine often reuses across launches — so it equalled
`old_pid` and the loop ran its full `60 × 3s` (plus a `wine reg query` each iteration ≈ ~5 min)
before proceeding. The user gave up long before then → "never launches." (On a warm second
launch `start_steam()` returns immediately, which is why the 2nd try worked.)

### 2. Launching before Steam is API-ready → crash before the menu

Even once the loop broke, gating on "logged in" (`ActiveUser` nonzero) was **not enough**: a
cold client sets `ActiveUser` seconds before it can service `SteamAPI_Init`. The game called
`SteamAPI_Init`, it failed, and Ostriv **crashed before the main menu** — a wine "Program
Error" dialog that *hangs the process* (so a naive "is the process alive?" check reads a hung
crash as a running game — it is not).

## Initial fix (historical)

Wait until Steam can actually serve the API, then launch. `steam_ready()` requires all three:
client running, login finished (`ActiveUser` nonzero), **and** the CEF UI up (a
`steamwebhelper.exe … --type=renderer` process running — a reliable "client fully up" signal).

```python
def steam_ready():
    if not (steam_running() and steam_state()[1]):
        return False
    return subprocess.run(["pgrep", "-f", "steamwebhelper.exe.*type=renderer"],
                          capture_output=True).returncode == 0

# in start_steam(), after opening Steam:
for _ in range(80):
    if steam_ready():
        break
    time.sleep(3)
time.sleep(8)   # settle once the client's UI is up
```

This removed the reused-pid gate (defect 1), but the readiness predicate was still only a point-in-
time heuristic. The old warm-client early return and this heuristic-only wait are no longer the
current implementation.

## Current hardened behavior

`ostriv_macos/launcher_runtime.py` now owns a deterministic readiness state machine:

1. Acquire a per-bottle advisory lock so a repeated click cannot start another Steam/game path.
2. Probe all three signals without polling Wine: host `steam.exe` and
   `steamwebhelper.exe --type=renderer` processes must both have working directories inside the
   selected bottle, and that bottle's bounded `user.reg` data must report a nonzero `ActiveUser`.
   The file is trusted only when its modification time belongs to the current host Steam process
   generation; otherwise Wine confirms the live value. Logged-out results are checked again on
   the next poll, while positive results are cached for at most ten seconds and only for the same
   process generation and registry-file generation.
   CrossOver's Windows task PIDs are never reused as macOS PIDs; the two namespaces are unrelated.
   Another bottle's process or renderer, a non-renderer helper, process presence, or login alone is
   never ready. Captured process details and bottle paths are omitted from diagnostics.
3. For an already warm client, require two consecutive ready probes two seconds apart.
4. For a stopped or transitioning client, open the matching CrossOver Steam app at most once and
   require all readiness signals to remain stable for 15 seconds. Any dropped signal resets the
   stable interval; the absolute timeout is five minutes. An unavailable or malformed host probe
   is “unknown,” not “Steam absent,” so it never triggers another open.
5. If login or readiness fails, do not launch Ostriv. Show one concise action and keep details in
   the local launcher log.
6. Classify only Ostriv log bytes appended by this launch. A fresh `SteamAPI_Init() failed` marker
   receives one 30-second readiness pass and exactly one game retry. Graphics-context and unrelated
   failures never retry. Ostriv's fresh `done exiting.` marker is a clean session end even when
   CrossOver's wrapper returns status 1 after the player quits.

The runtime also creates the log before external adapters, restores a stale display-profile marker
before Steam work, restores the exact original profile on every handled exit path, and releases
the lock last. Automated tests use fake clocks, processes, logs, dialogs, and profiles; they do not
launch installed Steam or Ostriv.

## Dead ends (tried and removed — do not reintroduce)

- **`open -b <our launcher bundle id>` to re-focus ourselves** before the game (a
  `raise_launcher()` helper). On a CrossOver Menu Helper app `open -b` starts a **new
  instance** rather than activating the running one, so it re-ran the launcher → game →
  `open -b` → … cascading into dozens of Ostriv launches. Removed.
- **`open -g` (background) for Steam.** Speculative focus hardening; the real bug was never
  focus. Reverted to a plain foreground `open` (a backgrounded Steam may also initialise more
  slowly).
- **Using Wine `tasklist` PIDs with macOS `ps`.** CrossOver reports Windows PIDs (for example,
  `488`) that do not identify the corresponding host process (for example, `89570`). Query host
  candidates independently, then prove bottle ownership from the renderer's working directory.
- **Polling Wine `tasklist` and `reg query`.** During Steam startup either call can consume its
  ten-second subprocess limit even when Steam is healthy. Repeating both made a warm launch take
  more than a minute. Native process inspection plus a bounded bottle registry read keeps normal
  probes fast; a transient probe failure remains “not ready yet” until the overall deadline.

## Files changed

- `ostriv_macos/launcher_runtime.py` — stable readiness, lock, fresh-log classification, targeted
  retry, and profile recovery.
- `ostriv_macos/launcher.py` — copied runtime/config and matching CrossOver Steam-app paths.
- `tests/test_launcher_runtime.py` and `tests/test_launcher_profile.py` — deterministic state,
  retry, lock, and recovery matrices.

The former embedded `patch.py` launcher template and its early-return behavior are superseded.

## Historical verification (live, cold start)

Killed Steam, emptied the game log (so all markers are from the fresh session), launched once:
the launcher held the profile switch until a `steamwebhelper` renderer was up, then the game
reached `uiMainMenu` with **zero** `SteamAPI_Init() failed` lines and stayed stable. User
visually confirmed a **playable main menu** on the first cold try.

That live result is evidence for the original CrossOver 26.2 setup, not a community testing gate
or a claim about every machine. The hardened behavior is regression-tested without asking players
to run raw procedures.
