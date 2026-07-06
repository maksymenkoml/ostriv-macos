#!/bin/zsh
# Ostriv on macOS — installer.
# Drops the Mesa (D3D12→D3DMetal) OpenGL driver into your Ostriv CrossOver bottle and configures it.
# Safe & idempotent: backs up any file it replaces (.bak) and only adds missing env vars.
set -euo pipefail

HERE="${0:A:h}"
PREBUILT="${HERE:h}/prebuilt"

CX="/Applications/CrossOver.app/Contents/SharedSupport/CrossOver"
[[ -x "$CX/bin/wine" ]] || { echo "ERROR: CrossOver not found at $CX"; exit 1; }

BOTTLES="$HOME/Library/Application Support/CrossOver/Bottles"

# --- locate the bottle + game dir (first bottle containing ostriv.exe) ---
GAME_DIR=""
BOTTLE=""
for b in "$BOTTLES"/*; do
  [[ -d "$b" ]] || continue
  hit=$(find "$b/drive_c" -maxdepth 8 -iname 'ostriv.exe' 2>/dev/null | head -1 || true)
  if [[ -n "$hit" ]]; then GAME_DIR="${hit:h}"; BOTTLE="${b:t}"; break; fi
done
[[ -n "$GAME_DIR" ]] || { echo "ERROR: couldn't find ostriv.exe in any CrossOver bottle."; exit 1; }
echo "→ Bottle:   $BOTTLE"
echo "→ Game dir: $GAME_DIR"

# --- 1. copy Mesa driver DLLs (backup any existing) ---
for dll in opengl32.dll libgallium_wgl.dll dxil.dll libwinpthread-1.dll; do
  src="$PREBUILT/$dll"; dst="$GAME_DIR/$dll"
  [[ -f "$src" ]] || { echo "ERROR: missing prebuilt $dll"; exit 1; }
  if [[ -f "$dst" && ! -f "$dst.bak" ]]; then cp "$dst" "$dst.bak"; fi
  cp "$src" "$dst"
  echo "  installed $dll"
done

# --- 2. per-app opengl32=native override (NEVER global — that breaks Steam) ---
"$CX/bin/wine" --bottle "$BOTTLE" --no-update --no-lock \
  reg add 'HKCU\Software\Wine\AppDefaults\ostriv.exe\DllOverrides' /v opengl32 /d native /f >/dev/null 2>&1
echo "  set opengl32=native (ostriv.exe only)"

# --- 3. bottle environment variables (add if missing) ---
CONF="$BOTTLES/$BOTTLE/cxbottle.conf"
[[ -f "$CONF.bak" ]] || cp "$CONF" "$CONF.bak"
python3 - "$CONF" <<'PY'
import sys
p=sys.argv[1]; s=open(p).read()
env={
 "SteamAppId":"773790","SteamGameId":"773790",
 "GALLIUM_DRIVER":"d3d12","wgl_require_gdi_compat":"true","MESA_D3D12_ASYNC_PRESENT":"1",
 "MESA_GL_VERSION_OVERRIDE":"4.3","MESA_GLSL_VERSION_OVERRIDE":"430",
}
if "[EnvironmentVariables]" not in s:
    s=s.rstrip()+"\n\n[EnvironmentVariables]\n"
lines=[]
for k,v in env.items():
    if f'"{k}"' not in s:
        lines.append(f'"{k}" = "{v}"')
if lines:
    s=s.replace("[EnvironmentVariables]", "[EnvironmentVariables]\n"+"\n".join(lines),1)
    open(p,"w").write(s)
    print("  added env: "+", ".join(l.split('"')[1] for l in lines))
else:
    print("  env already present")
PY

cat <<EOF

✅ Installed. Three manual steps remain:

  1. Run ostriv_settings (in CrossOver: the "ostriv_settings" launcher) and set
     MULTISAMPLING = OFF, then Save.  (required — MSAA on crashes the game)

  2. Fully QUIT CrossOver and reopen it.  (loads the new bottle settings)

  3. Start Steam inside the bottle, then launch Ostriv.

If the game ever renders tiny on a big screen: fully quit CrossOver and reopen it.
To uninstall the driver: restore the *.bak files in
  $GAME_DIR
EOF
