"""
foreground_tracker.py -- background poller that remembers the last
non-TOKI window to hold OS foreground focus.

── The bug this exists to fix ──────────────────────────────────────────────
app_control.py's _get_focused_window() asks Windows for whatever window
currently has foreground focus (pywinauto's Desktop(backend="uia")
.window(active_only=True), which is itself just a wrapper over
GetForegroundWindow()). That's correct in general, but it breaks for
EVERY app_control.py / video_downloader/now_playing.py call that fires
as a direct result of the user typing a command into TOKI: by the
instant that code runs, the window that has OS focus is TOKI's OWN
window (the widget/hotkey/text-entry surface the user just typed into),
never the browser or app the user actually meant -- regardless of what
was focused a second earlier when the user actually issued the command.

This is the confirmed root cause behind two previously-separate-looking
bug reports: the video downloader saying it "couldn't find its link" or
"couldn't find its directory" even with YouTube genuinely open in
Chrome, and app-control clicks/types landing nowhere (or on TOKI's own
UI) instead of the target application.

── The fix ──────────────────────────────────────────────────────────────────
A lightweight background thread polls GetForegroundWindow() every
_POLL_INTERVAL_S seconds using raw ctypes/user32 (no new dependency --
consistent with target_memory.py's own "no pywin32/psutil anywhere in
this project" choice) and remembers the most recent window handle that
did NOT belong to TOKI's own process. app_control.py's
_get_focused_window() then falls back to that remembered handle
whenever the live foreground window turns out to be TOKI's own.

Windows are identified as "TOKI's own" by PROCESS ID, not window title
or class name -- TOKI is a single Python process, but it owns several
distinct top-level windows over its lifetime (the DesktopMark icon, the
commands panel, the reply bubble, any future popup), any of which could
have OS focus at poll time. Comparing GetWindowThreadProcessId()'s
output against os.getpid() catches all of them uniformly, with no need
to keep a title/class allowlist in sync as the widget UI changes.

── Design notes ─────────────────────────────────────────────────────────────
- Single background daemon thread, started once via start() (idempotent
  -- safe to call more than once, e.g. from both main_widget.py at
  startup and a test's own setup). Never anything that keeps the
  process alive on its own or blocks shutdown.
- Fails soft everywhere: any ctypes call failing (unlikely on real
  Windows, but this module must never be the thing that crashes a
  turn) just means this poll tick contributes nothing -- the last good
  value already recorded is left untouched, not cleared.
- get_last_foreground_window() re-validates the remembered handle with
  IsWindow() before returning it, since the window it pointed at may
  have since closed (user closed the browser tab/app between issuing
  the command and TOKI's code running the lookup a moment later) --
  callers must already treat a None return as "couldn't find it",
  identical to _get_focused_window()'s existing TargetNotFound path,
  so a stale handle is refused rather than handed back to look valid.
- Platform-gated the same way install_autostart.py already is
  (`sys.platform != "win32"`): importing ctypes.windll on a non-Windows
  platform doesn't fail (ctypes itself is stdlib), but touching
  `ctypes.windll` does, so every public function here checks the
  platform first and fails soft to "tracking unavailable" rather than
  raising on import or at call time. This lets the module be imported
  freely (and unit-tested with the Windows calls mocked) on any
  platform, matching how app_control.py's own pywinauto lazy-load
  already behaves.

NOT VERIFIED AGAINST REAL WINDOWS in this session -- ctypes/user32
GetForegroundWindow()/GetWindowThreadProcessId()/IsWindow() are the
documented, stable Win32 APIs for exactly this ("what window has
focus", "is this handle still a real window") and this is real,
syntactically valid code, unit-tested here with those calls mocked --
but there is no Windows environment available in this sandbox to
confirm it behaves as expected against a real desktop session. Test
this live before trusting it for anything destructive.
"""

import ctypes
import os
import sys
import threading
import time
from typing import Optional

# How often the background thread checks GetForegroundWindow(). Fast enough
# that "the user alt-tabbed to the real window, then hit Ctrl+K" is caught
# well before TOKI's own code needs the answer (a user's fastest realistic
# hotkey-then-speak/type turnaround is on the order of a second, not
# milliseconds), slow enough that this never shows up as measurable CPU
# usage sitting in the background for an entire session.
_POLL_INTERVAL_S = 0.2

# How long a remembered handle is trusted without a fresh poll confirming
# it's still real, in POLL TICKS worth of staleness this module itself
# would ever produce -- in practice IsWindow() at read time (see
# get_last_foreground_window()) is the real staleness guard, this constant
# exists only for the docstring/tests to reference a single source of truth
# for the poll cadence instead of a magic number reappearing in each test.
POLL_INTERVAL_S = _POLL_INTERVAL_S

