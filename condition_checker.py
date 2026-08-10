"""
condition_checker.py -- background polling for "if X then Y" conditional
commands.

SCOPE, STATED HONESTLY (see STATUS.md and intents.py's CONDITIONAL_COMMAND
entry): this only supports conditions this codebase can actually READ.
As of this file, that's battery level/charging state -- there is NO wifi
on/off check, no network-adapter state check, anywhere in this codebase
(confirmed by grep across apis.py/intents*.py/app_control.py before writing
this). So "if wifi is off, turn it on" is detected as a conditional
request, but the condition itself can't be evaluated -- TOKI says so
plainly (see orchestrator.py's CONDITIONAL_COMMAND dispatch branch) rather
than silently pretending to monitor something it can't check. Adding a
real wifi-state check is a real follow-up item (needs a PowerShell
Get-NetAdapter/Get-NetConnectionProfile call verified live on Windows,
which this sandbox can't do), not something bolted on here as a guess.

Design: a small registry of named condition-checker functions, each a
zero-arg callable returning True/False (or raising on a real check
failure, e.g. no battery present). ConditionPoller polls one of these on
an interval via threading.Timer (same in-process posture as scheduler.py
-- doesn't survive TOKI closing, see scheduler.py's own scope note) and
fires an action callback the first time the condition is True. Supports
cancellation the same way scheduler.py does.
"""

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional


def _run_powershell_sync(command: str, timeout: float = 5.0) -> str:
    """Synchronous PowerShell call, output as a plain string. Separate
    from executor.py's RunningCommand on purpose -- that class is
    async/streaming/killable for user-facing dispatch with a Stop button;
    polling needs a blocking call it can run silently every few seconds
    from a background thread, which is a different enough contract
    (sync return value vs. streamed callbacks) to not force into the same
    class. Raises on failure/timeout -- callers decide what that means
    for their specific condition rather than this function guessing."""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    return proc.stdout.strip()


def _check_battery_low(threshold: int = 20) -> bool:
    """True if a battery is present and its charge is below threshold%.
    Raises RuntimeError if no battery is present (desktop/VM) -- that's a
    real "can't check this" case, distinct from "checked it, it's False",
    so callers must not treat an exception as "condition not met"."""
    out = _run_powershell_sync(
        "$b = Get-CimInstance -ClassName Win32_Battery; "
        "if ($b) { $b.EstimatedChargeRemaining } else { 'NO_BATTERY' }"
    )
    if "NO_BATTERY" in out or not out:
        raise RuntimeError("No battery detected (desktop or VM) -- can't check battery level.")
    m = re.search(r"\d+", out)
    if not m:
        raise RuntimeError(f"Couldn't parse battery level from: {out!r}")
    return int(m.group(0)) < threshold


def _check_battery_full() -> bool:
    out = _run_powershell_sync(
        "$b = Get-CimInstance -ClassName Win32_Battery; "
        "if ($b) { $b.EstimatedChargeRemaining } else { 'NO_BATTERY' }"
    )
    if "NO_BATTERY" in out or not out:
        raise RuntimeError("No battery detected (desktop or VM) -- can't check battery level.")
    m = re.search(r"\d+", out)
    if not m:
        raise RuntimeError(f"Couldn't parse battery level from: {out!r}")
    return int(m.group(0)) >= 95


# Registry of conditions this file can ACTUALLY check right now. Deliberately
# tiny and explicit -- see module docstring for why wifi/network conditions
# are not here yet. Each entry: keyword phrases (for matching the user's
# plain-language condition) -> zero-arg checker callable.
CHECKABLE_CONDITIONS: Dict[str, Callable[[], bool]] = {
    "battery low": _check_battery_low,
    "battery is low": _check_battery_low,
    "battery full": _check_battery_full,
    "battery is full": _check_battery_full,
    "fully charged": _check_battery_full,
}

# Clean, de-duplicated summary for user-facing "here's what I can watch
# for" messages -- CHECKABLE_CONDITIONS itself intentionally has multiple
# phrasing variants pointing at the same checker (for substring matching
# in match_condition()), so deriving a display list directly from its
# keys produces a redundant/confusing list. This is the one place that
# should be updated when a new distinct condition (not just a new phrasing
# of an existing one) is added.
CHECKABLE_CONDITIONS_SUMMARY = ["battery is low", "battery is full"]


def match_condition(condition_text: str) -> Optional[Callable[[], bool]]:
    """Loose substring match against CHECKABLE_CONDITIONS. Returns the
    checker callable, or None if nothing in the (deliberately small)
    registry matches -- caller must treat None as 'can't monitor this
    yet', never silently skip it."""
    norm = condition_text.strip().lower()
    for phrase, checker in CHECKABLE_CONDITIONS.items():
        if phrase in norm:
            return checker
    return None


@dataclass
class PolledCondition:
    id: str
    description: str  # full "if X then Y" text, for narration/cancel-by-description
    cancelled: bool = False
    fired: bool = False
    error: Optional[str] = None
    _timer: Optional[threading.Timer] = field(default=None, repr=False)


