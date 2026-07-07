# Bug B: cold Steam start never launches the game

> **Fixed and verified live** — on a cold start (Steam not running) the game now launches on
> the first try to a playable main menu (user-confirmed). Two defects had to be fixed.

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

## Fix

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

This also removes the reused-pid gate (defect 1). No other launcher behavior changed.

## Dead ends (tried and removed — do not reintroduce)

- **`open -b <our launcher bundle id>` to re-focus ourselves** before the game (a
  `raise_launcher()` helper). On a CrossOver Menu Helper app `open -b` starts a **new
  instance** rather than activating the running one, so it re-ran the launcher → game →
  `open -b` → … cascading into dozens of Ostriv launches. Removed.
- **`open -g` (background) for Steam.** Speculative focus hardening; the real bug was never
  focus. Reverted to a plain foreground `open` (a backgrounded Steam may also initialise more
  slowly).

## Files changed

`patch.py` — only the `LAUNCHER_SCRIPT` template: added `steam_ready()` and gated
`start_steam()`'s wait on it.

## Verification (live, cold start)

Killed Steam, emptied the game log (so all markers are from the fresh session), launched once:
the launcher held the profile switch until a `steamwebhelper` renderer was up, then the game
reached `uiMainMenu` with **zero** `SteamAPI_Init() failed` lines and stayed stable. User
visually confirmed a **playable main menu** on the first cold try.
