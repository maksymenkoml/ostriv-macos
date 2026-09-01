# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

Tooling to run **Ostriv** (Steam city-builder, appid **773790**) on **Apple Silicon Macs** under
**CrossOver / Wine**, GPU-accelerated and fullscreen. Ostriv needs **OpenGL 4.3**; Apple's OpenGL
is frozen at **4.1**. The fix: a patched **Mesa** `opengl32` whose `d3d12` driver routes
GL → D3D12 → Apple's D3DMetal → Metal, plus Wine/bottle config and a launcher app.
This is **configuration + a native driver swap**, NOT binary patching of the game (contrast with
the sibling `cs2-macos-patcher`, which rewrites .NET IL).

## The six fixes (all required together)

1. **GL 4.3** — Mesa DLLs next to `ostriv.exe`, `opengl32=native` scoped to `ostriv.exe`, bottle
   env `GALLIUM_DRIVER=d3d12` + `wgl_require_gdi_compat=true` (+ `MESA_D3D12_ASYNC_PRESENT=1`,
   `MESA_OSTRIV_TREE_SHADER_HACK=1`, `MESA_GL_VERSION_OVERRIDE=4.3`,
   `MESA_GLSL_VERSION_OVERRIDE=430`). `wgl_require_gdi_compat` forces Mesa off its DXGI swapchain
   (winemac never composites it) onto the GDI present path winemac does composite.
2. **Startup crash = multisampling.** MSAA → `wglChoosePixelFormatARB` → recursive window winemac
   can't create (`windows_createWindow FAILED 0`). Fix: MSAA off in `settings.data`; the patched
   driver also suppresses MSAA pixel formats (`stw_pixelformat.c`).
3. **Fullscreen FPS** — inline GDI present caps at ~7 fps. The patch (`gdi_sw_winsys.c`) offloads
   the final GDI upload to a worker thread and drops frames while busy → 30–60. Toggle:
   `MESA_D3D12_ASYNC_PRESENT=0`.
4. **Trees** — Mesa/d3d12 breaks Ostriv's tree shader (flat int varying). The patch rewrites it at
   `glShaderSource` (`shaderapi.c`). Toggle: `MESA_OSTRIV_TREE_SHADER_HACK=0`.
5. **Reeds/grass/terrain (flat inputs)** — D3DMetal fails to compile the Metal fragment function
   of any GS-less pipeline whose DXIL has a flat input → `PSO no-op` → the geometry silently never
   draws. Two-part fix: the patch rewrites Ostriv's `flat int` varyings (`v_iSkip`,
   `v_iPatchType`) to floats (`MESA_OSTRIV_FLAT_VARYING_HACK=1`), and bottle env
   `MESA_GLSL_DISABLE_IO_OPT=true` stops Mesa's varying optimizer from promoting convergent
   smooth varyings to flat (minimap/UI). Diagnostics: `MESA_OSTRIV_PSO_LOG=1` →
   `mesa_ostriv_pso_log.txt`, and `mesa_ostriv_hack_log.txt` (both next to `ostriv.exe`).
6. **Display color profile** — any non-sRGB profile makes CoreAnimation colorspace-convert every
   frame on the CPU (~10 fps cap regardless of driver). The generated launcher switches the display
   to sRGB while playing and restores after.

## Layout

- `patch.py` — thin Python 3.9+ player entry point. It delegates Install, Reinstall, Restore,
  read-only diagnosis, and package preflight to `ostriv_macos.cli`.
- `ostriv_macos/` — standard-library-only package. `discovery.py` resolves CrossOver, bottles,
  and games; `payload.py` verifies the bundled manifest; `installer.py` owns journaled bottle
  changes; `launcher.py` materializes the app/runtime/config; `launcher_runtime.py` owns the
  one-click Steam/profile state machine; `diagnostics.py` owns commands, logs, and player output.
- `payload-manifest.json` — exact size, SHA-256, and PE-header contract for every required payload
  file. Validation finishes before the installer mutates a bottle.
