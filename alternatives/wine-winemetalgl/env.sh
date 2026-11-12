# Shared Wine settings for Ostriv. Sourced by play-ostriv.sh; setup.sh pins OSTRIV_HOME below.
OSTRIV_HOME="${OSTRIV_HOME:-$HOME/Games/Ostriv}"
export WINEPREFIX="$OSTRIV_HOME/prefix"
# Wine 11 (Gcenx build, x86_64 under Rosetta) + WineMetalGL (OpenGL 4.6 on Metal)
WINE_BIN="$OSTRIV_HOME/wine11/Wine Stable.app/Contents/Resources/wine/bin"
export PATH="$WINE_BIN:$PATH"
export WINEDEBUG="${WINEDEBUG:--all}"
export WINEESYNC=1 WINEMSYNC=1
# winemenubuilder writes shortcuts outside the install folder — keep it off.
export WINEDLLOVERRIDES="winemenubuilder.exe=d"
# Keep WineMetalGL logs inside the install folder instead of ~/Library/Logs.
export WINEMETALGL_LOG_FILE="$OSTRIV_HOME/logs"
