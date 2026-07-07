# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

Tooling to run **Ostriv** (Steam city-builder, appid **773790**) on **Apple Silicon Macs** under
**CrossOver / Wine**, **GPU-accelerated and fullscreen**. Ostriv needs **OpenGL 4.3**; Apple's
OpenGL is frozen at **4.1**, so it crashes on launch under stock CrossOver. This project replaces
the game's `opengl32` with a **Mesa build whose `d3d12` Gallium driver routes GL → D3D12 → Apple's
D3DMetal → Metal**, reaching GL 4.3 on the GPU, plus a small **Mesa source patch** that makes
fullscreen fast.

This is **configuration + a native driver swap**, NOT binary patching of the game (contrast with the
sibling `cs2-macos-patcher`, which rewrites managed .NET IL). Ostriv is native MSVC C++ — there's no
IL to patch; the whole fix is driver + Wine config.

## The four fixes (all required together)

1. **GL 4.3 itself** — `GALLIUM_DRIVER=d3d12` + `wgl_require_gdi_compat=true`, with the Mesa
   `opengl32`/`libgallium_wgl` DLLs dropped next to `ostriv.exe` and `opengl32=native` scoped to
   `ostriv.exe`. `wgl_require_gdi_compat` forces Mesa off its DXGI swapchain (which winemac never
   composites) onto the **GDI `flush_frontbuffer` present** path winemac *does* composite.
   The full bottle-env set patch.py installs: `GALLIUM_DRIVER=d3d12`, `wgl_require_gdi_compat=true`,
   `MESA_D3D12_ASYNC_PRESENT=1`, `MESA_OSTRIV_TREE_SHADER_HACK=1`,
   `MESA_GL_VERSION_OVERRIDE=4.3`, `MESA_GLSL_VERSION_OVERRIDE=430`.
