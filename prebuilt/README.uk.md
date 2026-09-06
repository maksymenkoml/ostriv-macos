[English](README.md) | Українська

# Зібраний драйвер Mesa (пропатчений)

Готові DLL, які `../patch.py` кладе поруч з `ostriv.exe`:

- `opengl32.dll`        — завантажувач Mesa WGL (пропатчений)
- `libgallium_wgl.dll`  — Mesa Gallium + драйвер d3d12 (пропатчений: асинхронний present, GDI-compat за замовчуванням, приглушення MSAA, обхід для шейдера дерев Ostriv, обхід flat-varying для Ostriv, опційний лог атрибуції PSO) — 45 МБ
- `dxil.dll`            — бібліотека підпису DXIL (з mesa-dist-win, без змін)
- `libwinpthread-1.dll` — рантайм mingw pthreads (залежність пропатченої збірки)

Зібрано для x86_64-windows (працює під x86_64 Wine у CrossOver). Перезбирання: `../scripts/build-driver.sh`.

Гравці отримують справжні (гідратовані) DLL всередині завантажуваного релізного архіву; Git або
Git LFS не потрібні. Git LFS стосується лише контриб'юторів репозиторію (див. `../.gitattributes`).
