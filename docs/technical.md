# Ostriv on Apple Silicon (CrossOver) — WORKING recipe

**Status (Jul 5 2026): SOLVED — GPU-accelerated, FULLSCREEN, ~30 fps in-town.** D3D12 → D3DMetal
→ Apple GPU, visible via `wgl_require_gdi_compat` GDI present. The **async-present patched driver**
(mesa-src/build-w64) takes full-resolution fullscreen from ~7 fps (stock, present-bound) to a
locked ~30 fps by pipelining the window flush on a worker thread + dropping frames. llvmpipe = fallback.

Tested on: Apple **M5 Max**, macOS, **CrossOver 26.2 (stable)**, Ostriv **Alpha 5 patch 9
hotfix 58 (0.5.9.58)**, Steam edition.

---

## Why Ostriv doesn't work out of the box

1. Ostriv needs **OpenGL 4.3** (all 52 embedded shaders are `#version 430`).
   Apple's OpenGL is frozen at **4.1** → the game can't get a context and null-derefs.
2. Even with a capable GL (Mesa), two more independent bugs bite:
   - **MSAA window creation**: with `multisampling=1` (the game default) the game calls
     `wglChoosePixelFormatARB`, which re-enters window creation for an 8×-MSAA format;
     that second window fails (`windows_createWindow FAILED 0`) → crash at startup.
   - **Invisible window**: Mesa's `d3d12` driver presents through a DXGI swapchain
     (D3DMetal); that present never reaches the winemac window → game runs, music plays,
     window is fully transparent.

## The recipe

Bottle assumed: `Ostriv` (win10_64), Steam + Ostriv installed inside.

### 1. Mesa (Windows build) next to the game exe

From [pal1000/mesa-dist-win](https://github.com/pal1000/mesa-dist-win) release
(**26.1.3**, release-msvc), copy from `x64/` into
`.../drive_c/Program Files (x86)/Steam/steamapps/common/Ostriv/`:

- `opengl32.dll`
- `libgallium_wgl.dll`
- `dxil.dll`

### 2. Per-app DLL override (never global!)

```
HKCU\Software\Wine\AppDefaults\ostriv.exe\DllOverrides
    "opengl32" = "native"
```

A *global* `opengl32=native` breaks Steam itself (`vgui2_s.dll` fatal error) — scope it
to `ostriv.exe` only.

### 3. Bottle environment (`cxbottle.conf` → `[EnvironmentVariables]`)

```
"GALLIUM_DRIVER" = "d3d12"
"wgl_require_gdi_compat" = "true"
"MESA_D3D12_ASYNC_PRESENT" = "1"
"MESA_OSTRIV_TREE_SHADER_HACK" = "1"
"MESA_GL_VERSION_OVERRIDE" = "4.3"
"MESA_GLSL_VERSION_OVERRIDE" = "430"
```

⚠️ **Do NOT put `SteamAppId`/`SteamGameId` in the bottle env.** Bottle env is applied to *every*
process in the bottle including `steam.exe`, which then thinks it IS app 773790 and its CEF browser
**crash-loops** (Play button dies). The game gets its app id from a game-scoped **`steam_appid.txt`**
(contents: `773790`) placed next to `ostriv.exe` instead.

**`wgl_require_gdi_compat=true` is the key line for GPU rendering.** It is a Mesa driconf
option (settable as an env var): it restricts pixel formats to GDI-compatible ones, which
makes the d3d12 winsys skip its DXGI swapchain (whose present never reaches the winemac
window under CrossOver) and instead present via `flush_frontbuffer` → CPU readback →
`StretchDIBits` (GDI) — the path winemac composites. Result: **full GPU rendering
(D3DMetal / Apple GPU) with a visible window.**

Fallback if anything breaks: `"GALLIUM_DRIVER" = "llvmpipe"` (software, slower, also visible).

⚠️ The bottle env **overrides** shell env — experiments via `env GALLIUM_DRIVER=... wine ...`
silently do nothing; edit `cxbottle.conf` instead.

- `d3d12` + `wgl_require_gdi_compat=true` = **GPU rendering with visible window** (the shipped config).
- `d3d12` alone = GPU but INVISIBLE window (DXGI swapchain present never lands in winemac).
- `llvmpipe` = software fallback, visible but slow.

### 4. Kill multisampling (the startup-crash fix)

Run `ostriv_settings.exe` (CrossOver launcher: `~/Applications/CrossOver/ostriv_settings.app`)
and set **multisampling OFF** (safest: anisotropy off, bloom off too). This writes
`drive_c/users/crossover/Saved Games/Ostriv/settings.data`.

If `settings.data` is **absent**, the game uses hardcoded defaults (`multisampling=1`)
and crashes at window creation — the file must exist with MSAA off.

### 5. Launch

- Start **Steam** in the bottle first — the Steam **client** only needs to be *running* (for
  `SteamAPI_Init`). You do not launch the game through it.
- **Launch the game via CrossOver "Run Command"** with the **quoted** exe path (quotes required —
  the path has spaces): `"C:\Program Files (x86)\Steam\steamapps\common\Ostriv\ostriv.exe"` (or save
  that as a launcher).
- **Do NOT use Steam's Play button.** Play injects the Steam in-game overlay
  (`gameoverlayrenderer64.dll`), whose GL hooks conflict with the Mesa `opengl32` and crash the game
  ~2s after launch (before it even writes its log). If you must use Play: disable the overlay for
  Ostriv first (Library → Properties → uncheck "Enable the Steam Overlay while in-game").
- **Why Run Command and not `wine --cx-app` from a shell:** a bare CLI launch reaches the menu but
  the window has no foreground app owning it, so winemac closes it (`PostQuitMessage`) and the game
  self-exits. CrossOver "Run Command" / a `.app` launcher gives the window a proper GUI app context,
  so it stays running.
- **If Steam itself keeps crashing** (`crash_steam.exe` dumps, CEF renderer restarts): clear its
  browser caches — quit Steam, delete `…/Steam/appcache/httpcache`,
  `…/AppData/Local/Steam/htmlcache`, and any `GPUCache`, then relaunch.

### Diagnostics cheat-sheet

- Game's own log: `drive_c/users/crossover/Saved Games/Ostriv/log.txt` (overwritten each run).
  - Success: `4.3 (Core Profile) Mesa 26.1.3` → ... → `uiMainMenu`.
  - MSAA crash: second `windows_createWindow ... FAILED 0` after `recursion`.
- The game process is named **`Menu Helper`** (the .app bundle execs wine in-place), *not*
  `ostriv.exe` — `pkill -f ostriv.exe` misses it.
- Registry keys set during debugging that are harmless but not required:
  `Mac Driver\UsePerPixelAlpha="n"`, `AppDefaults\ostriv.exe\DllOverrides\dcomp=""`.

---

## Performance tuning (solved: playable)

Measured bottleneck chain (GALLIUM_HUD=fps + feel):
1. Rendering is GPU-fast (menu = 100 fps at native window size).
2. In-town submissions were draw-call heavy → `mesa_glthread=true` decouples the game thread.
3. **The real ceiling is winemac's window-surface flush** (GDI pixels → screen), which
   scales with WINDOW PIXEL COUNT: native 3008×1692 "feels like 7-10 fps" even when the
   HUD says 30-100; a 1920×1080 window is PLAYABLE.

Working performance config (on top of the recipe above):
- `"mesa_glthread" = "true"` (safe — the old crash was MSAA, not glthread)
- game `max fps` = 30 (ostriv_settings) — stops glthread queueing ahead of the screen
- ~~**window size = the FPS dial**: per-app virtual desktop caps it reliably~~
  **SUPERSEDED — do not use.** The virtual-desktop trick (`HKCU\Software\Wine\Explorer\Desktops`
  `Ostriv=1920x1080` + `AppDefaults\ostriv.exe\Explorer` `Desktop=Ostriv`) predates the
  async-present driver patch and **corrupts winemac's display state** (see the Fullscreen section
  below). With the patched driver, fullscreen at native resolution is fluid without it.
