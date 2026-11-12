#!/bin/zsh
# Install Ostriv on an Apple Silicon Mac without CrossOver:
#   Wine 11.0 (Gcenx build, x86_64 under Rosetta) + WineMetalGL 3.9.0 (OpenGL 4.6 on Metal)
#   + Windows Steam in its own Wine prefix.
# Everything goes into one folder: $OSTRIV_HOME (default ~/Games/Ostriv).
# Safe to re-run: finished steps are skipped.
set -euo pipefail

SRC="${0:A:h}"
OSTRIV_HOME="${OSTRIV_HOME:-$HOME/Games/Ostriv}"
export OSTRIV_HOME

WINE_VERSION="11.0_1"
WINE_URL="https://github.com/Gcenx/macOS_Wine_builds/releases/download/$WINE_VERSION/wine-stable-$WINE_VERSION-osx64.tar.xz"
WINE_SHA256="b50dc50ec7f41d58b115a6b685d4d1315ba3c797bd3aa0f49213f2703cb82388"

WMGL_VERSION="3.9.0"
WMGL_BASE="https://github.com/metalsharp/WineMetalGL/releases/download/winemetalgl-$WMGL_VERSION"
WMGL_PKG="winemetalgl-$WMGL_VERSION-x86_64"

STEAM_SETUP_URL="https://cdn.cloudflare.steamstatic.com/client/installer/SteamSetup.exe"

# settings.data from this repository (MSAA off, borderless window). Without it the game starts with
# MSAA, which crashes on window creation on macOS.
SETTINGS_LOCAL="$SRC/../../assets/settings.data"
SETTINGS_URL="https://raw.githubusercontent.com/maksymenkoml/ostriv-macos/main/assets/settings.data"
SETTINGS_SHA256="08bdbb1bb0aacdbc2d4aff6d0a22d653af90516ab151cd4e3ec60847e2efd8e1"

step() { print -P "\n%B==> $1%b"; }
die()  { print -P "%F{red}Error:%f $1" >&2; exit 1; }

check_sha() { # file expected
    local got=$(shasum -a 256 "$1" | awk '{print $1}')
    [[ "$got" == "$2" ]] || die "checksum mismatch for $1 ($got)"
}

step "Checking the system"
[[ "$(uname -m)" == arm64 ]] || die "an Apple Silicon Mac is required"
arch -x86_64 /usr/bin/true 2>/dev/null || {
    echo "Rosetta 2 is required. Installing it (may ask for an admin password)..."
    softwareupdate --install-rosetta --agree-to-license
}
echo "Install folder: $OSTRIV_HOME"
mkdir -p "$OSTRIV_HOME/downloads" "$OSTRIV_HOME/logs"

step "Launch scripts"
cp "$SRC/env.sh" "$SRC/play-ostriv.sh" "$SRC/Play Ostriv.command" "$OSTRIV_HOME/"
sed -i '' "s|^OSTRIV_HOME=.*|OSTRIV_HOME=\"$OSTRIV_HOME\"|" "$OSTRIV_HOME/env.sh"
chmod +x "$OSTRIV_HOME/play-ostriv.sh" "$OSTRIV_HOME/Play Ostriv.command"
source "$OSTRIV_HOME/env.sh"

step "Wine $WINE_VERSION (Gcenx)"
if [[ ! -x "$WINE_BIN/wine" ]]; then
    tmp="$OSTRIV_HOME/downloads/wine.tar.xz"
    curl -fL --progress-bar -o "$tmp" "$WINE_URL"
    check_sha "$tmp" "$WINE_SHA256"
    mkdir -p "$OSTRIV_HOME/wine11"
    tar -xf "$tmp" -C "$OSTRIV_HOME/wine11"
    rm -f "$tmp"
    xattr -dr com.apple.quarantine "$OSTRIV_HOME/wine11" 2>/dev/null || true
else
    echo "already installed"
fi
"$WINE_BIN/wine" --version

step "Wine prefix (Windows 10)"
if [[ ! -d "$WINEPREFIX/drive_c/windows" ]]; then
    WINEDLLOVERRIDES="winemenubuilder.exe=d;mscoree,mshtml=d" wine wineboot --init >"$OSTRIV_HOME/logs/setup-wineboot.log" 2>&1
    wineserver -w