2. **Startup crash = multisampling.** MSAA makes the game call `wglChoosePixelFormatARB`, which makes
   Mesa recursively create a second window — winemac can't, so it crashes (`windows_createWindow
   FAILED 0`). Fix: **multisampling OFF** in the game settings. The patched driver also suppresses
   MSAA pixel formats as a belt-and-suspenders (`stw_pixelformat.c`).
3. **Fullscreen FPS** — the GDI present flush is expensive; done inline it caps fullscreen at ~7 fps.
   The patch (`gdi_sw_winsys.c`, `gdi_sw_displaytarget_display`) keeps Mesa's stock D3D12
   synchronization/readback path, then offloads only the final GDI upload to a **worker thread
   and drops completed uploads** while it's busy → locked 30–60. Toggle with
   `MESA_D3D12_ASYNC_PRESENT=0`.
4. **Trees** — Mesa/d3d12 mishandles Ostriv's original tree shader path. The patch rewrites the
   matched tree shader at `glShaderSource`: it avoids the broken flat integer varying and replaces
   the fragment shader with a conservative direct texture-array version that keeps leaves, blossom,
   fruit, snow, fog, lighting, and opacity. Toggle with `MESA_OSTRIV_TREE_SHADER_HACK=0`.

## Layout

- `patch.py` — the user entry point (Python 3, stdlib only). Auto-detects the Ostriv bottle across
  CrossOver bottles, installs the driver (with `.bak` backups), sets the per-app override, bottle env
  vars, writes `steam_appid.txt`, and installs/adjusts `settings.data` (see `assets/`).
  Interactive Install/Reinstall/Restore, or `python3 patch.py <game-dir>`. Idempotent. Modeled on
  `cs2-macos-patcher/patch.py` for a consistent UX (same colours, banner, locator, restore
  instructions).
- `assets/settings.data` — bundled known-good game settings (multisampling off +
  `bFullscreenBorderlessWindow=1`). `ensure_settings()` installs it when the game has no
  `settings.data` yet; otherwise it flips the `bMultisampling` byte in place. Marked `binary` in
  `.gitattributes`.
- `scripts/build-driver.sh` — clones Mesa `mesa-26.1.3`, applies the patch, cross-compiles
  `libgallium_wgl.dll`+`opengl32.dll` with mingw. Output → `prebuilt/`.
- `patches/mesa-26.1.3-winemac-async-present.patch` — the Mesa source patch (GDI async final
  upload + stw gdi-compat default/MSAA suppression + Ostriv tree shader workaround). Regenerate with
  `git diff` in the Mesa tree.
- `prebuilt/` — drop-in DLLs (`opengl32`, `libgallium_wgl` 45 MB, `dxil`, `libwinpthread-1`),
  tracked via **Git LFS** (`.gitattributes` → `prebuilt/*.dll filter=lfs`).
- `docs/technical.md` — full narrative of every bug and fix, including dead-ends (Zink, llvmpipe).
  Caution: its "Performance tuning" section describes the old per-app virtual-desktop trick, which
  is **superseded** by the async-present driver patch — the "Fullscreen" section further down (and
  the rules below) are the current guidance.

## Commands

```bash
python3 patch.py            # interactive install/restore into the detected bottle
./scripts/build-driver.sh   # rebuild the patched driver from source (needs mingw-w64, meson, ninja, bison, python-mako)
```
There are no unit tests. Verify by launching the game and reading its log at
`<bottle>/drive_c/users/crossover/Saved Games/Ostriv/log.txt`
(success = `4.3 (Core Profile) Mesa` → `uiMainMenu`; failure = `windows_createWindow FAILED 0`).

Launching the game correctly:

- Start **Steam inside the bottle first** (it only needs to be *running*, for `SteamAPI_Init`), then
  launch via CrossOver **"Run Command"** with the **quoted** exe path (quotes required — spaces):
  `"C:\Program Files (x86)\Steam\steamapps\common\Ostriv\ostriv.exe"`.
- **Not Steam's Play button**: it injects the Steam overlay (`gameoverlayrenderer64.dll`), whose GL
  hooks conflict with the Mesa `opengl32` and crash the game ~2 s in — *before it writes its log*,
  so `log.txt` will be stale from the previous run. (Workaround if Play is needed: disable the
  overlay for Ostriv in its Steam properties.)
- **Not a bare `wine --cx-app` shell launch**: the window gets no foreground GUI app, winemac posts
  quit, and the game self-exits at the menu.
- The game process is named **`Menu Helper`**, not `ostriv.exe` — `pkill -f ostriv.exe` misses it.

## Conventions / hard-won rules — DO NOT violate

- **Never make `opengl32=native` a *global* override** — it breaks Steam's `vgui2_s.dll`. Always
  scope to `AppDefaults\ostriv.exe`.
- **Never force fullscreen with a Wine virtual desktop or `displayplacer`/resolution tools.** That
  corrupts winemac's display state (it then reports 1024×768 to the game regardless of the real
  resolution), and ONLY a full **quit-and-reopen of CrossOver** resets it (`wineserver -k` is not
  enough). Fullscreen = the game's own `bFullscreenBorderlessWindow=1` flag, no VD.
- **Never set `SteamAppId`/`SteamGameId` as *bottle* env vars** — they hit `steam.exe` too and
  crash-loop Steam's CEF browser (the Play button dies). Use a game-scoped `steam_appid.txt` next to
  `ostriv.exe` instead.
- **Multisampling must stay off** in the game settings, or it crashes. And `settings.data` must
  **exist**: if absent, the game uses hardcoded defaults (`multisampling=1`) and crashes at window
  creation — that's why patch.py installs the `assets/settings.data` template on fresh installs.
- **Bottle env overrides shell env.** `env GALLIUM_DRIVER=… wine …` experiments silently do
  nothing; edit the bottle's `cxbottle.conf` `[EnvironmentVariables]` instead.
- **Keep the DLLs in Git LFS.** Don't commit the 45 MB `libgallium_wgl.dll` as a raw blob.
- Pin the Mesa version (`mesa-26.1.3`) — the prebuilt DLLs and the patch line-numbers assume it.
- `settings.data` (game config) is a typed key-value blob (`fmt=5`, 25 keys) at
  `Saved Games/Ostriv/`; bools are 1 byte after the key name. It has **no absolute resolution field**
  — resolution is `flResolutionCoef × detected monitor`.

## Environment (reference machine)

Apple **M5 Max**, macOS, **CrossOver 26.2**, Ostriv **Alpha 5 patch 9 (0.5.9.58)**. Mesa **26.1.3**,
D3DMetal from CrossOver's Apple GPTK. Toolchain: mingw-w64, meson, ninja, bison ≥ 3, python-mako.
