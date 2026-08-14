"""
macro_recorder.py — "start seeing" / named replay macros.

Explicitly NOT a TOKI-command replay -- design discussion this session
rejected that in favor of recording RAW physical input (mouse clicks + key
presses) the user makes by hand after saying "start seeing", exactly as
performed, replayed later on a single bare wake word.

Two safety properties, both deliberate, both settled this session, both
non-negotiable:

  1. NO BLIND COORDINATE REPLAY. Every recorded click also captures the UI
     Automation identity (name/control_type/class) of whatever was under
     the cursor at record time, via app_control.py's
     capture_identity_at_point() -- the exact same mechanism
     target_memory.py's click-to-teach uses. At REPLAY time, the element
     currently at that coordinate is re-checked against the recorded
     identity before clicking. A mismatch ABORTS the whole macro
     immediately and tells the user which step failed, rather than
     clicking whatever happens to be there now. If identity capture
     itself failed at record time (some elements throw on property
     access, or the click landed on empty desktop), that step degrades to
     coordinate-only and is replayed as-is -- a known, accepted, and
     stated gap, not a silently swallowed one.

  2. TRIGGER SAFETY VIA "ONE MADE-UP WORD, EXACT MATCH, HOTKEY-GATED".
     A macro name is matched ONLY when the ENTIRE utterance is exactly
     that one word (see orchestrator.py's pre-check, mirroring the
     scheduling/conditional pre-check pattern already in this codebase)
     AND the utterance arrived through the existing Ctrl+K-gated voice
     pipeline in the first place -- there is no ambient/always-listening
     path anywhere in this app for a macro to accidentally fire from.
     Deliberate scope decision, not an oversight: no fuzzy matching, no
     partial-phrase matching, no free-form trigger phrases.

Recording uses pynput -- already a hard dependency for the Ctrl+K hotkey
(see toki_desktop_mark.py's _start_hotkey_listener()) -- listening for
mouse button-down and keyboard key-down events on background threads, same
mechanism, no new dependency.

Storage is a flat JSON file per macro under macros/ next to this script --
same "flat file, not a database" choice as target_memory.py, for the same
reason (small, infrequent writes, no query need beyond exact-name lookup).

NOT VERIFIED AGAINST REAL WINDOWS in this session (no way to run real
pynput/pywinauto against a live desktop from this environment) -- the
identity-verification-before-click logic is exercised in
tests/test_macro_recorder.py against a mocked capture function, but the
actual live record/replay loop needs a real machine before this is called
genuinely done rather than logically done. Same honesty standard as
app_control.py's own click/type mechanism when it was first given test
coverage this session.
"""

import json
import re
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

MACROS_DIR = Path(__file__).parent / "macros"

# Best-effort mapping from pynput's non-character Key enum (str(key) looks
# like "Key.enter") to send_keys' own special-key syntax. Deliberately not
# exhaustive -- a key not in this map and not a single printable character
# is skipped on replay rather than guessed, consistent with "never guess"
# everywhere else in this app. Extend as real gaps turn up in use.
_PYNPUT_KEY_TO_SENDKEYS = {
    "Key.enter": "{ENTER}",
    "Key.tab": "{TAB}",
    "Key.space": " ",
    "Key.backspace": "{BACKSPACE}",
    "Key.esc": "{ESC}",
    "Key.delete": "{DELETE}",
    "Key.up": "{UP}",
    "Key.down": "{DOWN}",
    "Key.left": "{LEFT}",
    "Key.right": "{RIGHT}",
}


