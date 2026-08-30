# Technical notes — every bug and fix

Status: **solved** — GPU-accelerated, fullscreen, 30–60 fps.
Tested: Apple M5 Max · macOS · CrossOver 26.2 · Ostriv 0.5.9.58 (Steam).

## Why the game doesn't work out of the box

1. **GL version.** Ostriv needs OpenGL 4.3 (all 52 embedded shaders are `#version 430`);
   Apple's OpenGL stops at 4.1 → no context → null-deref crash at launch.
2. **MSAA crash.** With `multisampling=1` (the game default) the game calls
   `wglChoosePixelFormatARB`, which re-enters window creation for an MSAA format; winemac
   can't create that second window (`windows_createWindow FAILED 0`) → startup crash.
3. **Invisible window.** Mesa's `d3d12` driver presents through a DXGI swapchain; under
   CrossOver that present never reaches the winemac window — game runs, music plays,
   window stays transparent.

## The fix stack (what patch.py installs)

1. **Patched Mesa 26.1.3 DLLs** (`opengl32`, `libgallium_wgl`, `dxil`, `libwinpthread-1`)
   next to `ostriv.exe`. GL → D3D12 → D3DMetal → Metal = GL 4.3 on the GPU.
2. **Per-app DLL override** — `HKCU\Software\Wine\AppDefaults\ostriv.exe\DllOverrides`
   `opengl32=native`. Never global: a global override breaks Steam (`vgui2_s.dll` fatal).
3. **Bottle env** (`cxbottle.conf` → `[EnvironmentVariables]`):

   ```
   "GALLIUM_DRIVER" = "d3d12"
   "wgl_require_gdi_compat" = "true"
   "MESA_D3D12_ASYNC_PRESENT" = "1"
   "MESA_OSTRIV_TREE_SHADER_HACK" = "1"
   "MESA_OSTRIV_FLAT_VARYING_HACK" = "1"
   "MESA_GLSL_DISABLE_IO_OPT" = "true"
   "MESA_GL_VERSION_OVERRIDE" = "4.3"
   "MESA_GLSL_VERSION_OVERRIDE" = "430"
   ```

   The `MESA_OSTRIV_*` hack toggles take literally `1`/`0` only (the driver checks the
   first character against `'1'`) — setting one to `true` silently disables it;
   `MESA_GLSL_DISABLE_IO_OPT` is a stock Mesa boolean and accepts `true`.

   `wgl_require_gdi_compat=true` is the visibility fix: it forces Mesa off the DXGI
   swapchain onto the GDI present path (`flush_frontbuffer` → CPU readback →
   `StretchDIBits`) — the only path winemac composites. Bottle env **overrides** shell
   env; `env VAR=… wine …` experiments silently do nothing.
   Never put `SteamAppId`/`SteamGameId` in bottle env — it hits `steam.exe` too and
   crash-loops Steam's CEF browser. The game gets its app id from a game-scoped
   **`steam_appid.txt`** (`773790`) instead.
4. **`settings.data`** with multisampling OFF + `bFullscreenBorderlessWindow=1`. The file
   must *exist*: without it the game uses hardcoded defaults (MSAA on) and crashes.
   Format: typed key-value blob (`fmt=5`); bools are 1 byte after the key name; resolution
   is `flResolutionCoef × detected monitor` (no absolute resolution field).
5. **`Ostriv (patched).app` launcher** — see "Display color profile" below.

Software fallback if the GPU path breaks: `GALLIUM_DRIVER=llvmpipe` (visible, slow).

## Hardened installer architecture

`patch.py` is only the Python 3.9 guard and CLI entry point. The standard-library-only
`ostriv_macos` package separates ownership so player output and bottle mutation cannot leak into
each other:

| Module | Ownership |
| --- | --- |
| `cli.py` | Install/Reinstall/Restore selection, read-only preflight/diagnosis, and final player outcomes |
| `diagnostics.py` | Tolerant subprocess decoding, typed failures, concise terminal rendering, and local log setup |
| `payload.py` | `payload-manifest.json` parsing and pre-mutation file/type/size/SHA-256 validation |
| `discovery.py` | CrossOver discovery and resolved `Bottle`/`GameInstallation` models |
| `installer.py` | Preflight, journaled driver/registry/config/settings operations, verification, and Restore |
| `launcher.py` | Verified app materialization, copied runtime/config, cxmenu registration, and legacy migration |
| `launcher_runtime.py` | Per-bottle lock, Steam readiness/retry, display-profile recovery, game launch, and launcher logging |

