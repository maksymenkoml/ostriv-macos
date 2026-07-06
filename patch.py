#!/usr/bin/env python3
"""
Ostriv — macOS / Wine GPU-acceleration installer
Tested: CrossOver 26 · Ostriv 0.5.9.58 · Apple Silicon (M5 Max)

Installs a Mesa OpenGL driver (D3D12 -> D3DMetal -> Metal) into your Ostriv
CrossOver bottle so the game runs GPU-accelerated and fullscreen. No game files
are modified; the driver DLLs sit next to ostriv.exe and every replaced file is
backed up to *.bak.
"""

import os
import sys
import re
import plistlib
import subprocess
import shutil

try:
    import termios
    import tty
except ImportError:  # non-POSIX; select() falls back to numbered input
    termios = tty = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PREBUILT = os.path.join(SCRIPT_DIR, "prebuilt")
DRIVER_DLLS = ["opengl32.dll", "libgallium_wgl.dll", "dxil.dll", "libwinpthread-1.dll"]

BOTTLES_ROOT = os.path.expanduser("~/Library/Application Support/CrossOver/Bottles")
CROSSOVER = "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver"

APPID = "773790"
TESTED_CROSSOVER = "26.2"
TESTED_OSTRIV = "0.5.9.58"


def crossover_version():
    """Installed CrossOver version, or None."""
    try:
        with open(os.path.join(CROSSOVER.split("/SharedSupport")[0], "Info.plist"), "rb") as f:
            d = plistlib.load(f)
        return d.get("CFBundleShortVersionString") or d.get("CFBundleVersion")
    except Exception:
        return None


def ostriv_version(bottle):
    """Ostriv version parsed from its log (only exists after the game has run once), or None.
    Log line 3 looks like: 'Alpha 5 patch 9 hotfix 58 (0.5.9.58 Jun  4 2026 ...)'."""
    log = os.path.join(BOTTLES_ROOT, bottle, "drive_c", "users", "crossover",
                       "Saved Games", "Ostriv", "log.txt")
    try:
        with open(log, errors="ignore") as f:
            head = f.read(400)
        m = re.search(r"\((\d+\.\d+\.\d+\.\d+)", head)
        return m.group(1) if m else None
    except Exception:
        return None

# Bottle environment the game needs (GL 4.3 via d3d12, visible via gdi present, fast via async present).
# NOTE: do NOT put SteamAppId/SteamGameId here — bottle env is applied to *every* process in the
# bottle, incl. steam.exe, which then thinks it IS the game and its CEF browser crash-loops. The
# game instead gets its app id from a game-scoped steam_appid.txt (see write_appid_file).
BOTTLE_ENV = {
    "GALLIUM_DRIVER": "d3d12",
    "wgl_require_gdi_compat": "true",
    "MESA_D3D12_ASYNC_PRESENT": "1",
    "MESA_GL_VERSION_OVERRIDE": "4.3",
    "MESA_GLSL_VERSION_OVERRIDE": "430",
}

# ──────────────────────────────────────────────────────────────────────────────
# Colours
# ──────────────────────────────────────────────────────────────────────────────

def c(text, code): return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text
def green(t):  return c(t, "32")
def yellow(t): return c(t, "33")
def cyan(t):   return c(t, "36")
def red(t):    return c(t, "31")
def bold(t):   return c(t, "1")


# ──────────────────────────────────────────────────────────────────────────────
# Game locator — CrossOver bottles
# ──────────────────────────────────────────────────────────────────────────────

def find_game_installations():
    """Return list of (bottle_name, game_dir) where ostriv.exe lives."""
    results = []
    if not os.path.isdir(BOTTLES_ROOT):
        return results
    for bottle in sorted(os.listdir(BOTTLES_ROOT)):
        drive_c = os.path.join(BOTTLES_ROOT, bottle, "drive_c")
        if not os.path.isdir(drive_c):
            continue
        for game_dir in _search_ostriv(drive_c, depth=8):
            results.append((bottle, game_dir))
            break  # one hit per bottle is enough
    return results


