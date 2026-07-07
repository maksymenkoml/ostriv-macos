# Prebuilt Mesa driver (patched)

Drop-in DLLs, placed next to `ostriv.exe` by `../patch.py`:

- `opengl32.dll`        — Mesa WGL loader (patched)
- `libgallium_wgl.dll`  — Mesa Gallium + d3d12 driver (patched: async present, GDI-compat default, MSAA suppressed, Ostriv tree shader workaround) — 45 MB
- `dxil.dll`            — DXIL signing lib (from mesa-dist-win, unmodified)
- `libwinpthread-1.dll` — mingw pthreads runtime (dependency of the patched build)

Built for x86_64-windows (runs under CrossOver's x86_64 Wine). Rebuild with `../scripts/build-driver.sh`.

All `*.dll` here are tracked via **Git LFS** (see `../.gitattributes`).