- Optional HUD: `"GALLIUM_HUD" = "fps"` (drawn into the frame, works on this path)
- Fullscreen feel: macOS display → 1920×1080 scaled mode (GPU upscale) + game Fullscreen ON
  inside the virtual desktop; or the desktop window's native macOS full-screen if it stretches.

Ruled out en route: Zink (MoltenVK lacks nullDescriptor, checked 1.4.1); llvmpipe as main
driver (Rosetta CPU raster too slow in-town); raw d3d12 DXGI present (never composited —
no Metal layer attach, confirmed with D3DM_SHOW_HUD_STATS showing nothing).

## Fullscreen (the correct, simple way)

Do NOT use a Wine virtual desktop or `displayplacer` to force fullscreen — that path
corrupts winemac's display state (it starts reporting 1024x768 to the game regardless of
the real resolution, and only a full CrossOver restart clears it).

Correct fullscreen:
1. In-game settings.data flag: `bFullscreenBorderlessWindow=1` (and `bFullscreen=0`).
   This makes the game render a borderless window at the FULL monitor resolution.
2. No virtual desktop (`HKCU\Software\Wine\AppDefaults\ostriv.exe\Explorer` Desktop key absent).
3. Just launch — the game fills the screen at your display's current resolution.

If the game ever renders at 1024x768 while the monitor is bigger:
- winemac display state is stuck. **Fully quit CrossOver and reopen it** (this resets winemac).
  Then the game detects the real monitor resolution again.

FPS note: fullscreen = large window = large per-frame winemac surface flush = lower FPS
(the flush, not the GPU, is the limiter). Options: lower macOS display resolution BEFORE
launching (in System Settings, not via tools), or use the patched Mesa driver
(mesa-src/build-w64) which does async/frame-dropping present to keep it fluid.
