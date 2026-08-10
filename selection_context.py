"""
selection_context.py — ephemeral "what file is the user pointing at right
now" store, feeding the same anaphora-resolution path extractor.py already
uses for "it"/"that" (see extractor.py's ANAPHORA_ELIGIBLE_INTENTS /
resolve_anaphoric_target).

WHY A SEPARATE MODULE FROM target_memory.py AND orchestrator._last_touched
----------------------------------------------------------------------------
Three different "what does this refer to" stores exist on purpose, each
answering a different question:

  - target_memory.py      -> "what UI element did I click, on THIS window,
                              LAST TIME I taught it" (persistent, JSON, keyed
                              by window title)
  - orchestrator._last_touched -> "what file did TOKI ITSELF most recently
                              create/rename/move/copy/generate THIS SESSION"
                              (in-memory, TOKI-authored history)
  - SelectionContext (here) -> "what file did the USER most recently point
                              at, from OUTSIDE toki -- a drag-drop onto the
                              overlay, or an Explorer selection read at
                              invocation time" (in-memory, user-authored,
                              short TTL)

Conflating any of these would produce wrong answers: "rename it" after
TOKI just created a folder should never resolve to a photo the user
dragged in five minutes ago, and "shrink this image" should never resolve
to a file TOKI itself wrote out as a side effect of an unrelated command.
Keeping them separate means each one stays a small, auditable, exact
lookup -- same "no guessing, deterministic" posture as the rest of this
project.

TTL, NOT PERSISTENCE
----------------------------------------------------------------------------
Deliberately NOT written to disk like target_memory.py. A "selected file"
that's still sitting in memory from an hour-old drag-drop is more likely
to be stale/wrong than helpful -- so this expires on its own after
SELECTION_TTL_SECONDS. A miss here just means "nothing currently selected,
ask the user" -- never a wrong file.

SINGLE SLOT, NOT A LIST
----------------------------------------------------------------------------
Only ever remembers ONE file, matching this feature's whole pitch: "this
file you're pointing at." Multi-file batch selection is a real future
feature but a different one -- not folded in here to keep this small and
fully testable.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional, Dict, Any

# How long a selection stays "current" before it's treated as stale and
# ignored. 10 minutes: long enough to survive "drag file, then think for a
# bit, then talk to TOKI", short enough that yesterday's drag-drop can
# never silently resurface.
SELECTION_TTL_SECONDS = 600


class SelectionContext:
    """Single-instance-per-process store for "the file the user most
    recently pointed TOKI at". app.py/orchestrator.py hold one shared
    instance (same singleton pattern as target_memory.py's _target_memory),
    fed by:
      - toki_desktop_mark.py's dropEvent (drag a file onto the overlay)
      - (future) an Explorer-selection reader, same call site
    and read by extractor.py's resolve_selected_file_target().
    """

    def __init__(self) -> None:
        self._path: Optional[Path] = None
        self._set_at: float = 0.0

    def set_selected(self, path: str) -> Optional[Dict[str, Any]]:
        """Record a newly selected/dropped file. Returns a small metadata
        dict on success, or None if the path doesn't actually exist --
        a bad drop shouldn't silently poison the next command with a
        dead path."""
        p = Path(path)
        if not p.exists() or not p.is_file():
            return None
        self._path = p
        self._set_at = time.monotonic()
        return self._describe(p)

    def get_selected(self) -> Optional[Dict[str, Any]]:
        """Returns the current selection's metadata, or None if nothing is
        selected, the TTL has expired, or the file has since been deleted
        or moved out from under us."""
        if self._path is None:
            return None
        if time.monotonic() - self._set_at > SELECTION_TTL_SECONDS:
            self.clear()
            return None
        if not self._path.exists():
            # File moved/deleted since selection -- stale, not a wrong
            # click waiting to happen.
            self.clear()
            return None
        return self._describe(self._path)

    def clear(self) -> None:
        self._path = None
        self._set_at = 0.0

    def _describe(self, p: Path) -> Dict[str, Any]:
        try:
            stat = p.stat()
            size = stat.st_size
        except OSError:
            size = None
        return {
            "path": str(p),
            "name": p.name,
            "extension": p.suffix.lower().lstrip("."),
            "size": size,
        }


# ── module-level singleton, same pattern as target_memory.py ───────────────
_selection_context = SelectionContext()


def get_selection_context() -> SelectionContext:
    return _selection_context
