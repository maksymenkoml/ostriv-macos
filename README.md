# Ostriv on macOS (Apple Silicon) — GPU-accelerated, fullscreen

Run **[Ostriv](https://store.steampowered.com/app/773790/Ostriv/)** on an Apple Silicon Mac
(M1–M5) under **CrossOver / Wine** — **GPU-accelerated, fullscreen, smooth**.

> **Why this is needed:** Ostriv requires **OpenGL 4.3**. Apple's OpenGL is permanently frozen at
> **4.1**, so the game crashes on launch under stock Wine/CrossOver. This project routes the game's
> OpenGL through **Mesa's D3D12 driver → Apple's D3DMetal → Metal**, reaching GL 4.3 on the GPU, and
> fixes the two follow-on bugs (a multisampling crash and an invisible window). A small **Mesa
> source patch** then makes fullscreen actually fast (~7 fps → **locked 30–60**).

Tested on **Apple M5 Max**, macOS, **CrossOver 26.2**, Ostriv **Alpha 5 patch 9 (0.5.9.58)**.

---

## Quick start

**Requirements**
- Apple Silicon Mac
- [CrossOver](https://www.codeweavers.com/crossover) (paid; 14-day trial works)
- Ostriv installed via Steam inside a CrossOver bottle

**Install**
```bash
git clone https://github.com/<you>/ostriv-macos
cd ostriv-macos
python3 patch.py
```
`patch.py` auto-detects your Ostriv bottle, installs the Mesa driver, and configures everything
(Python 3, stdlib only — no dependencies). It's interactive; you can also pass the game dir directly:
`python3 patch.py "/path/to/…/steamapps/common/Ostriv"`. Then:

1. **Fully quit CrossOver and reopen it.** *(required once, to load the new bottle settings)*
2. Start **Steam** inside the bottle, then launch **Ostriv**.

`patch.py` installs a `settings.data` with **multisampling off + borderless fullscreen** for fresh
installs, so there's no manual settings step. *(If you already had a `settings.data`, it just flips
multisampling off in place.)*

That's it — the game runs GPU-accelerated and fullscreen. To undo, re-run `python3 patch.py` and
choose **Restore**.

---

## What `patch.py` does

| Step | Detail |
|---|---|
| Mesa driver | Copies `opengl32.dll`, `libgallium_wgl.dll`, `dxil.dll`, `libwinpthread-1.dll` next to `ostriv.exe` |
| DLL override | `HKCU\Software\Wine\AppDefaults\ostriv.exe\DllOverrides` → `opengl32=native` (scoped to Ostriv only — a *global* override breaks Steam) |
| Bottle env | `GALLIUM_DRIVER=d3d12`, `wgl_require_gdi_compat=true`, `MESA_D3D12_ASYNC_PRESENT=1`, `MESA_GL_VERSION_OVERRIDE=4.3`, `MESA_GLSL_VERSION_OVERRIDE=430` |
| Steam app id | Writes `steam_appid.txt` (773790) next to `ostriv.exe` — **not** a bottle-wide `SteamAppId` env, which would make `steam.exe` itself think it's the game and crash Steam's browser |
| Settings | Installs a `settings.data` (multisampling off + borderless fullscreen) on fresh installs, or flips multisampling off in an existing one |

Nothing is overwritten without a `.bak` backup, and **Restore** (option 2) undoes it all. See
**[docs/technical.md](docs/technical.md)** for the full explanation of every bug and fix.

---

## Two things that trip people up

- **Multisampling must be OFF.** With MSAA on, Ostriv calls `wglChoosePixelFormatARB`, which makes
  Mesa recursively create a second window — an operation winemac can't do — and the game crashes at
  startup (`windows_createWindow FAILED 0`). MSAA off avoids that path entirely; the game looks
  virtually identical.
- **If the game ever renders tiny (e.g. 1024×768) on a big screen:** winemac's display state got
  confused (usually after changing your Mac's resolution). **Fully quit CrossOver and reopen it** —
  do *not* use display-resolution tools to force fullscreen, they corrupt winemac's state.
- **If Play launches the game but it closes after ~2 seconds** (Steam shows it "running" briefly):
  the **Steam in-game overlay** conflicts with the custom Mesa `opengl32` and crashes it at startup.
  Fix: Steam → **Library → right-click Ostriv → Properties → General → uncheck "Enable the Steam
  Overlay while in-game"**, then Play. (Launching directly via CrossOver "Run Command" → `ostriv.exe`
  also works — the overlay only injects on a Steam-Play launch.)
- **If Steam's Play button does nothing / Steam keeps crashing** (`crash_steam.exe` dumps, the
  browser UI restarting): that's a Steam-under-Wine issue, not the patch. Quit Steam, delete these
  folders in the bottle, and relaunch Steam: `…/Steam/appcache/httpcache`,
  `…/AppData/Local/Steam/htmlcache`, and any `GPUCache`. (A bottle-wide `SteamAppId` env var also
  causes this — `patch.py` deliberately avoids it, using a game-scoped `steam_appid.txt` instead.)

## Performance / fullscreen

- Fullscreen is the game's own **`bFullscreenBorderlessWindow`** setting (borderless, no Wine
  virtual desktop). It fills your screen at the display's current resolution.
- The prebuilt driver includes an **async-present patch**: it pushes finished GPU frames to the
  window on a worker thread and drops frames instead of stalling, so full-resolution fullscreen
  stays fluid. Disable with `MESA_D3D12_ASYNC_PRESENT=0` if you ever want stock behavior.
- Want more FPS headroom? Lower your **macOS display resolution** (System Settings → Displays) before
  launching, or raise Ostriv's in-game **FPS limit**.

## Building the driver yourself

The prebuilt DLLs in [`prebuilt/`](prebuilt/) are ready to use. To rebuild from source:

```bash
./scripts/build-driver.sh   # clones Mesa 26.1.3, applies the patch, cross-compiles with mingw
```
The patch is [`patches/mesa-26.1.3-winemac-async-present.patch`](patches/) — two files:
`d3d12_screen.cpp` (async GDI present) and `stw_pixelformat.c` (default `wgl_require_gdi_compat`
on + suppress MSAA formats).

## Credits & license

- OpenGL implementation: **[Mesa 3D](https://www.mesa3d.org/)** (MIT) — `d3d12` driver by Microsoft,
  base Windows build by [pal1000/mesa-dist-win](https://github.com/pal1000/mesa-dist-win).
- D3D→Metal translation: **D3DMetal** (Apple Game Porting Toolkit, shipped in CrossOver).
- The winemac async-present patch in this repo is MIT, matching Mesa.

This project is unaffiliated with Ostriv's developer (Yevheniy Grebenyuk). Buy the game on
[Steam](https://store.steampowered.com/app/773790/Ostriv/).
