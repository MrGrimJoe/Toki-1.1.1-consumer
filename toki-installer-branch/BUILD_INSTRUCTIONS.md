# TOKI installer — build instructions

## Branch layout

This is the **`installer`** branch of
[MrGrimJoe/Toki-1.1.1-consumer](https://github.com/MrGrimJoe/Toki-1.1.1-consumer).
It holds *only* the files needed to build the installer itself:

| File | What it does |
|---|---|
| `installer.iss` | the Inno Setup script itself |
| `setup_runtime.ps1` | builds the embedded Python 3.12 runtime + installs deps, run automatically at install time |
| `Launch TOKI.bat.template` | becomes `Launch TOKI.bat` in the install dir; this is what shortcuts point to |

No app source (`main.py`, `extractor.py`, `toki_graph_db\`, etc.) lives on
this branch, and none needs to be checked out locally to build the
installer. At **install time**, `TokiInstaller.exe` downloads a fresh copy
of TOKI straight from the **`main`** branch on GitHub (as a zip, via
`codeload.github.com`) and extracts it into the install folder itself —
so every install always ships whatever's currently on `main`, and this
branch never needs updating just because `main` changed.

## One-time setup on your Windows dev machine

1. Install **Inno Setup 6.x** (stock installer from jrsoftware.org — nothing extra needed, no plugins).
2. Check out this `installer` branch on its own — that's the whole checkout, nothing from `main` needed alongside it.
3. Open `installer.iss` in the Inno Setup Compiler (or right-click → Compile).
4. Output lands in `dist\TokiInstaller.exe` — this is the single, standalone installer you ship.

## What actually happens when someone runs the installer

1. Standard Inno wizard: license, install location (defaults to `C:\Program Files\TOKI`), components (core / +voice), and a tasks page with a desktop-icon checkbox plus a **"Launch TOKI when Windows starts" checkbox (unchecked by default)** -- opt-in autostart, no separate script to run.
2. **Folder Access page** — checkboxes for every drive letter that exists on their machine, `D:\` pre-checked to match TOKI's default sandbox. The real Desktop (resolved via Inno's own OneDrive-aware `{userdesktop}` constant) is always added too, regardless of what's checked here.
3. **Additional Folder page** — one optional folder outside those drives.
4. **Ollama page** — install it now / already have it / skip. The in-wizard copy explains what Ollama is for and that TOKI works fully without it.
5. Post-install: downloads `main` branch as a zip from GitHub and extracts it into `{app}` (the actual `.py` files, `toki_graph_db\`, `wcl_kg\` — nothing bundled into a single exe, and this step needs internet access even before dependency install starts).
6. `setup_runtime.ps1` then runs, which:
   - downloads the official Python 3.12.7 **embeddable** package from python.org into `{app}\runtime\python312`
   - enables site-packages on it (disabled by default in the embeddable distro)
   - bootstraps pip via `get-pip.py`
   - installs TOKI's core dependencies (PyQt6, requests, Pillow, kuzu, pywinauto, comtypes, winsdk, pynput, yt-dlp), plus the voice-pipeline packages if that component was checked
7. If "install Ollama" was picked, downloads `OllamaSetup.exe` and hands control to it — this opens Ollama's *real* installer UI for the user to click through, not a silent install (see caveat below).
8. Shortcuts point at `Launch TOKI.bat`, which runs `runtime\python312\pythonw.exe main.py` — no console window, no interference with any other Python on the machine.

## Important caveats — read before shipping this

- **Requires internet during install, more than before.** In addition to the Python runtime/pip wheels (~150-400MB) and Ollama's installer if requested, this now also downloads TOKI's own source from GitHub as its very first post-install step. If that download fails (bad connection, GitHub unreachable, repo made private), the installer stops cleanly with a message rather than continuing with nothing installed.
- **If you ever rename the default branch away from `main`**, or move/rename the repo, update `MyRepoZipUrl` (and `MyRepoZipRootFolder`, which must match `<repo-name>-<branch-name>` exactly as GitHub names the zip's inner folder) at the top of `installer.iss`.
- **This installer will be unsigned**, so Windows SmartScreen will show the "Windows protected your PC" warning on first run. Fixing this for real needs a code-signing certificate (~$100-400/yr from a CA); there's no free workaround that removes the warning for an unknown publisher.
- **Ollama isn't installed silently.** There's no documented unattended-install flag for `OllamaSetup.exe`, so rather than promise a silent install that might not actually be silent, this opens Ollama's real installer window for one click-through.
- **Admin rights required** as written (`PrivilegesRequired=admin`), since it installs to Program Files and requires drive-level filesystem access for the sandbox picker to be meaningful. For a no-admin-prompt install instead, switch `DefaultDirName` to `{userpf}\{#MyAppName}` (or `{localappdata}\{#MyAppName}`) and `PrivilegesRequired` to `lowest` — the sandbox page's drive checkboxes still work the same either way, since that's a config file, not a permissions grant.
- **Autostart uses a plain HKCU Run-key value**, written only if the user checks the task, and removed automatically by Inno's uninstaller (`uninsdeletevalue`).
- **Uninstall now removes the whole `{app}` folder**, not just `runtime\`/`config\` — since the app source itself is no longer tracked by Inno's own `[Files]` (it was fetched by script, not installed the normal way), Inno can't otherwise account for it on uninstall.
- **If `main` ever has an unreleased/broken commit sitting on it**, every fresh install picks that up immediately — there's no version pinning to a tag or release here. Worth knowing if you want `main` to always be "whatever's safe to ship right now."
- **I could not actually compile this `.iss`** — Inno Setup is Windows-only and my working environment is Linux. I checked the Pascal Script carefully against Inno's documented API, but budget an extra pass on your end for anything Inno-version-specific I couldn't verify directly.