fi
wine winecfg -v win10 >/dev/null 2>&1; wineserver -w
echo "done: $WINEPREFIX"

step "WineMetalGL $WMGL_VERSION"
WMGL_DIR="$OSTRIV_HOME/downloads/winemetalgl"
if [[ ! -f "$WINE_BIN/../lib/wine/x86_64-unix/winemetalgl.so" ]] || \
   ! grep -q '"opengl32"="native"' "$WINEPREFIX/user.reg" 2>/dev/null; then
    mkdir -p "$WMGL_DIR"
    curl -fsSL -o "$WMGL_DIR/SHA256SUMS.txt" "$WMGL_BASE/SHA256SUMS.txt"
    curl -fL --progress-bar -o "$WMGL_DIR/$WMGL_PKG.tar.gz" "$WMGL_BASE/$WMGL_PKG.tar.gz"
    check_sha "$WMGL_DIR/$WMGL_PKG.tar.gz" "$(awk -v f="$WMGL_PKG.tar.gz" '$2==f{print $1}' "$WMGL_DIR/SHA256SUMS.txt")"
    tar -xzf "$WMGL_DIR/$WMGL_PKG.tar.gz" -C "$WMGL_DIR"
    rm -f "$WMGL_DIR/$WMGL_PKG.tar.gz"
    # Installs winemetalgl.* into Wine's lib/wine, opengl32.dll into the prefix, and opengl32=native.
    sh "$WMGL_DIR/$WMGL_PKG/install-wine-module.sh" "$OSTRIV_HOME/wine11/Wine Stable.app" "$WINEPREFIX"
else
    echo "already installed"
fi

step "Windows Steam"
STEAM_DIR="$WINEPREFIX/drive_c/Program Files (x86)/Steam"
if [[ ! -f "$STEAM_DIR/steam.exe" ]]; then
    curl -fsSL -o "$OSTRIV_HOME/downloads/SteamSetup.exe" "$STEAM_SETUP_URL"
    # wine waits for the installer itself; if it leaves Steam running, stop everything —
    # the first real start happens through play-ostriv.sh.
    wine "$OSTRIV_HOME/downloads/SteamSetup.exe" /S >/dev/null 2>&1 || true
    sleep 3
    wineserver -k 2>/dev/null || true
    rm -f "$OSTRIV_HOME/downloads/SteamSetup.exe"
else
    echo "already installed"
fi
[[ -f "$STEAM_DIR/steam.exe" ]] || die "Steam did not install"

step "Ostriv app manifest (Steam downloads the game by itself after sign-in)"
mkdir -p "$STEAM_DIR/steamapps"
MANIFEST="$STEAM_DIR/steamapps/appmanifest_773790.acf"
if [[ ! -f "$MANIFEST" ]]; then
    printf '"AppState"\n{\n\t"appid"\t\t"773790"\n\t"Universe"\t\t"1"\n\t"name"\t\t"Ostriv"\n\t"StateFlags"\t\t"1026"\n\t"installdir"\t\t"Ostriv"\n\t"AutoUpdateBehavior"\t\t"0"\n}\n' > "$MANIFEST"
fi
echo "done"

step "settings.data (MSAA off)"
SAVE_DIR="$WINEPREFIX/drive_c/users/$USER/Saved Games/Ostriv"
mkdir -p "$SAVE_DIR"
if [[ ! -f "$SAVE_DIR/settings.data" ]]; then
    if [[ -f "$SETTINGS_LOCAL" ]]; then
        cp "$SETTINGS_LOCAL" "$SAVE_DIR/settings.data"
    else
        curl -fsSL -o "$SAVE_DIR/settings.data" "$SETTINGS_URL"
    fi
    check_sha "$SAVE_DIR/settings.data" "$SETTINGS_SHA256"
fi
echo "done: $SAVE_DIR"

step "Finished"
cat <<EOF
Everything is installed in $OSTRIV_HOME.

Next:
  1. Quit other apps (the game needs a lot of memory).
  2. Double-click "Play Ostriv.command" in $OSTRIV_HOME
     or run: $OSTRIV_HOME/play-ostriv.sh
  3. Enter your Steam login and password (hidden) and approve the sign-in on your phone.
     The Steam window will be black — that's expected.
  4. On the first run Steam downloads Ostriv (~560 MB); the script waits and then starts the game.
EOF
