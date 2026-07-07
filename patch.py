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
import filecmp
import itertools
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


def cx_bin(cx_root, name):
    """Path of bin/<name> under a CrossOver SharedSupport root, or None if not executable."""
    path = os.path.join(cx_root, "bin", name)
    return path if os.access(path, os.X_OK) else None


def _cx_root(app):
    """The SharedSupport/CrossOver dir inside a CrossOver.app bundle."""
    return os.path.join(app, "Contents", "SharedSupport", "CrossOver")


def _crossover_from_spotlight():
    """Ask macOS where CrossOver.app is (users who moved it out of ~/ or /Applications).
    Spotlight first, then LaunchServices via lsregister — both best-effort, stdlib-only.
    Skips trashed / translocated / temp copies so we never resolve to a stale bundle."""
    def ok(path):
        junk = ("/.Trash/", "/AppTranslocation/", "/private/var/folders/", "/var/folders/")
        return path.endswith("CrossOver.app") and not any(j in path for j in junk)

    for cmd in (["mdfind", "kMDItemCFBundleIdentifier == 'com.codeweavers.CrossOver'"],
                ["mdfind", "-name", "CrossOver.app"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            line = line.strip()
            if ok(line):
                yield line
    lsreg = ("/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/"
             "LaunchServices.framework/Support/lsregister")
    try:
        out = subprocess.run([lsreg, "-dump"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for m in re.finditer(r"(/\S+/CrossOver\.app)", out):
        if ok(m.group(1)):
            yield m.group(1)


def find_crossover_app():
    """The installed CrossOver.app bundle. CrossOver's installer defaults to ~/Applications,
    but users may move it; check the common spots, then ask macOS. Falls back to the
    conventional /Applications path when nothing matches."""
    def usable(app):
        return bool(cx_bin(_cx_root(app), "wine"))

    fixed = []
    env = os.environ.get("OSTRIV_CROSSOVER_APP")
    if env:
        fixed.append(os.path.expanduser(env))
    fixed += [os.path.expanduser("~/Applications/CrossOver.app"), "/Applications/CrossOver.app"]
    # fixed paths first (no subprocess); only ask macOS if they all miss (chain stays lazy)
    for app in itertools.chain(fixed, _crossover_from_spotlight()):
        if usable(app):
            return app
    return "/Applications/CrossOver.app"


CROSSOVER_APP = find_crossover_app()
CROSSOVER = _cx_root(CROSSOVER_APP)

APPID = "773790"
TESTED_CROSSOVER = "26.2"
TESTED_OSTRIV = "0.5.9.58"


def crossover_version():
    """Installed CrossOver version, or None."""
    try:
        with open(os.path.join(CROSSOVER_APP, "Contents", "Info.plist"), "rb") as f:
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
    "MESA_OSTRIV_TREE_SHADER_HACK": "1",
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

def cx_tool(name):
    """Path of a CrossOver CLI tool (bin/<name>), or None if not executable."""
    return cx_bin(CROSSOVER, name)


def bottle_conf(bottle):
    """Path of the bottle's cxbottle.conf."""
    return os.path.join(BOTTLES_ROOT, bottle, "cxbottle.conf")


def wine_reg(bottle, *args):
    """Run a wine reg command in the given bottle. Returns (returncode, stdout)."""
    wine = cx_tool("wine")
    if not wine:
        return (1, "")
    cmd = [wine, "--bottle", bottle, "--no-update", "--no-lock", "reg", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return (r.returncode, r.stdout)


# ──────────────────────────────────────────────────────────────────────────────
# Install steps
# ──────────────────────────────────────────────────────────────────────────────

def _same_file(a, b):
    """True if both paths exist with byte-identical contents (used to tell an original
    file apart from one of our own driver DLLs)."""
    try:
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False


def install_driver(game_dir):
    """Copy the Mesa driver DLLs next to ostriv.exe, backing up any genuine pre-existing
    file. Never back up one of our own driver DLLs: reinstalling over an already-patched
    bottle must not capture a Mesa DLL as the 'original' (that would defeat Restore)."""
    for dll in DRIVER_DLLS:
        src = os.path.join(PREBUILT, dll)
        dst = os.path.join(game_dir, dll)
        if not os.path.isfile(src):
            print(f"  {red('MISS')}  prebuilt/{dll} not found — is the repo complete (Git LFS pulled)?")
            return False
        if (os.path.isfile(dst) and not os.path.isfile(dst + ".bak")
                and not _same_file(dst, src)):
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
    conf = bottle_conf(bottle)
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
          (f"({len(added)} vars added)" if added else "(already present)"))
    return True


def display_profile_is_srgb():
    """True/False = main display's ColorSync profile is/isn't sRGB; None = couldn't tell.

    winemac hands the game's frames to macOS tagged as sRGB. If the display profile is
    anything else (factory wide-gamut ICC, calibration), CoreAnimation colorspace-converts
    every frame on the CPU — measured as a hard ~10 fps cap at 3008x1692. With the sRGB
    profile the conversion is skipped entirely (see docs/technical.md)."""
    if sys.platform != "darwin":
        return None
    import ctypes
    try:
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayCopyColorSpace.restype = ctypes.c_void_p
        cg.CGDisplayCopyColorSpace.argtypes = [ctypes.c_uint32]
        cg.CGColorSpaceGetName.restype = ctypes.c_void_p
        cg.CGColorSpaceGetName.argtypes = [ctypes.c_void_p]
        cg.CFStringGetCString.restype = ctypes.c_bool
        cg.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                          ctypes.c_long, ctypes.c_uint32]
        cg.CFRelease.argtypes = [ctypes.c_void_p]

        cs = cg.CGDisplayCopyColorSpace(cg.CGMainDisplayID())
        if not cs:
            return None
        name = ""
        name_ref = cg.CGColorSpaceGetName(cs)  # NULL for custom ICC-based profiles
        if name_ref:
            buf = ctypes.create_string_buffer(256)
            if cg.CFStringGetCString(name_ref, buf, 256, 0x08000100):  # UTF-8
                name = buf.value.decode("utf-8", "replace")
        cg.CFRelease(cs)
        return name == "kCGColorSpaceSRGB"
    except Exception:
        return None


def check_display_profile():
    """Print an OK/TODO line for the display color profile; returns the check result.
    Only worth calling when there is no launcher — the launcher switches the profile
    itself while playing."""
    srgb = display_profile_is_srgb()
    if srgb:
        print(f"  {green('OK')}    display color profile is sRGB")
    elif srgb is False:
        print(f"  {yellow('TODO')}  display color profile is not sRGB — costs most of your "
              f"FPS (see launch steps)")
    return srgb


CX_APPS_ROOT = os.path.expanduser("~/Applications/CrossOver")
# NB: parentheses, not [brackets] — bracket glob chars break CrossOver's launcher-app
# generation (the cxmenu entry registers but the .app is never written).
LAUNCHER_NAME = "Ostriv (patched)"
LAUNCHER_MENU = "StartMenu/" + LAUNCHER_NAME
LAUNCHER_SCRIPT_NAME = "play-ostriv-patched.py"
PLAY_LAUNCHER = os.path.join(CX_APPS_ROOT, LAUNCHER_NAME + ".app")

# The generated launcher: switches the display to sRGB (winemac frames are sRGB-tagged;
# any other profile makes macOS colorspace-convert every frame on the CPU at ~10 fps),
# makes sure Steam is up, runs the game, and restores the previous profile afterwards.
LAUNCHER_SCRIPT = '''#!/usr/bin/env python3
"""Generated by ostriv-macos patch.py — re-run patch.py instead of editing.

Plays Ostriv with the display in sRGB: winemac hands frames to macOS tagged as
sRGB, so any other profile forces a per-frame CPU colorspace conversion that
caps the game at ~10 fps. This launcher switches the main display's profile to
sRGB, starts Steam in the bottle if needed, runs the game, and restores the
previous profile when the game exits.

Run by CrossOver's Menu Helper (the 'Ostriv (patched)' launcher app), which
provides the foreground-GUI context winemac needs — a bare shell launch of the
same wine command makes the game self-exit at the menu."""
import ctypes
import os
import plistlib
import subprocess
import time

GAME_CMD = {game_cmd!r}
WINE = {wine!r}
BOTTLE = {bottle!r}
SRGB_ICC = "/System/Library/ColorSync/Profiles/sRGB Profile.icc"
CX_APPS = os.path.expanduser("~/Applications/CrossOver")
# Steam's Start Menu shortcut inside the bottle (either scope) — last-resort fallback.
STEAM_LNKS = (
    "users/crossover/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Steam/Steam.lnk",
    "ProgramData/Microsoft/Windows/Start Menu/Programs/Steam/Steam.lnk",
)

V = ctypes.c_void_p
UTF8 = 0x08000100
CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
CG = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
CS = ctypes.CDLL("/System/Library/Frameworks/ColorSync.framework/ColorSync")
for fn, res, args in [
    (CF.CFStringCreateWithCString, V, [V, ctypes.c_char_p, ctypes.c_uint32]),
    (CF.CFStringGetCString, ctypes.c_bool, [V, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]),
    (CF.CFDictionaryGetValue, V, [V, V]),
    (CF.CFDictionaryCreateMutable, V, [V, ctypes.c_long, V, V]),
    (CF.CFDictionarySetValue, None, [V, V, V]),
    (CF.CFURLCreateWithFileSystemPath, V, [V, V, ctypes.c_long, ctypes.c_bool]),
    (CF.CFURLCopyFileSystemPath, V, [V, ctypes.c_long]),
    (CG.CGMainDisplayID, ctypes.c_uint32, []),
    (CS.CGDisplayCreateUUIDFromDisplayID, V, [ctypes.c_uint32]),  # lives in ColorSync
    (CS.ColorSyncDeviceCopyDeviceInfo, V, [V, V]),
    (CS.ColorSyncDeviceSetCustomProfiles, ctypes.c_bool, [V, V, V]),
]:
    fn.restype, fn.argtypes = res, args

DISPLAY_CLASS = V.in_dll(CS, "kColorSyncDisplayDeviceClass")
PROFILE_ID = V.in_dll(CS, "kColorSyncDeviceDefaultProfileID")
CUSTOM_KEY = V.in_dll(CS, "kColorSyncCustomProfiles")
KEY_CB = ctypes.addressof(ctypes.c_char.in_dll(CF, "kCFTypeDictionaryKeyCallBacks"))
VAL_CB = ctypes.addressof(ctypes.c_char.in_dll(CF, "kCFTypeDictionaryValueCallBacks"))


def _cfstr(s):
    return CF.CFStringCreateWithCString(None, s.encode(), UTF8)


def _py_str(ref):
    buf = ctypes.create_string_buffer(2048)
    if ref and CF.CFStringGetCString(ref, buf, 2048, UTF8):
        return buf.value.decode("utf-8", "replace")
    return None


def _display_uuid():
    return CS.CGDisplayCreateUUIDFromDisplayID(CG.CGMainDisplayID())


def get_custom_profile():
    """Path of the display's current custom ICC profile, or None if none is set."""
    info = CS.ColorSyncDeviceCopyDeviceInfo(DISPLAY_CLASS, _display_uuid())
    if not info:
        return None
    custom = CF.CFDictionaryGetValue(info, CUSTOM_KEY)
    if not custom:
        return None
    # the stored key is the profile-ID string "1" (the constant is accepted on write
    # but normalized on read)
    url = CF.CFDictionaryGetValue(custom, _cfstr("1")) or \\
          CF.CFDictionaryGetValue(custom, PROFILE_ID)
    if not url:
        return None
    return _py_str(CF.CFURLCopyFileSystemPath(url, 0))


def set_custom_profile(icc_path):
    """Set the display's custom profile; icc_path=None removes it (factory default)."""
    d = CF.CFDictionaryCreateMutable(None, 0, KEY_CB, VAL_CB)
    if icc_path:
        value = CF.CFURLCreateWithFileSystemPath(None, _cfstr(icc_path), 0, False)
    else:
        value = V.in_dll(CF, "kCFNull")
    CF.CFDictionarySetValue(d, PROFILE_ID, value)
    return bool(CS.ColorSyncDeviceSetCustomProfiles(DISPLAY_CLASS, _display_uuid(), d))


def steam_running():
    return subprocess.run(["pgrep", "-f", r"steam\\.exe"],
                          capture_output=True).returncode == 0


def steam_state():
    """(pid, active_user) from Steam's ActiveProcess registry key — the exact data
    SteamAPI_Init reads to find the client. active_user != 0 means login finished."""
    r = subprocess.run([WINE, "--bottle", BOTTLE, "--no-update", "--no-lock", "reg",
                        "query", "HKCU\\\\Software\\\\Valve\\\\Steam\\\\ActiveProcess"],
                       capture_output=True, text=True)
    pid = active = 0
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2].startswith("0x"):
            if parts[0] == "pid":
                pid = int(parts[2], 16)
            elif parts[0] == "ActiveUser":
                active = int(parts[2], 16)
    return pid, active


def find_steam_app():
    """The CrossOver launcher .app for Steam in this bottle, resolved at launch time."""
    if not os.path.isdir(CX_APPS):
        return None
    folders = [CX_APPS] + [e.path for e in os.scandir(CX_APPS)
                           if e.is_dir() and not e.name.endswith(".app")]
    for folder in folders:
        for e in os.scandir(folder):
            if not (e.is_dir() and e.name.endswith(".app")):
                continue
            try:
                with open(os.path.join(e.path, "Contents", "Info.plist"), "rb") as f:
                    pl = plistlib.load(f)
            except Exception:
                continue
            cmd = pl.get("CrossOverHelperCommand", "")
            if pl.get("CXHelperAppBottleName") == BOTTLE and \
                    cmd.rstrip('"').lower().endswith("/steam.lnk"):
                return e.path
    return None


def start_steam():
    """The game only needs the Steam client to be *running* (SteamAPI_Init).

    Steam must be opened through its own launcher app: started as a child of this
    script (wine --start) it comes up in a broken half-state — tray icon, no window."""
    if steam_running():
        return
    old_pid, _ = steam_state()
    app = find_steam_app()
    if app:
        subprocess.run(["open", app], check=False)
    else:
        drive_c = os.path.expanduser(
            "~/Library/Application Support/CrossOver/Bottles/" + BOTTLE + "/drive_c")
        for rel in STEAM_LNKS:
            if os.path.isfile(os.path.join(drive_c, rel)):
                subprocess.Popen([WINE, "--bottle", BOTTLE, "--start", "C:/" + rel])
                break
        else:
            return
    # steam.exe existing is not enough for SteamAPI_Init — wait until Steam has
    # (re)written its ActiveProcess registry key and the user login finished.
    for _ in range(60):
        pid, active_user = steam_state()
        if pid and pid != old_pid and active_user:
            break
        time.sleep(3)
    time.sleep(5)  # settle


def main():
    start_steam()
    original = get_custom_profile()
    switched = set_custom_profile(SRGB_ICC)
    try:
        subprocess.run(GAME_CMD, check=False)
    finally:
        if switched and original != SRGB_ICC:
            set_custom_profile(original)  # None -> back to factory/default


if __name__ == "__main__":
    main()
'''


def cxmenu(bottle, *args):
    """Run CrossOver's cxmenu tool against the bottle. Returns True on success."""
    tool = cx_tool("cxmenu")
    if not tool:
        return False
    r = subprocess.run([tool, "--bottle", bottle, *args], capture_output=True, text=True)
    return r.returncode == 0


def bottle_id(bottle):
    """The bottle's unique ID from cxbottle.conf, or None."""
    conf = bottle_conf(bottle)
    try:
        with open(conf) as f:
            m = re.search(r'^"BottleID"\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


def materialize_launcher_app(bottle, command):
    """Write the launcher .app from CrossOver's own Menu Helper template.

    cxmenu --install only registers the menu entry (plist); the .app itself is
    normally written by the CrossOver GUI whenever it happens to sync — which is
    unreliable (often never, until a restart). Materialize it deterministically
    instead: extract the template CrossOver uses and fill in the same Info.plist
    keys its generated launcher apps carry."""
    template = os.path.join(CROSSOVER_APP, "Contents", "Resources", "Menu Helper.cpbz2")
    if not os.path.isfile(template):
        return False
    shutil.rmtree(PLAY_LAUNCHER, ignore_errors=True)
    os.makedirs(PLAY_LAUNCHER, exist_ok=True)
    r = subprocess.run(["/bin/sh", "-c", 'bunzip2 -c "$0" | cpio -id', template],
                       cwd=PLAY_LAUNCHER, capture_output=True)
    plist_path = os.path.join(PLAY_LAUNCHER, "Contents", "Info.plist")
    if r.returncode != 0 or not os.path.isfile(plist_path):
        shutil.rmtree(PLAY_LAUNCHER, ignore_errors=True)
        return False
    with open(plist_path, "rb") as f:
        info = plistlib.load(f)
    import hashlib
    info["CFBundleName"] = LAUNCHER_NAME
    info["CFBundleDisplayName"] = LAUNCHER_NAME
    # CrossOver's scheme: CrossOverHelper.<MD5(bottle)>.<MD5(menu leaf name)>, uppercase.
    # Matching it keeps CrossOver's menu sync from treating the app as foreign and
    # deleting it.
    info["CFBundleIdentifier"] = "com.codeweavers.CrossOverHelper.%s.%s" % (
        hashlib.md5(bottle.encode()).hexdigest().upper(),
        hashlib.md5(LAUNCHER_NAME.encode()).hexdigest().upper())
    info["CrossOverHelperCommand"] = command
    info["CXHelperAppBottleName"] = bottle
    bid = bottle_id(bottle)
    if bid:
        info["CXHelperAppBottleTag"] = "CrossOver-%s/" % bid
    with open(plist_path, "wb") as f:
        plistlib.dump(info, f)
    return True


def remove_launcher(bottle):
    """Tear down every piece of the generated launcher: cxmenu entry, the script in
    the bottle dir, and the .app (cxmenu's purge doesn't reliably remove it)."""
    cxmenu(bottle, "--purge", "--filter", LAUNCHER_MENU)
    script = os.path.join(BOTTLES_ROOT, bottle, LAUNCHER_SCRIPT_NAME)
    if os.path.isfile(script):
        os.remove(script)
    shutil.rmtree(PLAY_LAUNCHER, ignore_errors=True)


def create_play_launcher(bottle, game_dir):
    """Create the 'Ostriv (patched)' launcher via CrossOver's cxmenu — portable,
    no manually saved Run Command launcher needed. The menu entry runs our
    profile-switching script, and CrossOver's Menu Helper app gives the game the
    foreground-GUI context winemac needs (a bare shell launch self-exits at the menu)."""
    wine = cx_tool("wine")
    if not wine:
        print(f"  {yellow('WARN')}  CrossOver wine not found; skipping launcher creation")
        return None
    exe_win = windows_path(game_dir, "ostriv.exe").replace("\\", "/")
    game_cmd = [wine, "--bottle", bottle, "--check", "--wait-children", "--start", exe_win]

    remove_launcher(bottle)  # recreate every piece from scratch
    script = os.path.join(BOTTLES_ROOT, bottle, LAUNCHER_SCRIPT_NAME)
    with open(script, "w") as f:
        f.write(LAUNCHER_SCRIPT.format(game_cmd=game_cmd, wine=wine, bottle=bottle))
    os.chmod(script, 0o755)

    # env, not sys.executable: the installer's interpreter (venv, homebrew) may not
    # outlive the install; /usr/bin/env python3 resolves one at launch time.
    command = 'exec /usr/bin/env python3 "%s"' % script
    # Register the menu entry so the launcher also shows up in CrossOver's bottle UI
    # (best-effort — the .app itself is materialized below, not left to the GUI).
    cxmenu(bottle, "--create", LAUNCHER_MENU, "--type", "raw",
           "--command", command,
           "--description", "Ostriv with the sRGB color-profile FPS fix",
           "--install")
    if materialize_launcher_app(bottle, command):
        print(f"  {green('OK')}    {cyan(LAUNCHER_NAME + '.app')} launcher")
        return PLAY_LAUNCHER
    print(f"  {yellow('WARN')}  couldn't create the {LAUNCHER_NAME} launcher — "
          f"use the manual launch steps below")
    return None


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

    # 1. driver DLLs — restore a genuine original, else remove what we added. A .bak whose
    #    bytes match our prebuilt DLL is a stale self-backup from an earlier reinstall (not a
    #    real original), so ignore it and delete both — otherwise "restore" leaves Mesa DLLs.
    for dll in DRIVER_DLLS:
        src = os.path.join(PREBUILT, dll)
        dst = os.path.join(game_dir, dll)
        bak = dst + ".bak"
        if os.path.isfile(bak) and not _same_file(bak, src):
            shutil.move(bak, dst)
            print(f"  {green('OK')}    restored {dll}")
        else:
            if os.path.isfile(bak):
                os.remove(bak)  # stale self-backup
            if os.path.isfile(dst):
                os.remove(dst)
                print(f"  {green('OK')}    removed {dll}")

    # 2. steam_appid.txt we wrote
    appid = os.path.join(game_dir, "steam_appid.txt")
    if os.path.isfile(appid):
        os.remove(appid)
        print(f"  {green('OK')}    removed steam_appid.txt")

    # 2b. the generated launcher (menu entry + app + script)
    had_launcher = os.path.isdir(PLAY_LAUNCHER)
    remove_launcher(bottle)
    if had_launcher:
        print(f"  {green('OK')}    removed the {LAUNCHER_NAME} launcher")

    # 3. opengl32=native override — only if actually set
    key = r"HKCU\Software\Wine\AppDefaults\ostriv.exe\DllOverrides"
    _, out = wine_reg(bottle, "query", key, "/v", "opengl32")
    if "native" in out.lower():
        wine_reg(bottle, "delete", key, "/v", "opengl32", "/f")
        print(f"  {green('OK')}    removed opengl32 override")

    # 4. bottle env vars we added — only if any are present (leaves other env untouched)
    conf = bottle_conf(bottle)
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
    if (cv and cv != TESTED_CROSSOVER) or (ov and ov != TESTED_OSTRIV):
        print(f"  {yellow(f'tested with CrossOver {TESTED_CROSSOVER} · Ostriv {TESTED_OSTRIV}')}")


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

    if not cx_tool("wine"):
        print(red("  CrossOver not found (checked ~/Applications, /Applications, and Spotlight)."))
        print(red("  Install CrossOver first: https://www.codeweavers.com/crossover"))
        print(red("  If it's installed somewhere unusual, set OSTRIV_CROSSOVER_APP to its path."))
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
        options = ["Install  — GPU driver + bottle config"]

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
    srgb = launcher = None
    if ok:
        write_appid_file(game_dir)
        set_override(bottle)
        set_env(bottle)
        ensure_settings(bottle)
        launcher = create_play_launcher(bottle, game_dir)
        srgb = None if launcher else check_display_profile()
    print()

    # ── Step 4: summary + next steps ─────────────────────────────────────────
    if ok:
        # Quoted — CrossOver's Run Command requires quotes because of the spaces in the path.
        game_win = f'"{windows_path(game_dir, "ostriv.exe")}"'
        print(green(bold("Reinstalled!" if installed else "Installed!")) + "\n")
        steps = [f"Quit CrossOver ({bold('⌘Q')}) and reopen it — once, to load the new settings."]
        if launcher:
            steps.append(f"Double-click {bold(LAUNCHER_NAME)} in "
                         f"{cyan('~/Applications/CrossOver')}.\n")
        else:
            if srgb is False:
                steps.append(f"System Settings → Displays → Color profile → "
                             f"{bold('sRGB IEC61966-2.1')} (else ~10 fps).")
            steps.append(f"Start {bold('Steam')} in the bottle (it just needs to be running).")
            steps.append(f"CrossOver → {bold(bottle)} bottle → {bold('Run Command')} "
                         f"(quotes included):\n\n        {cyan(game_win)}\n")
        for i, step in enumerate(steps, 1):
            print(f"  {i}. {step}")
        if launcher:
            print("  That's it — it starts Steam, fixes the display profile, and runs the game.")
        print(f"  {bold('Not')} via Steam's Play button — its overlay crashes the game.")
        print("\nTo undo everything: re-run this script and choose Restore.")
    else:
        print(yellow("Completed with warnings — check the output above.\n"))
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
