"""
intents_app_control.py — the APP_CONTROL category's intents.

Kept separate from intents.py/intents_extended.py because these have a
different "kind" than template/api/generate: dispatch goes to app_control.py,
not template.format() or a fixed API method. Same shape contract as
everywhere else (description, kind, slots), so the two-tier classifier and
process_request() treat it uniformly -- see orchestrator.py's dispatch for
kind == "app_control".
"""

from typing import Dict, Any

INTENTS_APP_CONTROL: Dict[str, Dict[str, Any]] = {
    "LAUNCH_APP": {
        "description": "Open/launch a named application (e.g. Notepad, Chrome, Calculator)",
        "kind": "app_control",
        "action": "launch_app",
        "slots": ["app_name"],
        "reversible": False,
    },
    "CLICK_ELEMENT": {
        "description": "Click a named button, link, or UI element in the currently focused window",
        "kind": "app_control",
        "action": "click",
        "slots": ["target_description"],
        "reversible": False,
    },
    "DOUBLE_CLICK_ELEMENT": {
        "description": "Double-click a named button, icon, or UI element in the currently focused window",
        "kind": "app_control",
        "action": "click",
        "extra_args": {"double": True},
        "slots": ["target_description"],
        "reversible": False,
    },
    "RIGHT_CLICK_ELEMENT": {
        "description": "Right-click a named button, icon, or UI element in the currently focused window",
        "kind": "app_control",
        "action": "click",
        "extra_args": {"right": True},
        "slots": ["target_description"],
        "reversible": False,
    },
    "TYPE_INTO_ELEMENT": {
        "description": "Click a named text field/box in the focused window and type given text into it",
        "kind": "app_control",
        "action": "type_text",
        "slots": ["target_description", "text"],
        "reversible": False,
    },
    "LIST_INSTALLED_APPS": {
        "description": "List the applications installed/available on this computer",
        "kind": "app_control",
        "action": "list_installed_apps",
        "slots": [],
        "reversible": False,
    },
    "START_SEEING": {
        "description": "Start recording every click and keypress the user makes by hand, to save as a named replayable macro",
        "kind": "app_control",
        "action": "start_seeing",
        "slots": [],
        "reversible": False,
    },
    "STOP_SEEING": {
        "description": "Stop the current macro recording and save it under a name the user gives",
        "kind": "app_control",
        "action": "stop_seeing_and_save",
        "slots": ["macro_name"],
        "reversible": False,
    },
    "RUN_MACRO": {
        # Reached ONLY via orchestrator.py's single-bare-word pre-check
        # (see macro_recorder.py's module docstring, safety property 2) --
        # never through graph classification, so this never competes for
        # graph vocabulary and doesn't need Tier A phrasings at all.
        "description": "Replay a previously recorded macro by name",
        "kind": "app_control",
        "action": "run_macro",
        "slots": ["macro_name"],
        "reversible": False,
    },
}

INTENTS_APP_CONTROL_NAMES = list(INTENTS_APP_CONTROL.keys())
