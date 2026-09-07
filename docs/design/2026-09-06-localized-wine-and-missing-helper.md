# Localized Wine Output, Missing Ostriv Helper, and Mixed CrossOver Copies

**Status:** Approved in conversation on 2026-09-06 from the Steam Community support thread
report of a fully Ukrainian macOS, Steam, and CrossOver setup with three CrossOver copies.
This file records the approved intent; [technical.md](../technical.md) documents the current
behaviour.

## Purpose

Make Install, Reinstall, and Restore complete on a Mac whose Wine speaks a language other than
English, on a bottle where CrossOver never created its own Ostriv helper app, and give one
actionable message when a second CrossOver copy still owns the bottle.

## Evidence

- CrossOver 26.3 under `LANG=uk_UA.UTF-8`: `wine --bottle Steam reg query <missing key>` exits
  with status 1 and prints `reg: Не вдалося знайти вказаний ключ реєстру` encoded in CP866.
  `WineRegistry._missing` matched only the English wording, so the first registry query of a
  clean Install raised `install.registry`; Restore's delete verification failed the same way.
- `LC_ALL=C` in the subprocess environment restores the English wording even when `LANG` stays
  Ukrainian; CrossOver's Perl `wine` wrapper passes the variable through.
- Every wine process rewrites `HKCU\Control Panel\International` to its own locale at start,
  and the next process rewrites it again, so a forced C locale is transient and self-healing.
- `cxbottle --status` prints `Mode=` and `Status=` lines and is checked by exit status only, so
  no other installer command depends on wording.
- `LauncherInstaller._find_game_icon` required a helper app whose command ends in
  `Ostriv.lnk` or `Ostriv.url`; without one, Install raised `install.launcher_icon` although
  launcher verification already accepted CrossOver's default `exeIcon.icns`.
- A wine process meeting a wineserver from another CrossOver copy fails with the unlocalized
  `wine client error:<tid>: version mismatch …`; the player saw only the generic
  "Installation failed." action.

## Decisions

1. `WineRegistry` sends every `reg` command through one `_run` helper with `LC_ALL=C`, and
   `CommandRunner` allows `LC_ALL` alongside `CX_BOTTLE_PATH`. The English regex stays as the
   structural check on top of the forced locale.
2. `_find_game_icon` also accepts helper commands ending in `ostriv.exe`; when no helper app
   exists, Install and menu rollback use the selected CrossOver's `exeIcon.icns`. Only a
   CrossOver without that file still fails with `install.launcher_icon`.
3. `CommandRunner.run` raises `command.wine_version_mismatch` for any failed command whose
   output carries the wine client version-mismatch error. The CLI maps it to one action: quit
   every copy of CrossOver and Steam completely, then try again.
4. The README troubleshooting tables gain a **Several CrossOver copies** row in both languages.

## Verification

- Fake-process installer tests emulate a localized `reg.exe` that answers in CP866 unless the
  command environment carries `LC_ALL=C`, and run Install plus Restore against it.
- Launcher tests install without any Ostriv helper app, with a helper targeting `ostriv.exe`,
  and roll back a failed Restore without a helper app.
- Diagnostics and CLI tests cover the typed version-mismatch error and its player action.
- Release documentation tests keep the new troubleshooting row present in both READMEs.

## Out of scope

- Detecting a foreign wineserver before running any command; the typed error is reactive.
- Resolving the CrossOver bundle at launch time instead of storing its `wine` path in the
  launcher configuration; Reinstall remains the way to follow a moved CrossOver.
- A separate "Update launcher" action; Reinstall already refreshes the launcher idempotently.
