# TOKI BETA 0.3.39

Created by **MrMIB**.

A Windows desktop assistant that executes instantly for anything
confidently safe, and — as of BETA 0.3.38 — asks a plain-text, one-line
question before running anything caution/destructive-rated, rather than
either a modal dialog or staying fully unreachable. The safety controls
are the sandbox (`D:\` and Desktop only), a conservative "don't auto-run
it if it isn't confidently safe" dispatch gate, that one narrow
confirmation step, and the always-visible Stop button.

> **Note on this file's age:** everything below `## v2.11 changes` is a
> changelog from an earlier phase of the project (the original Ollama-
> classification-only design, before the graph router/WCL resolver existed)
> and is kept only as historical context — it does **not** describe the app
> as it works today. `STATUS.md` is the actual up-to-date, chronological
> changelog (3,500+ lines); `PROJECT_STATE_OVERVIEW.md` is a shorter
> structured snapshot of current state. Everything from here down to that
> divider has been rewritten to match the code as of BETA 0.3.37.

## Status at a glance

**What it works on:** Windows only (PowerShell execution + pywinauto/UI
Automation for app control — both Windows-specific). Python 3.11+. A local
Ollama instance running a small model (phi4-mini recommended) as the
fallback for anything the router doesn't confidently resolve — most
requests never reach it at all (see "Flow" below). No cloud LLM calls and no
API keys anywhere — the only network calls this app makes are to Ollama
(localhost) and four keyless public APIs (Open-Meteo, Wikipedia, DuckDuckGo
Instant Answer, ipinfo.io for one-time location caching).

**Architecture, top to bottom:**
```
main.py            entry point — dependency check, then launches app.py
app.py             PyQt6 UI: chat bubbles, Stop button, intent pills, a
                   QThread Worker per message so the UI never blocks
orchestrator.py    the brain (WindowsAIAssistant + OllamaRouter) —
                   process_request() runs the full pipeline below, in order
graph_router.py    Tier A — TOKI's own 65 hand-written intents, matched via
                   TF-IDF cosine similarity against a prebuilt Kùzu graph
                   database (toki_graph_db/); fast, deterministic, offline
wcl_resolver.py    Tier B — only consulted when Tier A misses; matches
                   against the 1,160-command Windows Command Library
                   (wcl_kg/windows_commands_db) through an ordered cascade
                   of increasingly-loose matching tiers
categories.py      7 Tier A categories, and the map from every Tier A
                   intent to its category
intents*.py        the closed Tier A vocabulary (65 intents across 3 files)
                   — each intent's description, execution "kind",
                   template/API binding, and required slot names
extractor.py       turns raw user text into slot VALUES via regex only, for
                   BOTH Tier A intents and eligible WCL commands — plus
                   sandbox path resolution and the fixed missing-slot
                   questions; never guesses when a value isn't confidently
                   present
scheduler.py       "in 10 minutes"-style scheduled requests — parsed ahead
                   of the main classification pipeline, handed to a
                   background timer
condition_checker.py   "when the CPU drops below X"-style conditional
                   requests — a background poller, concurrency-safe against
                   cancellation races
executor.py        runs PowerShell as a real killable subprocess
app_control.py     UI Automation (pywinauto) — resolves "the Save button"
                   to real on-screen coordinates via fuzzy matching, never
                   the model; failure and success are cached separately so
                   a transient hiccup doesn't permanently degrade a feature
apis.py            weather/search/time/location — the non-PowerShell
                   tools, with the same failure/success caching split
generator.py       GENERATE_FILE — the one place real free-text generation
                   happens, isolated from the command pipeline, written via
                   plain file I/O, never through a shell
voice_pipeline.py  Ctrl+K hotkey-triggered voice input — openWakeWord
                   (wake word), Silero VAD via ONNX (torch-free), and
                   faster-whisper (tiny.en, int8, CPU) for transcription
```

**What works (wired end-to-end, per the current code):**
- **Tier A**: 65 hand-written intents across 7 categories (file
  operations, process control, system info, app launching/control via real
  UI Automation coordinates, one file-generation intent, one chat intent),
  matched via a Kùzu graph database, fully offline
- **Tier B**: 630 of the Windows Command Library's 1,160 commands are
  auto-dispatchable today — every `danger_level == "safe"` command at 0/1/2
  variable counts (see `PROJECT_STATE_OVERVIEW.md` §2 for the exact
  breakdown by danger level and variable count). `caution`/`destructive`
  commands never auto-dispatch, at any variable count, by deliberate design
- The destructive-shadow guard (`orchestrator.py::_check_destructive_shadow`)
  — if Tier A matches something, this independently checks whether WCL also
  has a genuinely destructive command for the same phrase, and asks a
  clarifying question instead of silently trusting Tier A's guess
- Regex-only slot extraction (single-variable AND 2-variable WCL commands,
  plus a numeric-hint strategy for path+count/size pairs like "show me the
  5 largest files") with a fixed follow-up question whenever a required
  slot can't be confidently pulled from the message — never invented
- Sandbox enforcement to `D:\` and the real Desktop (resolved via Windows'
  own known-folder API, not assumed from `%USERPROFILE%`)
- Command-injection protection on every PowerShell template substitution —
  `_ensure_quoted_placeholders()` + `_escape_ps_slot()` guarantee a value
  lands inside a fully-literal, properly-escaped string regardless of how
  the source template itself was written
- A categorical blocklist for variables representing literal code/
  scriptblock content, and a path-variable allowlist routing 23 distinct
  variable names through the sandboxed `resolve_path()` rather than
  trusting them as free text
- PowerShell execution with live streaming output and a Stop button that
  kills the whole process tree
- Weather/forecast (Open-Meteo), web search (Wikipedia primary + DuckDuckGo
  fallback), time/date, cached location
- File generation: streamed model content written straight to a sandboxed
  file via plain Python I/O, never through a command string
- App control: launch apps, click/double-click/right-click/type into
  whatever's focused, via fuzzy-matched real UI Automation coordinates —
  fails safe (no click) if nothing matches confidently
- Command chaining ("make a folder and then open it")
- Scheduled ("in 10 minutes") and conditional ("when the CPU drops below X")
  requests, handled by a separate pre-check before the main pipeline
- Ctrl+K voice input, fully offline (wake word + VAD + transcription)
- `//command` direct override and `""`/`''` literal-value quoting, both of
  which skip classification/heuristic guessing entirely when used
- Live streaming narration for the LLM-fallback path
- Creator identity (MrMIB) known to the model

**What's still under dev / known gaps** (see `PROJECT_STATE_OVERVIEW.md` §3
for the full, current list — this is a summary):
- No generic slot-filler for 3+ variable WCL commands (181 of them)
- No auto-dispatch for `caution`/`destructive` WCL commands at any
  variable count — deliberate, not a gap
- No auto-dispatch for `caution`/`destructive` WCL commands without
  confirmation — as of BETA 0.3.38 these pause and show the exact
  command via a plain-text question (a bare Enter or an avatar click
  both confirm) instead of being 100% unreachable; still no MODAL dialog
  anywhere, and 3+ variable commands remain entirely out of scope either
  way
- No full natural-language-coverage audit of the 1,160-command WCL alias
  dataset — only targeted audits so far
- No live Ollama or live Windows testing anywhere in this project's recent
  history — everything is verified via direct unit/integration testing of
  the Python logic
- Windows-only — no macOS/Linux support, since PowerShell and pywinauto's
  UI Automation backend are both Windows-specific

**Capabilities, in plain terms — what you can actually ask it:**
- *Files/folders* (sandboxed to `D:\`/Desktop): create, delete, rename,
  move, copy, list, find, read, open; disk usage; path existence/
  properties/resolve/split; count files/folders; file-type breakdown; find
  duplicates; find files by content; clipboard get/set; export a folder
  listing to CSV; largest/most-recent/oldest files, largest folders (with
  an optional count, e.g. "show me the 5 largest files"); find files over
  a given size ("find large files over 500MB")
- *Processes*: list, find, wait for, kill by name; top CPU consumers; open
  Task Manager
- *System*: uptime, hostname, locale, printers, USB devices, temperature
  sensors, services, mute/volume, screenshot, lock the workstation, battery
  status, empty the Recycle Bin, network info, current user, basic system
  info; plus 1,160 real Windows commands via Tier B, for anything Tier A's
  own 65 intents don't cover
- *Info lookups*: current weather/forecast (by city or your cached
  location), time, date, your location, web search
- *App control*: launch any app by name; click/double-click/right-click/
  type into whatever's currently focused on screen
- *File generation*: ask for a script/document/etc. and get real generated
  content written to a sandboxed file
- *Scheduling*: "remind me in 10 minutes to..." / "when the CPU usage drops
  below 20%, tell me"
- *Chaining*: multi-step single messages ("make a folder called Homework
  and then open it")
- *Voice*: press Ctrl+K and speak, fully offline
- *Chat*: anything else, conversational, via the local Ollama fallback

## How a message actually flows through the app

```
your message
   -> canned replies (a handful of greetings/thanks) get an instant, fixed
      response with zero model calls                                        [orchestrator.py]
   -> scheduling / conditional pre-check ("in 10 minutes" / "when X drops
      below Y") gets parsed and handed to a background timer/poller,
      separate from everything below                                        [scheduler.py, condition_checker.py]
   -> Tier A: the graph router tries TOKI's own 65 intents against a
      prebuilt Kùzu graph via TF-IDF cosine similarity                       [graph_router.py]
   -> Tier B: only if Tier A missed, the WCL resolver tries the
      1,160-command Windows library through its ordered matching cascade    [wcl_resolver.py]
   -> the destructive-shadow guard: if Tier A DID match something, this
      independently asks WCL "is there also a genuinely destructive
      command for this same phrase?" and asks the user instead of
      silently trusting Tier A's guess if so                                [orchestrator.py]
   -> the WCL eligibility gate: a RESOLVED WCL command only becomes
      directly runnable if danger_level == "safe", at every variable count  [orchestrator.py]
   -> slot extraction: regex-only, for both Tier A intents and eligible
      WCL commands — asks a fixed follow-up question if a required value
      can't be confidently pulled from the message, never guesses          [extractor.py]
   -> anything that falls through all of the above (a genuine miss, or an
      ineligible/ambiguous/dangerous WCL match) goes to Ollama for
      open-ended handling                                                    [orchestrator.py::OllamaRouter]
   -> dispatch: runs the PowerShell command as a real, killable subprocess,
      or calls the relevant tool (weather/search/location, UI Automation,
      file generation)                                                       [orchestrator.py::_dispatch, executor.py]
```

A message can also contain more than one request, split on literal
conjunctions the user actually typed ("and then", "then", ";", ", and").
Each resulting piece runs through the exact same pipeline above, in order,
capped at 4 segments — this is deliberately **not** a general planner:
there's no model call deciding how to break up the request, and no
inferred steps beyond what's literally separated in the user's own text.

## Safety model

TOKI has **no MODAL permission dialogs**. Safety comes from four things
working together, not from a dialog box on every action:

- **Not auto-running things that aren't confidently safe** — Tier A intents
  are a hand-vetted closed set; Tier B (WCL) only auto-dispatches commands
  whose `danger_level` is `"safe"`, at every variable count including zero.
  `caution`/`destructive` commands (0/1/2 variables) now pause for a plain-
  text confirmation instead (BETA 0.3.38, see below) — never straight to
  execution without it, and 3+ variable commands remain unreachable either
  way.
- **A narrow, single-choke-point confirmation step for the one case above**
  — `orchestrator.py`'s `_dispatch_or_confirm()` is the one place every
  dispatch-ready call site routes through, so it can't be accidentally
  bypassed at a new call site. Shows the EXACT command that would run
  (built by the same code path the real dispatch uses, so the preview can
  never drift from reality), renders through `"kind": "chat"` — the same
  plain-text path as any other response, no modal, no new UI needed. A
  bare Enter, a short word ("y"/"yes"/"ok"/"confirm"/"run it"), or an
  avatar click all confirm; anything else cancels silently and that
  message is processed as an ordinary new turn.
- **A sandboxed filesystem boundary** — every resolved path is checked
  against `D:\` and `%USERPROFILE%\Desktop` only (`extractor.get_sandbox_roots`
  / `is_within_sandbox`). Anything outside — System32, Program Files, `..`
  traversal — is rejected before it ever reaches PowerShell. A curated
  allowlist of 23 WCL variable names is routed through this same sandbox
  rather than trusted as free text (e.g. `call {batch_file}` can't be
  pointed at an arbitrary script anywhere on disk).
- **An always-visible Stop button** — not just while busy. Calls
  `WindowsAIAssistant.stop()`, which cancels the in-flight Ollama request
  and/or kills the running PowerShell process tree (`taskkill /F /T`) so a
  bad call can be killed the instant it looks wrong.

On top of those three, a few more specific mechanisms close narrower gaps
found by testing (see `PROJECT_STATE_OVERVIEW.md` §4 for the full history
of what was found broken and fixed):

- Every PowerShell template substitution goes through a quote-state-
  tracking scanner (`_ensure_quoted_placeholders()`) plus backtick/`$`
  escaping (`_escape_ps_slot()`) — closes a confirmed-live command-
  injection path that existed in 297 of 298 single-variable "safe" WCL
  commands before it was fixed.
- A categorical blocklist (by variable name substring) for WCL variables
  representing literal code/scriptblock content — quoting alone doesn't
  protect against PowerShell implicitly compiling a string into an
  executable scriptblock for certain parameter types.
- The destructive-shadow guard described above.
- **Delete** goes through the Shell COM `InvokeVerb('delete')`, i.e. the
  Recycle Bin, not a permanent delete.
- **Chaining has a hard cap** (`_MAX_CHAIN_SEGMENTS = 4`) and stops
  immediately if any segment needs clarification, rather than guessing at
  later steps out of order.

## Requirements

- Windows (PowerShell execution + UI Automation are Windows-only)
- Python 3.11+
- [Kùzu](https://kuzudb.com/) (`kuzu>=0.11.0`) — the embedded graph database
  behind Tier A's router and Tier B's WCL matching. Not optional: this is
  how the majority of requests get handled before any LLM call happens.
- [Ollama](https://ollama.com/), with a small model pulled — **Phi-4-mini**
  is the recommended default for a 7GB VRAM budget, used only as the
  fallback path for requests neither Tier A nor Tier B resolves:
  ```
  ollama pull phi4-mini
  ```
- `pywinauto` + `comtypes` (Windows-only) for `app_control.py`'s real
  on-screen coordinate resolution.
- `openwakeword` + `faster-whisper` + `sounddevice` for the optional Ctrl+K
  voice pipeline (wake word, offline transcription, mic capture) — TOKI
  runs fine without these installed, just without voice input.
- `PyQt6-WebEngine`, optional — only needed to see the animated header icon
  and desktop overlay actually animate; both fail soft to a static
  placeholder without it.

See `requirements.txt` for exact version pins and per-package rationale.

## Run it

```
pip install -r requirements.txt
python main.py
```

## Run the tests

```
pytest tests/ -q
```

Currently: 536 passed, 1 skipped (environment-dependent), 2 xfailed (both
pre-existing, unrelated, documented gaps) — see `PROJECT_STATE_OVERVIEW.md`
for what those xfails are. This is the fast, fully-offline, deterministic
suite (no live Ollama, no live Windows needed) that covers the router, the
WCL resolver, the extractor, the safety guards, and the orchestrator's
dispatch logic directly.

`run.ps1` / `run_all_tests.py` also exist for driving live-Ollama batch
tests (`batch_test_*.py`) on top of the pytest suite — see those scripts'
own `--help` / comment headers for current flags, since their scope is
separate from (and slower than) the pytest suite above.

## APIs used

- **Weather**: [Open-Meteo](https://open-meteo.com/) — free, no key required.
- **Search**: Wikipedia's REST API (search + summary) as the primary
  source, with DuckDuckGo's Instant Answer API kept as a secondary
  fallback for topics Wikipedia doesn't cover.
- **Time/date**: pure Python (`datetime.now()`), zero network calls.
- **Location**: IP-based geolocation via ipinfo.io, fetched once at
  startup (off the UI thread) and cached for the session.

## File generation

`GENERATE_FILE` is fully wired end to end: `generator.py` streams generated
content token-by-token via `on_generate_token`, then calls
`on_generate_done(path, error)` once the file is saved. `app.py` shows a
live monospace preview while it streams, then replaces it with a
confirmation ("✓ Saved to ...") once done. Writes are plain Python file
I/O — never through PowerShell — and still land inside the same
`D:\`/Desktop sandbox as everything else.

## Command chaining

A message can contain more than one request, split on literal conjunctions
the user actually typed — "and then", "then", ";", ", and". Each resulting
piece runs through the exact same single-request pipeline as any other
message, in order, capped at 4 segments, and each shows its own result in
the chat. This is deliberately **not** a general planner: there's no model
call deciding how to break up the request, and no inferred steps beyond
what's literally separated in the user's own text — same "never let the
model invent structure" rule that governs slot extraction everywhere else
in this app. See `orchestrator.py`'s `_split_chain()` docstring for the
full reasoning, including why a bare "and" without "then" or a comma
deliberately does *not* split (so a folder literally named "Homework and
Projects" doesn't get chopped in half).

## Extending the intent list

**Tier A (a new hand-written intent):** add a new entry to
`INTENTS_EXTENDED` (or a new file, following the same shape) with a
description + PowerShell template + slots, register it in `categories.py`'s
`INTENT_CATEGORY` map (required — `orchestrator.py` asserts every intent
has a category at import time), and add a matching branch in
`extract_slots()` in `extractor.py` if it needs non-trivial variable
extraction. Then re-run the graph-database build step so the new intent's
phrasing corpus is actually indexed (see `migrate_to_kuzu.py`).

**Tier B (unlocking more of the existing WCL dataset):** the 1,160 commands
already exist in `wcl_kg/windows_command_library.widened.json` — most of
the extension surface here isn't adding new commands, it's extending
`extractor.py`'s generic WCL slot-filler to cover more variable-count/shape
combinations (see `_extract_wcl_slots()` / `_extract_wcl_slots_pair()` /
`_extract_wcl_numeric_pair()`), or deciding whether/how `caution`/
`destructive` commands should ever become dispatchable (currently: never,
by design).

If a template has zero slots but still contains literal PowerShell braces
(`if (...) { ... }`), escape them as `{{ }}` — every template goes through
Python's `.format(**slots)`, so an unescaped brace will raise at dispatch
time even with an empty slots dict.

## v2.11 changes (this session)

**Fixed the fabricated-narration bug for real this time — structurally, not with a better prompt.**
The v2.10 fix only added an instruction to `_build_thinking_system_prompt()`
telling the model it didn't know the outcome yet. That was **not enough on
its own** — confirmed live: web search still produced a fabricated
narration sentence before the real result. Root cause of why prompting
alone couldn't fix it: `stream_thinking()` for an `api`-kind intent
(`GET_WEATHER`/`GET_FORECAST`/`SEARCH_WEB`/`GET_TIME`/`GET_DATE`/
`GET_LOCATION`) was still being started **concurrently** with the real
`ToolDispatcher.call()`, with the user's actual question sitting in the
same call as the "don't guess" instruction — under free-text generation, a
small model given a real question just answers it, and a negative
instruction competes with (and loses to) that pull.

**The actual fix (`orchestrator.py`, `_dispatch()`):** for `api`-kind
intents specifically, the real API call now runs **first**, and thinking
only starts **afterward** — seeded with the real result directly in its
context ("Here is the actual result you just got back: '...'. Narrate this
using ONLY the facts in that result"). There is no longer a window where
thinking runs with an unfilled blank next to the user's literal question —
it's handed the fact and asked to restate it, not asked to predict it.
Every other kind (`powershell`/`app_control`/`generate`) still starts
thinking concurrently with the real work, since those don't ask the model
to state a specific fact it doesn't have yet — the correctness risk is
specific to `api`-kind intents. **Cost**: API-kind calls lose the
overlap-for-speed optimization from v2.4 — thinking now waits for the
Open-Meteo/Wikipedia round-trip (typically well under a second) before it
starts streaming. Verified with a stubbed dispatcher that the grounded
context reaching `stream_thinking()` contains the real result string
before any narration is generated, and that the final `response` and
`result` in the returned dict are derived from the same call. **Not yet
re-verified against real Ollama** — the concurrency window that let this
happen is now structurally gone, but confirm live before trusting it.

**Correction, BETA 0.3.7:** the assumption above — that `powershell`/
`app_control`/`generate` kinds are safe to leave concurrent because "those
don't ask the model to state a specific fact it doesn't have yet" — turned
out to be wrong. They do: a filename, a folder name, a process name — the
fact is just synchronously known (from `extract_slots()`) rather than
requiring a live call, so there was no reason it needed the same
run-first-then-narrate treatment as `api`-kind, but the fact itself was
never actually being *told* to the model either way. `decision_context`
for these kinds was still the same generic per-intent description with no
slot values in it, so the model had to independently re-derive specifics
from the raw message — confirmed live, this is what produced a file
correctly created as `python.txt` but narrated as if it were named
`'nmed'`. Fixed by `_decision_context()`, which now injects the real
resolved value(s) directly (no need to delay dispatch — synchronous facts
don't have a "not fetched yet" window the way an API result does), plus a
buffer-and-validate structural backstop (`_is_narration_grounded()` /
`_fallback_narration()`) for when even a fully-grounded prompt still isn't
followed. See STATUS.md's BETA 0.3.7 entry for the full mechanism.

**Fixed: `who founded X` / `founder` vs `founded` search mismatch**
(`apis.py`, `_best_matching_sentence`). The v2.10 targeted-answer scorer
matched query keywords against article sentences via plain substring
containment — `"founder"` (from the question) is not a substring of
`"founded"` (in the article text), so scoring silently found nothing and
fell back to the generic lead paragraph. Fixed with a small
suffix-stripping stemmer (`_stem()`) that reduces both sides to their word
family (`founder`/`founded`/`founding`/`founders` → `found`) before
comparing, and switched from raw substring scanning to whole-word set
comparison — which also closes a latent false-positive hole the old
substring check had (a short keyword like `"art"` could spuriously match
inside `"started"` or `"particle"`). Verified against the founder/founded
case, a plural variant, the existing "capital of X"/"tell me about X"
cases (no regression), and the `"art"` false-positive case directly.

**Fixed: classification prompts never told the model to use the history
they were already receiving.** Traced a reported misclassification
("but who founded it" → `GET_WEATHER` instead of a `SEARCH_WEB`
follow-up) to a real gap: `classify()`'s own docstring says history was
added specifically so ambiguous follow-ups resolve correctly, but the
actual system prompt text sent to the model
(`_build_category_prompt()` / `_build_command_prompt()`) never said
anything about *using* that history — it was present in the message list
with zero instruction attached. Added an explicit rule to both prompts:
a short follow-up that depends on what was just said (starts with "but"/
"and", or uses a bare pronoun with no named topic of its own) should be
resolved against the conversation history's topic, not classified cold on
its own words. This is a genuine, actionable fix for the described gap;
it is not a guarantee against every possible small-model misclassification
on a fundamentally probabilistic two-tier classify call.

**Also fixed: thinking narration asking rhetorical questions it can't
honor** (e.g. "Would you like me to share it with you?" immediately
followed by the answer regardless). Added an explicit instruction to
`_build_thinking_system_prompt()` that the action always runs and its
result always shows right after the narration no matter what it says, so
it should never pose a question there.

**Why the earlier fabricated-weather bug read as "random hallucination"
rather than an obvious error, worth understanding for future debugging:**
`GET_WEATHER`/`GET_TIME`/`GET_DATE`/`GET_LOCATION` all have **zero
required slots** in `extract_slots()` — `GET_WEATHER` with no city
extracted returns `{}` (empty dict, not `None`), which the caller treats
as success and silently falls back to the cached IP location. So when
classification mis-picks one of these four intents, nothing downstream
ever produces an error to catch it — the regex layer "succeeds"
unconditionally, the API call succeeds, and the turn completes cleanly
with a plausible-looking wrong answer. This is why the failure looked like
regex or a hallucinating extractor when it was actually purely a
classification-layer issue — every other intent in this app (all 56 of
the other 60) has at least one slot that can legitimately fail closed and
trigger the fixed follow-up question instead of silently guessing.

**Creator identity added.** `_build_thinking_system_prompt()` now opens
with "You are TOKI, a Windows assistant created by MrMIB," plus an
explicit instruction to say MrMIB made it if asked who built/created it,
rather than deflecting or guessing. This is the one system prompt shared
by every `stream_thinking()` call (action narration, CHAT, missing-slot
questions), so it's the single place this needed to be added for the
identity to be available regardless of which category/command a message
resolves to.

**Verified in this session's sandbox** (no real Ollama available here):
all three orchestrator.py changes pass a syntax check and the full
project still imports cleanly; the `_dispatch()` api-kind restructuring
was exercised end-to-end with a stubbed dispatcher/thinking call,
confirming the real result reaches thinking's context before any
narration is generated; the stemming fix was verified against 5 concrete
input/output pairs including a regression check. **Not yet verified
live**: real phi4-mini behavior under the new prompts, the real Wikipedia
API, real Ollama timing for the now-sequential api-kind dispatch path.

## v2.10 changes (this session)

**"Cold model load" was a false positive, not a real reload.** Real
evidence from the target machine: `ollama ps` showed `phi4-mini:latest`
staying resident continuously (countdown never reset to a fresh 30m
across several checks), and Ollama is running `100% CPU` there — no GPU
offload at all. The old `_log_timing()` threshold (500ms) was tuned for
GPU-speed warm loads (near-zero ms); on a CPU-only box, a genuinely warm
call still naturally reports `load_duration` around 800-1100ms just from
general request/runner overhead being slower on CPU, which tripped the
flag on *every* call. A real captured cold load on that same machine was
~8000ms. Threshold raised to 3000ms, which cleanly separates both real
clusters actually seen. The `keep_alive: "30m"` fix from a prior session
was already working correctly the whole time — this was purely a
misleading diagnostic label, not an actual behavior bug. The real,
unavoidable cost on this hardware is inference itself: CPU prompt-eval
alone hit 23,599ms on one captured classify() call. That's a hardware
ceiling (no dedicated GPU), not something fixable in this codebase.

**Web search now attempts a targeted answer to specific sub-questions**
(`apis.py`, `WebSearchAPI`). Previously `search()` always returned the
matched Wikipedia page's fixed lead-paragraph summary, regardless of what
was actually asked — "who founded Pakistan" got Pakistan's generic
geography/population intro, not anything about its founding, because the
summary REST endpoint only ever returns the lead paragraph. Fix: pull a
bigger real extract (Wikipedia's own `action=query&prop=extracts` API,
still not a scrape) and score its sentences against the query's
DISTINGUISHING keywords — the topic name itself is deliberately excluded
from scoring, since it appears in nearly every sentence and would match
everything. Falls back to the original lead-summary behavior unchanged
for generic "tell me about X" queries where nothing distinguishing is
left to search for. Verified with a stubbed extract against realistic
Wikipedia-shaped text: "who founded X" correctly picks the founding
sentence, "what is the capital of X" picks the capital sentence, "tell me
about X" correctly falls through to the old summary path. Not yet run
against the real Wikipedia API.

**New: `""` / `''` quoting convention for unambiguous literal values.**
`""` around text is now an explicit literal for a name/content slot (file
name, folder name, etc.) — used verbatim, no heuristic guessing.
`extract_slots()`'s `_extract_name()` already had a merged quote-matching
fallback; this makes `""` specifically take priority over it, so both
still work but `""` is now the deliberate, reliable way to pin an exact
name down. `''` is the equivalent for `LAUNCH_APP`'s app name
specifically (`'Chrome'`) — kept as a separate quote character from `""`
on purpose, so one instruction could eventually name both a literal and
an app without the two colliding. Neither breaks anything that worked
unquoted before; both are additive priority checks in front of the
existing heuristics.

**New: `//command` direct override**
(`orchestrator.py`'s `parse_command_override()`). A message starting with
`//` plus a known command name (a curated short alias like `//weather`,
or any real intent name verbatim like `//MAKE_FOLDER`) skips BOTH
classification calls entirely and goes straight to `extract_slots()` with
the intent already decided. Two real wins: faster (2 fewer Ollama
round-trips), and fully deterministic — there's no classification step
left to misfire on it. Verified end-to-end with a stubbed router:
`classify()` was called zero times for `"//weather"`, and it correctly
dispatched straight to `GET_WEATHER`. An unrecognized `//something` falls
through to normal classification untouched rather than erroring.

**Important clarification on what this DOES and DOESN'T fix, since it
was asked directly:** none of this changes `stream_thinking()` or
anything CHAT-kind narrates. Thinking has never been the thing that
picked a wrong category/command or a wrong name — by design (see
v2.4 note above), it only runs AFTER the real decision (classify +
extract_slots) is already fully made, and just narrates it. Wrong calls
like a misnamed folder come from classification picking the wrong
intent, or `extract_slots()`'s regex grabbing the wrong span of text —
that's the layer `""`/`''`/`//` target directly. Quoting and the
override don't make classification itself smarter or hallucination-proof
on their own; they give the user a way to make a specific instruction
UNAMBIGUOUS enough that the extraction layer doesn't have to guess at
all, and `//` skips the classification layer's guessing entirely for
known commands. A genuinely ambiguous unquoted instruction with no
override can still be misclassified — that hasn't changed and isn't
something this session's fix claims to solve.

**Verified in this session's sandbox only:** stubbed-router and
stubbed-extractor tests, real captured timing numbers (from the user's
actual machine, not this sandbox), syntax checks. **Not yet verified**:
the Wikipedia targeted-answer fix against the real API, or `//command`
against a real Ollama instance end-to-end.

## v2.9 changes (this session)

**Fixed: tier-1 classification and CHAT-kind thinking had zero conversation
history**, by design, from an earlier session's reasonable-sounding
performance optimization ("picking a category never depends on prior
turns"). That assumption broke as soon as chaining existed: "make a
folder called Homework and then open it" splits into two segments, and
the second segment, "open it", used to be classified with nothing to go
on. With zero history, "open it" is genuinely ambiguous between the
`FILESYSTEM` category (open the folder just created) and `APP_CONTROL`
(open/launch an application) — `CATEGORIES`'s own description text reads
as an equally or more plausible match for that bare phrase without
context. This was reproduced directly with a stubbed `classify()` before
being called a real bug: it's a genuine ambiguity in the classification
input, not a made-up "hallucination."

**Fix**: `OllamaRouter.classify()` now passes `history` into *both*
tier-1 (category) and tier-2 (command) calls — previously only tier-2
received it. `stream_thinking()`'s CHAT-kind call also now receives
`self.history`, so if the user follows up with "why didn't that work,"
the narration is grounded in what actually happened instead of
improvising a disconnected-sounding answer. History stays capped at the
last 4 entries (2 exchanges) via `_commit_history()`, unchanged from
before — this fix widens *who* sees history, not how much of it there is,
so it doesn't reopen the original prompt-processing cost concern that
motivated the cap in the first place.

**Was a known gap, fixed in BETA 0.3.7**: `extract_slots()` still extracts
only from the user's raw per-segment text, never from history or model
output — that part is unchanged. But "open it" (correctly *classified* as
`OPEN_ITEM` since this session) no longer dead-ends at the fixed follow-up
question either: `WindowsAIAssistant._last_touched` plus
`extractor.resolve_anaphoric_target()` now resolve the pronoun against
whatever TOKI itself most recently created, so "make a folder called X and
then open it" both creates AND opens it in one turn. See STATUS.md's BETA
0.3.7 entry for the mechanism and its deliberate scope (single-target
intents only; `RENAME_ITEM`/`MOVE_ITEM`/`COPY_ITEM`'s two-slot shape is
still a separate follow-up).

**Verified in this session's sandbox** (no real Ollama available here):
reproduced the original bug and confirmed the fix with a stubbed
`OllamaRouter._call()` — both the tier-1 and tier-2 calls now receive the
identical `history` list passed into `classify()`, and a full chained
`process_request()` run against a stubbed router confirmed the second
segment's `classify()` call actually receives the first segment's
committed history entry (`[ran: MAKE_FOLDER]`) rather than an empty list.
The full project still imports cleanly and all files pass a syntax check.
**Not yet verified against a real Ollama model** — whether phi4-mini's
actual classification accuracy improves on ambiguous chained follow-ups
with this history in front of it (rather than just confirming the
history is *mechanically delivered*) needs a real run before trusting it
for a live demo.

## v2.8 changes (this session)

**Investigated reported "model response is slow"** after the v2.7 COM/DPI
startup fix. Two real, separate contributors identified:

1. **No `keep_alive` was set on any of this app's three Ollama call sites**
   (`OllamaRouter._call()`, `OllamaRouter.stream_thinking()`,
   `FileGenerator`'s call in `generator.py`), so each relied on Ollama's
   server-side default -- typically 5 minutes of inactivity before the
   model unloads from VRAM. A demo has natural pauses (talking to judges,
   answering questions) that can exceed that window, and reloading a model
   costs several real seconds on the next message. **Fixed**: all three
   call sites now explicitly pass `"keep_alive": "30m"`, keeping the model
   resident in memory for the length of a realistic presentation without
   pinning it forever.

2. **This app's architecture inherently makes 2-3 sequential Ollama calls
   per message** (tier-1 category, tier-2 command, then a separate
   `stream_thinking()` narration call -- see the two-tier classification
   section above), and chaining multiplies this further per segment. This
   is a genuine, deliberate design tradeoff from earlier sessions (schema
   -constrained closed-vocabulary picks, kept in small scoped grammars so
   Ollama doesn't crash/hang on a big one) -- **not a bug**, and not
   something this session changed, but it does mean per-message latency is
   inherently higher than a single-call chatbot's, especially on larger
   models or slower hardware.

**Added `OllamaRouter._log_timing()`**, which prints Ollama's own reported
`load_duration` / `prompt_eval_duration` / `eval_duration` breakdown (in ms)
to the console after every classify/thinking call, flagging any call whose
`load_duration` exceeds 500ms as a cold model load. This exists so it's
possible to SEE on the actual target machine, tonight, whether "slow"
means paying reload cost (should now be fixed by `keep_alive`) or is just
the inherent cost of 2-3 sequential inference calls per message (which
`keep_alive` cannot fix -- that would need a smaller/faster model, fewer
calls, or better hardware).

**What this session could NOT verify**: there's no real Ollama instance
available in this dev sandbox, so none of this was measured against actual
latency numbers. Run a real message through tonight and watch the console
for `[TOKI timing]` lines -- if `load_duration` stays near 0ms across
consecutive messages, the `keep_alive` fix is working and any remaining
slowness is the inherent per-call cost described above, which would need a
different fix (e.g. `qwen2.5:0.5b`/smaller model, or reducing to one
classification call) rather than another keep_alive-style patch.

## v2.7 changes (previous session)

**Fixed a real startup bug affecting every run on Windows**: on launch, the
app printed `QWindowsContext: OleInitialize() failed: "COM error
0x80010106: Cannot change thread mode after it is set."` plus a DPI
-awareness warning, and ran noticeably slower.

Root cause, confirmed by reading the actual installed `pywinauto` package
source: `pywinauto`'s own top-level `__init__.py` calls
`pythoncom.CoInitializeEx(COINIT_MULTITHREADED)` **at import time** — not
lazily on first use, contrary to what an earlier session's review assumed.
`app_control.py` used to `import pywinauto` at the top of the file, which
is imported by `orchestrator.py`, which `app.py` imports *before*
constructing `QApplication()`. So by the time `QApplication()` ran and Qt
tried to claim the main thread as STA via its internal `OleInitialize()`
call, the thread was already locked into MTA by pywinauto's import —
`RPC_E_CHANGED_MODE` / `0x80010106` is exactly that collision, and the DPI
failure right after it in the same startup sequence is a downstream
symptom of the same conflict, not a second unrelated bug. This most likely
also explains the reported general sluggishness — a failed OLE/DPI
negotiation at startup can degrade rendering for the rest of the process's
life, not just print one warning and move on.

**Fix**: `pywinauto`/`comtypes` are no longer imported at module level in
`app_control.py` at all. `_load_pywinauto()` now performs that import
lazily, the first time an `APP_CONTROL` action actually runs — which
always happens inside `app.py`'s `Worker`, a fresh `QThread` created per
message, well after `QApplication()` already exists and has already
claimed the main thread's apartment mode. Importing pywinauto for the
first time on a later, different thread can't retroactively break a
decision the main thread already made. `_PYWINAUTO_AVAILABLE` is now a
tri-state (`None` = not yet probed) instead of a plain bool set at import
time, and every call site that used to read it directly now calls
`_load_pywinauto()` instead, so it's correctly probed on whichever thread
hits it first rather than relying on a value that may never have been set.

Verified in this session's sandbox (no real Windows/pywinauto available
here): `import app_control` no longer touches pywinauto/comtypes at all
(`_PYWINAUTO_AVAILABLE` stays `None` after plain import); `_load_pywinauto()`
correctly reports unavailability without crashing when pywinauto genuinely
isn't present; the full project still imports cleanly and `MainWindow`
still constructs correctly with the change in place. **Not yet verified
that this actually eliminates the OLE/DPI error on a real Windows
machine** — that needs a real run, but the mechanism is confirmed directly
against pywinauto's actual source code, not guessed.

## v2.6 changes (previous session)

- **Web search fixed.** Replaced DuckDuckGo Instant Answer alone with
  Wikipedia search+summary (primary) + DuckDuckGo Instant Answer
  (secondary fallback). Also fixed `SEARCH_WEB`'s query extraction in
  `extractor.py`, which previously only stripped the trigger word if it
  was the literal first token — "can you search for X" left the filler
  words IN the query. Same filler-stripping approach `LAUNCH_APP` already
  used, applied to `SEARCH_WEB`.
- **"Two replies" bug fixed at the root.** Removed the collapsible
  thinking widget entirely; narration now streams straight into the one
  visible answer label. See `app.py`'s `_on_thinking_token` docstring.
- **Chaining added.** `orchestrator.py`: new `_split_chain()` +
  `process_request()` now wraps the renamed `_process_single_request()`,
  running each split segment through the unchanged single-intent pipeline
  in sequence. `app.py`: `ChatBubble.add_step_block()` renders one
  pill+text section per extra step.
- **File generation re-enabled and wired.** `_DISABLED_CATEGORIES` cleared
  in `orchestrator.py`; `app.py`'s `Worker` and `MainWindow` now actually
  connect `generate_token`/`generate_done` (previously defined in the
  orchestrator's callback contract but never wired up on the UI side, so
  GENERATE was silently a no-op even when enabled).
- **7 new commands** in `intents_extended.py`, each checked against real
  PowerShell/.NET documentation before being added: `TOGGLE_MUTE`,
  `VOLUME_UP`, `VOLUME_DOWN`, `TAKE_SCREENSHOT`, `LOCK_WORKSTATION`,
  `BATTERY_STATUS`, `EMPTY_RECYCLE_BIN`. Deliberately did **not** add
  volume-to-a-specific-percent (no native PowerShell API exists for that;
  every real solution needs a third-party module or a separate download)
  or sleep/shutdown/hibernate (too disruptive to risk misfiring live).
- **Busy indicator added.** `ChatBubble.start_pending()`/`stop_pending()`
  show an animated dot sequence from the moment a turn starts until the
  first real token/output/generated content arrives — previously the
  bubble sat empty and static for however long classification took.
- **UI visual pass.** Bubbles now align to opposite sides (user right,
  assistant left) instead of both stretching full-width; stronger border
  contrast; added drop shadow; increased inter-bubble spacing. New
  "intent pill" (`CATEGORY → COMMAND`) shown above any resolved action
  turn, making the two-tier classification decision visible in the UI
  instead of only explained in prose.

Verified this session (no live Ollama/PowerShell available in the dev
sandbox): all new/changed Python files parse and the full project imports
cleanly; `MainWindow` constructs and renders correctly under Qt's offscreen
platform; chaining, file generation, and the CHAT-vs-action rendering paths
were each exercised end-to-end against stubbed `classify()` /
`stream_thinking()` / `generate_and_save()` calls; every new PowerShell
template was checked against `.format(**{})` to catch brace-escaping bugs
before they could show up live (caught and fixed one, in
`BATTERY_STATUS`). **Not yet tested against a real Windows machine, real
Ollama, or real PowerShell** — run through every new command and the
chaining flow live before presenting.
