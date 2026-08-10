"""
target_memory.py — persistent "click it once, I'll remember it" store for
app_control.py's resolve_target().

Deliberately NOT a graph-db/ML feature: it's a flat JSON key-value map from
(window_title, target_description) -> a captured UI Automation identity
{name, control_type, class_name}. Same "no guessing, exact deterministic
lookup" posture as everything else in this app -- a miss here just means
"haven't been taught this one yet", never a wrong click.

Keyed by WINDOW TITLE, not process name -- app_control.py has no existing
process-name lookup (no pywin32/psutil dependency anywhere in this project)
and adding one just for this felt like scope creep for a feature this
small; window title was already reachable through the same pywinauto
element app_control.py already walks.

Known accepted limitation, stated plainly rather than hidden: browser
windows change title per tab/page, so a teach captured on one page won't
be found again on a different page of the same site. Not fixed in this
pass.
"""

import json
import re
from pathlib import Path
from typing import Optional, Dict, Any

STORE_PATH = Path(__file__).parent / "learned_targets.json"


def _normalize_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


class TargetMemory:
    """One instance per process is plenty -- app_control.py holds a module-
    level singleton (_target_memory) rather than constructing this per call,
    same "cheap object, shared instance" pattern as AppController's own
    class-level caches."""

    def __init__(self, path: Path = STORE_PATH):
        self._path = path
        self._data: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                # Corrupt file -- start clean rather than crash every single
                # resolve_target() call for the rest of the session.
                self._data = {}

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except Exception:
            # Best-effort persistence -- a failed write shouldn't undo the
            # click that already happened live in this session, and
            # shouldn't crash the caller either.
            pass

    def _key(self, window_title: str, target_description: str) -> str:
        return f"{_normalize_key(window_title)}||{_normalize_key(target_description)}"

    def get(self, window_title: str, target_description: str) -> Optional[Dict[str, Any]]:
        self._load()
        return self._data.get(self._key(window_title, target_description))

    def remember(self, window_title: str, target_description: str, identity: Dict[str, Any]) -> None:
        self._load()
        self._data[self._key(window_title, target_description)] = identity
        self._save()

    def forget(self, window_title: str, target_description: str) -> bool:
        self._load()
        key = self._key(window_title, target_description)
        if key in self._data:
            del self._data[key]
            self._save()
            return True
        return False
