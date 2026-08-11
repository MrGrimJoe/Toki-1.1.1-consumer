# TOKI

**A Windows desktop assistant that actually runs your computer for you — instantly, offline by default, and without a wall of confirmation dialogs.**

Created by **MrMIB**.

Ask it in plain English to make a folder, find your biggest files, check the weather, launch an app, click a button, kill a stuck process, or a hundred other things, and it just does it — no cloud, no account, no API key. Anything it isn't sure is safe, it asks about first instead of guessing.

## What TOKI can do

- **Files & folders** — create, delete, rename, move, copy, open, list, and search; check disk usage; find your largest, newest, or oldest files; find duplicates; find files over a certain size; export a folder listing to a spreadsheet-ready CSV; copy/paste via the clipboard
- **Apps** — launch anything by name, and click, double-click, right-click, or type into whatever's on screen, just by describing it ("click Save")
- **System stuff** — check uptime, battery, network info, temperature sensors, printers, USB devices; mute/unmute and adjust volume; take a screenshot; lock your PC; empty the Recycle Bin; open Task Manager; see what's using your CPU; and over a thousand more built-in Windows commands for anything more specific
- **Info on demand** — current weather and forecast, the time and date, your location, and web search — no separate browser tab needed
- **File conversion** — convert, resize, compress, or extract files by just asking
- **Generate files** — ask for a script, note, or document and TOKI writes it straight to disk for you
- **Scheduling** — "remind me in 10 minutes to..." or "tell me when CPU usage drops below 20%"
- **Chaining** — do several things in one message: "make a folder called Homework and then open it"
- **Voice** — press Ctrl+K and just talk to it, fully offline
- **Chat** — anything conversational that doesn't fit the above still gets a normal, natural reply

## How to use it

1. Install TOKI (via the installer, or see "Running it manually" below).
2. Open it and just type — or press **Ctrl+K** and speak.
3. Anything TOKI is confident is safe runs immediately. If something you ask for is more sensitive (like deleting something outside the ordinary), TOKI will ask you a quick, plain-text question first instead of just doing it or popping up a dialog box.
4. A **Stop** button is always visible — click it any time to immediately cancel whatever TOKI is doing.

TOKI only works inside a set of folders you (or the installer) choose — by default your Desktop, so it can never wander off and touch something it shouldn't.

## Ollama — completely optional

TOKI can use a local AI model (via [Ollama](https://ollama.com/)) as a fallback for open-ended chat and anything its built-in command list doesn't directly cover. **This is entirely optional.** The vast majority of what TOKI does — files, apps, system commands, scheduling, and more — is handled instantly by TOKI's own built-in logic and never touches Ollama or any AI model at all.

If you skip installing Ollama, or your setup can't run it, TOKI still works normally for everything above. The only thing you lose is free-form conversational chat and open-ended questions that don't map to a specific command — and if you ever do run into that case, TOKI will tell you plainly ("I didn't get that — that needed my AI fallback, which isn't running right now") instead of just going silent.

If you do want the AI fallback, the installer can set up Ollama and a small model (Phi-4-mini) for you — or you can install it yourself:

```
ollama pull phi4-mini
```

## Privacy & safety

- **No cloud calls, no accounts, no API keys.** The only network requests TOKI ever makes are to Ollama on your own machine (if installed) and a handful of free, keyless public services for weather, search, and time zone lookups.
- **Sandboxed by design.** TOKI can only touch the folders you've allowed — nothing outside that boundary is reachable, no matter how it's asked.
- **Nothing risky runs without asking.** Anything TOKI isn't confident is safe pauses and asks you first, in plain text — no modal popups to click through blindly.
- **Deletions go to the Recycle Bin**, never a permanent delete.
- **Stop is always available**, and immediately cancels whatever's running.

## Requirements

- Windows
- Python 3.11 or newer
- [Ollama](https://ollama.com/) — optional, only needed for open-ended chat (see above)

## Running it manually

If you're not using the installer:

```
pip install -r requirements.txt
python main.py
```

## APIs used

- **Weather**: [Open-Meteo](https://open-meteo.com/) (free, no key required)
- **Search**: Wikipedia, with DuckDuckGo as a backup
- **Location**: IP-based lookup via ipinfo.io, fetched once and cached

## License

MIT — see `LICENSE`.