The payload manifest is the release contract, not a best-effort inventory. Every required DLL and
`assets/settings.data` entry has an exact size and SHA-256 digest; DLLs must also be regular PE
files with an `MZ` header and not Git LFS pointers. The complete payload is validated before the
first destination mutation and revalidated in the extracted player ZIP.

Discovery carries a resolved `Bottle` object through the rest of the installer. It keeps the
bottle's display name, canonical root, private/managed scope, and owning CrossOver installation.
Private and symlinked external bottles therefore use their absolute resolved roots, while managed
bottles use their registered names and `--scope managed`; later modules never reconstruct a
bottle from a fixed default directory.

Each selected bottle stores two distinct durable records:

- `.ostriv-macos-journal.json` is the in-progress transaction journal. Every mutation records its
  idempotent undo operation before applying the change, so an error rolls back in reverse and the
  next run can recover an interrupted operation.
- `ostriv-macos-state.json` is the completed ownership state. It records genuine backups, prior
  registry/config/settings values, owned files, launcher artifacts, and verification metadata so
  Restore changes only project-owned content and is safe to repeat.

The launcher is data plus packaged code, not interpolated source. Installation copies
`launcher_runtime.py` byte-for-byte to `<bottle>/play-ostriv-patched.py`, writes the resolved
values to `<bottle>/launcher-config.json`, verifies both and the pending app bundle, then swaps the
app into place. The runtime holds an advisory flock at `<bottle>/.ostriv-launcher.lock` and keeps
the exact original display profile in `<bottle>/.ostriv-profile-recovery.json` until restoration
succeeds. A killed run leaves the marker for recovery on the next click; a stale lock pathname is
harmless because the kernel owns the lock.

Steam process identity comes from the selected bottle's task table, and ActiveUser comes from its
registry, both queried through that bottle's resolved CrossOver `wine --bottle` command identity.
Only helper PIDs in that task table are correlated with bounded macOS process detail, and a helper
counts as ready only with the exact `--type=renderer` role. Captured command lines are omitted from
diagnostics. Global process matches and canonical-path argv substrings do not contribute readiness
signals.

Detailed installer logs stay at `~/Library/Logs/ostriv-macos/install.log`. Launcher logs use a
filesystem-safe bottle identity under `~/Library/Logs/ostriv-macos/`. They contain command and
state detail locally; the terminal and dialogs receive only one concise outcome and action. The
read-only `python3 patch.py --diagnose` path does not create logs, start processes, mutate files,
access the network, or upload data.

## The Mesa patch (patches/mesa-26.1.3-winemac-async-present.patch)

- **`gdi_sw_winsys.c` — async present.** The GDI upload is expensive; done inline it caps
  fullscreen at ~7 fps. The patch keeps Mesa's stock D3D12 sync/readback and offloads only
  the final `StretchDIBits` to a worker thread, dropping frames while the worker is busy
  → locked 30–60. Toggle: `MESA_D3D12_ASYNC_PRESENT=0`.
- **`stw_pixelformat.c`** — defaults `wgl_require_gdi_compat` on (env var honored even if
  driconf is compiled out) and suppresses MSAA pixel formats entirely, so the game's own
  MSAA setting becomes a harmless no-op (`MESA_WGL_ALLOW_MSAA=1` restores stock).
- **`shaderapi.c` — tree shader workaround.** Mesa/d3d12 mishandles Ostriv's tree shader
  (broken flat integer varying). The patch rewrites the matched shader at `glShaderSource`:
  float varying in the VS, and a conservative direct texture-array FS that keeps leaves,
  blossom, fruit, snow, fog, lighting, opacity. Toggle: `MESA_OSTRIV_TREE_SHADER_HACK=0`.
