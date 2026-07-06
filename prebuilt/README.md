# Prebuilt Mesa driver (patched)

Drop-in DLLs, placed next to `ostriv.exe` by `../scripts/install.sh`:

- `opengl32.dll`        — Mesa WGL loader (patched)
- `libgallium_wgl.dll`  — Mesa Gallium + d3d12 driver (patched: async present, GDI-compat default, MSAA suppressed) — 45 MB
- `dxil.dll`            — DXIL signing lib (from mesa-dist-win, unmodified)
- `libwinpthread-1.dll` — mingw pthreads runtime (dependency of the patched build)

Built for x86_64-windows (runs under CrossOver's x86_64 Wine). Rebuild with `../scripts/build-driver.sh`.

> `libgallium_wgl.dll` is 45 MB — consider Git LFS or attaching these to a GitHub Release
> rather than committing directly.