def _search_ostriv(root, depth):
    if depth == 0:
        return
    try:
        entries = os.listdir(root)
    except (PermissionError, OSError):
        return
    for entry in entries:
        full = os.path.join(root, entry)
        if entry.lower() == "ostriv.exe" and os.path.isfile(full):
            yield os.path.dirname(full)
            return
        if os.path.isdir(full):
            yield from _search_ostriv(full, depth - 1)


def _bottle_of(game_dir):
    """Recover the bottle name from a game dir path."""
    rel = os.path.relpath(game_dir, BOTTLES_ROOT)
    return rel.split(os.sep)[0]


# ──────────────────────────────────────────────────────────────────────────────
# CrossOver / wine
# ──────────────────────────────────────────────────────────────────────────────

def wine_reg(bottle, *args):
    """Run a wine reg command in the given bottle. Returns (returncode, stdout)."""
    wine = os.path.join(CROSSOVER, "bin", "wine")
    if not os.access(wine, os.X_OK):
        return (1, "")
    cmd = [wine, "--bottle", bottle, "--no-update", "--no-lock", "reg", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return (r.returncode, r.stdout)


# ──────────────────────────────────────────────────────────────────────────────
# Install steps
# ──────────────────────────────────────────────────────────────────────────────

def install_driver(game_dir):
    """Copy the Mesa driver DLLs next to ostriv.exe, backing up any existing file."""
    for dll in DRIVER_DLLS:
        src = os.path.join(PREBUILT, dll)
        dst = os.path.join(game_dir, dll)
        if not os.path.isfile(src):
            print(f"  {red('MISS')}  prebuilt/{dll} not found — is the repo complete (Git LFS pulled)?")
            return False
        if os.path.isfile(dst) and not os.path.isfile(dst + ".bak"):
            shutil.copy2(dst, dst + ".bak")
        shutil.copy2(src, dst)
        print(f"  {green('OK')}    {dll}")
    return True


def write_appid_file(game_dir):
    """Write steam_appid.txt next to ostriv.exe so SteamAPI_Init finds the app id WITHOUT a
    bottle-wide SteamAppId env var (which would crash Steam's own processes)."""
    path = os.path.join(game_dir, "steam_appid.txt")
    if os.path.isfile(path) and open(path).read().strip() == APPID:
        print(f"  {green('OK')}    steam_appid.txt (already present)")
        return
    with open(path, "w") as f:
        f.write(APPID)
    print(f"  {green('OK')}    steam_appid.txt ({APPID})")


def set_override(bottle):
    """Scope opengl32=native to ostriv.exe (never global — that breaks Steam).

    The first wine call after editing cxbottle.conf can reboot wineserver and return
    non-zero mid-boot, so retry a couple of times and verify by querying the value back.
    """
    key = r"HKCU\Software\Wine\AppDefaults\ostriv.exe\DllOverrides"
    ok = False
    for _ in range(3):
        wine_reg(bottle, "add", key, "/v", "opengl32", "/d", "native", "/f")
        _, out = wine_reg(bottle, "query", key, "/v", "opengl32")
        if "native" in out.lower():
            ok = True
            break
    print(f"  {green('OK') if ok else yellow('WARN')}    opengl32=native (ostriv.exe only)")
    return ok


def set_env(bottle):
    """Add the needed env vars to the bottle's cxbottle.conf (idempotent)."""
    conf = os.path.join(BOTTLES_ROOT, bottle, "cxbottle.conf")
    if not os.path.isfile(conf):
        print(f"  {yellow('WARN')}  cxbottle.conf not found; skipping env setup")
        return False
    if not os.path.isfile(conf + ".bak"):
        shutil.copy2(conf, conf + ".bak")
    with open(conf) as f:
        s = f.read()
    if "[EnvironmentVariables]" not in s:
        s = s.rstrip() + "\n\n[EnvironmentVariables]\n"
    added = []
    new_lines = []
    for k, v in BOTTLE_ENV.items():
        if f'"{k}"' not in s:
            new_lines.append(f'"{k}" = "{v}"')
            added.append(k)
    if new_lines:
        s = s.replace("[EnvironmentVariables]",
                      "[EnvironmentVariables]\n" + "\n".join(new_lines), 1)
        with open(conf, "w") as f:
            f.write(s)
    print(f"  {green('OK')}    bottle env " +
          ("(" + ", ".join(added) + ")" if added else "(already present)"))
    return True


def ensure_settings(bottle):
    """Guarantee multisampling is OFF (MSAA on = crash). If the game has no settings.data
    yet, drop in our bundled template (MSAA off, borderless fullscreen); otherwise just
    flip the multisampling flag in place."""
    sg_dir = os.path.join(BOTTLES_ROOT, bottle, "drive_c", "users", "crossover",
                          "Saved Games", "Ostriv")
    settings = os.path.join(sg_dir, "settings.data")
    template = os.path.join(SCRIPT_DIR, "assets", "settings.data")
    if not os.path.isfile(settings):
        if os.path.isfile(template):
            os.makedirs(sg_dir, exist_ok=True)
            shutil.copy2(template, settings)
            print(f"  {green('OK')}    settings.data created (multisampling off, borderless fullscreen)")
            return True
        print(f"  {yellow('TODO')}  no settings.data — run ostriv_settings once and set Multisampling OFF")
        return False
    import struct
    with open(settings, "rb") as f:
        d = bytearray(f.read())
    key = b"bMultisampling"
    idx = d.find(struct.pack("<i", len(key)) + key)
    if idx < 0:
        print(f"  {yellow('WARN')}  couldn't find multisampling flag in settings.data")
        return False
    vpos = idx + 4 + len(key)
    if d[vpos] != 0:
        d[vpos] = 0
        if not os.path.isfile(settings + ".bak"):
            shutil.copy2(settings, settings + ".bak")
        with open(settings, "wb") as f:
            f.write(d)
        print(f"  {green('OK')}    multisampling disabled")
    else:
        print(f"  {green('OK')}    multisampling already off")
    return True


def windows_path(game_dir, exe):
    """Convert a macOS bottle path (…/drive_c/rest) to the Windows path CrossOver's
    'Run Command' expects, e.g. C:\\Program Files (x86)\\Steam\\...\\ostriv.exe."""
    marker = os.sep + "drive_c" + os.sep
    idx = game_dir.find(marker)
    if idx < 0:
        return os.path.join(game_dir, exe)  # fallback: raw path
    rest = game_dir[idx + len(marker):]
    return "C:\\" + rest.replace("/", "\\") + "\\" + exe


# ──────────────────────────────────────────────────────────────────────────────
# Restore
# ──────────────────────────────────────────────────────────────────────────────

def restore(game_dir, bottle):
    """Completely undo everything install did — driver DLLs, steam_appid.txt, the
    opengl32 override, the bottle env vars, and our settings.data. No leftovers."""
    import re
    print("Restoring to the pre-patch state...\n")

    # 1. driver DLLs — put back the backup, or remove the one we added
    for dll in DRIVER_DLLS:
        dst = os.path.join(game_dir, dll)
        bak = dst + ".bak"
        if os.path.isfile(bak):
            shutil.move(bak, dst)
            print(f"  {green('OK')}    restored {dll}")
        elif os.path.isfile(dst):
            os.remove(dst)
            print(f"  {green('OK')}    removed {dll}")

    # 2. steam_appid.txt we wrote
    appid = os.path.join(game_dir, "steam_appid.txt")
    if os.path.isfile(appid):
        os.remove(appid)
        print(f"  {green('OK')}    removed steam_appid.txt")

    # 3. opengl32=native override — only if actually set
    key = r"HKCU\Software\Wine\AppDefaults\ostriv.exe\DllOverrides"
    _, out = wine_reg(bottle, "query", key, "/v", "opengl32")
    if "native" in out.lower():
        wine_reg(bottle, "delete", key, "/v", "opengl32", "/f")
        print(f"  {green('OK')}    removed opengl32 override")

    # 4. bottle env vars we added — only if any are present (leaves other env untouched)
    conf = os.path.join(BOTTLES_ROOT, bottle, "cxbottle.conf")
    if os.path.isfile(conf):
        with open(conf) as f:
            s = f.read()
        new = s
        for k in BOTTLE_ENV:
            new = re.sub(rf'^"{re.escape(k)}"\s*=\s*"[^"]*"\n', "", new, flags=re.M)
        if new != s:
            with open(conf, "w") as f:
                f.write(new)
            print(f"  {green('OK')}    removed bottle env vars")
        if os.path.isfile(conf + ".bak"):
            os.remove(conf + ".bak")

    # 5. settings.data — restore the backup, or remove the template we dropped in
    settings = os.path.join(BOTTLES_ROOT, bottle, "drive_c", "users", "crossover",
                            "Saved Games", "Ostriv", "settings.data")
    template = os.path.join(SCRIPT_DIR, "assets", "settings.data")
    if os.path.isfile(settings + ".bak"):
        shutil.move(settings + ".bak", settings)
        print(f"  {green('OK')}    restored settings.data")
    elif (os.path.isfile(settings) and os.path.isfile(template)
          and open(settings, "rb").read() == open(template, "rb").read()):
        os.remove(settings)
        print(f"  {green('OK')}    removed settings.data (installed by us)")

    print(green(bold("\nFully restored.")) + " The bottle is back to its pre-patch state.\n")


# ──────────────────────────────────────────────────────────────────────────────
# UI helpers
# ──────────────────────────────────────────────────────────────────────────────

def shorten(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def is_installed(game_dir):
    """True if the Mesa driver is already present next to ostriv.exe."""
    return os.path.isfile(os.path.join(game_dir, "libgallium_wgl.dll"))


def print_status(game_dir, bottle):
    print("Game:")
    print(f"  {green('✓')} ostriv.exe" +
          (yellow("  (driver already installed)") if is_installed(game_dir) else ""))
    cv = crossover_version()
    ov = ostriv_version(bottle)
    print(f"  CrossOver {bold(cv or 'unknown')}   Ostriv "
          f"{bold(ov) if ov else yellow('unknown (run the game once)')}")
    print(f"  {cyan(f'tested with CrossOver {TESTED_CROSSOVER} · Ostriv {TESTED_OSTRIV}')}")


def select(prompt, options):
    """Interactive ↑/↓ + Enter menu. `options` is a list of label strings.
    Returns the chosen index, or None if the user quits (q / Ctrl-C).
    Falls back to numbered input when stdin/stdout isn't an interactive TTY."""
    n = len(options)

    if not (termios and sys.stdin.isatty() and sys.stdout.isatty()):
        print(prompt)
        for i, label in enumerate(options, 1):
            print(f"  [{i}] {label}")
        while True:
            val = input("Select (number, or Q to quit): ").strip().lower()
            if val in ("q", "quit"):
                return None
            if val.isdigit() and 1 <= int(val) <= n:
                return int(val) - 1
            print(f"  Enter 1-{n} or Q")

    print(prompt)
    print(cyan("  (↑/↓ to move · Enter to select · q to quit)"))
    idx = 0

    def draw(first=False):
        if not first:
            sys.stdout.write(f"\x1b[{n}A")            # cursor up n lines
        for i, label in enumerate(options):
            row = (green("❯ ") + bold(label)) if i == idx else "  " + label
            sys.stdout.write("\r\x1b[K  " + row + "\r\n")  # clear line, write, CRLF
        sys.stdout.flush()

    assert termios and tty  # narrowed: TTY branch only reached when available
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        draw(first=True)
        while True:
            ch = sys.stdin.read(1)
            if ch == "\x1b" and sys.stdin.read(1) == "[":
                arrow = sys.stdin.read(1)
                if arrow == "A":
                    idx = (idx - 1) % n; draw()
                elif arrow == "B":
                    idx = (idx + 1) % n; draw()
            elif ch in ("\r", "\n"):
                return idx
            elif ch in ("q", "Q"):
                return None
            elif ch == "\x03":
                raise KeyboardInterrupt
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    print(cyan(bold("=================================================")))
    print(cyan(bold("  Ostriv — macOS / Wine GPU-acceleration installer")))
    print(cyan(bold("=================================================")))
    print()

    if not os.access(os.path.join(CROSSOVER, "bin", "wine"), os.X_OK):
        print(red("  CrossOver not found at /Applications/CrossOver.app"))
        print(red("  Install CrossOver first: https://www.codeweavers.com/crossover"))
        sys.exit(1)

    # ── Step 1: locate game ──────────────────────────────────────────────────
    cli_path = sys.argv[1] if len(sys.argv) > 1 else None
    if cli_path and os.path.isdir(os.path.expanduser(cli_path)):
        game_dir = os.path.expanduser(cli_path)
        bottle = _bottle_of(game_dir)
        print(f"Using path from argument:\n  {shorten(game_dir)}\n")
    else:
        print("Scanning for Ostriv installations...")
        found = find_game_installations()
        if not found:
            print(red("  No Ostriv installation found in any CrossOver bottle."))
            sys.exit(1)
        elif len(found) == 1:
            bottle, game_dir = found[0]
            print(f"  Found in bottle '{bottle}':\n  {shorten(game_dir)}\n")
        else:
            print()
            sel = select("Select the Ostriv installation to patch:",
                         [f"{b}  ({shorten(p)})" for b, p in found])
            if sel is None:
                return
            bottle, game_dir = found[sel]
            print()

    if not os.path.isfile(os.path.join(game_dir, "ostriv.exe")):
        print(red(f"\n  '{game_dir}' doesn't contain ostriv.exe."))
        sys.exit(1)

    print()
    print_status(game_dir, bottle)
    print()

    # ── Step 2: choose action ────────────────────────────────────────────────
    installed = is_installed(game_dir)
    if installed:
        # already patched → the primary action is Reinstall; Restore is available to undo
        options = [
            "Reinstall  — re-apply driver + config (e.g. after a game update)",
            "Restore    — undo: put the bottle back to its original state",
        ]
    else:
        # not patched → the only sensible action is Install (nothing to restore)
        options = ["Install  — GPU driver + bottle config (fullscreen, ~30-60 fps)"]

    sel = select("Choose action:", options)
    if sel is None:
        return
    print()

    if installed and sel == 1:
        restore(game_dir, bottle)
        return

    # ── Step 3: install ──────────────────────────────────────────────────────
    print(("Reinstalling...\n" if installed else "Installing...\n"))
    ok = install_driver(game_dir)
    if ok:
        write_appid_file(game_dir)
        set_override(bottle)
        set_env(bottle)
        ensure_settings(bottle)
    print()

    # ── Step 4: summary + next steps ─────────────────────────────────────────
    if ok:
        game_win = windows_path(game_dir, "ostriv.exe")
        settings_win = windows_path(game_dir, "ostriv_settings.exe")
        print(green(bold("Reinstalled!" if installed else "Installed!")) + "\n")
        print("How to launch:\n")
        print(f"  1. {bold('Fully quit CrossOver and reopen it')} "
              f"(loads the new bottle settings).")
        print(f"  2. Start {bold('Steam')} in the bottle — it just needs to be "
              f"{bold('running')} (for the Steam API).")
        print(f"  3. In CrossOver, select the {bold(bottle)} bottle → "
              f"{bold('Run Command')}, and run the game with this path:\n")
        print(f"        {cyan(game_win)}\n")
        print(f"     {bold('Do NOT use the Steam Play button')} — it injects the Steam overlay, "
              f"which crashes the game.\n")
        print(f"     Tip: in the Run Command dialog, {bold('Save a Launcher')} so you get a "
              f"double-click app.\n")
        print("To change graphics settings, run this one the same way (Run Command):\n")
        print(f"        {cyan(settings_win)}\n")
        print(yellow("  Keep Multisampling OFF in settings, or the game crashes at launch.\n"
                     "  If the game ever renders tiny on a big screen: quit CrossOver and reopen it.\n"))
        print("To undo everything: re-run this script and choose Restore.")
    else:
        print(yellow("Completed with warnings — check the output above.\n"))
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
