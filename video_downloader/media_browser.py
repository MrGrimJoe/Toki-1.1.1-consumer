"""
media_browser.py -- launches a DEDICATED Chrome/Edge instance, in its own
profile directory, with the Chrome DevTools Protocol debugging port open,
strictly for TOKI's own media/video browsing (currently: YouTube search
results opened via apis.py's WebSearchAPI.search(site="youtube")).

WHY THIS EXISTS
----------------------------------------------------------------------------
cdp_now_playing.py can answer "which tab actually has a video playing
right now" over CDP -- but only if SOME already-running Chrome/Edge
process happens to have been launched with --remote-debugging-port. Most
people don't run their day-to-day browser that way, so in practice CDP
almost never fires and DOWNLOAD_PLAYING_VIDEO falls back to
now_playing.py's address-bar read every single time -- confirmed exactly
the gap raised in chat ("the link grabber for video isn't as strong right
now"). That fallback still requires the right window to be focused at the
right moment; CDP doesn't.

Two realistic fixes exist for this. Injecting the debug flag into the
user's REAL, already-running browser was rejected -- Chrome refuses to
accept new launch flags on an already-running process, so "injecting" it
really means force-closing the user's actual tabs/session to relaunch
with the flag, which is a far bigger surprise than "the download command
didn't work this time" (see cdp_now_playing.py's own docstring for the
same reasoning applied to a different piece of this problem). This module
implements the other one: a SEPARATE, dedicated browser instance, its own
profile directory, used only when TOKI itself opens something video-
shaped. The user's main browser -- whatever they use for banking, email,
everything else -- is never touched, never relaunched, never even looked
at.

SECURITY POSTURE (done properly, not glossed over)
----------------------------------------------------------------------------
- The debug port is NEVER exposed off localhost. This deliberately never
  passes --remote-debugging-address -- Chrome/Edge's own default for that
  flag is loopback-only; explicitly setting it to 0.0.0.0 is what would
  make it reachable from other devices on the network, and this code
  never does that. Standard local-firewall rules cover the rest, same as
  any other localhost-bound dev server.
- cdp_now_playing.py's own probe already only ever talks to
  127.0.0.1/localhost -- this module doesn't change that contract, it
  just makes sure something real is listening there when TOKI itself is
  the one opening the video.
- A dedicated, TOKI-owned profile directory means this can't leak the
  user's real browsing history/cookies/logins into anything the CDP
  probe reads -- it's a fresh profile, not a debug hook into their real
  one. First launch is effectively a clean, logged-out browser; the user
  can sign in to it separately if they want, same as any other
  independent browser profile.
- Fails soft everywhere: any failure (browser not found, launch error)
  is caught by the caller and falls back to WebSearchAPI's existing plain
  _open_in_chrome() path -- the search still opens, just without the CDP
  fast path for that one turn.

NOT DONE HERE, ON PURPOSE
----------------------------------------------------------------------------
- Does not attempt to inject the debug flag into an already-running
  browser, or close/relaunch it.
- Does not widen the debug port to the user's main browser under any
  circumstances, and never will.
- Does not (yet) reuse an already-launched dedicated instance for a
  second call in the same session -- each call launches fresh. Chrome
  itself handles a second launch against the same --user-data-dir by
  opening a new tab in the existing window rather than a second process,
  so this is safe in practice, just not something this module verifies
  or optimizes for explicitly.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

# Same default as cdp_now_playing.py's own _DEFAULT_PORTS -- kept as a
# separate constant (not imported from that module) so this file has no
# import-time dependency on the optional `websocket-client` package
# cdp_now_playing.py needs; this module only ever launches a process, it
# never talks CDP itself.
DEFAULT_CDP_PORT = 9222


def _cdp_port() -> int:
    override = os.environ.get("TOKI_CHROME_CDP_PORT")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    return DEFAULT_CDP_PORT


def _dedicated_profile_dir() -> str:
    """A TOKI-owned profile directory, separate from the user's real
    Chrome profile -- %LocalAppData%\\TOKI\\MediaBrowserProfile on
    Windows. Created on first use if it doesn't exist yet; a plain
    directory, no special permissions needed."""
    base = os.environ.get("LocalAppData") or os.path.expanduser("~")
    profile_dir = os.path.join(base, "TOKI", "MediaBrowserProfile")
    os.makedirs(profile_dir, exist_ok=True)
    return profile_dir


# Same three real Chrome install locations apis.py's WebSearchAPI already
# checks, plus the two standard Edge locations -- Edge is a real fallback
# here (unlike apis.py's own chrome-only path) since it speaks CDP
# identically and plenty of Windows machines have it but not Chrome.
_BROWSER_PATH_CANDIDATES = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
)


def _find_browser_exe() -> Optional[str]:
    for candidate in _BROWSER_PATH_CANDIDATES:
        expanded = os.path.expandvars(candidate)
        if os.path.exists(expanded):
            return expanded
    return None


def launch_media_browser(url: Optional[str] = None, port: Optional[int] = None) -> bool:
    """Launches the dedicated media-browser instance (fresh TOKI-owned
    profile, CDP debugging on, loopback-only) and points it at `url` if
    given. Returns True if the process was actually started, False on
    any failure (browser not found, launch error) -- never raises, same
    fail-soft contract as every other browser-launching path in this
    project (WebSearchAPI._open_in_chrome, app_control.launch_app).
    Caller is expected to fall back to a plain browser open on a False
    return, exactly like every other optional-enhancement path here."""
    exe = _find_browser_exe()
    if not exe:
        return False

    resolved_port = port if port is not None else _cdp_port()
    args = [
        exe,
        f"--remote-debugging-port={resolved_port}",
        f"--user-data-dir={_dedicated_profile_dir()}",
        "--no-first-run",
    ]
    if url:
        args.append(url)

    try:
        subprocess.Popen(
            args,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        return True
    except Exception:
        return False
