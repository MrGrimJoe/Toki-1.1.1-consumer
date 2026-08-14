"""
app_control.py — APP_CONTROL category: launching applications and controlling
the mouse/keyboard against whatever window is currently focused.

Kept as its own module, separate from generator.py and the PowerShell-template
path, per plan: same overall strategy (model picks a closed-vocabulary action,
Python resolves the real target, Python executes it) but a genuinely different
execution mechanism (UI Automation instead of PowerShell), so it doesn't belong
mixed into executor.py or extractor.py.

── The actual safety property, same as everywhere else in TOKI ─────────
The model NEVER decides pixel coordinates and NEVER decides which widget is
correct. It only ever picks one of a few action verbs (CLICK, TYPE,
RIGHT_CLICK, DOUBLE_CLICK) from a closed enum -- same two-tier mechanism as
every other category. The *target description* ("the Save button", "the
search box") is pulled from the user's own text by plain regex, exactly like
every other slot in this app.

What's different here versus a PowerShell template: there's no fixed template
to fill, because "where is the Save button right now" can't be known ahead of
time -- it depends on whatever's on screen at the moment. So instead of a
string template, resolve_target() does the equivalent job at runtime:
  1. Get the currently focused top-level window (via UI Automation).
  2. Walk its descendant elements -- every button, field, menu item, checkbox,
     etc. -- and collect each one's visible name, control type, and bounding
     rectangle (its actual on-screen coordinates).
  3. Score each element's name against the target description using plain
     fuzzy string matching (difflib -- deterministic, no model call, fully
     auditable).
  4. If the best match clears a confidence threshold, return its center
     coordinates. If nothing clears the threshold, return None -- fail safe,
     no click happens, the user is told to be more specific.

This is "click through coordinates" done safely: coordinates are how the
click physically happens, but WHICH coordinates is decided by matching
against the real, current widget tree, never guessed blind and never chosen
by the model.

Backend choice: 'uia' (MS UI Automation), not 'win32'. win32 is faster but
only sees classic/legacy control types; uia sees a much broader range of
modern apps (browsers, UWP, Electron apps like VS Code/Discord, etc.), which
matters far more here than raw speed -- the model call already dominates
per-turn latency, so uia's slower tree walk is not the bottleneck.
"""

import difflib
import os
import re
import threading
import time
from typing import Dict, Any, Optional, List, Tuple

from target_memory import TargetMemory
import foreground_tracker

# pywinauto/comtypes are DELIBERATELY NOT imported at module level.
#
# ROOT CAUSE this avoids: pywinauto's own top-level __init__.py calls
# pythoncom.CoInitializeEx(COINIT_MULTITHREADED) at IMPORT TIME (confirmed
# by reading the actual installed package source -- it's not lazy, despite
# earlier assumptions in this project). This module used to be imported at
# the top of orchestrator.py, which app.py imports before constructing
# QApplication() -- so by the time QApplication() ran and Qt tried to claim
# the main thread as STA via its internal OleInitialize() call, the thread
# was already locked into MTA by pywinauto's import. That collision is
# exactly RPC_E_CHANGED_MODE / "0x80010106: Cannot change thread mode after
# it is set" -- and the DPI-awareness failure right after it in the same
# startup sequence is a downstream symptom of the same collision, not a
# second unrelated bug. This also very plausibly explains reported general
# sluggishness: a failed OLE/DPI negotiation at startup can cascade into
# degraded rendering paths for the rest of the process's life, not just a
# one-time warning.
#
# Fix: defer the import to the first time an APP_CONTROL action actually
# runs. That call always happens inside app.py's Worker, a QThread created
# fresh per user message -- which only ever starts well after
# QApplication() already exists and has already claimed the main thread's
# apartment mode. Importing pywinauto for the first time on that later,
# different thread can't retroactively break a decision the main thread
# already made.
_PYWINAUTO_AVAILABLE: Optional[bool] = None  # None = not yet probed
_Desktop = None
_comtypes = None


def _escape_ps_slot(value: str) -> str:
    """Doubles a single quote before it goes into a PowerShell single-quoted
    string literal ('...') -- the same fix orchestrator.py's own
    _escape_ps_slot() applies to every "powershell"-kind template, needed
    here SEPARATELY because launch_app() builds its own PowerShell command
    string directly, outside orchestrator.py's centralized _dispatch()
    escaping (which only runs for "powershell"-kind intents -- LAUNCH_APP
    is "app_control"-kind, so it never passed through that path).

    Confirmed directly: an app_name containing a single quote -- even an
    entirely ordinary one, e.g. "Assassin's Creed", not an attack payload
    -- produced an unbalanced, broken -Command string before this fix:
    Start-Process 'Assassin's Creed'. Same bug class BETA 0.2 fixed for
    template-based intents; it just wasn't covered on this second code
    path that builds its own command string independently.
    """
    return value.replace("'", "''")


def _load_pywinauto() -> bool:
    """
    Imports pywinauto + comtypes on first call, caches the result, and
    never re-imports afterward. Safe to call from any function in this
    module before touching Desktop/comtypes -- see the module-level
    comment above for why this must NOT happen at import time.
    """
    global _PYWINAUTO_AVAILABLE, _Desktop, _comtypes
    if _PYWINAUTO_AVAILABLE is not None:
        return _PYWINAUTO_AVAILABLE
    try:
        from pywinauto import Desktop as _D
        import comtypes as _c
        _Desktop = _D
        _comtypes = _c
        _PYWINAUTO_AVAILABLE = True
    except Exception:
        # Broad on purpose: pywinauto's submodules can fail at import time
        # for reasons other than ImportError depending on the platform/
        # display environment (e.g. mouse/keyboard submodules probing for a
        # display connection on import). Any such failure means cursor
        # control simply isn't available here -- fail safe and let every
        # function below report that clearly, rather than crashing the
        # whole app.
        _PYWINAUTO_AVAILABLE = False
    return _PYWINAUTO_AVAILABLE


