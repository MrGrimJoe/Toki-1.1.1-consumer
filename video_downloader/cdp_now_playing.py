"""
cdp_now_playing.py -- Chrome DevTools Protocol probe for "which open tab
has a video actually playing right now", for DOWNLOAD_PLAYING_VIDEO.

WHY THIS EXISTS, ON TOP OF now_playing.py
----------------------------------------------------------------------------
now_playing.py answers "what page is focused right now" by reading the
address bar of whatever browser window currently has OS focus -- honest,
but not the same question as "which tab has a video actively playing".
A user can be tabbed away to a different app (Discord, a text editor)
while a video keeps playing in a background browser tab; when that
happens now_playing.py reports nothing (the focused window isn't a
browser at all), even though "download what's playing" has an obvious,
unambiguous answer a human would know instantly by ear.

This module answers that narrower, harder question directly: it talks to
Chrome/Edge's own DevTools Protocol (CDP) -- the same protocol Chrome's
own devtools panel, Playwright, and Selenium's CDP mode all use -- and
asks each open tab in turn "do you contain a <video> element that is not
paused right now", returning the URL of the first tab that says yes.
Unlike now_playing.py's address-bar text scrape, this isn't reading a
label off a curated name list; it's asking the actual page, through the
browser's own debugging interface, whether a video is playing in it.

THIS IS DELIBERATELY OPT-IN, NEVER THE ONLY PATH
----------------------------------------------------------------------------
- CDP is only reachable if Chrome/Edge was actually launched with
  --remote-debugging-port=<port> (or the TOKI_CHROME_CDP_PORT override
  below points at one that was). That is NOT how a browser runs day to
  day for most people -- most of the time this module will correctly
  find nothing and get out of the way, which is why
  video_downloader.now_playing.get_now_playing_url() (the address-bar
  reader) stays the fallback, not the other way around.
- This never launches, relaunches, or reconfigures the user's browser.
  No killing the existing process, no injecting the debugging flag
  itself -- that would close the user's actual tabs/session, a far
  bigger surprise than "the download command didn't work this time".
  If CDP isn't already up, this simply reports nothing.
- Needs the OPTIONAL `websocket-client` package (see requirements.txt).
  Only the discovery step (`GET /json/list`) is plain HTTP -- the
  per-tab "is a video playing" check is a CDP websocket RPC
  (Runtime.evaluate), which needs a real websocket client. Missing the
  package means this probe is skipped entirely, not an error -- same
  "optional dependency, clear degrade" posture as text_backend.py's
  pyyaml/toml handling.
- Every result is checked twice before being trusted: the tab must both
  report a real http(s) URL AND have at least one <video> element that
  is unpaused, not ended, and has actually buffered playable data
  (readyState > 2 -- rules out a <video> tag that merely exists on the
  page but hasn't started, e.g. behind a cookie-consent overlay). A tab
  that fails either check is skipped, never guessed into a match.

CALL ORDER (see apis.py's VideoDownloadAPI.download_playing()):
    1. get_now_playing_url_via_cdp() -- this module. Tries every
       configured port, returns the first tab with an actually-playing
       video, or None immediately if CDP isn't reachable at all.
    2. Falls back to now_playing.get_now_playing_url() (the focused
       browser's address bar) if step 1 finds nothing.
"""

from __future__ import annotations

import json
import os
from typing import List, Optional
from urllib.error import URLError
from urllib.request import urlopen

# Chrome/Edge's default remote-debugging port when launched with
# --remote-debugging-port=9222. Overridable via TOKI_CHROME_CDP_PORT for
# anyone running a non-default port -- never guessed or scanned across a
# range of ports, since silently probing arbitrary local ports is exactly
# the kind of unsolicited network behavior this project avoids elsewhere
# (see app_control.py's "ask, don't guess" posture).
_DEFAULT_PORTS = (9222,)

# CDP is localhost-only and, when actually running, answers near-
# instantly -- a short timeout keeps a NOT-running Chrome (the common
# case) from stalling every "download this" command.
_HTTP_TIMEOUT = 0.75
_WS_TIMEOUT = 2.0

# readyState > 2 (HAVE_FUTURE_DATA/HAVE_ENOUGH_DATA) rules out a <video>
# tag that exists on the page but hasn't actually started playing yet
# (e.g. sitting behind a cookie-consent overlay or a paused preview).
_IS_VIDEO_PLAYING_JS = (
    "(() => {"
    "  const vids = document.querySelectorAll('video');"
    "  for (const v of vids) {"
    "    if (!v.paused && !v.ended && v.readyState > 2) return true;"
    "  }"
    "  return false;"
    "})()"
)


def _configured_ports() -> tuple:
    override = os.environ.get("TOKI_CHROME_CDP_PORT")
    if override:
        try:
            return (int(override),)
        except ValueError:
            pass  # Malformed override -- fall through to the default,
            # never crash a "download this" command over a bad env var.
    return _DEFAULT_PORTS


def _list_tabs(port: int) -> Optional[List[dict]]:
    """GET /json/list -- CDP's plain-HTTP tab inventory. Returns None
    (not an error) if nothing is listening on this port, which is the
    normal case for a browser started without the debug flag."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (URLError, OSError, ValueError, TimeoutError):
        return None
    return data if isinstance(data, list) else None


def _tab_has_playing_video(ws_url: str) -> bool:
    """Opens the tab's own per-target CDP websocket and asks it directly
    (Runtime.evaluate) whether a <video> element is currently playing.
    Any failure -- package missing, connection refused, malformed
    response -- resolves to False, never a guess."""
    try:
        import websocket  # websocket-client, OPTIONAL -- see requirements.txt
    except ImportError:
        return False

    try:
        ws = websocket.create_connection(ws_url, timeout=_WS_TIMEOUT)
    except Exception:
        return False

    try:
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": _IS_VIDEO_PLAYING_JS, "returnByValue": True},
        }))
        raw = ws.recv()
    except Exception:
        return False
    finally:
        try:
            ws.close()
        except Exception:
            pass

    try:
        result = json.loads(raw)
        return bool(result["result"]["result"]["value"])
    except Exception:
        return False


def get_now_playing_url_via_cdp() -> Optional[str]:
    """Returns the URL of the first open tab (across configured CDP
    ports) that has an actively-playing <video> element, or None if:
      - CDP isn't reachable on any configured port (by far the most
        common case -- most browsers aren't launched with the debug
        flag), or
      - the optional `websocket-client` package isn't installed, or
      - CDP is reachable but no open tab currently has a playing video.
    Never raises -- every failure mode here means "keep looking
    elsewhere" (see now_playing.get_now_playing_url() as the fallback),
    not "something broke"."""
    for port in _configured_ports():
        tabs = _list_tabs(port)
        if not tabs:
            continue
        for tab in tabs:
            if tab.get("type") != "page":
                continue
            url = tab.get("url") or ""
            if not url.startswith(("http://", "https://")):
                continue
            ws_url = tab.get("webSocketDebuggerUrl")
            if not ws_url:
                continue
            if _tab_has_playing_video(ws_url):
                return url
    return None


__all__ = ["get_now_playing_url_via_cdp"]
