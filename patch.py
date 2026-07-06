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
import subprocess
import shutil

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PREBUILT = os.path.join(SCRIPT_DIR, "prebuilt")
DRIVER_DLLS = ["opengl32.dll", "libgallium_wgl.dll", "dxil.dll", "libwinpthread-1.dll"]

BOTTLES_ROOT = os.path.expanduser("~/Library/Application Support/CrossOver/Bottles")
CROSSOVER = "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver"

# Bottle environment the game needs (GL 4.3 via d3d12, visible via gdi present, fast via async present).
BOTTLE_ENV = {
    "SteamAppId": "773790",
    "SteamGameId": "773790",
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


# ──────────────────────────────────────────────────────────────────────────────
# Restore
# ──────────────────────────────────────────────────────────────────────────────

def restore(game_dir, bottle):
    print("Restoring original driver...\n")
    for dll in DRIVER_DLLS:
        dst = os.path.join(game_dir, dll)
        bak = dst + ".bak"
        if os.path.isfile(bak):
            shutil.move(bak, dst)
            print(f"  {green('OK')}    restored {dll}")
        elif os.path.isfile(dst):
            os.remove(dst)
            print(f"  {green('OK')}    removed {dll}")
    wine_reg(bottle, "delete", r"HKCU\Software\Wine\AppDefaults\ostriv.exe\DllOverrides",
             "/v", "opengl32", "/f")
    print(f"  {green('OK')}    removed opengl32 override")
    print(green("\nRestored. Bottle env vars in cxbottle.conf were left in place "
                "(harmless); remove them by hand if you like.\n"))


# ──────────────────────────────────────────────────────────────────────────────
# UI helpers
# ──────────────────────────────────────────────────────────────────────────────

def shorten(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def print_status(game_dir):
    installed = os.path.isfile(os.path.join(game_dir, "libgallium_wgl.dll"))
    print("Game:")
    print(f"  {green('✓')} ostriv.exe" +
          (yellow("  (driver already installed)") if installed else ""))


def ask(prompt, valid):
    while True:
        val = input(prompt).strip().upper()
        if val in valid:
            return val
        print(f"  Please enter one of: {', '.join(sorted(valid))}")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    print(cyan(bold("=================================================")))
    print(cyan(bold("  Ostriv — macOS / Wine GPU-acceleration installer")))
    print(cyan(bold("  Tested: CrossOver 26 · Ostriv 0.5.9.58")))
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
            for i, (b, p) in enumerate(found, 1):
                print(f"  [{i}] {b}: {shorten(p)}")
            print("  [Q] Quit\n")
            valid = {str(i) for i in range(1, len(found) + 1)} | {"Q"}
            sel = ask("Select: ", valid)
            if sel == "Q":
                return
            bottle, game_dir = found[int(sel) - 1]

    if not os.path.isfile(os.path.join(game_dir, "ostriv.exe")):
        print(red(f"\n  '{game_dir}' doesn't contain ostriv.exe."))
        sys.exit(1)

    print()
    print_status(game_dir)
    print()

    # ── Step 2: choose action ────────────────────────────────────────────────
    print("Choose action:\n")
    print("  [1] Install  — GPU driver + bottle config (fullscreen, ~30-60 fps)")
    print("  [2] Restore  — undo: put back the original driver")
    print("  [Q] Quit\n")
    sel = ask("Action: ", {"1", "2", "Q"})
    if sel == "Q":
        return
    print()

    if sel == "2":
        restore(game_dir, bottle)
        return

    # ── Step 3: install ──────────────────────────────────────────────────────
    print("Installing...\n")
    ok = install_driver(game_dir)
    if ok:
        set_override(bottle)
        set_env(bottle)
        ensure_settings(bottle)
    print()

    # ── Step 4: summary + next steps ─────────────────────────────────────────
    if ok:
        print(green(bold("Installed!")) + "\n")
        print("Two steps remain:\n")
        print(f"  1. {bold('Fully quit CrossOver and reopen it')} "
              f"(loads the new bottle settings).")
        print(f"  2. Start Steam in the bottle, then launch Ostriv.\n")
        print(yellow("  If it warned about settings.data above: run ostriv_settings once, "
                     "Multisampling OFF.\n"
                     "  If the game ever renders tiny on a big screen: quit CrossOver and reopen it.\n"))
        print("To undo later: re-run this script and choose Restore.")
    else:
        print(yellow("Completed with warnings — check the output above.\n"))
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