- **`shaderapi.c` — flat-varying workaround (reeds/grass/terrain).** See
  [the D3DMetal flat-input bug](#reeds-never-render-the-d3dmetal-flat-input-bug) below.
  Rewrites `flat int` varyings (`v_iSkip`, `v_iPatchType`) to plain floats at
  `glShaderSource`, with length-preserving in-place substitutions. Toggle:
  `MESA_OSTRIV_FLAT_VARYING_HACK=0`. Every fired hack (this one and the tree one) appends a
  line to `mesa_ostriv_hack_log.txt` next to `ostriv.exe`.
- **`d3d12_pipeline_state.cpp` — PSO attribution log.** With `MESA_OSTRIV_PSO_LOG=1` the
  driver writes `mesa_ostriv_pso_log.txt` (next to `ostriv.exe`) listing every graphics PSO
  in creation order with the GLSL program names of its stages — this is how a D3DMetal
  `marking PSO(N) as no-op` error is attributed to a specific game shader. Off by default.

Rebuild: `scripts/build-driver.sh` (mingw-w64, meson, ninja, bison ≥ 3, python-mako).
Mesa version is pinned to 26.1.3 — the patch hunks assume it.

## Reeds never render: the D3DMetal flat-input bug

**Symptom:** reeds (the thatchery's resource, drawn by `createPlantsProgram`) never appear;
grass tufts (`createGrassProgram`) are missing too, less visibly. Nothing in the game log —
every `glLinkProgram` reports success.

**Root cause:** D3DMetal (CrossOver's GPTK D3D12→Metal layer) fails to convert the Metal
fragment function of any **GS-less** pipeline whose DXIL has a **flat-interpolated input**
(int or float), logs `Failed to compile fragment function … marking PSO(N) as no-op` on
stderr, and silently draws nothing for those pipelines. Pipelines that get one of Mesa's
auto-inserted geometry-shader variants (`GS=edgeflags`) compile fine — which is why terrain,
which also has a flat varying, still mostly rendered while plants/grass did not.

Ostriv feeds three programs flat **int** varyings from per-instance data: `v_iSkip` in the
plants and grass shaders (the FS discards on it) and `v_iPatchType` in the terrain shader.
Two more programs (minimap/world-map, UI-rect) pick up flat inputs **at compile time**: Mesa's
varying optimizer (`nir_opt_varyings`) promotes convergent smooth varyings to flat.

**Fix (two parts, both installed by patch.py):**

1. `MESA_OSTRIV_FLAT_VARYING_HACK=1` — the driver rewrites the game's flat int varyings to
   plain floats at `glShaderSource` (safe: they are per-instance constants, so interpolation
   reproduces them exactly; int→float assignments compile via GLSL implicit conversion).
   Comparisons are rewritten length-preserving: `v_iSkip == 1` → `v_iSkip > 0.5`,
   `v_iPatchType == 0` → `v_iPatchType < 0.5`.
2. Bottle env `MESA_GLSL_DISABLE_IO_OPT=true` — upstream Mesa escape hatch that skips
   `nir_opt_varyings`, so no *new* flat inputs are invented for shaders that had none.

Verified: with both in place the startup goes from **14 failed PSOs to 0** (plants ×~5,
grass ×~4 before part 1; terrain ×3 + minimap ×2 before part 2).

## Display color profile (the ~10 fps ColorSync ceiling)

Found by `sample`-profiling the live process: the **main thread** (winemac's CoreAnimation
thread) was 99% busy in `CGColorTransformConvertUsingCMSConverter`. Wine hands its window
surface to CoreAnimation as a `CGImage` tagged plain sRGB; if the display's ColorSync
profile is anything else (factory wide-gamut ICC, P3, calibration), CoreAnimation
re-renders **every frame through a per-pixel CPU colorspace conversion** — single-threaded,
under Rosetta. At 3008×1692 that alone caps the game at ~10 fps while total CPU looks low.

**Fix: set the display profile to sRGB** (System Settings → Displays → Color profile →
*sRGB IEC61966-2.1*). Source and destination then match, CoreAnimation takes its
zero-conversion path (re-measured: main thread 94% idle, game render thread becomes the
limiter). Applies live. The *root* fix belongs in winemac — tagging the surface with
`CGDisplayCopyColorSpace` — see the ready-to-post report in
[crossover-bug-report.md](crossover-bug-report.md).

**The launcher automates it.** The installer creates `~/Applications/CrossOver/Ostriv
(patched).app` plus a cxmenu entry. Its command runs the packaged runtime copied into the bottle
with a separate JSON configuration: recover a previous marker → establish stable Steam readiness
→ install cleanup handlers → save the exact current profile → set sRGB
(`ColorSyncDeviceSetCustomProfiles`, per-user, no admin) → run
`wine --bottle <bottle> --check --wait-children --start
"C:/…/ostriv.exe"` → restore the original profile in every handled exit path.

Hard-won implementation facts:

- `cxmenu --install` only registers the menu entry; the `.app` is written by the CrossOver
  GUI *eventually* (unreliable). patch.py materializes the bundle itself from CrossOver's
  template (`CrossOver.app/Contents/Resources/Menu Helper.cpbz2`, bunzip2 + cpio).
- The bundle ID must match CrossOver's scheme —
  `com.codeweavers.CrossOverHelper.<MD5(bottle)>.<MD5(menu name)>` (uppercase) — or
  CrossOver's menu sync deletes the app as foreign.
- A raw cxmenu `--command` is run through `sh`: it must be a shell command, not a Windows
  path. `[` `]` in a cxmenu entry name break app generation (menu registers, app never
  written) — hence parentheses in "Ostriv (patched)".
- `CGDisplayCreateUUIDFromDisplayID` is exported by ColorSync.framework, not CoreGraphics.
- Profiles picked in System Settings live in ColorSync's *custom profile* slot (key `"1"`);
  restore must re-apply the saved profile — a bare reset falls back to the **factory**
  profile and loses the user's choice.
- The Menu Helper app context is what keeps winemac happy: the *same* wine command from a
  bare shell reaches the menu, then self-exits (`PostQuitMessage` — no foreground GUI app).
- Never launch via the `.url` entry CrossOver makes from Steam's menu — that is the Steam
  Play path and injects the overlay (`gameoverlayrenderer64.dll`), which crashes the game
  ~2 s in, before it writes its log.

## Fullscreen and winemac display state

- Fullscreen = the game's own `bFullscreenBorderlessWindow=1`. Nothing else.
- The generated launcher sets `NSPrefersDisplaySafeAreaCompatibilityMode=true`.
  On MacBooks with a camera housing, macOS reduces the app's active display area so
  Ostriv's top-edge UI stays below the notch; displays without a camera housing are
  unaffected. Reinstall upgrades launchers created before this field was added.
- **Never** force resolution with a Wine virtual desktop or `displayplacer`: winemac's
  display state corrupts (reports 1024×768 forever) and only a full CrossOver quit+reopen
  resets it (`wineserver -k` is not enough).

## Diagnostics

- Game log: `<bottle>/drive_c/users/crossover/Saved Games/Ostriv/log.txt` (overwritten
  each run). Healthy: `4.3 (Core Profile) Mesa 26.1.3` → `uiMainMenu`.
  MSAA crash: `windows_createWindow FAILED` after `recursion`.
  Stock-driver symptom: `2.1 Metal` context + `FAILED 8341`.
- The game process is named **`Menu Helper`**, not `ostriv.exe`.
- Optional FPS HUD: bottle env `"GALLIUM_HUD" = "fps"` (drawn into the frame).
- `mesa_glthread=true` is safe and helps draw-call-heavy towns.
- **Capturing driver stderr** (Mesa messages, D3DMetal `PSO no-op` errors): the launcher's
  `wine … --start` path drops the PE side's std handles — Mesa's `fprintf(stderr)` output
  vanishes (only mac-side D3DMetal lines survive). Run the game exe **directly** instead
  (`wine --bottle <b> --workdir "C:/…/Ostriv" "C:/…/ostriv.exe" 2>file`); the game
  self-exits at the menu without the launcher app context, but every shader has already
  been compiled by then, which is exactly what a shader-diagnosis run needs.
- `MESA_GLSL=dump` prints final **NIR** (with `name: GLSL<prog-id>` headers matching program
  creation order in the game log) — the GLSL-source dump lines don't survive on this build.
  `MESA_SHADER_DUMP_PATH`/`MESA_LOG_FILE` are compiled out of Windows builds entirely. The
  game's GLSL sources are plain strings inside `ostriv.exe` (grep for a varying name).
- `MESA_OSTRIV_PSO_LOG=1` + `mesa_ostriv_pso_log.txt` maps D3DMetal `PSO(N)` errors to game
  shaders; `mesa_ostriv_hack_log.txt` confirms which shader hacks fired (both land next to
  `ostriv.exe`).

## Dead ends (don't retry)

- **Zink**: MoltenVK lacks `nullDescriptor` (checked 1.4.1).
- **llvmpipe as main driver**: Rosetta CPU raster too slow in-town.
- **Raw d3d12 DXGI present**: never composited by winemac (no Metal layer attach).
- **Per-app virtual desktop as an FPS dial**: superseded by the async-present patch, and
  it corrupts winemac display state.
