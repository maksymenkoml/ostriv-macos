#!/bin/zsh
# Launch Ostriv: Wine 11 + WineMetalGL (OpenGL 4.6 on Metal) + Windows Steam without its window.
# - If Steam isn't running, asks for login/password (hidden, never stored) and waits for login.
# - If the game isn't downloaded yet, waits for Steam to download it (setup.sh adds the manifest).
# - The game must be registered by Steam ("adding PID" in gameprocess_log), otherwise it crashes
#   right after loading on SteamApps() == NULL — the script checks this.
source "${0:A:h}/env.sh"
STEAM_DIR="$WINEPREFIX/drive_c/Program Files (x86)/Steam"
CONN_LOG="$STEAM_DIR/logs/connection_log.txt"
GP_LOG="$STEAM_DIR/logs/gameprocess_log.txt"
CONTENT_LOG="$STEAM_DIR/logs/content_log.txt"
GAME_DIR="$STEAM_DIR/steamapps/common/Ostriv"
LOGS="$OSTRIV_HOME/logs"
mkdir -p "$LOGS"

# Steam is alive only if steam.exe is in Wine's process list: after its watchdog fires
# ("BMainLoop stalled") the unix process can linger while Steam is already dead.
steam_alive() {
    wine tasklist 2>/dev/null | tr -d '\r' | grep -qi '^steam\.exe '
}

steam_logged_on() {
    steam_alive || return 1
    grep -E '\[(Logged On|Logged Off|Logging On|Connecting)' "$CONN_LOG" 2>/dev/null | tail -1 | grep -q '\[Logged On'
}

# Steam thinks the game is still running (last entry is "adding PID"), but it isn't.
steam_has_stale_game() {
    pgrep -f 'ostriv.exe' >/dev/null && return 1
    grep -E 'AppID 773790 (adding PID|no longer tracking)' "$GP_LOG" 2>/dev/null | tail -1 | grep -q 'adding PID'
}

game_installed() {
    [[ -f "$GAME_DIR/ostriv.exe" ]] && grep -q 'AppID 773790 state changed : Fully Installed' "$CONTENT_LOG" 2>/dev/null
}

restart_steam() {
    echo "Restarting Steam."
    # steam.exe -shutdown STARTS Steam if it isn't running, so only call it when Steam is alive.
    if steam_alive; then
        wine "C:\\Program Files (x86)\\Steam\\steam.exe" -shutdown >/dev/null 2>&1 &
        sleep 8
    fi
    wineserver -k 2>/dev/null
    pkill -f 'Steam.steam' 2>/dev/null
    pkill -f 'steamwebhelper' 2>/dev/null
    sleep 1
    read "STEAM_USER?Steam login: "
    read -s "STEAM_PASS?Steam password (hidden): "
    echo
    echo "Signing in. If Steam Guard is on, approve the sign-in in the Steam mobile app."
    echo "The Steam window may be black — that's expected, leave it alone."
    (wine "C:\\Program Files (x86)\\Steam\\steam.exe" -silent -cef-disable-gpu -cef-disable-gpu-compositing \
        -login "$STEAM_USER" "$STEAM_PASS" > "$LOGS/steam.log" 2>&1 &)
    unset STEAM_PASS
    for i in {1..150}; do
        sleep 2
        steam_logged_on && break
    done
    if ! steam_logged_on; then
        echo "Steam did not sign in within 5 minutes. Check login/password and the mobile approval, then retry."
        exit 1
    fi
    echo "Steam signed in. Waiting 40 s for it to settle..."
    sleep 40
}

wait_for_download() {
    game_installed && return 0
    echo "Ostriv isn't downloaded yet — Steam downloads it by itself (~560 MB). Waiting..."
    for i in {1..720}; do
        sleep 5
        game_installed && { echo "Download finished."; sleep 5; return 0; }
        (( i % 6 == 0 )) && printf "  downloaded: %s\n" "$(du -sh "$STEAM_DIR/steamapps/downloading" 2>/dev/null | cut -f1)"
    done
    echo "The game didn't download within an hour. See $CONTENT_LOG"
    exit 1
}

# Starts the game and waits up to 40 s for Steam to register it. Returns 0 when registered.
launch_game() {
    pkill -f 'winedbg' 2>/dev/null
    pkill -f 'ostriv.exe' 2>/dev/null
    sleep 2
    local before=$(grep -c 'AppID 773790 adding PID' "$GP_LOG" 2>/dev/null)
    echo "Starting Ostriv..."
    (cd "$GAME_DIR" && WINEMETALGL_SETTINGS=0 wine ostriv.exe > "$LOGS/game.log" 2>&1 &)
    for i in {1..20}; do
        sleep 2
        [[ $(grep -c 'AppID 773790 adding PID' "$GP_LOG" 2>/dev/null) -gt $before ]] && return 0
    done
    return 1
}

if ! steam_logged_on || steam_has_stale_game; then
    steam_has_stale_game && echo "Steam still thinks the previous game is running."
    restart_steam
fi

wait_for_download

if launch_game; then
    echo "Steam picked up the game — have fun! Logs: $LOGS"
    exit 0
fi
pkill -f 'ostriv.exe' 2>/dev/null
echo "Steam didn't pick up the game (most likely it stalled on low memory)."
restart_steam
launch_game && { echo "Steam picked up the game — have fun!"; exit 0; }
echo "Didn't work. Logs are in $LOGS"
exit 1
