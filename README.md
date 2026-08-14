# TOKI v1.1.1

Created by **MrMIB**.

TOKI is a lightweight desktop assistant for Windows. It sits as a small
mark at the top of your screen, listens for a hotkey or your voice, and
runs the request instantly — no chat window, no waiting on a cloud
service, no account.

## What TOKI can do

- **Files & folders** — create, move, rename, delete, search, and open
  files and folders by plain description ("open my resume," "make a
  folder called Taxes on the Desktop").
- **Convert & compress files** — images, documents, text, archives, and
  audio/video, just by asking ("convert this to PDF," "shrink this
  image," "zip these up").
- **Download video** — grab a video from a link, or download whatever's
  currently playing in your browser tab.
- **Apps & windows** — launch, close, and switch between apps; click and
  type into on-screen buttons and fields by name.
- **System control** — volume, brightness, power actions, and dozens of
  other one-off Windows commands.
- **Info on demand** — weather, running processes, system stats, and more.
- **Chained commands** — string several requests together in one go
  ("close Chrome and then open Notepad").
- **Scheduling & timers** — "shut down in 10 minutes," "remind me in 20
  minutes to check the oven."
- **Voice or text** — press the hotkey (Ctrl+K) and speak, or double-click
  the mark to type instead.
- **Macros** — record a sequence of actions once and replay it later.
- **Plugins** — extend TOKI with your own drop-in plugins.

Most requests are matched instantly against TOKI's own built-in command
set and run with zero delay. If a request doesn't match anything TOKI
already knows how to do, it can optionally hand the request to a local
AI model (see below) instead of just giving up.

## Ollama (optional AI fallback)

TOKI works completely fine on its own — the vast majority of requests
never need any AI model at all. Ollama, running a small local model
(phi4-mini recommended), is only used as a fallback for the rare request
TOKI's built-in commands don't cover.

- It's entirely optional. You can skip installing it and TOKI will run
  normally.
- It runs 100% locally on your machine — nothing you say or type is ever
  sent to the cloud, by TOKI or by Ollama.
- If a request actually needed the AI fallback and Ollama isn't
  installed or isn't running, TOKI will tell you plainly instead of
  silently doing nothing — something like *"I didn't get that — that
  needed my AI fallback (Ollama), which isn't running or isn't reachable
  right now."*

## Safety

TOKI only ever touches files inside a sandbox — by default your D: drive
and Desktop, or whichever folders you chose during setup. It never
touches System32, Program Files, or anything outside that sandbox.
Anything TOKI isn't confident is safe gets a one-line confirmation
question instead of running automatically, and there's always a visible
Stop button to cancel whatever's in progress.

## Requirements

- Windows 10 or 11
- Python 3.11+
- (Optional) Ollama + a small local model, if you want the AI fallback

## Getting started

Run the installer, choose which folders TOKI is allowed to work in, and
decide whether you want the optional Ollama AI fallback installed. Once
it's done, TOKI runs quietly in the background — press **Ctrl+K** any
time to talk to it.
