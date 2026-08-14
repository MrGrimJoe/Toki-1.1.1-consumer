"""
display_strategy.py -- classifies a completed turn into one of three
display strategies (DONE / INFO / ERROR), so the widget can render each
one appropriately: a quick fading note for DONE, a persistent "island"
card for INFO/ERROR that stays until the person actually reads it.

WHY THIS EXISTS
---------------
Before this module, every turn's response text -- whether it was "Done."
or a full directory listing or a scheduling confirmation or an outright
failure -- went through the exact same 3-second auto-fading reply bubble
in toki_desktop_mark.py. That's fine for "Done.", but wrong for anything
the person actually needs to read: a `LIST_FILES` result, a clarifying
question, a confirmation prompt for a destructive command, an error
message. All of those could silently vanish before being read.

THE MAPPING
-----------
INTENT_DISPLAY_MAP below is a manual classification of every real intent
in this codebase (all 80 entries across intents.py / intents_extended.py
/ intents_app_control.py, plus GENERATE_FILE, which orchestrator.py
registers at runtime rather than in those files -- see orchestrator.py's
comment right above `INTENTS["GENERATE_FILE"] = {...}` for why). Verified
complete and exact against the live intent set via
tests/test_display_strategy.py's
test_map_covers_every_real_intent_exactly -- if a new intent is ever
added to any of those three files without updating this map, that test
fails loudly instead of the new intent silently defaulting.

Classification principle: does the person need to READ the result, or
just get a quick confirmation it happened?
  - DONE:  the request was an action/mutation -- create, delete, move,
    launch, click, lock, schedule, mute, etc. There's nothing to read
    beyond "it happened."
  - INFO:  the request was for information, or the response IS the
    actual content the person asked for (a listing, a reading, a
    question they need to answer, generated file content). These must
    not auto-vanish before being read.

classify_display() below is the actual per-turn decision function --
it combines this static intent map with orchestrator.py's runtime
`kind` field (since `kind` alone can't distinguish DONE from INFO --
e.g. MAKE_FOLDER and LIST_FILES are both kind="powershell" -- but IS
needed for cases the static map can't cover: PowerShell's real dispatch
result streams in asynchronously via on_output/on_done, entirely
separate from the synchronous "Done." placeholder that
process_request() itself returns for kind="powershell" -- see that
function's docstring for why).
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Tuple


class DisplayStrategy(Enum):
    DONE = "done"
    INFO = "info"
    ERROR = "error"


# ─── ACTIONS (DONE) ──────────────────────────────────────────────────────
# ─── INFORMATION (INFO) ──────────────────────────────────────────────────
INTENT_DISPLAY_MAP = {
    # ── Filesystem: mutations ──
    "MAKE_FOLDER": DisplayStrategy.DONE,
    "MAKE_FILE": DisplayStrategy.DONE,
    "DELETE_ITEM": DisplayStrategy.DONE,
    "RENAME_ITEM": DisplayStrategy.DONE,
    "MOVE_ITEM": DisplayStrategy.DONE,
    "COPY_ITEM": DisplayStrategy.DONE,
    "OPEN_ITEM": DisplayStrategy.DONE,
    "SORT_FOLDER_BY_TYPE": DisplayStrategy.DONE,
    "ORGANIZE_FILES_BY_TOPIC": DisplayStrategy.DONE,
    "GROUP_FILES_BY_EXTENSION": DisplayStrategy.DONE,
    "EXPORT_FOLDER_LISTING_CSV": DisplayStrategy.DONE,
    "EMPTY_RECYCLE_BIN": DisplayStrategy.DONE,

    # ── Filesystem: reads ──
    "LIST_FILES": DisplayStrategy.INFO,
    "FIND_FILES": DisplayStrategy.INFO,
    "READ_FILE": DisplayStrategy.INFO,
    "PATH_EXISTS": DisplayStrategy.INFO,
    "ITEM_PROPERTIES": DisplayStrategy.INFO,
    "CURRENT_LOCATION": DisplayStrategy.INFO,
    "RESOLVE_PATH": DisplayStrategy.INFO,
    "SPLIT_PATH": DisplayStrategy.INFO,
    "COUNT_FILES": DisplayStrategy.INFO,
    "COUNT_FOLDERS": DisplayStrategy.INFO,
    "FILE_TYPE_BREAKDOWN": DisplayStrategy.INFO,
    "FIND_DUPLICATE_FILES": DisplayStrategy.INFO,
    "FIND_FILES_BY_CONTENT": DisplayStrategy.INFO,
    "DISK_USAGE": DisplayStrategy.INFO,

    # ── Clipboard ──
    "GET_CLIPBOARD": DisplayStrategy.INFO,
    "SET_CLIPBOARD": DisplayStrategy.DONE,

    # ── Processes / services ──
    "PROCESS_LIST": DisplayStrategy.INFO,
    "KILL_PROCESS": DisplayStrategy.DONE,
    "WAIT_FOR_PROCESS": DisplayStrategy.DONE,
    "FIND_PROCESS": DisplayStrategy.INFO,
    "TOP_PROCESSES_BY_CPU": DisplayStrategy.INFO,
    "OPEN_TASK_MANAGER": DisplayStrategy.DONE,
    "FIND_SERVICE": DisplayStrategy.INFO,

    # ── System info ──
    "SYSTEM_INFO": DisplayStrategy.INFO,
    "NETWORK_INFO": DisplayStrategy.INFO,
    "CURRENT_USER": DisplayStrategy.INFO,
    "SYSTEM_UPTIME": DisplayStrategy.INFO,
    "HOSTNAME": DisplayStrategy.INFO,
    "SYSTEM_LOCALE": DisplayStrategy.INFO,
    "LIST_USB_DEVICES": DisplayStrategy.INFO,
    "TEMPERATURE_SENSORS": DisplayStrategy.INFO,
    "BATTERY_STATUS": DisplayStrategy.INFO,
    "LIST_PRINTERS": DisplayStrategy.INFO,
    "LIST_SCHEDULED_TASKS": DisplayStrategy.INFO,

    # ── Time / date / weather / web / location ──
    "GET_TIME": DisplayStrategy.INFO,
    "GET_DATE": DisplayStrategy.INFO,
    "GET_WEATHER": DisplayStrategy.INFO,
    "GET_FORECAST": DisplayStrategy.INFO,
    "SEARCH_WEB": DisplayStrategy.INFO,
    "GET_LOCATION": DisplayStrategy.INFO,

    # ── Scheduling ──
    "SCHEDULE_COMMAND": DisplayStrategy.DONE,
    "SET_TIMER": DisplayStrategy.DONE,
    "CANCEL_SCHEDULED": DisplayStrategy.DONE,
    "CONDITIONAL_COMMAND": DisplayStrategy.DONE,

    # ── Selected-file operations ──
    "CONVERT_SELECTED_FILE": DisplayStrategy.DONE,
    "RESIZE_SELECTED_FILE": DisplayStrategy.DONE,
    "COMPRESS_SELECTED_FILE": DisplayStrategy.DONE,
    "EXTRACT_SELECTED_FILE": DisplayStrategy.DONE,

    # ── Video download ──
    "DOWNLOAD_PLAYING_VIDEO": DisplayStrategy.DONE,
    "DOWNLOAD_VIDEO_URL": DisplayStrategy.DONE,

    # ── Volume / power / lock ──
    "TOGGLE_MUTE": DisplayStrategy.DONE,
    "VOLUME_UP": DisplayStrategy.DONE,
    "VOLUME_DOWN": DisplayStrategy.DONE,
    "LOCK_WORKSTATION": DisplayStrategy.DONE,
    "TAKE_SCREENSHOT": DisplayStrategy.DONE,

    # ── App control / macros / dictation ──
    "LAUNCH_APP": DisplayStrategy.DONE,
    "CLICK_ELEMENT": DisplayStrategy.DONE,
    "DOUBLE_CLICK_ELEMENT": DisplayStrategy.DONE,
    "RIGHT_CLICK_ELEMENT": DisplayStrategy.DONE,
    "TYPE_INTO_ELEMENT": DisplayStrategy.DONE,
    "LIST_INSTALLED_APPS": DisplayStrategy.INFO,
    "START_SEEING": DisplayStrategy.DONE,
    "STOP_SEEING": DisplayStrategy.DONE,
    "RUN_MACRO": DisplayStrategy.DONE,
    "START_LISTENING": DisplayStrategy.DONE,
    "STOP_LISTENING": DisplayStrategy.DONE,

    # ── Chat / generation ──
    "CHAT": DisplayStrategy.INFO,
    "ASK_CONTEXT": DisplayStrategy.INFO,
    "GENERATE_FILE": DisplayStrategy.INFO,

    # ── Plugins ──
    # PLUGIN_HELLO is plugins/example_plugin's demo intent (kind="chat",
    # just echoes a greeting) -- classified the same as CHAT itself.
    # Plugin-defined intents aren't in intents.py/intents_extended.py/
    # intents_app_control.py, so test_map_covers_every_real_intent_exactly
    # (which diffs this map against orchestrator.INTENT_NAMES, the true
    # merged set including plugin injections) is what actually catches a
    # real plugin's intent going unclassified, same as it would for a
    # new core intent.
    "PLUGIN_HELLO": DisplayStrategy.INFO,
}


# ─── kind-based fallback (used only when `intent` is missing or unmapped,
# e.g. an internal "missing a detail" chat note, or a brand-new intent
# added to one of the *.py files but not yet classified above -- see
# test_map_covers_every_real_intent_exactly, which is what actually
# prevents that second case in practice). Deliberately biased toward
# INFO on anything uncertain: a DONE default risks truncating real
# content into a 1.5s fade note, which loses information; an INFO
# default in the worst case just shows a short "Done."-style string in
# the persistent card instead of the quick one -- annoying, never lossy.
_KIND_FALLBACK = {
    "chat": DisplayStrategy.INFO,
    "schedule": DisplayStrategy.DONE,
    "timer": DisplayStrategy.DONE,
    "cancel_scheduled": DisplayStrategy.DONE,
    "generate": DisplayStrategy.INFO,
    "api": DisplayStrategy.INFO,
    "app_control": DisplayStrategy.DONE,
    "powershell": DisplayStrategy.INFO,
}

# apis.py's dispatcher prefixes a failed API result with this exact
# string (see orchestrator.py's `if is_api_failure(result): thinking_text
# = f"Hmm, that didn't work. {result}"` in the kind=="api" branch) --
# checked as a plain string prefix here rather than re-importing
# apis.is_api_failure, to keep this module decoupled from apis.py (it
# only needs orchestrator.py's already-decided text, not to re-run the
# failure classification itself).
_API_FAILURE_PREFIX = "Hmm, that didn't work."


def classify_display(
    result: Optional[dict],
    collected_output: str = "",
    exit_code: Optional[int] = None,
    timed_out: bool = False,
) -> Tuple[DisplayStrategy, str]:
    """
    Decide how a completed turn should be displayed, and the exact text
    to show.

    `result` is process_request()'s own return value.

    `collected_output` / `exit_code` / `timed_out` are ONLY relevant for
    kind == "powershell": process_request() starts that command on its
    own background thread and returns almost immediately with a
    placeholder `{"response": "Done.", ...}` -- the real stdout/exit code
    arrive later via the on_output/on_done callbacks passed into
    process_request() (see executor.py's RunningCommand). The caller is
    responsible for collecting on_output lines and waiting for on_done
    before calling this function for a powershell-kind result; every
    other kind is already fully resolved by the time process_request()
    returns, so these three parameters simply don't apply to them.

    `timed_out=True` means the caller gave up waiting for on_done within
    its own bound (main_widget.py's _POWERSHELL_RESULT_TIMEOUT_S) --
    NOT that the command failed or finished. The command is very likely
    still running in the background regardless (this function has no way
    to cancel it, and isn't trying to). This is deliberately its own
    parameter rather than being folded into `exit_code=None` -- an
    unset/None exit_code on its own is indistinguishable from "the
    caller simply didn't pass one", and treating that as silently
    equivalent to a successful 0-exit (which earlier versions of this
    function did) meant a slow command's incomplete, still-arriving
    output could get shown as if it were the final, complete result --
    including, for a DONE-classified intent like COMPRESS_SELECTED_FILE,
    showing a bare "Done." while the operation was, in fact, still
    running. `timed_out` overrides the normal DONE/INFO split entirely
    (there is now something the person genuinely needs to read: this
    isn't finished yet) and is checked before the exit_code check, since
    a timeout means exit_code is still unknown, not confirmed-successful.
    """
    if not result:
        return DisplayStrategy.ERROR, "Something went wrong: no response."

    if "error" in result and not result.get("response"):
        return DisplayStrategy.ERROR, str(result["error"])

    kind = result.get("kind")
    intent = result.get("intent")
    base_text = result.get("response") or ""

    if kind == "powershell":
        if timed_out:
            partial = collected_output.strip()
            note = "Still running -- taking longer than expected. Showing what's come back so far:"
            text = f"{note}\n\n{partial}" if partial else f"{note} (nothing yet)"
            return DisplayStrategy.INFO, text
        if exit_code not in (None, 0):
            # base_text is never useful here -- process_request() always
            # returns the literal "Done." placeholder synchronously for
            # every powershell-kind intent, success or failure alike (see
            # this module's docstring). Only collected_output (the real
            # stdout/stderr) or the generic exit-code message are
            # meaningful fallbacks.
            text = collected_output.strip() or f"Command failed (exit code {exit_code})."
            return DisplayStrategy.ERROR, text
        strategy = INTENT_DISPLAY_MAP.get(intent, _KIND_FALLBACK["powershell"])
        if strategy == DisplayStrategy.INFO:
            # Same reasoning: base_text is just "Done." here too, never
            # the real listing/reading/etc. -- don't fall back to it.
            text = collected_output.strip() or "(no output)"
            return DisplayStrategy.INFO, text
        return DisplayStrategy.DONE, base_text or "Done."

    if kind == "api":
        if base_text.startswith(_API_FAILURE_PREFIX):
            return DisplayStrategy.ERROR, base_text
        strategy = INTENT_DISPLAY_MAP.get(intent, _KIND_FALLBACK["api"])
        return strategy, base_text or "(no result)"

    if kind in _KIND_FALLBACK:
        strategy = INTENT_DISPLAY_MAP.get(intent, _KIND_FALLBACK[kind])
        return strategy, base_text or ("Done." if strategy == DisplayStrategy.DONE else "(no response)")

    # Unknown kind entirely (shouldn't happen against a real
    # orchestrator, but a stray/renamed kind must still show something
    # rather than silently dropping the turn).
    strategy = INTENT_DISPLAY_MAP.get(intent, DisplayStrategy.INFO)
    return strategy, base_text or "(no response)"