class ConditionPoller:
    """Owns every active background condition poll. One instance lives on
    WindowsAIAssistant alongside ScheduledCommandManager -- see
    orchestrator.py's __init__."""

    # Same rationale as scheduler.py's MAX_PENDING: caps simultaneous
    # background pollers so an adversarial-testing session can't spin up
    # unbounded polling threads by accident.
    MAX_ACTIVE = 10
    POLL_INTERVAL_SECONDS = 5.0
    # Give up after this long so a condition that never becomes true
    # doesn't poll forever in the background with no visible end.
    MAX_POLL_DURATION_SECONDS = 3600.0  # 1 hour

    def __init__(self):
        self._items: Dict[str, PolledCondition] = {}
        self._lock = threading.Lock()
        self._next_id = 1

    def start(
        self,
        checker: Callable[[], bool],
        description: str,
        on_true: Callable[[], None],
        on_error: Callable[[str], None],
        on_timeout: Callable[[], None],
    ) -> PolledCondition:
        with self._lock:
            active = sum(1 for it in self._items.values() if not it.cancelled and not it.fired)
            if active >= self.MAX_ACTIVE:
                raise RuntimeError(
                    f"Already watching {active} conditions -- cancel one before adding another."
                )
            item_id = f"C{self._next_id}"
            self._next_id += 1

        item = PolledCondition(id=item_id, description=description)
        start_time = time.time()

        def _tick():
            # BETA 0.3.28 fix: this used to read/write item.cancelled,
            # item.fired, and item._timer with NO lock held at all, while
            # cancel()/shutdown() mutate those same fields UNDER self._lock.
            # checker() can block for up to 5s (a PowerShell subprocess) --
            # if the user cancels while _tick() is mid-checker() call, the
            # cancel takes effect against the CURRENT timer (correctly
            # stopping it), but _tick() then finishes, sees the condition
            # still false, and reschedules by creating a brand new timer
            # and assigning it to item._timer -- a poller the user just
            # cancelled comes back to life, invisible to cancel() (which
            # already ran and has nothing left to call .cancel() on).
            #
            # Fix (matches scheduler.py's _run(), which already gets this
            # right): every read of item.cancelled and every write to
            # item.fired/item._timer happens under self._lock, in the same
            # critical section as the write it's guarding -- so cancel()
            # can never interleave between "checked not-cancelled" and
            # "committed to firing/rescheduling". checker() ITSELF still
            # runs with the lock released (same as scheduler.py never
            # holds its lock across on_fire()) -- a 5s PowerShell call
            # must not block cancel()/shutdown() for other items, or for
            # this one's own cancel() attempt, for that whole duration.
            with self._lock:
                if item.cancelled:
                    return
            if time.time() - start_time > self.MAX_POLL_DURATION_SECONDS:
                with self._lock:
                    if item.cancelled:
                        return
                    item.fired = True
                on_timeout()
                return
            try:
                is_true = checker()
            except Exception as e:
                with self._lock:
                    if item.cancelled:
                        return
                    item.fired = True
                    item.error = str(e)
                on_error(str(e))
                return
            if is_true:
                with self._lock:
                    if item.cancelled:
                        return
                    item.fired = True
                on_true()
                return
            # Not true yet -- reschedule the next check. The cancelled
            # check and the new-timer creation/assignment/start all
            # happen inside ONE critical section: if cancel() already
            # ran, we see item.cancelled==True here and never create a
            # new timer at all; if cancel() hasn't run yet, it will
            # acquire the lock right after we release it and correctly
            # find (and cancel) the NEW timer via item._timer, instead of
            # racing against a timer it can't see yet.
            with self._lock:
                if item.cancelled:
                    return
                timer = threading.Timer(self.POLL_INTERVAL_SECONDS, _tick)
                timer.daemon = True
                item._timer = timer
                timer.start()

        first_timer = threading.Timer(self.POLL_INTERVAL_SECONDS, _tick)
        first_timer.daemon = True
        # Publish the item into self._items AND assign/start its first
        # timer in the SAME critical section, for the same reason as the
        # reschedule branch above: shutdown()/cancel() could otherwise run
        # between "item visible in self._items" and "item._timer
        # assigned", see item._timer still None, treat that as nothing-
        # to-cancel, and then have this thread hand it a live timer a
        # moment later -- a poller that outlives a shutdown() that
        # already ran before this method even returned.
        with self._lock:
            self._items[item_id] = item
            item._timer = first_timer
            first_timer.start()
        return item

    def cancel(self, ref: str) -> Optional[PolledCondition]:
        ref_norm = ref.strip().lower()
        with self._lock:
            for item in self._items.values():
                if item.id.lower() == ref_norm and not item.cancelled and not item.fired:
                    item.cancelled = True
                    if item._timer:
                        item._timer.cancel()
                    return item
            candidates = [
                it for it in self._items.values()
                if not it.cancelled and not it.fired and ref_norm in it.description.lower()
            ]
            if not candidates:
                return None
            chosen = candidates[0]
            chosen.cancelled = True
            if chosen._timer:
                chosen._timer.cancel()
            return chosen

    def list_active(self):
        with self._lock:
            return [it for it in self._items.values() if not it.cancelled and not it.fired]

    def shutdown(self):
        with self._lock:
            for item in self._items.values():
                if not item.fired:
                    item.cancelled = True
                    if item._timer:
                        item._timer.cancel()
