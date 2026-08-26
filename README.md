# Ostriv on Mac (Apple Silicon)

[Ostriv](https://store.steampowered.com/app/773790/Ostriv/) crashes on launch under
CrossOver. This project fixes it.

## Play

**[Download the latest player ZIP](https://github.com/maksymenkoml/ostriv-macos/releases/latest/download/ostriv-macos-player.zip).**

1. Extract the ZIP.
2. Open Terminal in the extracted folder and run `python3 patch.py`.
3. Choose **Install** and follow the prompts.
4. Open **Ostriv (patched)** from `~/Applications/CrossOver`.

You need an Apple Silicon Mac, [CrossOver](https://www.codeweavers.com/crossover), and
Ostriv installed through Steam in a CrossOver bottle. Don't launch through Steam's **Play** button;
use the patched launcher.

To remove the fix, open the patcher again and choose **Restore**. Game files are not
modified, and the installation is reversible.

## Troubleshooting

| Symptom | One action |
| --- | --- |
| **Package: FAILED** | Download and extract the latest player ZIP again. |
| **CrossOver/game not found** | Install Ostriv in a CrossOver Steam bottle, then rerun the patcher. |
| **Steam timeout** | Quit CrossOver completely, reopen it, then rerun the patcher. |
| **Graphics-context failure** | Quit CrossOver completely, then open the patched launcher again. |
| **Unexpected failure** | Run `python3 patch.py --diagnose` and attach the installer log to the bug report. |

Installer log: `~/Library/Logs/ostriv-macos/install.log`

## What it does

Ostriv needs OpenGL 4.3, while macOS provides 4.1. The patcher installs a custom Mesa
graphics driver routed to Apple's Metal through CrossOver, applies the required settings,
and creates the **Ostriv (patched)** launcher. It also disables multisampling, which
crashes the game on Mac, and enables borderless fullscreen.

The full technical explanation is in [docs/technical.md](docs/technical.md). Tested:
Apple M5 Max · CrossOver 26.2 · Ostriv 0.5.9.58.

## Development

Repository contributors need Git LFS so the checked-out DLLs are hydrated:

```bash
git clone https://github.com/maksymenkoml/ostriv-macos
cd ostriv-macos
git lfs install
git lfs pull
python3 -m unittest discover -s tests -v
```

Build and verify the no-Git player archive locally with:

```bash
python3 scripts/build-release.py --output dist/ostriv-macos-player.zip
```

To rebuild the driver itself, follow [docs/technical.md](docs/technical.md) and run
`scripts/build-driver.sh` from the repository checkout.

## Credits and license

- [Mesa 3D](https://www.mesa3d.org/) (MIT) — `d3d12` driver by Microsoft; base Windows
  build by [pal1000/mesa-dist-win](https://github.com/pal1000/mesa-dist-win).
- D3DMetal (Apple Game Porting Toolkit, shipped with CrossOver).
- The Mesa patch in this repository is MIT, matching Mesa.

This project is unaffiliated with Ostriv's developer, Yevheniy Grebenyuk. Buy the game on
[Steam](https://store.steampowered.com/app/773790/Ostriv/).
