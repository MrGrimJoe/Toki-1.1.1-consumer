"""
now_playing.py -- best-effort "what URL is the user currently watching"
reader, for the DOWNLOAD_PLAYING_VIDEO intent.

TWO STRATEGIES, TRIED IN ORDER (see get_now_playing_url() at the bottom)
----------------------------------------------------------------------------
1. cdp_now_playing.get_now_playing_url_via_cdp() -- if the browser was
   launched with remote debugging on, ask each open tab directly (via
   the Chrome DevTools Protocol) whether it actually has a <video>
   element playing right now, regardless of which window has OS focus.
   See that module's docstring for the full rationale; it's tried first
   because it answers the real question ("what's playing") rather than
   this module's narrower one ("what's focused").
2. This module's own address-bar read (below) -- the fallback, and the
   only strategy available when CDP isn't reachable (the common case,
   since most people don't run their browser with a debug port open).

HOW THE ADDRESS-BAR FALLBACK WORKS
----------------------------------------------------------------------------
Reuses app_control.py's own focused-window lookup (_get_focused_window(),
already lazy-loading pywinauto and already handling the COM-apartment-mode
ordering this whole app is built around -- see app_control.py's module
docstring for why that import can't happen at module load time) rather
than duplicating that logic here.

Once the focused window is found, this walks its descendants for an
Edit/ComboBox-typed control whose accessible NAME matches a known
address-bar label across the major Chromium/Firefox-family browsers, and
reads its VALUE. This is the same technique real "grab the current tab's
URL" utilities have used for years -- UI Automation exposes the address
bar as a genuine accessible control, independent of any browser
extension/API. It never touches page content, only the browser's own
chrome/toolbar, and it can't act on anything: it only reads a string.

WHY THIS IS DELIBERATELY "BEST EFFORT", NEVER A GUESS
----------------------------------------------------------------------------
- Only works if a browser window is actually focused when the command
  runs -- same "ask, don't guess" posture as everything else in this
  project. A miss returns None; the caller (apis.py's VideoDownloadAPI)
  reports that plainly rather than trying a stale/wrong URL.
- KNOWN_ADDRESS_BAR_NAMES/KNOWN_BROWSER_WINDOW_CLASSES are curated, not
  exhaustive -- a mismatch here should always mean "found nothing"
  (safe), never "found the wrong element".
- Reads whatever's actually in the address bar (the tab that's really
  open), not "whatever video the model thinks is playing" -- there's no
  reliable, extension-free way to ask a browser which embedded player is
  actively playing without CDP (strategy 1 above), so absent that, this
  deliberately answers the narrower, honest question: "what page is
  focused right now".
"""

from __future__ import annotations

import re
from typing import Optional

# Imported at module level (not lazily inside get_now_playing_url()) so
# it's a real name in THIS module's namespace -- lets callers/tests
# monkeypatch `now_playing.get_now_playing_url_via_cdp` directly, and
# keeps this file the single place that decides strategy order. Safe to
# import eagerly: cdp_now_playing itself only lazy-imports the optional
# `websocket` package inside its own functions, never at import time.
from video_downloader.cdp_now_playing import get_now_playing_url_via_cdp

KNOWN_ADDRESS_BAR_NAMES = (
    "address and search bar",   # Chrome, Edge, Brave, Opera
    "search or enter address",  # Firefox
    "address bar",               # generic/older builds
)

# Chromium/Firefox top-level window classes -- a lightweight sanity check
# that the focused window is actually a browser before walking its whole
# element tree. Not a security boundary, just noise reduction.
KNOWN_BROWSER_WINDOW_CLASSES = (
    "chrome_widgetwin",     # Chrome, Edge, Brave, Opera all share this prefix
    "mozillawindowclass",   # Firefox
)

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _looks_like_browser(win) -> bool:
    try:
        class_name = (win.element_info.class_name or "").lower()
    except Exception:
        return False
    return any(class_name.startswith(prefix) for prefix in KNOWN_BROWSER_WINDOW_CLASSES)


def _read_address_bar_value(win) -> Optional[str]:
    try:
        candidates = win.descendants(control_type="Edit") + win.descendants(control_type="ComboBox")
    except Exception:
        return None

    for elem in candidates:
        try:
            name = (elem.element_info.name or "").lower()
        except Exception:
            continue
        if not any(known in name for known in KNOWN_ADDRESS_BAR_NAMES):
            continue

        try:
            if hasattr(elem, "get_value"):
                value = elem.get_value()
            else:
                value = elem.window_text()
        except Exception:
            continue

        value = (value or "").strip()
        if not value:
            continue
        if not (_URL_RE.match(value) or "." in value):
            continue
        if not _URL_RE.match(value):
            # Chromium/Firefox both often show a bare "youtube.com/..."
            # once the bar isn't actively being edited -- add the scheme
            # back rather than reporting a non-fetchable value.
            value = "https://" + value
        return value

    return None


def _get_now_playing_url_from_focused_browser() -> Optional[str]:
    """The address-bar fallback strategy on its own -- kept as its own
    function (rather than folded into get_now_playing_url()) so the
    existing UI-Automation-focused tests can keep exercising exactly
    this behavior in isolation, unaffected by the CDP strategy tried
    ahead of it."""
    try:
        from app_control import _get_focused_window
    except Exception:
        return None

    try:
        win = _get_focused_window()
    except Exception:
        return None

    if not _looks_like_browser(win):
        return None

    return _read_address_bar_value(win)


def get_now_playing_url() -> Optional[str]:
    """Returns the best-effort URL of what the user is currently
    watching, or None if nothing could be determined confidently.
    Never guesses.

    Tries, in order:
      1. CDP (video_downloader.cdp_now_playing) -- which open tab
         actually has a video playing right now, independent of window
         focus. Silently skipped if CDP isn't reachable (the common
         case) or the optional websocket-client package isn't
         installed.
      2. This module's own address-bar read -- the focused browser
         window's current URL, regardless of whether anything in it is
         actually playing.
    """
    try:
        url = get_now_playing_url_via_cdp()
        if url:
            return url
    except Exception:
        # CDP probing is a pure bonus path -- any failure here (missing
        # optional dependency, network hiccup, malformed CDP response)
        # should never block the address-bar fallback below.
        pass

    return _get_now_playing_url_from_focused_browser()


__all__ = ["get_now_playing_url"]