def _ensure_com_initialized() -> None:
    """
    Every AppController entry point can run on a DIFFERENT OS thread each
    time -- app.py's Worker is a fresh QThread per user message, and COM
    apartment state is per-thread, not per-process. If this thread hasn't
    touched COM yet, initialize it ourselves as MTA (the mode pywinauto's
    uia backend expects) BEFORE pywinauto gets a chance to lazily
    initialize it some other way. This is a defensive measure against one
    real, reproducible source of apartment-mode conflicts -- not a proven
    fix for every possible COM issue.

    Safe to call every time: if this thread is already initialized (by us,
    by pywinauto, or by something else), CoInitializeEx raises a well-known
    "already initialized" error, which we swallow -- that's the expected,
    harmless case, not a real failure.

    Calls _load_pywinauto() first -- this is one of the few places in the
    module that runs on a fresh worker thread, so it's a safe point to
    trigger the lazy pywinauto/comtypes import if it hasn't happened yet.
    """
    if not _load_pywinauto():
        return
    try:
        _comtypes.CoInitializeEx(_comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass  # already initialized on this thread -- nothing to do


# Minimum fuzzy-match ratio (0-1) an element's name must clear against the
# target description before we're willing to click it. Deliberately
# conservative -- a missed click just means "try rephrasing", a wrong click
# could do real damage, so ties go to failing safe, not to guessing.
_MATCH_THRESHOLD = 0.55

# Same idea as _MATCH_THRESHOLD above, but for matching a user-typed app
# name against the real Get-StartApps list (see AppController._find_
# installed_app / _score_app_match below).
_APP_MATCH_THRESHOLD = 0.72


def _normalize_app_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _leading_initials_plus_trailing_words(query: str, name: str) -> bool:
    """True if `query` == (single-letter initials of the FIRST few words of
    `name`) + (the REMAINING words of `name`, spelled out in full and
    concatenated) -- e.g. "vscode" = "v"+"s" (from "Visual", "Studio") +
    "code" (from "Code"). Deliberately narrow: does NOT catch multi-letter
    prefix abbreviations like "ms" for "microsoft" (a different, harder
    pattern -- "MS Word" for "Microsoft Word" is a known separate gap, not
    handled here). See _score_app_match's docstring for why this exists
    and what it was checked against before being added."""
    name_words = re.findall(r"[a-z0-9]+", name.lower())
    if len(name_words) < 2:
        return False
    for split in range(1, len(name_words)):
        candidate = "".join(w[0] for w in name_words[:split]) + "".join(name_words[split:])
        if candidate == query:
            return True
    return False


def _score_app_match(query: str, name: str) -> float:
    """
    Deliberately NOT plain difflib.SequenceMatcher.ratio() -- tested that
    first and it's unsafe for this specific job. Short abbreviations
    against a real app list produce dangerous cross-matches: "vscode"
    scores HIGHER against "Discord" (0.615) than against the app it
    actually means, "Visual Studio Code" (0.5) -- same story for "code"
    and "word" both preferring "Discord" over their real targets, purely
    because raw character-sequence overlap doesn't track word/abbreviation
    structure at all. A wrong launch here is a real, visible mistake
    (launching Discord instead of VS Code), not a harmless retry -- so
    this needs to be precision-first, not recall-first.

    Scoring, in order of confidence:
      1. Exact match (normalized: lowercased, punctuation/spaces
         stripped) -- score 1.0.
      2. One normalized string fully CONTAINS the other, contiguously
         (e.g. "chrome" in "googlechrome", "calc" in "calculator", "code"
         as the tail of "visualstudiocode") -- score 0.75-1.0, scaled by
         how much of the longer string the match covers. This is the
         workhorse tier: it's what makes real abbreviations resolve
         correctly without also matching unrelated apps that just happen
         to share some scattered letters.
      3. Leading-initials-plus-trailing-words (e.g. "vscode" for "Visual
         Studio Code", "vs2022" for "Visual Studio 2022") -- found live:
         a real Get-StartApps entry for "Visual Studio Code" scored only
         0.38 against the literal query "VsCode", nowhere near threshold,
         because "vs" comes from two separate words' initials, not a
         contiguous run anywhere in "visualstudiocode". Scored at 0.85 --
         high-confidence but intentionally below a perfect substring
         match, since this pattern is a narrower, more specific structural
         match than tier 2's plain containment. Verified this does NOT
         also match "Discord" (checked directly before adding).
      4. Otherwise, a capped fuzzy-ratio fallback (ratio * 0.7, so it can
         never reach the substring tier's range on its own) -- exists only
         to catch near-exact typos of an otherwise-matching name; verified
         it cannot produce the vscode-style collisions above (max
         attainable score here is 0.7, below _APP_MATCH_THRESHOLD, unless
         the strings are close enough that the substring check would
         already have caught them).
    """
    q, n = _normalize_app_name(query), _normalize_app_name(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if q in n or n in q:
        shorter, longer = (q, n) if len(q) <= len(n) else (n, q)
        return 0.75 + 0.25 * (len(shorter) / len(longer))
    if _leading_initials_plus_trailing_words(q, name):
        return 0.85
    return difflib.SequenceMatcher(None, q, n).ratio() * 0.7

# Control types worth considering as click targets. Skips generic containers
# (Pane, Group, Window itself) that are almost never what a user means by
# "click the X" -- keeps the candidate set focused and the scoring cleaner.
_CLICKABLE_TYPES = {
    "Button", "CheckBox", "RadioButton", "MenuItem", "ListItem", "TabItem",
    "Hyperlink", "Edit", "ComboBox", "TreeItem", "Text",
}


def _normalize(s: str) -> str:
    return s.strip().lower().replace("&", "").replace("_", " ")


def _score(candidate_name: str, target_description: str) -> float:
    """Plain deterministic fuzzy match -- no model involvement whatsoever."""
    a, b = _normalize(candidate_name), _normalize(target_description)
    if not a or not b:
        return 0.0
    # Exact substring match (either direction) is a very strong signal --
    # e.g. target "save button" vs element name "Save" should score high
    # even though difflib's ratio alone would penalize the length mismatch.
    if a in b or b in a:
        return max(difflib.SequenceMatcher(None, a, b).ratio(), 0.75)
    return difflib.SequenceMatcher(None, a, b).ratio()


class TargetNotFound(Exception):
    """Raised internally, always caught -- never let this escape to the UI as a stack trace."""


def _identity_of(elem) -> Dict[str, str]:
    """The stable-ish subset of an element's properties used both by
    target_memory.py's click-to-teach store and macro_recorder.py's replay
    verification: name/control_type/class_name. Coordinates are NOT part of
    identity on purpose -- they're the one thing that's expected to change
    (window moved/resized), and identity is exactly the thing checked
    INSTEAD of trusting a stale coordinate blindly."""
    info = elem.element_info
    return {
        "name": getattr(info, "name", "") or "",
        "control_type": getattr(info, "control_type", "") or "",
        "class_name": getattr(info, "class_name", "") or "",
    }


def _get_focused_window_title() -> Optional[str]:
    """Best-effort window title for keying target_memory.py's learned
    mappings. Returns None on any failure -- the caller (AppController.teach)
    treats that as "can't learn this one right now", not an error."""
    try:
        win = _get_focused_window()
        return win.window_text() or None
    except Exception:
        return None


def capture_identity_at_point(x: int, y: int) -> Optional[Dict[str, str]]:
    """
    Resolves whatever UI element is currently at screen coordinates (x, y)
    into the same {name, control_type, class_name} identity shape
    resolve_target() already produces internally, via pywinauto's own
    from_point() (backed by UI Automation's ElementFromPoint under the
    'uia' backend) -- the same backend choice as everywhere else in this
    module, for the same reason (module docstring).

    NOT VERIFIED AGAINST REAL WINDOWS in this session -- Desktop.from_point
    is pywinauto's documented mechanism for exactly this ("what's under the
    cursor") use case, but this repo has no way to run real pywinauto
    against a real UI Automation tree, so treat this specific function as
    logically-reviewed, not live-tested, until confirmed on an actual
    Windows machine. Fails safe either way: any exception here just means
    "couldn't capture", never a wrong/fabricated identity.
    """
    if not _load_pywinauto():
        return None
    try:
        _ensure_com_initialized()
        elem_info = _Desktop(backend="uia").from_point(x, y)
        if elem_info is None:
            return None
        return {
            "name": getattr(elem_info, "name", "") or "",
            "control_type": getattr(elem_info, "control_type", "") or "",
            "class_name": getattr(elem_info, "class_name", "") or "",
        }
    except Exception:
        return None


# UIA control_type strings that read as an actual text-entry control --
# same vocabulary _CLICKABLE_TYPES already uses, narrowed down to the
# subset that's specifically "somewhere text can go", not just "clickable".
_TEXT_ENTRY_TYPES = {"Edit", "ComboBox"}


def _get_focused_text_element() -> Optional[Tuple[int, int, str]]:
    """
    Finds whichever descendant of the active window currently HAS
    keyboard focus (UIAWrapper.has_keyboard_focus() -- the real pywinauto/
    UIA property, walked over the same win.descendants() enumeration
    resolve_target() already uses) and returns (x, y, name) for it ONLY if
    its control_type reads as an actual text box (_TEXT_ENTRY_TYPES).

    This is the concrete version of the design discussion's "if you're on
    a screen with no other widgets, it's a text editor, don't ask" case:
    something IS already focused, and it's a place text can legitimately
    go, so AppController.start_dictation() can use it directly with no
    question asked. Returns None for every other case -- nothing focused,
    focus is on a button/list/anything that isn't itself a text box, or
    any error along the way -- and start_dictation() treats None as "ask
    the user" (via the same click-to-teach-style flow click() already
    uses on a miss), never as license to guess.

    NOT VERIFIED AGAINST REAL WINDOWS in this session -- same caveat as
    capture_identity_at_point()'s docstring above: has_keyboard_focus() is
    pywinauto's documented mechanism for this, logically reviewed against
    its real API, but this repo has no way to run it against a live UI
    Automation tree. Fails safe either way: any exception here is treated
    as "couldn't tell", never a false "yes, it's a text box".
    """
    if not _load_pywinauto():
        return None
    try:
        win = _get_focused_window()
        for elem in win.descendants():
            try:
                if not elem.has_keyboard_focus():
                    continue
            except Exception:
                continue
            # Found the focused element -- resolve it once, then stop
            # regardless of outcome. Only one element can have keyboard
            # focus at a time, so there's nothing left to check either way.
            try:
                ctrl_type = elem.element_info.control_type
                if ctrl_type not in _TEXT_ENTRY_TYPES:
                    return None
                rect = elem.rectangle()
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                name = elem.element_info.name or "the focused field"
                return (cx, cy, name)
            except Exception:
                return None
        return None
    except Exception:
        return None


# ── OCR fallback (BETA 0.3.44) ──────────────────────────────────────────────
#
# Design-doc request: "an OCR before asking the user ... a fast one and
# one that can be run on almost any machine". This exists specifically for
# resolve_target()'s clean-miss case: UI Automation found no accessible
# name close enough to target_description. That happens for real on
# exactly the surfaces the design doc calls out by name -- Chrome and
# Copilot -- where a lot of on-screen text is rendered into a <canvas> or
# a web component with no exposed UIA name at all, even though the text
# is plainly visible on screen. OCR reads what's actually painted,
# independent of whether the app bothered to expose an accessible name.
#
# Windows.Media.Ocr (via the winsdk package) rather than Tesseract/
# PaddleOCR/etc: it's a WinRT API already built into every Windows 10/11
# install, so "one that can run on almost any machine" is satisfied by
# construction -- zero extra system binary, zero model download, nothing
# to bundle. winsdk is added to requirements.txt as an optional,
# Windows-only, lazily-imported dependency -- same "missing it just means
# a clean degrade, not a crash" posture as pywinauto above: if it's not
# installed, _load_winsdk_ocr() returns None and resolve_target() falls
# straight through to its existing "ask the user" behavior, unchanged.
#
# Split into two functions specifically so the part that's actually
# testable (scoring OCR'd text against target_description, converting a
# matched line's bounding box into screen coordinates) is separate from
# the part that isn't (the real WinRT capture+recognize call) -- mirrors
# resolve_target()'s own tests, which mock _Desktop/win.descendants()
# rather than exercising real pywinauto.

def _load_winsdk_ocr():
    try:
        from winsdk.windows.media.ocr import OcrEngine
        from winsdk.windows.graphics.imaging import (
            SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode,
        )
        from winsdk.windows.storage.streams import DataWriter
        return OcrEngine, SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode, DataWriter
    except ImportError:
        return None


def _ocr_lines_from_bitmap(bbox: Tuple[int, int, int, int]) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    """
    The genuinely unverifiable half: grabs a screenshot of bbox
    (screen-absolute (left, top, right, bottom)) and runs Windows.Media.
    Ocr over it, returning [(text, (left, top, right, bottom)), ...] with
    each rect in BITMAP-LOCAL coordinates (i.e. relative to bbox's own
    top-left, NOT screen-absolute -- _ocr_find_text_on_screen() below adds
    bbox's offset back on for the one line it actually picks, so this
    layer doesn't need to know or care about the caller's coordinate
    space at all).

    NOT VERIFIED AGAINST REAL WINDOWS in this session -- same caveat as
    capture_identity_at_point()/_get_focused_text_element() above: this
    wraps winsdk's documented projection of Windows.Media.Ocr
    (OcrEngine.try_create_from_user_profile_languages() +
    recognize_async(SoftwareBitmap), with the bitmap itself built via
    SoftwareBitmap + a WinRT IBuffer from DataWriter.detach_buffer()) --
    logically reviewed against that documented API surface, but this repo
    has no Windows box to actually run it on. Returns [] on ANY failure
    at any point (missing package, no display, OCR engine unavailable,
    recognition error) -- resolve_target() treats an empty list exactly
    like "OCR didn't find anything", never as a crash or a wrong click.
    """
    winsdk_bits = _load_winsdk_ocr()
    if winsdk_bits is None:
        return []
    OcrEngine, SoftwareBitmap, BitmapPixelFormat, BitmapAlphaMode, DataWriter = winsdk_bits

    try:
        from PIL import ImageGrab
    except ImportError:
        return []

    try:
        img = ImageGrab.grab(bbox=bbox).convert("RGBA")
    except Exception:
        return []

    try:
        width, height = img.size
        writer = DataWriter()
        writer.write_bytes(list(img.tobytes()))
        buf = writer.detach_buffer()

        bitmap = SoftwareBitmap(BitmapPixelFormat.RGBA8, width, height, BitmapAlphaMode.STRAIGHT)
        bitmap.copy_from_buffer(buf)

        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            return []

        import asyncio
        result = asyncio.run(engine.recognize_async(bitmap))
    except Exception:
        return []

    lines: List[Tuple[str, Tuple[int, int, int, int]]] = []
    try:
        for line in result.lines:
            text = (line.text or "").strip()
            words = list(line.words)
            if not text or not words:
                continue
            lefts   = [w.bounding_rect.x for w in words]
            tops    = [w.bounding_rect.y for w in words]
            rights  = [w.bounding_rect.x + w.bounding_rect.width for w in words]
            bottoms = [w.bounding_rect.y + w.bounding_rect.height for w in words]
            lines.append((text, (min(lefts), min(tops), max(rights), max(bottoms))))
    except Exception:
        return []

    return lines


def _ocr_find_text_on_screen(target_description: str, window) -> Optional[Tuple[int, int, str]]:
    """
    The testable half: takes window (anything with a .rectangle(), same
    contract resolve_target() already relies on), grabs its bounding box,
    hands that to _ocr_lines_from_bitmap(), then scores every OCR'd line
    against target_description with the exact same _score()/
    _MATCH_THRESHOLD resolve_target() itself uses for UIA element names --
    same fuzzy-match vocabulary, same confidence bar, just a different
    source of candidate text. Returns None on no confident match, same
    fail-safe contract as resolve_target()'s own clean-miss path.
    """
    try:
        rect = window.rectangle()
        bbox = (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        return None

    lines = _ocr_lines_from_bitmap(bbox)
    if not lines:
        return None

    best_score = 0.0
    best_text = ""
    best_rect = None
    for text, line_rect in lines:
        score = _score(text, target_description)
        if score > best_score:
            best_score = score
            best_text = text
            best_rect = line_rect

    if best_rect is None or best_score < _MATCH_THRESHOLD:
        return None

    l, t, r, b = best_rect
    cx = bbox[0] + (l + r) // 2
    cy = bbox[1] + (t + b) // 2
    return (cx, cy, best_text)


def wait_for_single_click(timeout_seconds: float = 15.0) -> Optional[Tuple[int, int]]:
    """
    Blocks the CALLING thread (never the Qt main thread -- see below) until
    the user's next physical left-button-down click, or timeout_seconds
    elapses, whichever comes first. Returns (x, y) or None on timeout.

    Uses pynput.mouse.Listener, the same hard dependency already used for
    the Ctrl+K global hotkey in toki_desktop_mark.py -- no new dependency
    introduced by this feature.

    SAFE TO BLOCK HERE, SPECIFICALLY: both call sites that reach this
    (AppController.teach_from_next_click() via orchestrator.py's app_control
    dispatch, and MacroRecorder's own use elsewhere) are already invoked
    from process_request(), which app.py's Worker and main_widget's
    threading.Thread both already run OFF the Qt main thread -- this
    doesn't introduce a new threading concern, it relies on one that
    already exists everywhere else PowerShell/UI-Automation dispatch runs.
    Do NOT call this directly from a Qt slot/signal handler on the main
    thread; it would freeze the UI for up to timeout_seconds.
    """
    try:
        from pynput import mouse
    except ImportError:
        return None

    result: List[Optional[Tuple[int, int]]] = [None]
    got_click = threading.Event()

    def on_click(x, y, button, pressed):
        if pressed and button == mouse.Button.left:
            result[0] = (x, y)
            got_click.set()
            return False  # stop the listener

    listener = mouse.Listener(on_click=on_click)
    listener.start()
    got_click.wait(timeout=timeout_seconds)
    listener.stop()
    return result[0]


def escape_for_send_keys(text: str) -> str:
    """
    Shared by type_text() below and macro_recorder.py's replay: send_keys
    treats {}/+/^/%/~/() as special -- escape them so typed text is always
    treated as literal characters, never keyboard shortcuts or modifier
    combos the model's (or a replayed macro's) output shouldn't be able to
    trigger. Pulled out of type_text() into its own function so
    macro_recorder.py can reuse the exact same escaping instead of a second,
    possibly-drifting copy of the same table.

    Single pass over the ORIGINAL characters -- see the BUG FIX note this
    replaces in type_text()'s prior inline version for why a chain of
    sequential .replace() calls is broken (escaping "{" first inserts a NEW
    "}" character that a later .replace("}", ...) in the same chain then
    re-matches and mangles).
    """
    _ESCAPE = {
        "{": "{{}", "}": "{}}", "+": "{+}", "^": "{^}",
        "%": "{%}", "~": "{~}", "(": "{(}", ")": "{)}",
    }
    return "".join(_ESCAPE.get(ch, ch) for ch in text)


def _get_focused_window():
    """
    Returns the pywinauto window object for whatever should be treated as
    "the window the user means" right now.

    ── foreground_tracker fallback (fixes the video-download / app-control
    focus bug) ──
    The live active window (desktop.window(active_only=True), a thin
    wrapper over GetForegroundWindow()) is correct in general, but by the
    time ANY app_control.py/now_playing.py code actually runs, OS focus
    has almost always already moved to TOKI's own window -- the user just
    typed/spoke a command into it. Naively trusting the live active
    window here meant every video-download or click/type call resolved
    against TOKI itself, never the browser or app the user actually
    meant, regardless of what was genuinely focused a second earlier when
    the command was issued.

    Fix: if the live active window belongs to THIS process (own PID --
    see foreground_tracker.py's docstring for why PID, not title/class),
    fall back to foreground_tracker's remembered last non-TOKI window
    instead of returning TOKI's own window as if it were the real
    target. Only falls back on that specific "it's us" case -- if the
    live active window genuinely is some other app, it's used directly,
    unchanged from before this fix.
    """
    if not _load_pywinauto():
        raise TargetNotFound("pywinauto isn't available on this system.")
    _ensure_com_initialized()
    desktop = _Desktop(backend="uia")
    try:
        win = desktop.window(active_only=True)
        win.wait("exists", timeout=3)
    except Exception as e:
        # Keep the real exception type + message, not just str(e) -- e.g.
        # "OSError: [WinError -2147417850] ..." tells you far more than a
        # bare message would once this surfaces in the UI.
        raise TargetNotFound(f"Couldn't find a focused window: {type(e).__name__}: {e}")

    try:
        owner_pid = win.process_id()
    except Exception:
        # Can't tell whose window it is -- fail open to the live active
        # window exactly like before this fix, rather than guessing.
        return win

    if owner_pid != os.getpid():
        # Genuinely a different app -- this is the normal, common case
        # and nothing about this fix changes it.
        return win

    # The live active window is TOKI's own -- fall back to the last real
    # foreground window foreground_tracker observed.
    fallback_hwnd = foreground_tracker.get_last_foreground_window()
    if fallback_hwnd is None:
        # No usable fallback recorded (tracker never started, nothing
        # non-TOKI has had focus yet this session, or the remembered
        # window has since closed) -- fail open to TOKI's own window
        # rather than raising, same as this function's behavior before
        # foreground_tracker existed. Callers already treat "resolved to
        # TOKI's own window" as a clean miss (resolve_target() finds no
        # matching element and reports the existing generic message),
        # not a crash.
        return win

    try:
        fallback_win = desktop.window(handle=fallback_hwnd)
        fallback_win.wait("exists", timeout=3)
        return fallback_win
    except Exception:
        # Recorded handle turned out to be stale after all (closed in the
        # gap between foreground_tracker's IsWindow() check and this
        # wait() call) -- fail open to TOKI's own window rather than
        # raising, same reasoning as the "no fallback recorded" case above.
        return win


# Module-level singleton, same "cheap object, shared instance" pattern as
# AppController's own class-level caches -- see target_memory.py's own
# docstring for why one instance per process is enough.
_target_memory = TargetMemory()


def resolve_target(target_description: str) -> Tuple[Optional[Tuple[int, int, str]], Optional[str]]:

    """
    Walks the focused window's element tree, scores every clickable-typed
    descendant against target_description, and returns
    ((x, y, matched_name), None) for the best match.

    On failure returns (None, reason) where `reason` distinguishes two very
    different situations that used to be collapsed into the same silent
    None:
      - a real error (COM/UI Automation blew up, window walk failed, etc.)
        -> reason is the actual exception text, so it reaches the user
           instead of being swallowed.
      - a clean walk that just found no good match -> reason is None, and
        the caller shows the existing generic "couldn't confidently find"
        message. This is still the fail-safe path: no click happens either
        way.
    """
    try:
        win = _get_focused_window()
        candidates = win.descendants()
    except TargetNotFound as e:
        return None, str(e)
    except Exception as e:
        # Previously: bare `return None`, indistinguishable from "no match
        # found". Now the real error is preserved and handed back up.
        return None, f"{type(e).__name__}: {e}"

    best_score = 0.0
    best_elem = None
    best_name = ""

    for elem in candidates:
        try:
            ctrl_type = elem.element_info.control_type
            if ctrl_type not in _CLICKABLE_TYPES:
                continue
            name = elem.element_info.name or ""
            if not name:
                continue
            if not elem.is_visible() or not elem.is_enabled():
                continue
        except Exception:
            # Some elements throw on property access (stale refs, access
            # denied, etc.) -- skip them rather than let one bad element
            # crash the whole resolution pass.
            continue

        score = _score(name, target_description)
        if score > best_score:
            best_score = score
            best_elem = elem
            best_name = name

    if best_elem is None or best_score < _MATCH_THRESHOLD:
        # No confident fuzzy match -- before giving up entirely, check
        # whether this exact (window, description) pair was taught before
        # (see target_memory.py / AppController.teach_from_next_click()).
        # This is checked AFTER the fuzzy pass, never before, so a normal
        # working fuzzy match is always used first and this can't change
        # behavior for anything that already worked -- it only ever
        # recovers a case that would otherwise have been a clean miss.
        learned = _try_resolve_from_memory(target_description, candidates)
        if learned is not None:
            return learned, None
        # OCR pre-check, before giving up entirely (see _ocr_find_text_on_
        # screen()'s docstring above): catches exactly the Chrome/Copilot
        # case the design discussion called out -- on-screen text with no
        # accessible UIA name at all. Checked AFTER both the fuzzy pass
        # and memory recall, same reasoning as memory's own placement:
        # never changes behavior for anything that already worked via UIA,
        # only recovers cases that would otherwise be a clean miss.
        ocr_match = _ocr_find_text_on_screen(target_description, win)
        if ocr_match is not None:
            return ocr_match, None
        return None, None  # clean walk, genuinely no good match -- not an error

    try:
        rect = best_elem.rectangle()
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        return (cx, cy, best_name), None
    except Exception as e:
        return None, f"Found \"{best_name}\" but couldn't read its position: {type(e).__name__}: {e}"


def _try_resolve_from_memory(target_description: str, candidates) -> Optional[Tuple[int, int, str]]:
    """
    Second-tier lookup for resolve_target()'s clean-miss path: checks
    target_memory.py for a previously-taught identity for this exact
    (window title, target_description) pair, then searches the ALREADY-
    FETCHED candidates (the descendants list resolve_target() just walked
    for the fuzzy pass -- no second tree walk) for an element whose
    identity matches exactly.

    Deliberately exact identity match (name + control_type), not fuzzy --
    the whole point of teaching is a precise, deterministic recall, not
    another round of guessing. If the app's UI changed enough that the
    taught identity no longer appears, this returns None and the caller's
    existing fail-safe "couldn't confidently find" path applies unchanged
    -- it does NOT fall back to fuzzy-matching the stale identity's name,
    which would just reintroduce the same guessing this is meant to avoid.
    """
    window_title = _get_focused_window_title()
    if not window_title:
        return None
    learned = _target_memory.get(window_title, target_description)
    if not learned:
        return None

    for elem in candidates:
        try:
            identity = _identity_of(elem)
            if identity["name"] == learned.get("name") and identity["control_type"] == learned.get("control_type"):
                if not elem.is_visible() or not elem.is_enabled():
                    continue
                rect = elem.rectangle()
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                return (cx, cy, identity["name"])
        except Exception:
            continue
    return None


class AppController:
    """
    Executes APP_CONTROL actions. Same contract as executor.py's
    RunningCommand: the caller gets a plain result string/description back,
    never a raw exception.
    """

    # Cached across calls in the same process -- Get-StartApps enumerates
    # the whole Start Menu app list, which doesn't change mid-session under
    # normal use, and it's a genuinely slow-ish call (a few hundred ms).
    # Same "fetch once, reuse" pattern as apis.py's LocationCache. Call
    # invalidate_app_cache() if you ever need to force a re-scan (e.g. an
    # app was just installed in the same session -- not currently wired to
    # anything, but here so it's not a dead end if that's ever needed).
    #
    # BETA 0.3.28 fix: _app_list_cache used to be set to [] on ANY failure
    # (subprocess error, malformed JSON, etc.), and `if _app_list_cache is
    # not None: return` treated that [] identically to a genuine (rare,
    # basically never happens on real Windows) empty success -- so a
    # single Get-StartApps hiccup (common right after boot, or if
    # PowerShell is momentarily locked) got cached as "no apps" FOREVER,
    # silently degrading every app-launch/app-control action for the rest
    # of the session, with no TTL and no retry. Fixed by only ever writing
    # _app_list_cache on a REAL success; a failure is never cached and
    # instead just returns [] for THIS call, recording when it happened so
    # a burst of calls in the same failure window don't all pay the
    # subprocess cost again -- the next call after _FAILURE_RETRY_SECONDS
    # retries for real.
    _app_list_cache: Optional[List[Dict[str, str]]] = None
    _last_fetch_failure_time: Optional[float] = None
    _FAILURE_RETRY_SECONDS = 10.0  # local subprocess call -- cheap enough
                                    # to retry fairly often, but not on
                                    # literally every call in a tight loop

    def _get_installed_apps(self) -> List[Dict[str, str]]:
        """
        Real, verifiable list of what's actually launchable from the Start
        Menu -- Get-StartApps is a genuine Windows cmdlet (StartLayout
        module, built in since Windows 10), not a guess or a scrape. Each
        entry has a display Name and an AppID that shell:AppsFolder can
        launch directly, which works uniformly for both traditional .exe
        apps AND UWP/Store apps (Start-Process 'notepad' works for
        traditional exes on PATH, but NOT for UWP apps like the Store
        Calculator, which have no PATH-resolvable executable at all --
        AppID + shell:AppsFolder is the one launch mechanism that covers
        both).

        Returns [] (not None) on any failure (PowerShell unreachable,
        malformed JSON, etc.) -- callers already treat "no apps found" and
        "couldn't check" identically (both mean "don't trust an app match
        here"), so collapsing them into one return type keeps every call
        site simpler. A SUCCESSFUL result is cached indefinitely for the
        rest of the process (see _app_list_cache's comment above); a
        FAILURE is never cached -- see _FAILURE_RETRY_SECONDS above.
        """
        if AppController._app_list_cache is not None:
            return AppController._app_list_cache

        now = time.time()
        last_failure = AppController._last_fetch_failure_time
        if last_failure is not None and (now - last_failure) < AppController._FAILURE_RETRY_SECONDS:
            return []

        import subprocess
        import json
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-StartApps | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=10,
            )
            raw = result.stdout.strip()
            apps = json.loads(raw) if raw else []
            # ConvertTo-Json collapses a single-element result down to a
            # bare object instead of a 1-item array -- normalize that here
            # so every caller can just assume a list.
            if isinstance(apps, dict):
                apps = [apps]
            AppController._app_list_cache = [
                a for a in apps if isinstance(a, dict) and a.get("Name")
            ]
            AppController._last_fetch_failure_time = None
            return AppController._app_list_cache
        except Exception:
            AppController._last_fetch_failure_time = now
            return []

    def invalidate_app_cache(self):
        AppController._app_list_cache = None
        AppController._last_fetch_failure_time = None

    def prime_app_cache(self) -> None:
        """Populate _app_list_cache now instead of waiting for the first
        real app-control call. Safe to call from a background thread at
        startup (see orchestrator.py's WindowsAIAssistant.__init__) --
        _get_installed_apps() already fails soft and never raises, so
        there's nothing for a caller here to catch. A cache hit later
        just returns instantly instead of paying the Get-StartApps
        subprocess cost on the user's first LAUNCH_APP/KILL_PROCESS/etc.
        turn."""
        self._get_installed_apps()

    def _find_installed_app(self, name: str) -> Optional[Dict[str, str]]:
        """
        Scores `name` against every real installed app (see
        _get_installed_apps()) using _score_app_match() -- see that
        function's docstring for why this is deliberately NOT
        difflib.get_close_matches() -- and returns the best match's
        {"Name": ..., "AppID": ...} dict if it clears
        _APP_MATCH_THRESHOLD, else None. None is the "no confident app
        match" signal the orchestrator's open-target cascade uses to fall
        through to trying a file/folder instead.
        """
        apps = self._get_installed_apps()
        if not apps:
            return None
        best_app, best_score = None, 0.0
        for a in apps:
            s = _score_app_match(name, a["Name"])
            if s > best_score:
                best_app, best_score = a, s
        if best_app is not None and best_score >= _APP_MATCH_THRESHOLD:
            return best_app
        return None

    def app_exists(self, app_name: str) -> bool:
        """Public, side-effect-free existence check -- lets a caller (the
        orchestrator's open-target cascade) verify an app is real BEFORE
        deciding to launch it, without duplicating the matching logic."""
        return self._find_installed_app(app_name) is not None

    def list_installed_apps(self, max_shown: int = 40) -> str:
        """
        Bug this fixes: "what are all the apps on my computer" had no
        intent to land on at all (LIST_INSTALLED_APPS didn't exist), so it
        fell through to CHAT -- which, with no real action behind it,
        narrated a fabricated "I'm checking which applications you've
        installed" with no actual check and no result to follow it,
        confirmed live. The underlying capability already existed
        (_get_installed_apps(), the same Get-StartApps data app_exists()/
        launch_app() use) -- it just wasn't exposed as something a user
        could directly ask for. This is that: a plain formatter over the
        same real data.

        Deduplicated and alphabetized; capped at max_shown with an honest
        count of how many more there are, since a real Start Menu easily
        has 100+ entries and dumping all of them isn't readable or
        narratable in one sentence.
        """
        apps = self._get_installed_apps()
        if not apps:
            return "Couldn't get the list of installed apps."
        names = sorted({a["Name"] for a in apps if a.get("Name")}, key=str.casefold)
        shown = names[:max_shown]
        text = f"{len(names)} apps found: " + ", ".join(shown)
        text += f", and {len(names) - max_shown} more." if len(names) > max_shown else "."
        return text

    def launch_app(self, app_name: str) -> str:
        """
        Tries a real, verified Start Menu match first (via
        _find_installed_app -- covers both traditional and UWP/Store apps,
        see its docstring), launched by AppID through shell:AppsFolder,
        which is the one mechanism that works for both app kinds
        uniformly. Falls back to the old bare Start-Process behavior only
        if no confident match was found -- unchanged for anyone whose app
        genuinely isn't in the Start Menu list (a raw .exe not registered
        there, for instance), same fail-open posture as everywhere else in
        this app.
        """
        import subprocess
        match = self._find_installed_app(app_name)
        if match and match.get("AppID"):
            target = f"shell:AppsFolder\\{match['AppID']}"
            display = match["Name"]
        else:
            target = app_name
            display = app_name
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", f"Start-Process '{_escape_ps_slot(target)}'"],
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            return f"Launching {display}."
        except Exception as e:
            return f"Couldn't launch {display}: {e}"

    def click(self, target_description: str, double: bool = False, right: bool = False) -> str:
        if not _load_pywinauto():
            return "Cursor control isn't available on this system (pywinauto/UI Automation required)."

        match, reason = resolve_target(target_description)
        if match is None:
            if reason:
                # A real error happened during resolution -- surface it
                # instead of collapsing it into the generic "not found"
                # message, which used to hide things like COM/UI Automation
                # failures entirely.
                return f"Couldn't look for \"{target_description}\": {reason}"
            return f"Couldn't confidently find \"{target_description}\" on screen -- not clicking anything."

        x, y, matched_name = match
        try:
            from pywinauto.mouse import click as mouse_click
            button = "right" if right else "left"
            if double:
                mouse_click(button=button, coords=(x, y))
                time.sleep(0.05)
                mouse_click(button=button, coords=(x, y))
            else:
                mouse_click(button=button, coords=(x, y))
            return f"Clicked \"{matched_name}\" at ({x}, {y})."
        except Exception as e:
            return f"Found \"{matched_name}\" but the click failed: {e}"

    def type_text(self, target_description: str, text: str) -> str:
        if not _load_pywinauto():
            return "Cursor control isn't available on this system (pywinauto/UI Automation required)."

        match, reason = resolve_target(target_description)
        if match is None:
            if reason:
                return f"Couldn't look for \"{target_description}\": {reason}"
            return f"Couldn't confidently find \"{target_description}\" on screen -- not typing anything."

        x, y, matched_name = match
        try:
            from pywinauto.mouse import click as mouse_click
            from pywinauto.keyboard import send_keys
            mouse_click(button="left", coords=(x, y))  # focus the field first
            time.sleep(0.05)
            # send_keys treats {}/+/^/% as special -- escape them so typed
            # text is always treated as literal characters, never keyboard
            # shortcuts or modifier combos the model's output shouldn't be
            # able to trigger. See escape_for_send_keys()'s own docstring
            # for the single-pass-over-original-characters bug fix this
            # already includes.
            send_keys(escape_for_send_keys(text))
            return f"Typed into \"{matched_name}\"."
        except Exception as e:
            return f"Found \"{matched_name}\" but typing failed: {e}"

    def teach(self, target_description: str, x: int, y: int) -> str:
        """
        Captures whatever element is at (x, y) right now and remembers it
        against (current window title, target_description) in
        target_memory.py, so the NEXT time resolve_target() cleanly misses
        on the same description in the same window, it finds this instead
        of asking again. See target_memory.py's own docstring for the
        window-title-keying tradeoff and its one known limitation
        (per-tab browser titles).
        """
        if not _load_pywinauto():
            return "Cursor control isn't available on this system (pywinauto/UI Automation required)."
        identity = capture_identity_at_point(x, y)
        if identity is None or not identity.get("name"):
            return "Couldn't identify what's at that position -- not teaching anything."
        window_title = _get_focused_window_title()
        if not window_title:
            return "Couldn't tell which window this is for -- not teaching anything."
        _target_memory.remember(window_title, target_description, identity)
        return f"Got it -- I'll remember \"{identity['name']}\" as \"{target_description}\" in {window_title}."

    def teach_from_next_click(self, target_description: str, timeout_seconds: float = 15.0) -> str:
        """
        The actual "click it for me once" flow: blocks (see
        wait_for_single_click()'s own docstring for why that's safe to do
        here) waiting for the user's next physical left click, then calls
        teach() at that point. Only ever called from orchestrator.py after
        a real resolve_target() miss on a click/double-click/right-click
        action -- see orchestrator.py's dispatch for exactly where.
        """
        if not _load_pywinauto():
            return "Cursor control isn't available on this system (pywinauto/UI Automation required)."
        point = wait_for_single_click(timeout_seconds)
        if point is None:
            return f"Didn't see a click within {int(timeout_seconds)}s -- never mind, nothing was taught."
        x, y = point
        return self.teach(target_description, x, y)

    # ── "start seeing" / macro recording ────────────────────────────────
    # See macro_recorder.py's module docstring for the full design/safety
    # rationale. State (the in-progress recorder) lives on the instance,
    # same "persists across turns via the orchestrator's own
    # self.app_controller singleton" pattern as _app_list_cache above --
    # start_seeing() runs on one turn, stop_seeing_and_save() on a much
    # later one, and both need to see the SAME recorder in between.
    _active_recorder = None

    def start_seeing(self) -> str:
        from macro_recorder import MacroRecorder
        if self._active_recorder is not None:
            return "Already recording -- say \"stop seeing\" first if you want to save what's been captured so far."
        recorder = MacroRecorder()
        try:
            recorder.start_recording()
        except RuntimeError as e:
            return str(e)
        self._active_recorder = recorder
        return "Watching. Do whatever you want me to repeat later, then say \"stop seeing\" when you're done."

    def stop_seeing_and_save(self, macro_name: str) -> str:
        if self._active_recorder is None:
            return "Nothing's being recorded right now -- say \"start seeing\" first."
        name = macro_name.strip()
        if not name:
            return "I need a name to save this under -- say \"stop seeing\" again and give it one word."
        if " " in name:
            # Enforces the actual product decision behind this feature's
            # trigger-safety design (see macro_recorder.py's module
            # docstring, safety property 2): the trigger check in
            # orchestrator.py only fires on a SINGLE bare word with no
            # space, specifically so it can never be confused with a
            # normal sentence. A multi-word name would save successfully
            # here but could then never actually be triggered by anything
            # -- confirmed live this would otherwise be a silent dead end.
            # Deliberately does NOT stop the recording (still going in the
            # background) so retrying with a proper name doesn't lose
            # what's already been captured -- just say "stop seeing"
            # again once you've got a one-word name in mind.
            return (
                f"\"{name}\" is more than one word -- it needs to be one word so it can "
                f"reliably trigger later (e.g. \"zeta\" or \"flowmode\"). Still recording -- "
                f"say \"stop seeing\" again with a one-word name when you're ready."
            )
        events = self._active_recorder.stop_recording()
        recorder = self._active_recorder
        self._active_recorder = None
        if not events:
            return "Didn't see any clicks or key presses -- nothing saved."
        recorder.save(name)
        return f"Saved \"{name}\" ({len(events)} step(s)). Just say \"{name.lower()}\" any time to run it."

    def run_macro(self, macro_name: str) -> str:
        from macro_recorder import MacroPlayer
        return MacroPlayer().play(macro_name)

    # ── "start listening" continuous dictation ─────────────────────────────
    # See voice_pipeline.py's DictationPipeline docstring for the capture
    # side. This half owns target resolution + the actual typing, same
    # "state lives on the instance, persists turn-to-turn" pattern as
    # _active_recorder above -- start_dictation() runs on the turn that
    # says "start listening", stop_dictation() runs later from the widget's
    # stop button (main_widget.py), and both need to see the same session.
    _active_dictation = None  # DictationPipeline instance, or None

    def start_dictation(self, target_description: str = "") -> str:
        """
        Target resolution, per the design discussion (see STATUS.md for
        the full reasoning): a given target_description resolves exactly
        like click()/type_text() do. With nothing given, checks whatever
        currently has keyboard focus -- if it's already a real text-entry
        control (see _get_focused_text_element()'s docstring for exactly
        what that means), that's the target and nothing is asked; if focus
        is on something else entirely (or nothing identifiable), this
        falls back to the SAME "click it for me" flow click() already
        offers on a resolve_target() miss (see teach_from_next_click()) --
        asking a fixed question by blocking for one click, never guessing
        which field was meant.
        """
        if not _load_pywinauto():
            return "Cursor control isn't available on this system (pywinauto/UI Automation required)."
        if self._active_dictation is not None:
            return "Already listening -- tap the stop button on the widget first."

        matched_name = "the focused field"
        if target_description:
            match, reason = resolve_target(target_description)
            if match is None:
                if reason:
                    return f"Couldn't look for \"{target_description}\": {reason}"
                return f"Couldn't confidently find \"{target_description}\" to type into -- not starting."
            x, y, matched_name = match
        else:
            focused = _get_focused_text_element()
            if focused is not None:
                x, y, matched_name = focused
            else:
                point = wait_for_single_click(15.0)
                if point is None:
                    return "Didn't see a click within 15s -- never mind, dictation wasn't started."
                x, y = point
                identity = capture_identity_at_point(x, y)
                if identity and identity.get("name"):
                    matched_name = identity["name"]

        try:
            from pywinauto.mouse import click as mouse_click
            mouse_click(button="left", coords=(x, y))  # focus it once, up front
        except Exception as e:
            return f"Found \"{matched_name}\" but couldn't focus it: {e}"

        from voice_pipeline import DictationPipeline
        pipeline = DictationPipeline()

        def _type_utterance(text: str) -> None:
            # No re-click here on purpose -- see start_dictation()'s own
            # docstring / STATUS.md: clicking again before every single
            # utterance risks moving the caret if the user (or the app
            # itself) scrolled/selected something in between, which a
            # one-time focus click at session start does not risk.
            try:
                from pywinauto.keyboard import send_keys
                send_keys(escape_for_send_keys(text) + " ")
            except Exception as e:
                print(f"[TOKI-Dictation] Typing failed: {e}")

        pipeline.utterance_transcribed.connect(_type_utterance)
        pipeline.unavailable.connect(lambda reason: print(f"[TOKI-Dictation] {reason}"))
        self._active_dictation = pipeline
        pipeline.start()
        return f"Listening -- say something and I'll type it into \"{matched_name}\". Tap the stop button when you're done."

    def stop_dictation(self) -> str:
        if self._active_dictation is None:
            return "Nothing's being dictated right now."
        # Cleared HERE, synchronously, rather than via the pipeline's own
        # dictation_stopped signal -- that signal only fires once the
        # background QThread actually finishes closing its audio stream
        # (up to ~0.3s of polling latency later), but "the user asked to
        # stop" is true the moment this method is called, from EITHER
        # caller (the widget's stop button, or a plain "stop listening"
        # voice/typed command routed through the same orchestrator
        # dispatch as everything else). main_widget.py checks
        # _active_dictation right after every turn to show/hide the
        # dictation panel -- if this waited for the async signal instead,
        # a "stop listening" voice command would return its own success
        # message while _active_dictation was still momentarily non-None,
        # and the panel would wrongly stay visible for that same window.
        self._active_dictation.stop()
        self._active_dictation = None
        return "Stopped listening."
