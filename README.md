# Ostriv on Mac (Apple Silicon)

[Ostriv](https://store.steampowered.com/app/773790/Ostriv/) crashes on launch under
CrossOver. This project fixes it.

## How to play

You need: an Apple Silicon Mac, [CrossOver](https://www.codeweavers.com/crossover),
and Ostriv installed via Steam inside a CrossOver bottle.

Open Terminal and run:

```bash
git clone https://github.com/maksymenkoml/ostriv-macos
cd ostriv-macos
python3 patch.py
```

Choose **Install** and follow the instructions. Then:

1. Quit CrossOver (⌘Q) and reopen it — once.
2. Double-click **Ostriv (patched)** in `~/Applications/CrossOver`.

⚠️ Don't launch through Steam's **Play** button — the Steam overlay crashes the game.

To uninstall the fix: re-run `python3 patch.py` → **Restore**.

## If something goes wrong

- **Crash at startup** → re-run `python3 patch.py` and choose **Reinstall**.
- **Game window is tiny on a big screen** → fully quit CrossOver and reopen it.
- **Game closes ~2 seconds after launch** → you used Steam's Play button; use the
  **Ostriv (patched)** launcher.
- **Very low FPS (~10)** → you launched manually with a non-sRGB display profile; use the
  launcher, it sets the right profile automatically.
- Still stuck? Attach this file to a bug report:
  `<bottle>/drive_c/users/crossover/Saved Games/Ostriv/log.txt`

## What it does (short version)

Ostriv needs OpenGL 4.3; Macs only have 4.1, so the game can't start. The patcher installs
a custom graphics driver (Mesa, routed to Apple's Metal via CrossOver) next to the game,
tweaks a few CrossOver settings, and creates the **Ostriv (patched)** launcher. It also
preconfigures the game's graphics settings: multisampling off (it crashes the game on Mac)
and borderless fullscreen on. Game files are not modified; everything is undoable with
**Restore**.

## Under the hood

Full write-up of every bug and fix — GL 4.3 via Mesa's D3D12 driver, the MSAA startup
crash, the invisible-window present path, the async-present FPS patch, the tree-shader
workaround, the invisible reeds/grass (D3DMetal flat-input PSO bug), and the macOS
color-profile bottleneck: **[docs/technical.md](docs/technical.md)**.
That doc also covers rebuilding the patched driver from source instead of using the
prebuilt DLLs.

Tested: Apple M5 Max · CrossOver 26.2 · Ostriv 0.5.9.58.

## Credits & license

- [Mesa 3D](https://www.mesa3d.org/) (MIT) — `d3d12` driver by Microsoft; base Windows build
  by [pal1000/mesa-dist-win](https://github.com/pal1000/mesa-dist-win).
- D3DMetal (Apple Game Porting Toolkit, shipped with CrossOver).
- The Mesa patch in this repo is MIT, matching Mesa.

Unaffiliated with Ostriv's developer (Yevheniy Grebenyuk). Buy the game on
[Steam](https://store.steampowered.com/app/773790/Ostriv/).
