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

## The three fixes (all required together)

1. **GL 4.3 itself** — `GALLIUM_DRIVER=d3d12` + `wgl_require_gdi_compat=true`, with the Mesa
   `opengl32`/`libgallium_wgl` DLLs dropped next to `ostriv.exe` and `opengl32=native` scoped to
   `ostriv.exe`. `wgl_require_gdi_compat` forces Mesa off its DXGI swapchain (which winemac never
   composites) onto the **GDI `flush_frontbuffer` present** path winemac *does* composite.
2. **Startup crash = multisampling.** MSAA makes the game call `wglChoosePixelFormatARB`, which makes
   Mesa recursively create a second window — winemac can't, so it crashes (`windows_createWindow
   FAILED 0`). Fix: **multisampling OFF** in the game settings. The patched driver also suppresses
   MSAA pixel formats as a belt-and-suspenders (`stw_pixelformat.c`).
3. **Fullscreen FPS** — the GDI present flush is expensive; done inline it caps fullscreen at ~7 fps.
   The patch (`d3d12_screen.cpp`, `d3d12_flush_frontbuffer`) offloads the flush to a **worker thread
   and drops frames** while it's busy → locked 30–60. Toggle with `MESA_D3D12_ASYNC_PRESENT=0`.

## Layout

- `patch.py` — the user entry point (Python 3, stdlib only). Auto-detects the Ostriv bottle across
  CrossOver bottles, installs the driver (with `.bak` backups), sets the per-app override, bottle env
  vars, and multisampling-off. Interactive Install/Restore, or `python3 patch.py <game-dir>`.
  Idempotent. Modeled on `cs2-macos-patcher/patch.py` for a consistent UX (same colours, banner,
  locator, restore instructions).
- `scripts/build-driver.sh` — clones Mesa `mesa-26.1.3`, applies the patch, cross-compiles
  `libgallium_wgl.dll`+`opengl32.dll` with mingw. Output → `prebuilt/`.
- `patches/mesa-26.1.3-winemac-async-present.patch` — the two-file source patch (d3d12 async present +
  stw gdi-compat default/MSAA suppression). Regenerate with `git diff` in the Mesa tree.
- `prebuilt/` — drop-in DLLs (`opengl32`, `libgallium_wgl` 45 MB, `dxil`, `libwinpthread-1`),
  tracked via **Git LFS** (`.gitattributes` → `prebuilt/*.dll filter=lfs`).
- `docs/technical.md` — full narrative of every bug and fix, including dead-ends (Zink, llvmpipe).

## Commands

```bash
python3 patch.py            # interactive install/restore into the detected bottle
./scripts/build-driver.sh   # rebuild the patched driver from source (needs mingw-w64, meson, ninja, bison)
```
There are no unit tests. Verify by launching the game and reading its log at
`<bottle>/drive_c/users/crossover/Saved Games/Ostriv/log.txt`
(success = `4.3 (Core Profile) Mesa` → `uiMainMenu`; failure = `windows_createWindow FAILED 0`).

## Conventions / hard-won rules — DO NOT violate

- **Never make `opengl32=native` a *global* override** — it breaks Steam's `vgui2_s.dll`. Always
  scope to `AppDefaults\ostriv.exe`.
- **Never force fullscreen with a Wine virtual desktop or `displayplacer`/resolution tools.** That
  corrupts winemac's display state (it then reports 1024×768 to the game regardless of the real
  resolution), and ONLY a full **quit-and-reopen of CrossOver** resets it (`wineserver -k` is not
  enough). Fullscreen = the game's own `bFullscreenBorderlessWindow=1` flag, no VD.
- **Multisampling must stay off** in the game settings, or it crashes.
- **Keep the DLLs in Git LFS.** Don't commit the 45 MB `libgallium_wgl.dll` as a raw blob.
- Pin the Mesa version (`mesa-26.1.3`) — the prebuilt DLLs and the patch line-numbers assume it.
- `settings.data` (game config) is a typed key-value blob (`fmt=5`, 25 keys) at
  `Saved Games/Ostriv/`; bools are 1 byte after the key name. It has **no absolute resolution field**
  — resolution is `flResolutionCoef × detected monitor`.

## Environment (reference machine)

Apple **M5 Max**, macOS, **CrossOver 26.2**, Ostriv **Alpha 5 patch 9 (0.5.9.58)**. Mesa **26.1.3**,
D3DMetal from CrossOver's Apple GPTK. Toolchain: mingw-w64, meson, ninja, bison ≥ 3.
