#!/bin/zsh
# Build the patched Mesa D3D12 WGL driver (libgallium_wgl.dll + opengl32.dll) from source.
# Requires: mingw-w64, meson, ninja, bison, python-mako  (brew install mingw-w64 meson ninja bison)
set -euo pipefail

HERE="${0:A:h}"
ROOT="${HERE:h}"
WORK="$ROOT/.build"
MESA_TAG="mesa-26.1.3"

export PATH="/opt/homebrew/opt/bison/bin:$PATH"   # Mesa needs bison >= 3, macOS ships 2.3

mkdir -p "$WORK"; cd "$WORK"
if [[ ! -d mesa-src ]]; then
  git clone --depth 1 --branch "$MESA_TAG" https://gitlab.freedesktop.org/mesa/mesa.git mesa-src
fi
cd mesa-src

# apply our patch (idempotent-ish: skip if already applied)
if ! git apply --reverse --check "$ROOT/patches/${MESA_TAG}-winemac-async-present.patch" 2>/dev/null; then
  git apply "$ROOT/patches/${MESA_TAG}-winemac-async-present.patch"
  echo "patch applied"
else
  echo "patch already applied"
fi

# mingw cross file
cat > cross-x86_64.txt <<'EOF'
[binaries]
c = 'x86_64-w64-mingw32-gcc'
cpp = 'x86_64-w64-mingw32-g++'
ar = 'x86_64-w64-mingw32-ar'
strip = 'x86_64-w64-mingw32-strip'
windres = 'x86_64-w64-mingw32-windres'
pkg-config = 'false'
[properties]
needs_exe_wrapper = true
[host_machine]
system = 'windows'
cpu_family = 'x86_64'
cpu = 'x86_64'
endian = 'little'
EOF

[[ -d build-w64 ]] || meson setup build-w64 --cross-file cross-x86_64.txt \
  -Dbuildtype=release -Dgallium-drivers=d3d12 "-Dvulkan-drivers=" \
  -Dglx=disabled -Degl=disabled -Dgles1=disabled -Dgles2=disabled -Dllvm=disabled \
  -Dshared-glapi=disabled -Dmicrosoft-clc=disabled -Dspirv-to-dxil=false -Dzlib=disabled "-Dvideo-codecs="
ninja -C build-w64

cp build-w64/src/gallium/targets/wgl/libgallium_wgl.dll "$ROOT/prebuilt/"
cp build-w64/src/gallium/targets/libgl-gdi/opengl32.dll "$ROOT/prebuilt/"
echo "✅ Built. Updated prebuilt/libgallium_wgl.dll + opengl32.dll"
echo "   (dxil.dll + libwinpthread-1.dll are shipped as-is; not part of this build.)"