- `assets/settings.data` — known-good settings template (MSAA off, borderless fullscreen).
- `scripts/build-release.py` — builds and verifies the allowlisted, no-Git player ZIP.
- `scripts/build-driver.sh` — clones Mesa 26.1.3, applies the patch, cross-compiles with mingw.
- `patches/mesa-26.1.3-winemac-async-present.patch` — the Mesa source patch (4 files, see fixes
  2–5). Regenerate with `git diff` in the Mesa tree.
- `prebuilt/` — drop-in DLLs, tracked via **Git LFS**.
- `tests/` — deterministic unit, fake-process integration, rollback, launcher, CLI snapshot, and
  release-artifact coverage. Tests must never launch installed CrossOver, Steam, or Ostriv.
- `docs/technical.md` — every bug, fix, and dead end in detail.

## Commands

```bash
python3 -m unittest discover -s tests -v
python3 scripts/build-release.py --output dist/ostriv-macos-player.zip
python3 patch.py --preflight
python3 patch.py --diagnose
./scripts/build-driver.sh
```

`--preflight` is silent and checks only the downloaded package. `--diagnose` is process-free,
read-only, and prints a concise local summary. The driver rebuild still needs mingw-w64, meson,
ninja, bison, and python-mako; none of those are player dependencies.

## Hard-won rules — DO NOT violate

- **Launch via the generated launcher app** (or CrossOver Run Command). NOT Steam's Play button —
  its overlay crashes the game before it writes its log. NOT a bare shell `wine` command — no
  foreground GUI app → winemac posts quit and the game self-exits at the menu.
- **The display profile must be sRGB while playing** — otherwise ~10 fps (CPU ColorSync per
  frame). Launcher handles it. Diagnose with `sample <pid>` of `Menu Helper`.
- **Never make `opengl32=native` global** — breaks Steam (`vgui2_s.dll`). Scope to
  `AppDefaults\ostriv.exe`.
- **Never force fullscreen/resolution with a virtual desktop or `displayplacer`, and never
  change the display while a bottle is running** — corrupts winemac display state
  (1024×768 forever); only a full CrossOver quit+reopen resets it. Fullscreen = the game's
  own `bFullscreenBorderlessWindow=1`. Switching the *display mode* around a launch is the
  one sanctioned exception: the launcher selects the panel's 16:10 twin before Wine starts
  and restores it after Wine exits (notch fix, fix 7), which is the same ordering the sRGB
  profile switch already uses.
- **Never set `SteamAppId`/`SteamGameId` as bottle env** — crash-loops Steam's CEF browser. Use
  game-scoped `steam_appid.txt`.
- **Multisampling stays off**, and `settings.data` must exist (absent = MSAA defaults = crash).
- **Bottle env overrides shell env** — `env VAR=… wine …` does nothing; edit `cxbottle.conf`.
- **cxmenu specifics**: a raw `--command` runs through `sh` (shell command, not a Windows path);
  `[` `]` in an entry name break launcher-app generation (use parentheses); cxmenu registers the
  menu but the `.app` must be materialized from CrossOver's `Menu Helper.cpbz2` template with
  bundle ID `com.codeweavers.CrossOverHelper.<MD5(bottle)>.<MD5(menu name)>` (uppercase), or
  CrossOver's sync deletes it as foreign.
- **ColorSync specifics**: `CGDisplayCreateUUIDFromDisplayID` lives in ColorSync.framework;
  System-Settings profile picks occupy the custom-profile slot (key `"1"`) — save and re-apply on
  restore, never bare-reset (that reverts to factory, losing the user's choice).
- **Keep the DLLs in Git LFS**; pin Mesa `26.1.3` (patch hunks assume it).
- `settings.data` is a typed key-value blob (`fmt=5`); bools are 1 byte after the key name; no
  absolute resolution field — resolution is `flResolutionCoef × detected monitor`.

## Environment (reference machine)

Apple **M5 Max**, macOS, **CrossOver 26.2**, Ostriv **0.5.9.58**, Mesa **26.1.3**, D3DMetal from
CrossOver's GPTK. Toolchain: mingw-w64, meson, ninja, bison ≥ 3, python-mako.