def _safe_macro_filename(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", name.strip().lower()) or "macro"


def list_macros() -> List[str]:
    """Names of every currently-saved macro, for orchestrator.py's
    single-bare-word trigger pre-check. Returns [] (not an error) if the
    macros/ directory doesn't exist yet -- nothing's been taught."""
    if not MACROS_DIR.exists():
        return []
    return sorted(p.stem for p in MACROS_DIR.glob("*.json"))


def load_macro(name: str) -> Optional[List[Dict[str, Any]]]:
    path = MACROS_DIR / f"{_safe_macro_filename(name)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


class MacroRecorder:
    """One recording session: start_recording() then stop_recording()."""

    def __init__(self):
        self._events: List[Dict[str, Any]] = []
        self._start_time: Optional[float] = None
        self._mouse_listener = None
        self._keyboard_listener = None
        self._recording = False

    def start_recording(self) -> None:
        try:
            from pynput import mouse, keyboard
        except ImportError:
            raise RuntimeError(
                "pynput not installed -- macro recording unavailable. Fix: pip install pynput"
            )

        self._events = []
        self._start_time = time.monotonic()
        self._recording = True

        def on_click(x, y, button, pressed):
            if not pressed or not self._recording:
                return
            identity = None
            try:
                # Local import: same lazy-pywinauto-import discipline as
                # app_control.py itself (see that module's own docstring
                # for why pywinauto can never be imported at module level).
                from app_control import capture_identity_at_point
                identity = capture_identity_at_point(x, y)
            except Exception:
                identity = None  # degrades to coordinate-only, see module docstring
            self._events.append({
                "type": "click",
                "x": x, "y": y,
                "button": str(button),
                "identity": identity,
                "t": time.monotonic() - self._start_time,
            })

        def on_press(key):
            if not self._recording:
                return
            try:
                k = key.char if getattr(key, "char", None) is not None else str(key)
            except Exception:
                k = str(key)
            self._events.append({
                "type": "key",
                "key": k,
                "t": time.monotonic() - self._start_time,
            })

        self._mouse_listener = mouse.Listener(on_click=on_click)
        self._keyboard_listener = keyboard.Listener(on_press=on_press)
        self._mouse_listener.start()
        self._keyboard_listener.start()

    def stop_recording(self) -> List[Dict[str, Any]]:
        self._recording = False
        if self._mouse_listener:
            self._mouse_listener.stop()
        if self._keyboard_listener:
            self._keyboard_listener.stop()
        return self._events

    def save(self, name: str) -> Path:
        MACROS_DIR.mkdir(exist_ok=True)
        path = MACROS_DIR / f"{_safe_macro_filename(name)}.json"
        path.write_text(json.dumps(self._events, indent=2), encoding="utf-8")
        return path


class MacroPlayer:
    """Replays a saved macro. Verifies identity before every click that has
    one recorded; aborts (never falls back to a blind click) on a mismatch
    -- see module docstring, safety property 1."""

    # Caps the reproduced delay between steps so a long pause mid-recording
    # (user got distracted, went to make coffee) doesn't turn into an
    # equally long dead-air wait on every future replay.
    MAX_STEP_GAP_SECONDS = 5.0

    def play(self, name: str) -> str:
        events = load_macro(name)
        if events is None:
            return f"No macro named \"{name}\" is saved."
        if not events:
            return f"Macro \"{name}\" has no recorded steps."

        import app_control

        if not app_control._load_pywinauto():
            return "Cursor control isn't available on this system (pywinauto/UI Automation required)."

        from pywinauto.mouse import click as mouse_click
        from pywinauto.keyboard import send_keys
        from app_control import capture_identity_at_point, escape_for_send_keys

        prev_t = 0.0
        for i, ev in enumerate(events):
            gap = min(max(ev.get("t", prev_t) - prev_t, 0.0), self.MAX_STEP_GAP_SECONDS)
            if gap > 0:
                time.sleep(gap)
            prev_t = ev.get("t", prev_t)

            if ev["type"] == "click":
                x, y = ev["x"], ev["y"]
                recorded_identity = ev.get("identity")
                if recorded_identity:
                    current_identity = capture_identity_at_point(x, y)
                    if current_identity != recorded_identity:
                        return (
                            f"Macro \"{name}\" stopped at step {i + 1}: what's on screen "
                            f"there doesn't match what I recorded (expected "
                            f"\"{recorded_identity.get('name', '?')}\"). Not clicking blind -- "
                            f"you may need to re-record this step."
                        )
                button = "right" if "right" in ev.get("button", "") else "left"
                try:
                    mouse_click(button=button, coords=(x, y))
                except Exception as e:
                    return f"Macro \"{name}\" stopped at step {i + 1}: click failed: {e}"

            elif ev["type"] == "key":
                raw_key = ev.get("key", "")
                try:
                    if raw_key in _PYNPUT_KEY_TO_SENDKEYS:
                        send_keys(_PYNPUT_KEY_TO_SENDKEYS[raw_key])
                    elif len(raw_key) == 1:
                        send_keys(escape_for_send_keys(raw_key))
                    # else: unrecognized special key -- skip rather than
                    # guess, see _PYNPUT_KEY_TO_SENDKEYS's own comment.
                except Exception as e:
                    return f"Macro \"{name}\" stopped at step {i + 1}: key press failed: {e}"

        return f"Macro \"{name}\" finished ({len(events)} step(s))."
