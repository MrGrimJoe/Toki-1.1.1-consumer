"""
scheduler.py -- in-process delayed command execution ("do X in N minutes" /
"do X at HH:MM"), with cancellation support.

SCOPE, STATED HONESTLY: this is an IN-PROCESS scheduler (threading.Timer).
It only fires while TOKI itself is still running. Closing the app, logging
out, or the machine sleeping/shutting down cancels every pending schedule
silently -- there is no persistence layer and no Windows Task Scheduler
integration. That's a deliberate, explicit scope decision (see STATUS.md),
not an oversight: schtasks-based scheduling would survive app restarts but
needs to be built and verified on a real Windows machine, which this
environment cannot do. If TOKI needs to survive its own restart later,
that's a real follow-up, not a bug in this file.

Each scheduled item gets a short, stable, user-facing ID (S1, S2, ...) so a
user can cancel a specific one ("cancel S2") or refer to it by the command
text it holds ("cancel the shutdown"). IDs are never reused within a
process lifetime, so an old ID never accidentally refers to a new item.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


@dataclass
class ScheduledItem:
    id: str
    description: str          # human-readable, e.g. "shut down the computer"
    fire_at: float             # time.time() epoch seconds
    timer: threading.Timer = field(repr=False)
    cancelled: bool = False
    fired: bool = False

    def seconds_remaining(self) -> float:
        return max(0.0, self.fire_at - time.time())


class SchedulerFullError(Exception):
    """Raised when MAX_PENDING is reached -- surfaced as a plain chat
    message, never silently dropped, so a runaway loop of schedule requests
    can't quietly pile up unbounded background timers."""


class ScheduledCommandManager:
    """Owns every pending threading.Timer for delayed dispatch. One
    instance lives on WindowsAIAssistant, same lifetime as the rest of its
    per-session state (self._pending, self.history, etc.) -- see
    orchestrator.py's __init__.
    """

    # Hard ceiling on simultaneous pending schedules. Not a real-world
    # tuning number, just a sane guard against an adversarial-testing
    # session (exactly what this project's own STATUS.md describes doing)
    # spinning up hundreds of live background timers by accident.
    MAX_PENDING = 25

    def __init__(self):
        self._items: Dict[str, ScheduledItem] = {}
        self._lock = threading.Lock()
        self._next_id = 1

    def schedule(
        self,
        delay_seconds: float,
        description: str,
        on_fire: Callable[[], None],
    ) -> ScheduledItem:
        """Schedules on_fire() to run after delay_seconds. Raises
        SchedulerFullError if MAX_PENDING active items already exist --
        caller (orchestrator._dispatch) turns that into an honest chat
        message, never a silent no-op."""
        with self._lock:
            active = sum(1 for it in self._items.values() if not it.cancelled and not it.fired)
            if active >= self.MAX_PENDING:
                raise SchedulerFullError(
                    f"Already have {active} scheduled commands pending -- "
                    f"cancel one before adding another."
                )
            item_id = f"S{self._next_id}"
            self._next_id += 1

        def _run():
            with self._lock:
                item = self._items.get(item_id)
                if item is None or item.cancelled:
                    return
                item.fired = True
            on_fire()

        timer = threading.Timer(max(0.0, delay_seconds), _run)
        timer.daemon = True  # never blocks process/app exit -- same posture
                             # as every other background thread in this app
                             # (see orchestrator.py's _start_thinking)
        item = ScheduledItem(
            id=item_id,
            description=description,
            fire_at=time.time() + delay_seconds,
            timer=timer,
        )
        with self._lock:
            self._items[item_id] = item
        timer.start()
        return item

    def cancel(self, ref: str) -> Optional[ScheduledItem]:
        """Cancels by exact ID (\"S2\") or by a case-insensitive substring
        match against description (\"cancel the shutdown\" matches an item
        whose description is \"shut down the computer\" only if \"shutdown\"
        is judged the same request by the caller's own extraction -- this
        method itself does plain substring matching, nothing fuzzier).
        Returns the cancelled item, or None if nothing matched. If the
        item already fired, cancellation is a no-op (returns None) --
        can't un-ring a bell that already rang."""
        ref_norm = ref.strip().lower()
        with self._lock:
            # Exact ID match first (case-insensitive: "s2" == "S2").
            for item in self._items.values():
                if item.id.lower() == ref_norm and not item.cancelled and not item.fired:
                    item.cancelled = True
                    item.timer.cancel()
                    return item
            # Fall back to substring match against description. If more
            # than one active item matches, this picks the SOONEST one to
            # fire (most likely what "cancel it" means) rather than
            # guessing further -- ambiguity beyond that is the caller's
            # job to ask about, not this method's.
            candidates = [
                it for it in self._items.values()
                if not it.cancelled and not it.fired and ref_norm in it.description.lower()
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda it: it.fire_at)
            chosen = candidates[0]
            chosen.cancelled = True
            chosen.timer.cancel()
            return chosen

    def list_active(self) -> List[ScheduledItem]:
        with self._lock:
            return sorted(
                (it for it in self._items.values() if not it.cancelled and not it.fired),
                key=lambda it: it.fire_at,
            )

    def shutdown(self):
        """Cancels every pending timer. Call on app close so daemon
        threads don't fire commands after the user thinks TOKI is closed
        (daemon=True already prevents them from BLOCKING exit, but without
        this they could still fire in the instant before the process
        actually dies)."""
        with self._lock:
            for item in self._items.values():
                if not item.fired:
                    item.cancelled = True
                    item.timer.cancel()
