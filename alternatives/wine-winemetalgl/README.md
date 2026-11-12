English | [Українська](README.uk.md)

# Alternative: Ostriv on Mac without CrossOver (Wine 11 + WineMetalGL)

A free, experimental alternative to the main CrossOver-based patcher in this repository.

**Stack:** [Wine 11.0 (Gcenx build)](https://github.com/Gcenx/macOS_Wine_builds), x86_64 under Rosetta
+ [WineMetalGL 3.9.0](https://github.com/metalsharp/WineMetalGL) (OpenGL 4.6 implemented directly on Metal)
+ Windows Steam in a dedicated Wine prefix.

Ostriv needs OpenGL 4.3 while macOS stops at 4.1. WineMetalGL translates OpenGL straight to Metal,
so this route needs neither CrossOver, nor D3DMetal, nor the patched Mesa from the main patcher.
Only `assets/settings.data` (MSAA off) is reused from this repository.

## Status
Tested on MacBook Pro M1 (8 GB), macOS 27.2, Ostriv 0.5.9.65 (Steam), November 2026:

- ✅ the game starts, loads a map and is playable;
- ⚠️ **low FPS** on M1 / 8 GB. The game's main thread is pinned at ~100% of one core (game, Wine
  and WineMetalGL all run x86 code through Rosetta), and the game (~5–6 GB with textures, unified
  memory) plus Wine/Steam (~2 GB) don't fit into 8 GB. A faster Mac with more memory should do better;
- ⚠️ the Windows Steam window renders black under this Wine, so sign-in goes through the command
  line (`steam.exe -login`); the launcher asks for the password with hidden input and never stores it.

If you own CrossOver, the main patcher is the better-tested and faster option.

## Install
Requires an Apple Silicon Mac, ~5 GB of disk space and Ostriv owned on Steam.

```bash
cd alternatives/wine-winemetalgl
./setup.sh
```

Everything is installed into `~/Games/Ostriv` (override with `OSTRIV_HOME=/path ./setup.sh`).
The script downloads pinned versions of Wine, WineMetalGL and Steam, verifies SHA-256 checksums,
creates the prefix and adds the Ostriv app manifest so Steam downloads the game by itself.
Re-running it is safe. Nothing is installed outside the chosen folder.

## Play
1. Quit other apps — memory is tight.
2. Double-click **Play Ostriv.command** in `~/Games/Ostriv` (or run `~/Games/Ostriv/play-ostriv.sh`).
3. Enter your Steam login and password and approve the sign-in in the Steam mobile app.
   The Steam window stays black — leave it alone.
4. On first run the script waits for Steam to download Ostriv (~560 MB), then starts the game.

Do **not** start the game with Steam's Play button — only through the script.

## What the launcher takes care of
- **Start the game ~40 s after Steam signs in.** Started earlier, Steam doesn't register the process
  (no `AppID 773790 adding PID` in `Steam/logs/gameprocess_log.txt`), `SteamApps()` returns NULL and the
  game crashes right after the UI loads (`read 0 at ostriv.exe+0x359a77`, `ISteamApps::BIsDlcInstalled`).
- **Steam's watchdog kills Steam under memory pressure** (`CSteamEngine::BMainLoop appears to have
  stalled > 15 seconds`). The unix process can linger, so liveness is checked via `wine tasklist`;
  the launcher restarts Steam when needed.
- `steam.exe -shutdown` *starts* Steam when it isn't running, so it's only called when Steam is alive.

## Dead ends (so nobody repeats them)
| Tried | Result |
|---|---|
| Game Porting Toolkit (Gcenx cask, Wine 7.7) + D3DMetal | current Steam crashes in Wine 7.7's `bcrypt.dll` |
| Sikarugir engine `WS12WineCX24.0.7_7` (Wine 9 / CrossOver 24) | Steam error 0x3008 (WebUITransport: Connection rejected) |
| GPTK's D3DMetal in upstream Wine 11 | `d3d12.dll` imports `ntdll.__wine_unix_call` (gone in Wine 11); after patching the import, D3DMetal faults in its unix side reading `gs:0x8E8` (TLS layout of Apple/CrossOver Wine) |
| Patched Mesa → vkd3d (Wine 11) → MoltenVK | vkd3d 1.18 `CreateCommandQueue1` is a stub; MoltenVK has no geometry shaders |
| Mesa llvmpipe on Wine 11 | `ChoosePixelFormat` fails, no window |

Known cosmetic issue: WineMetalGL fails to build one pipeline
(`Vertex attribute in_flHeight(7) of type int cannot be read using MTLAttributeFormatFloat`),
so some element may not render.

## Uninstall
Delete `~/Games/Ostriv`.