_IS_WINDOWS = sys.platform == "win32"

_lock = threading.Lock()
_last_foreign_hwnd: Optional[int] = None
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()

# Populated lazily by _get_user32(), same "import/bind once, cache the
# result" shape as app_control.py's _load_pywinauto() -- touching
# ctypes.windll at module import time would break importing this module
# at all on non-Windows platforms (this file's own tests included), so
# nothing Windows-specific happens until a caller actually asks for it.
_user32 = None


def _get_user32():
    """Binds ctypes.windll.user32 on first call, caches it, never
    re-binds. Returns None (never raises) on any platform where it isn't
    available -- callers treat None exactly like "tracking unavailable"."""
    global _user32
    if _user32 is not None:
        return _user32
    if not _IS_WINDOWS:
        return None
    try:
        _user32 = ctypes.windll.user32
    except Exception:
        _user32 = None
    return _user32


def _get_foreground_window_and_pid(user32) -> "tuple[Optional[int], Optional[int]]":
    """One poll tick's worth of raw Win32 calls, isolated into its own
    function so both the real poll loop and tests can exercise exactly
    this step without also spinning up a background thread. Returns
    (hwnd, owning_pid), either half None on any failure or if there's
    currently no foreground window at all (both legitimately happen --
    e.g. briefly during a workspace/virtual-desktop switch)."""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None, None
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return hwnd, pid.value
    except Exception:
        return None, None


def _poll_loop() -> None:
    """Runs on the background daemon thread started by start(). Never
    raises out of the loop -- a single bad tick (a window closing mid-
    call, a transient API failure) must not kill tracking for the rest
    of the session, it should just be retried on the next tick."""
    global _last_foreign_hwnd
    user32 = _get_user32()
    if user32 is None:
        return
    my_pid = os.getpid()
    while not _stop_event.is_set():
        try:
            hwnd, owner_pid = _get_foreground_window_and_pid(user32)
            if hwnd and owner_pid is not None and owner_pid != my_pid:
                with _lock:
                    _last_foreign_hwnd = hwnd
        except Exception:
            # Fails soft, same reasoning as every try/except in this
            # module's docstring -- one bad tick must not stop the next.
            pass
        _stop_event.wait(_POLL_INTERVAL_S)


def start() -> None:
    """Starts the background poller if it isn't already running.
    Idempotent and safe to call from anywhere (main_widget.py at
    startup, orchestrator.py's WindowsAIAssistant.__init__, a test's
    own setup) -- a second call while already running is a silent
    no-op, not a second competing thread. No-op entirely on a
    non-Windows platform, same fail-soft posture as every other
    Windows-only feature in this project (see install_autostart.py)."""
    global _thread
    if not _IS_WINDOWS:
        return
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(target=_poll_loop, daemon=True, name="ForegroundTracker")
        _thread.start()


def stop() -> None:
    """Signals the poller to exit and waits briefly for it to actually
    stop. Not strictly required for a clean process exit (the thread is
    a daemon, same guarantee every other background thread in this
    project relies on -- see orchestrator.py's own priming threads) but
    called from WindowsAIAssistant.shutdown() anyway for the same
    "don't leave things running past when they're needed" reasoning
    that method already applies to the scheduler/condition poller.
    Safe to call even if start() was never called."""
    _stop_event.set()
    t = _thread
    if t is not None:
        t.join(timeout=1.0)


def get_last_foreground_window() -> Optional[int]:
    """Returns the most recently observed non-TOKI foreground window
    handle, or None if either nothing's been observed yet or the
    remembered window has since closed. This is the ONLY function
    app_control.py's _get_focused_window() should call -- it re-
    validates staleness on every call via IsWindow() rather than trust
    the last-written value blindly, since the window it pointed at may
    have closed in the time between being observed and being asked
    for."""
    user32 = _get_user32()
    if user32 is None:
        return None
    with _lock:
        hwnd = _last_foreign_hwnd
    if hwnd is None:
        return None
    try:
        if not user32.IsWindow(hwnd):
            return None
    except Exception:
        return None
    return hwnd


def is_running() -> bool:
    """For tests/diagnostics -- whether the background thread is
    currently alive."""
    t = _thread
    return t is not None and t.is_alive()


def _reset_for_tests() -> None:
    """Test-only helper: clears remembered state and stops the thread
    without needing a fresh process. Not called anywhere in production
    code -- exists so tests/test_foreground_tracker.py can start from a
    known-clean state instead of leaking state between test cases."""
    global _last_foreign_hwnd
    stop()
    with _lock:
        _last_foreign_hwnd = None
