"""
orchestrator.py — TOKI brain, v2.4 (sequential decide-then-narrate + two-tier
classification + missing-slot follow-up).

Per-turn call order (SEQUENTIAL now, not parallel -- see v2.3->v2.4 note below):
  1. Tier 1 -- schema-constrained, NOT streamed: model picks ONE of ~7
     CATEGORIES (categories.py). Tiny, fixed-size grammar -- doesn't grow no
     matter how many commands exist in the database.
  2. Tier 2 -- schema-constrained, NOT streamed, SKIPPED entirely if tier 1
     is CHAT: model picks ONE command from JUST that category's commands.
     Grammar is scoped to that category, not the whole database.
  3. extract_slots() -- plain Python regex against the user's own raw text.
     If a required slot can't be found, we do NOT guess and we do NOT ask
     the model to invent one: a fixed, Python-authored follow-up question is
     asked (extractor.MISSING_SLOT_QUESTIONS) and the turn pauses in
     self._pending until the user answers directly.
  4. stream_thinking() -- a plain FREE-TEXT call (no schema), streamed token
     by token, so the user sees a live sentence appear. This now runs AFTER
     1-3 resolve and is told what was actually decided, so it narrates a
     real decision instead of being an independent guess.

v2.3 -> v2.4 change, and why: thinking and classification used to run
CONCURRENTLY on separate threads, sharing nothing -- thinking gave a
generic "here's roughly what I understood" gloss while classify() made the
real decision completely independently. That's exactly why it could read
like two unrelated replies in the same bubble, and why nothing stopped
thinking from drifting off in its own direction. Making the calls
sequential and handing the resolved decision to stream_thinking() as
context fixes that structurally: thinking is now a genuine post-hoc
rationale of the decision that was just made, not a parallel guess that
might not even match it. The cost is real and worth naming: total latency
per turn goes up by however long tier-1+tier-2 classification takes before
the user sees anything stream, since thinking can no longer overlap with
it. That tradeoff was made deliberately in favor of coherence over raw
speed to first token.

Why thinking is still its own plain-text call, not folded back into the
classification JSON: Ollama can stream schema-constrained output, but the
JSON isn't valid/parseable until the whole object arrives -- you can't
safely pull a partial string out of a half-finished JSON blob. So thinking
stays a separate unconstrained call that streams cleanly, same as
generator.py's file generation.

Everything else is unchanged:
  - The model NEVER writes a command, invents a variable shape, or supplies
    an open-ended string value for any FILESYSTEM/PROCESS/SYSTEM/INFO/
    APP_CONTROL intent -- not directly, not via thinking text, not via any
    other free-text surface. Variables are filled by plain Python regex
    (extractor.py) straight from the user's own text, or by the user's
    direct answer to a fixed follow-up question when regex comes up empty.
  - GENERATE_FILE is the one deliberate exception: real free-text
    generation, handled entirely by generator.py, never touching a
    PowerShell template. See generator.py's docstring for why that's safe
    despite being genuine generation. (Currently not wired into
    process_request()'s category set while the core loop is being verified
    -- see categories.py.)
  - Everything executes immediately -- no confirmation dialogs. Safety
    comes from the sandbox and the Stop button, not permission dialogs.
"""

import json
import ntpath
import re
import requests
import threading
import time
from typing import Dict, Any, Optional, Callable, List

from intents import INTENTS as _BASE_INTENTS
from intents_extended import INTENTS_EXTENDED
from intents_app_control import INTENTS_APP_CONTROL
from categories import (
    CATEGORIES, CATEGORY_NAMES, INTENT_CATEGORY,
    commands_in_category, validate_category_map,
)
from extractor import (
    extract_slots, resolve_missing_slot, resolve_open_target, has_explicit_open_convention,
    MISSING_SLOT_QUESTIONS, GENERATE_FILE_SKIP_NAME_ANSWERS, file_index,
    is_anaphoric_reference, resolve_anaphoric_target, ANAPHORA_ELIGIBLE_INTENTS,
    find_time_expression, looks_conditional, looks_like_cancel_scheduled, format_delay,
    looks_like_bare_timer,
    looks_like_start_seeing, looks_like_stop_seeing,
    looks_like_start_listening, looks_like_stop_listening,
    looks_like_function_creation,
    looks_like_ambiguous_start_recording, looks_like_ambiguous_stop_recording,
    canned_reply, _is_wcl_code_like_var, _strip_answer_filler,
)
from apis import WeatherAPI, WebSearchAPI, TimeAPI, LocationAPI, FileConvertAPI, VideoDownloadAPI, FileOrganizerAPI, FileGroupingAPI, is_api_failure, location_cache
from executor import RunningCommand
from generator import FileGenerator, extract_explicit_name
from app_control import AppController
import foreground_tracker
from graph_router import GraphRouter
from wcl_resolver import WCLResolver
from tier_a_wcl_map import is_equivalent
from vocab_staging import log_graph_ask, confirm_graph_ask, reject_graph_ask
from scheduler import ScheduledCommandManager, SchedulerFullError
from condition_checker import ConditionPoller, match_condition, CHECKABLE_CONDITIONS_SUMMARY
from plugin_manager import plugin_manager as _plugin_manager


_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _ensure_quoted_placeholders(template: str) -> str:
    """BETA 0.3.37 fix -- CRITICAL, found while scoping the 2-variable
    WCL slot-filler extension: _escape_ps_slot() above only protects a
    slot value if the TEMPLATE ITSELF already wraps {var} in matching
    single quotes ('...') -- its own docstring says so explicitly
    ("Every powershell-kind template ... wraps its slots in single
    quotes"). That's true for TOKI's own 62 hand-written intents (98%
    of them; confirmed the 2 exceptions are both actually benign on
    inspection -- see STATUS.md), but FALSE for nearly all of the
    windows_command_library-sourced ("WCL_"-prefixed) templates:
    confirmed live, 297 of 298 currently-eligible "safe" single-variable
    WCL commands have a completely UNQUOTED {var} in their syntax
    string (e.g. "Get-Content -Path {path}", "subst {drive_letter}
    {path}", "Start-Job -ScriptBlock {script_block}"). Compounding this:
    _looks_like_real_name() (extractor.py) -- the plausibility gate for
    non-path slot values -- does ZERO character-level filtering; it only
    checks length/word-count/pronoun/TOKI's-own-verb-vocabulary, nothing
    about ';', backticks, '$(...)', etc. A value like "pwned; Remove-Item
    -Recurse -Force D:\\" passes that check cleanly. Combined, this meant
    an ordinary phrase could have built and RUN a real multi-statement
    PowerShell command with this app's own privileges, for the vast
    majority of the already-shipped "safe" single-variable WCL auto-
    dispatch feature (BETA 0.3.15) -- not a theoretical gap, an active one.

    Fix, applied here rather than by rewriting ~1,200 WCL syntax strings
    by hand (infeasible to do correctly and completely in one pass, and
    a single missed template would silently reopen this): scan the
    template LEFT TO RIGHT tracking PowerShell single-quote ('...')
    state exactly the way PowerShell's own parser would, and wrap any
    {var} placeholder found OUTSIDE a single-quoted region in its own
    single quotes -- so _dispatch()'s later .format(**safe_slots) call
    always lands every value inside a fully-literal '...' string,
    regardless of what the source template did or didn't do itself.

    Deliberately conservative about what counts as "already safe":
      - A placeholder immediately adjacent to matching quotes
        ("-Path '{path}'") is left alone -- already correct.
      - A placeholder WITHIN a single-quoted span but not immediately
        adjacent to the quote characters ("'*{query}*'", one of TOKI's
        own 2 exceptions above) is ALSO left alone -- the surrounding
        '...' still makes the whole substitution a single literal
        string; inserting more quotes here would BREAK it, not protect
        it further. This needs actual quote-state tracking, not just a
        regex checking immediate adjacency, precisely to get this case
        right without breaking it.
      - A "..." (double-quoted) region is NOT treated as already safe --
        PowerShell still expands $(...) and $variables inside a
        double-quoted string, so a placeholder inside one still gets
        wrapped in single quotes too (accepted, minor, deliberate
        cosmetic trade-off: e.g. eventcreate's `/D "{message}"` becomes
        `/D "'value'"` -- the external command receives literal
        single-quote characters as part of the string, which is safe,
        just not perfectly clean output, versus the alternative of
        leaving a real injection vector open).
      - A placeholder outside ANY quote at all gets wrapped -- the
        common case (297/298 above).

    Applied universally to every powershell-kind template (TOKI's own
    62 intents too, not just WCL_-prefixed ones) -- harmless for a
    template that's already fully quoted (scanner correctly leaves it
    unchanged, per the two cases above) and closes the 1 real gap found
    in TOKI's own set (TOP_PROCESSES_BY_CPU's `count`) for free, with no
    separate special-casing needed.
    """
    result = []
    in_single_quote = False
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if ch == "'":
            in_single_quote = not in_single_quote
            result.append(ch)
            i += 1
            continue
        if ch == "{":
            m = _PLACEHOLDER_RE.match(template, i)
            if m:
                placeholder = m.group(0)
                result.append(placeholder if in_single_quote else "'" + placeholder + "'")
                i += len(placeholder)
                continue
        result.append(ch)
        i += 1
    return "".join(result)


def _escape_ps_slot(value: Any) -> Any:
    """Doubles any single quote in a slot value before it goes into a
    PowerShell single-quoted string literal ('...') -- PowerShell's own
    escaping convention for a literal ' inside '...' (same idea as SQL's
    '' for an embedded '). Every powershell-kind template in intents.py /
    intents_extended.py wraps its slots in single quotes (e.g.
    "New-Item -Path '{path}' ..."), and single-quoted PowerShell strings
    are fully literal -- no variable expansion, no backtick escapes, no
    subexpression evaluation -- so doubling the one character that CAN
    terminate the string early is a complete fix for this class of
    problem, not a partial mitigation.

    Without this, a slot value containing a single quote closes the
    quoted argument early and everything after it (up to the next `;` or
    end of string) runs as a SEPARATE PowerShell statement with the same
    privileges as this app -- confirmed directly: a folder name typed as
    `"pwned' ; Remove-Item -Recurse -Force D:\\ ; New-Item ... 'x"` built
    a real multi-statement command, and a plain, non-malicious name like
    `O'Brien's Homework` broke New-Item's syntax outright (mismatched
    quotes), silently failing for a totally ordinary reason the user
    would never guess. Neither needs an attacker -- an apostrophe is
    enough.

    Applied to every slot value in ONE place (_dispatch(), immediately
    before every powershell-kind template is filled) rather than patched
    per-template, so no future powershell-kind intent can reintroduce
    this by skipping it. Non-string slot values pass through unchanged.

    BETA 0.3.37 addition -- also escapes backtick and '$'. Reason,
    confirmed against 3 REAL currently-eligible commands (eventcreate,
    find, findstr): _ensure_quoted_placeholders() (below) wraps an
    unquoted {var} in NEW single quotes, but if that placeholder was
    already sitting inside a PRE-EXISTING double-quoted ("...") region
    in the template (e.g. `/D "{message}"`), the result is single quotes
    NESTED inside double quotes -- and PowerShell parses the OUTER
    double-quoted string for `$(...)`/`$variable` expansion regardless
    of literal single-quote characters inside it; nesting alone does
    NOT stop that. Confirmed directly: a message value of
    "$(Remove-Item -Recurse -Force D:\\)" would still have been evaluated
    as a live subexpression even after single-quote wrapping. Escaping
    backtick and '$' here closes this regardless of which quote type the
    value ends up inside: single-quoted strings don't process backtick-
    escapes AT ALL (so this is inert, harmless there), while double-
    quoted strings do (so `$ becomes the literal PowerShell escape for
    "a literal dollar sign, not the start of an expansion" -- exactly
    the protection needed there). Order matters: backtick escaped FIRST,
    so a backtick introduced by the '$' step is never re-escaped.

    Accepted, deliberate trade-off: a legitimate value containing a
    literal '$' (rare -- e.g. a file named "budget$2024.xlsx") will show
    up with a literal backtick character in front of it even in the
    (safe either way) single-quoted case, since this function can't tell
    in advance which quote context a given value will land in. Chose
    safety over that cosmetic edge case, consistent with every other
    "ties go to rejecting/escaping" decision in this codebase.
    """
    if not isinstance(value, str):
        return value
    value = value.replace("`", "``")
    value = value.replace("$", "`$")
    value = value.replace("'", "''")
    return value


def _build_powershell_command(meta: Dict[str, Any], slots: Dict[str, str]) -> str:
    """BETA 0.3.38: factored out of _dispatch()'s powershell branch so the
    confirmation preview shown to the user (_ask_for_confirmation()) and
    the command that ACTUALLY runs once confirmed are guaranteed to be
    built by the exact same code, not two copies that could quietly drift
    apart. Raises KeyError/ValueError/IndexError exactly like the old
    inline version did -- callers still catch those the same way."""
    if meta.get("slots"):
        safe_slots = {k: _escape_ps_slot(v) for k, v in slots.items()}
        # BETA 0.3.37 fix: don't trust the template's own quoting -- see
        # _ensure_quoted_placeholders()'s docstring for exactly why this
        # line exists and what it closes.
        template = _ensure_quoted_placeholders(meta["template"])
        return template.format(**safe_slots)
    # No slots declared -- run the template verbatim. Skipping .format()
    # here matters for windows_command_library commands whose raw
    # PowerShell syntax contains literal {} (e.g.
    # @{Name=...;Expression={...}} calculated properties) -- .format()
    # would misparse those as format fields and raise, even though
    # nothing needs substituting.
    return meta["template"]

# Merge batch-1 database-sourced intents into the same dict the model
# classifies against -- additive only, same shape as the original.
INTENTS: Dict[str, Dict[str, Any]] = {**_BASE_INTENTS, **INTENTS_EXTENDED, **INTENTS_APP_CONTROL}

# ── Plugin intents ── load community plugins and merge their intents in.
# Plugins are graph-only (same as WCL_ commands) -- never LLM-reachable.
# Fails open: a broken plugin is logged and skipped, never crashes the app.
_plugin_manager.load_all()
INTENTS.update(_plugin_manager.intents)
_plugin_manager.fire_startup()

# GENERATE_FILE isn't a template/api/chat intent like the others -- it has
# its own "kind" so process_request() routes it to generator.py instead of
# extract_slots()/template.format(). Registered here (not in intents.py or
# intents_extended.py) since it has no template and no regex slot-filler --
# it would be misleading to list it alongside intents that DO follow the
# closed-vocabulary-value contract those files document.
INTENTS["GENERATE_FILE"] = {
    "description": "Write a brand-new file with specific generated content (a script, code, a document)",
    "kind": "generate",
    "slots": [],
    "reversible": True,  # the file itself, once written, is a normal deletable file
}
INTENT_NAMES: List[str] = list(INTENTS.keys())

# ─── WCL_COMMANDS: graph-router-only dispatch table ─────────────────────────
#
# The 284 windows_command_library commands with variables == [] -- no
# {slot} placeholders in their syntax, so they're directly runnable the
# moment graph_router.py matches one, no extract_slots() work needed.
# Keyed f"WCL_{original_id}" to match exactly what migrate_to_kuzu.py
# stores as the Command node's id (minus prefix-stripping, since
# graph_router._to_intent_name() only strips "A_", not "WCL_").
#
# Deliberately kept OUT of INTENTS: these were never meant to be picked by
# the LLM's tier-1/tier-2 classify() calls (categories.py has no mapping
# for them, and there isn't a WCL-scale category prompt to show anyway --
# see the two-tier-LLM design categories.py documents). Only graph_router
# can ever return a "WCL_..." intent; _intent_meta() below is what lets
# the rest of _process_single_request()/_dispatch() treat it exactly like
# any other powershell-kind intent once that happens.
# ─── WCL_COMMANDS: graph-router-only dispatch cache ─────────────────────────
#
# Used to be pre-scanned from windows_command_library.json at import time
# (the 284 zero-variable commands). Now populated JUST-IN-TIME by
# _process_single_request() whenever wcl_resolver.resolve() returns a
# RESOLVED, zero-variable command -- see wcl_resolver.py's module
# docstring for why matching moved to its own dedicated graph. Same
# contract as before: _intent_meta() checks this dict first, and nothing
# in here is ever visible to the LLM's classify() (categories.py has no
# mapping for "WCL_..." keys, same as always).
WCL_COMMANDS: Dict[str, Dict[str, Any]] = {}


def _intent_meta(intent: str) -> Dict[str, Any]:
    """Looks up intent metadata from whichever table actually has it.
    WCL_-prefixed intents only ever come from graph_router.py (never from
    the LLM router or a //override), so checking WCL_COMMANDS first is
    always safe and never masks a real INTENTS lookup."""
    if intent in WCL_COMMANDS:
        return WCL_COMMANDS[intent]
    return INTENTS[intent]


def pill_category(intent: str) -> str:
    """What app.py's intent pill should show as the CATEGORY half of
    "CATEGORY -> COMMAND". categories.py's INTENT_CATEGORY only ever maps
    TOKI's own 3 LLM-reachable intents (CHAT/GENERATE/ASK_CONTEXT) by
    design (see its module docstring) -- every WCL_ intent is registered
    at runtime in WCL_COMMANDS instead, carrying the windows_command_
    library's own 26-bucket category field (network, hyperv_vm,
    disk_storage, ...), which used to go completely unsurfaced (see
    STATUS.md's "category taxonomy mismatch" item). This is the single
    place that knows to check both tables, so app.py doesn't need to
    import WCL_COMMANDS directly or duplicate this fallback."""
    if intent in WCL_COMMANDS:
        return WCL_COMMANDS[intent].get("wcl_category", "")
    return INTENT_CATEGORY.get(intent, "")

# ─── Direct command override ("//weather", "//MAKE_FOLDER \"Homework\"") ───
#
# A message starting with "//" plus a known command name skips BOTH
# classification calls (tier-1 category, tier-2 command) entirely and goes
# straight to extract_slots() with that intent already decided. Two real
# wins, not just one: it's faster (2 fewer Ollama round-trips per turn),
# and it's fully deterministic for whatever the user explicitly names --
# there's no classification step left to misfire on it. Slot extraction
# (extract_slots) still runs exactly as normal on whatever text follows the
# override token, so "" / '' literal-quoting still works the same way here
# as anywhere else.
#
# Curated short aliases below cover the common cases worth a one-word
# trigger; ANY real intent name also works verbatim and case-insensitively
# (e.g. "//MAKE_FOLDER \"Homework\"", "//get_weather Lahore") as a fallback,
# so this isn't limited to only the aliased ones.
_COMMAND_ALIASES: Dict[str, str] = {
    "weather": "GET_WEATHER",
    "forecast": "GET_FORECAST",
    "time": "GET_TIME",
    "date": "GET_DATE",
    "location": "GET_LOCATION",
    "search": "SEARCH_WEB",
    "clipboard": "GET_CLIPBOARD",
    "battery": "BATTERY_STATUS",
    "uptime": "SYSTEM_UPTIME",
    "sysinfo": "SYSTEM_INFO",
    "screenshot": "TAKE_SCREENSHOT",
    "lock": "LOCK_WORKSTATION",
    "mute": "TOGGLE_MUTE",
    "hostname": "HOSTNAME",
    "user": "CURRENT_USER",
    "processes": "PROCESS_LIST",
    "taskmgr": "OPEN_TASK_MANAGER",
    "network": "NETWORK_INFO",
    "open": "OPEN_ITEM",
    "launch": "LAUNCH_APP",
    "run": "LAUNCH_APP",
}

_COMMAND_OVERRIDE_RE = re.compile(r"^//(\S+)\s*(.*)$", re.DOTALL)


def parse_command_override(text: str) -> Optional[tuple]:
    """
    If text starts with "//<token>", returns (intent, remainder_text) when
    <token> matches a known alias or a real intent name (case-insensitive).
    Returns None otherwise -- including for an unrecognized "//something",
    which just falls through to normal classification untouched rather
    than erroring, so a mistyped override never breaks the turn.
    """
    m = _COMMAND_OVERRIDE_RE.match(text.strip())
    if not m:
        return None
    token, remainder = m.group(1), m.group(2).strip()
    intent = _COMMAND_ALIASES.get(token.lower()) or (
        token.upper() if token.upper() in INTENTS else None
    )
    return (intent, remainder) if intent else None


# Fail loudly at import time if a category mapping is missing/stale/empty,
# rather than failing confusingly deep inside a live classify() call.
validate_category_map(INTENT_NAMES)

# GENERATE is held back from the model's active choices for now -- per plan,
# we're confirming the core FILESYSTEM/PROCESS/SYSTEM/INFO/CHAT loop is solid
# before reintroducing file generation. The category, its command, and
# generator.py all still exist; this is the one line that re-enables it
# later (just remove it from this exclusion set).
_DISABLED_CATEGORIES: set = set()  # GENERATE re-enabled -- see app.py's on_generate_token/on_generate_done wiring added alongside this change
ACTIVE_CATEGORY_NAMES: List[str] = [c for c in CATEGORY_NAMES if c not in _DISABLED_CATEGORIES]


# ─── Prompts ──────────────────────────────────────────────────────────────────

def _build_category_prompt() -> str:
    lines = [
        "You are TOKI, a Windows assistant. Decide which ONE category best",
        "matches what the user wants.",
        "",
        "Available categories:",
    ]
    for name in ACTIVE_CATEGORY_NAMES:
        lines.append(f"  {name}: {CATEGORIES[name]}")
    lines += [
        "",
        "Rules:",
        "- 'category' must be exactly one of the names above, nothing else.",
        "- Casual conversation, greetings ('hey', 'what's up', 'thanks') and anything",
        "  that isn't a clear request for a fact or action -> CHAT. When unsure, use CHAT.",
        "- If the message clearly wants something done or looked up but is missing a key",
        "  detail (what/which/where) -- e.g. 'delete it', 'find the file', 'search for",
        "  that' -- use ASK_CONTEXT, not CHAT. ASK_CONTEXT is for a specific missing",
        "  detail blocking a real request; CHAT is for messages with no request at all.",
        "- Most requests that sound like a specific computer action (files, processes,",
        "  system settings, weather/time/search, opening apps, etc.) are handled by a",
        "  separate matcher BEFORE you ever see this message -- you're only seeing it",
        "  because that matcher didn't find a confident match. So if the message reads",
        "  like a real action/lookup request but doesn't fit CHAT, prefer ASK_CONTEXT",
        "  over guessing GENERATE just because a word in it (e.g. a language or file",
        "  type name) sounds generation-related -- GENERATE is ONLY for requests to",
        "  actually write new text/code content, not for naming/creating things.",
        "- If the message is short and depends on what was just said (starts with 'but', ",
        "  'and', 'what about...', or uses a pronoun like 'it'/'that'/'them' with no named ",
        "  topic of its own), look at the conversation history above and treat it as a ",
        "  continuation of THAT topic, not a new unrelated request -- e.g. a follow-up ",
        "  question about something you just looked up stays in the same category as that ",
        "  lookup, it does not jump to an unrelated one like weather just because no topic ",
        "  is named in the follow-up itself.",
    ]
    return "\n".join(lines)



def _build_command_prompt(category: str) -> str:
    names_in_cat = commands_in_category(category)
    lines = [
        f"You are TOKI. The user's request falls under {category}:",
        f"  {CATEGORIES[category]}",
        "",
        "Decide which ONE specific command best matches what they want.",
        "",
        "Available commands:",
    ]
    for name in names_in_cat:
        lines.append(f"  {name}: {INTENTS[name]['description']}")
    lines += [
        "",
        "Rules:",
        "- 'command' must be exactly one of the names above, nothing else.",
        "- If the message is a short follow-up referring back to something without naming ",
        "  a new topic (e.g. 'but who founded it', 'when did that happen'), check the ",
        "  conversation history above for what it's following up on, and pick the command ",
        "  that continues THAT same thread rather than the one that best matches the bare ",
        "  words alone.",
    ]
    return "\n".join(lines)




# Schemas now hold ONLY the classification field itself. 'thinking' used to
# live in here too, but a schema-constrained call can't be usefully streamed
# (the JSON isn't parseable until the whole object arrives), so thinking is
# now its own separate free-text streamed call -- see stream_thinking() below.
_CATEGORY_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ACTIVE_CATEGORY_NAMES},
    },
    "required": ["category"],
}


def _command_schema(category: str) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "command": {"type": "string", "enum": commands_in_category(category)},
        },
        "required": ["command"],
    }


# Human-friendly labels for slot keys, used only to build narration
# context (never shown to PowerShell/the API layer -- this is purely
# about what the narration model is told). "path"/"dest" are shown as
# their basename, not the full "C:\Users\...\Desktop\" prefix -- that's
# what a person would actually say out loud, and it keeps the sentence
# short per _build_thinking_system_prompt()'s own instruction.
_SLOT_NARRATION_LABELS = {
    "path": "name", "dest": "destination", "new_name": "new name",
    "process": "process name", "process_name": "process name",
    "app_name": "app name", "service_name": "service name",
    "query": "search term", "pattern": "text to find", "value": "value",
    "city": "city", "target_description": "target", "text": "text to type",
}


_NARRATION_SLOT_ORDER = (
    "new_name", "process", "process_name", "app_name", "service_name",
    "query", "pattern", "value", "city", "target_description", "text",
    "path", "dest",
)


def _narration_values(slots: Dict[str, Any]) -> List[str]:
    """The real, already-resolved display value(s) worth narrating, most
    narration-worthy first (a rename's NEW name before its old one; a
    path's basename, not the full 'C:\\Users\\...\\Desktop\\' prefix --
    what a person would actually say out loud). Shared by
    _decision_context() (what the model is TOLD), _is_narration_grounded()
    (what a valid answer must CONTAIN), and _fallback_narration() (what
    the guaranteed-correct backup sentence NAMES) so all three agree on
    what "the real value" means for a given dispatch."""
    values = []
    for key in _NARRATION_SLOT_ORDER:
        val = slots.get(key)
        if not val:
            continue
        val = str(val)
        if key in ("path", "dest"):
            val = ntpath.basename(val.rstrip("\\")) or val
        if val and val not in values:
            values.append(val)
    return values


def _decision_context(meta: Dict[str, Any], slots: Dict[str, Any]) -> str:
    """
    Bug this fixes: narration for every powershell/app_control-kind
    dispatch (the bulk of TOKI's intents -- everything except the 6 INFO/
    api intents, which already had their own grounding fix) was built
    from ONLY the intent's static description ("Create a new empty
    file."), with NO mention of the actual slot values extract_slots()
    already resolved before this ever runs. The model then had to
    independently re-derive specifics (a filename, a folder name) by
    re-reading the raw user message itself -- unreliably: confirmed live,
    "Create a file nmed \"python\"" (a typo for "named") got dispatched
    correctly (the real extractor grabbed "python" from the "" literal),
    but narrated as if the file were called 'nmed', because the model was
    never told the real answer and had to guess. Same root cause behind a
    plain "open it" getting narrated as opening a specific, entirely
    invented folder name that appeared nowhere in the conversation.

    Fix: put the REAL resolved value(s) directly in decision_context, so
    there's nothing left to guess -- the model's only job is rewording,
    the same structural fix already used for api-kind results (see
    _dispatch()'s docstring), just applied here without needing to wait
    for a live result first, since slots are already known synchronously.
    """
    base = f"You've decided on this action: {meta['description']}."
    values = _narration_values(slots)
    if not values:
        return base
    quoted = ", ".join(f"'{v}'" for v in values)
    return (
        base + f" The exact value(s) already decided, and the ONLY one(s) you may "
        f"name: {quoted}. If your sentence mentions a specific name/value at all, "
        "it must be exactly one of these -- never a different word read off the "
        "user's raw message."
    )


# Leading-verb -> gerund, covering every leading word across intents.py /
# intents_extended.py / intents_app_control.py's "description" fields that
# is actually a verb (a handful of INFO-category descriptions read as noun
# phrases instead, e.g. "Current weather..." -- those fall through to the
# generic wrapper below, which stays accurate even if slightly less smooth).
_VERB_GERUND = {
    "break": "Breaking", "capture": "Capturing", "check": "Checking",
    "click": "Clicking", "copy": "Copying", "count": "Counting",
    "create": "Creating", "decrease": "Decreasing", "delete": "Deleting",
    "double-click": "Double-clicking", "empty": "Emptying", "export": "Exporting",
    "find": "Finding", "increase": "Increasing", "list": "Listing", "lock": "Locking",
    "move": "Moving", "mute": "Muting", "open": "Opening",
    "open/launch": "Opening/launching", "read": "Reading", "rename": "Renaming",
    "resolve": "Resolving", "right-click": "Right-clicking", "search": "Searching",
    "show": "Showing", "stop/close": "Stopping/closing", "wait": "Waiting",
}


def _fallback_narration(meta: Dict[str, Any], slots: Dict[str, Any]) -> str:
    """
    Deterministic, guaranteed-accurate narration sentence -- nothing here
    is generated, so it cannot drift from what's actually about to happen.
    Used in two situations (see _is_narration_grounded() / _start_thinking()):
      1. Ollama is unreachable, so there's no model output to show at all
         (previously this silently showed an EMPTY narration -- see
         stream_thinking()'s "text" being "" on a ConnectionError).
      2. The model's own narration doesn't mention any of the real
         resolved value(s) -- the structural backstop for exactly the
         "small-model instruction-adherence failure" STATUS.md's BETA
         0.3.3 entry and this file's own api-kind docstring both already
         concluded prompting alone can't fully prevent.
    """
    words = meta["description"].split(" ", 1)
    first, rest = words[0].lower().rstrip("."), (words[1] if len(words) > 1 else "")
    gerund = _VERB_GERUND.get(first)
    phrase = f"{gerund} {rest}".rstrip(".") if gerund else f"Working on: {meta['description'].rstrip('.')}"
    values = _narration_values(slots)
    return f"{phrase}: '{values[0]}'." if values else f"{phrase}."


def _is_narration_grounded(text: str, expected_values: List[str]) -> bool:
    """
    True if there's nothing specific to check (expected_values empty --
    about a third of TOKI's intents take no slots at all, so
    meta['description'] alone is already fully specific and low-risk), or
    if the model's narration actually contains at least one of the real
    resolved values. False (not grounded) if there WERE real values to
    mention and the narration -- or an outright empty/failed one -- didn't
    mention any of them. Deliberately a low, "contains at least one"
    bar, not "mentions all of them correctly" -- catching the severe
    failure mode (a fully invented name, or a completely unrelated
    sentence) matters far more than nitpicking an otherwise-fine sentence
    that only names one of two values in a two-slot intent.
    """
    checkable = [v for v in expected_values if len(v) >= 2]
    if not checkable:
        return True
    if not text:
        return False
    lowered = text.lower()
    return any(v.lower() in lowered for v in checkable)


def _build_thinking_system_prompt(decision_context: str, has_history: bool = False) -> str:
    """
    decision_context describes what's ALREADY been decided by classify() /
    slot-resolution before this call ever runs (e.g. "You've decided to
    create a new folder." or "You're going to ask the user which file, since
    they didn't say."). This is the piece that makes thinking narrate a real
    decision instead of independently guessing at one -- see the v2.3->v2.4
    note at the top of this file.

    has_history controls the closing instruction: action-narration calls
    (has_history=False, the default) still get the original "this is a
    fresh message" instruction, which is what prevents small-model
    echo/reword of a previous turn. The CHAT call site passes
    has_history=True instead, which flips that instruction so the model is
    told it's ALLOWED to reference what actually happened -- otherwise a
    person asking "wait, why didn't that work?" gets a response that's
    silently forbidden from acknowledging the history it was just given.
    """
    if has_history:
        continuity_instruction = (
            "The conversation history above is real and happened -- if the user is asking "
            "about something you just did (or didn't do), answer based on what's actually "
            "in that history. Don't invent a reason that isn't supported by it."
        )
    else:
        continuity_instruction = (
            "This is a fresh message with no relation to anything said before -- respond "
            "only to what's written below, don't reuse or reword an earlier reply."
        )
    return (
        "You are TOKI, a Windows assistant created by MrMIB. If asked who made, built, "
        "created, or is behind you, say MrMIB made you -- don't deflect that and don't "
        "attribute yourself to anyone/anything else. " + decision_context + " "
        "Reword ONLY the decision above into ONE short, natural, first-person-plural "
        "sentence, as if you're telling the user what you're doing right now.\n"
        "CRITICAL: the sentence below is a STYLE example ONLY, showing tone and length --"
        " it is for a completely different, unrelated action. Never reuse its subject "
        "matter (never mention recycle bins, disk space, or anything else from it) unless "
        "the decision above is actually about that. If you catch yourself writing "
        "something close to this example's wording, stop and reword the REAL decision "
        "above instead: 'Got it, emptying the Recycle Bin now.'\n"
        "Do NOT include any command, path, code, or technical detail -- just a short, "
        "friendly, natural-language sentence.\n"
        "IMPORTANT: you do NOT know the outcome yet -- the actual command/API call this "
        "describes hasn't finished (or, for weather/search/file lookups, hasn't even been "
        "sent) by the time you're writing this sentence. Only describe the ACTION you're "
        "about to take, never its result. Do NOT state, guess, or imply any specific "
        "value you don't already have in the context above -- no temperatures, "
        "conditions, names, numbers, dates, file counts, or answers of any kind. If you "
        "don't have a concrete fact, don't invent one; keep the sentence limited to what "
        "you're doing, e.g. 'Checking the weather for you now...' not 'It's sunny and "
        "75°F.'\n"
        "Do NOT ask the user a question or offer them a choice (e.g. 'want me to share "
        "it?') -- the action always runs and its result is always shown right after this "
        "sentence no matter what you write, so a question here would be one you can't "
        "actually wait for an answer to.\n"
        + continuity_instruction
    )


# ─── Router ───────────────────────────────────────────────────────────────────

class _ThinkingHandle:
    """
    Tiny wrapper returned by WindowsAIAssistant._start_thinking() -- lets a
    caller start the thinking stream, go do other work (like kicking off a
    PowerShell command), and collect the finished text later via .join().
    Not meant to be constructed directly anywhere else.
    """
    def __init__(self, thread, result: Dict[str, Any]):
        self._thread = thread
        self._result = result

    # Shown instead of a silent blank reply when the AI fallback was
    # actually needed for this turn (graph + WCL both missed) and Ollama
    # wasn't reachable to answer it -- see join() below. Wording is
    # deliberately plain, not a stack trace or connection-error string:
    # the user doesn't run Ollama themselves in most consumer installs.
    _OLLAMA_UNREACHABLE_MESSAGE = (
        "I didn't get that -- that needed my AI fallback (Ollama), "
        "which isn't running or isn't reachable right now."
    )

    def join(self, timeout: float = 30) -> str:
        self._thread.join(timeout=timeout)
        text = self._result.get("text", "")
        if not text and self._result.get("error"):
            return _ThinkingHandle._OLLAMA_UNREACHABLE_MESSAGE
        return text


class OllamaRouter:
    """Talks to a local Ollama instance using schema-constrained decoding, twice per turn."""

    # BETA 0.3.51: same "don't re-pay a doomed network call" fail-soft
    # pattern as apis.py's LocationCache and app_control.py's
    # AppController (_FAILURE_RETRY_SECONDS) -- Ollama is documented
    # elsewhere in this file as "now the RARE path" (project owner has
    # mostly retired it), which means on a real session where it's not
    # running, EVERY graph+WCL miss used to attempt a fresh connection to
    # localhost:11434 and wait out however long that connection attempt
    # takes before falling through to the search-first default -- paying
    # that cost again and again for the rest of the session even though
    # the answer never changes until Ollama is actually started. Once a
    # call confirms Ollama is unreachable, classify() short-circuits
    # straight to the same {"error": ...} result for
    # _UNREACHABLE_RETRY_SECONDS without attempting a new connection --
    # then retries for real after that, in case Ollama gets started
    # mid-session. This is purely a latency/priority change: the
    # RESPONSE (fall through to search-first) is identical either way,
    # confirmed by the fact that _process_single_request already just
    # checks `"error" in llm_result` regardless of which path produced it.
    _UNREACHABLE_RETRY_SECONDS = 30.0
    _last_unreachable_time: Optional[float] = None

    def __init__(self, model_name: str = "phi4-mini", base_url: str = "http://localhost:11434"):
        self.model_name = model_name
        self.base_url = base_url
        # Separate session attributes per call path -- stream_thinking() now
        # runs concurrently with _call() (classify's tier 1/tier 2) on a
        # different thread, so sharing one self._session would race: each
        # call would clobber the other's session reference, and cancel()
        # could end up only closing whichever was written last, leaking the
        # other connection and breaking Stop for it.
        self._classify_session: Optional[requests.Session] = None
        self._thinking_session: Optional[requests.Session] = None
        self._cancelled = False
        self._last_unreachable_time: Optional[float] = None

    def cancel(self):
        """Called by the Stop button while a request is in flight -- closes both sessions."""
        self._cancelled = True
        for sess in (self._classify_session, self._thinking_session):
            if sess:
                try:
                    sess.close()
                except Exception:
                    pass

    def _log_timing(self, label: str, resp_json: Dict[str, Any]) -> None:
        """
        Prints Ollama's own reported timing breakdown for one call, in
        milliseconds, to the console TOKI was launched from. This exists
        purely so it's possible to SEE, on the actual target machine,
        whether "slow" means a cold model reload (load_duration -- fixed by
        the keep_alive setting added alongside this) or genuinely just the
        cost of running inference 2-3 times per message (prompt_eval /
        eval duration -- an inherent cost of this app's two-tier
        classify-then-narrate architecture, not a bug to fix here).
        Ollama returns these fields in nanoseconds; converted to ms for
        readability. Silently does nothing if the fields aren't present
        (e.g. an older Ollama version) -- this is diagnostic only, never
        load-bearing for correctness.

        THRESHOLD (recalibrated from a real capture, not guessed): on a
        CPU-only Ollama instance (no GPU offload -- confirmed via `ollama
        ps` showing "100% CPU"), a genuinely warm/resident call still
        naturally reports load_duration in the 800-1100ms range, because
        this field also covers request/runner setup overhead, which is
        just slower on CPU generally -- it does NOT mean the model was
        actually reloaded. A REAL cold reload on that same machine measured
        ~8000ms. The old 500ms threshold was tuned for GPU-speed warm loads
        (near-zero ms there) and was firing on every single call on a
        CPU-only box, which is exactly why it looked like it was reloading
        constantly during active use when `ollama ps` confirms it never
        actually unloaded. 3000ms sits well clear of both real clusters
        seen so far. This is still a heuristic based on one field Ollama
        happens to expose, not a certainty -- `ollama ps` is the actual
        ground truth if this ever looks wrong again.
        """
        try:
            load_ms = resp_json.get("load_duration", 0) / 1e6
            prompt_ms = resp_json.get("prompt_eval_duration", 0) / 1e6
            eval_ms = resp_json.get("eval_duration", 0) / 1e6
            total_ms = resp_json.get("total_duration", 0) / 1e6
            print(
                f"[TOKI timing] {label}: load={load_ms:.0f}ms  "
                f"prompt_eval={prompt_ms:.0f}ms  eval={eval_ms:.0f}ms  "
                f"total={total_ms:.0f}ms"
                + ("  <- COLD MODEL LOAD, this call paid a reload" if load_ms > 3000 else "")
            )
        except Exception:
            pass  # diagnostic only -- never let logging break a real call

    def _call(self, system_prompt: str, user_prompt: str, schema: Dict[str, Any],
               history: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """One schema-constrained, non-streamed call. Shared by both tiers."""
        messages = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_prompt})

        self._classify_session = requests.Session()
        try:
            resp = self._classify_session.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model_name,
                    "messages": messages,
                    "stream": False,
                    "format": schema,
                    "options": {"temperature": 0.2, "num_predict": 30},
                    # Keep the model pinned in VRAM for a long session rather
                    # than relying on Ollama's server-side default (5 minutes
                    # of inactivity, unless the service was configured
                    # otherwise) -- a demo has natural pauses (talking to
                    # judges, etc.) that can exceed that window, and a
                    # reload after unload costs several real seconds on top
                    # of this app's inherent 2-3 sequential model calls per
                    # message. "30m" comfortably covers a presentation
                    # without pinning the model forever if the app is left
                    # open afterward.
                    "keep_alive": "30m",
                },
                timeout=60,
            )
            resp.raise_for_status()
            self._last_unreachable_time = None
        except requests.exceptions.ConnectionError:
            self._last_unreachable_time = time.time()
            return {"error": "Can't reach Ollama — is it running on localhost:11434?"}
        except requests.exceptions.Timeout:
            self._last_unreachable_time = time.time()
            return {"error": "Ollama timed out."}
        except Exception as e:
            if self._cancelled:
                return {"error": "Stopped."}
            return {"error": f"Unexpected error: {e}"}
        finally:
            self._classify_session = None

        if self._cancelled:
            return {"error": "Stopped."}

        try:
            resp_json = resp.json()
            self._log_timing("classify", resp_json)
            content = resp_json.get("message", {}).get("content", "")
            return json.loads(content)
        except (json.JSONDecodeError, ValueError, AttributeError):
            return {"error": "Model response wasn't valid — try again."}

    def stream_thinking(self, user_prompt: str, decision_context: str,
                         on_token: Callable[[str], None],
                         history: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """
        Plain free-text call, NO schema, streamed token by token via on_token
        as it arrives. Runs AFTER classification and slot-resolution now
        (v2.4) -- decision_context tells it what's already been decided, so
        it narrates that real decision in one natural sentence instead of
        independently guessing at one in parallel. See the v2.3->v2.4 note
        at the top of this file for why this changed and what it costs.

        `history` defaults to None and should stay that way for every
        ACTION-narration call site (the ones telling the user "Got it,
        creating a new folder" etc.): passing prior turns in there caused
        the model to anchor on the most recent assistant turn and
        echo/reword it instead of generating a new sentence for the
        current message -- a well-known small-model failure mode under
        loose (non-schema) free-text prompting.

        It's passed explicitly ONLY for the CHAT-kind call site in
        _process_single_request(), and only the last 1-2 turns (same cap
        as classify()'s history). Reasoning: a CHAT-classified message is
        exactly where a person is likely to ask something like "wait, why
        didn't that work?" or "what did you just do?" -- and with zero
        history, this call has no way to answer that grounded in what
        actually happened. It previously generated a plausible-sounding
        but disconnected sentence instead (reproduced directly this
        session: a user asked why a folder wasn't opened after a chained
        command, and got an ungrounded, made-up-sounding answer, because
        this call genuinely had no memory of the folder or the open
        attempt to draw on). That's a different failure mode from the
        echo/reword problem that justified removing history here in the
        first place -- CHAT already isn't a schema-constrained pick that
        could get destabilized the way classify() could, it's free text
        either way, so there's no equivalent risk being reintroduced for
        this one call site.

        Returns {"text": full_text} on success or {"error": ...} on failure.
        Never raises -- callers can treat a failure here as non-fatal and
        still proceed with dispatch, since thinking text is cosmetic.
        """
        # Same fast-fail as classify() -- see _UNREACHABLE_RETRY_SECONDS'
        # docstring on the class. Thinking text is cosmetic and already
        # non-fatal on failure, so skipping a doomed connection attempt
        # here is purely a latency win, never a behavior change.
        if self._last_unreachable_time is not None:
            if (time.time() - self._last_unreachable_time) < self._UNREACHABLE_RETRY_SECONDS:
                return {"error": "Can't reach Ollama — is it running on localhost:11434?", "text": ""}

        messages = [
            {"role": "system", "content": _build_thinking_system_prompt(decision_context, has_history=bool(history))},
        ]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_prompt})

        self._thinking_session = requests.Session()
        full_text = ""
        try:
            resp = self._thinking_session.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model_name,
                    "messages": messages,
                    "stream": True,
                    "options": {"temperature": 0.4, "num_predict": 60},
                    "keep_alive": "30m",  # see _call()'s comment for why
                },
                timeout=30,
                stream=True,
            )
            resp.raise_for_status()
            self._last_unreachable_time = None
            for raw_line in resp.iter_lines():
                if self._cancelled:
                    return {"error": "Stopped.", "text": full_text}
                if not raw_line:
                    continue
                try:
                    chunk = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                token = chunk.get("message", {}).get("content", "")
                if token:
                    full_text += token
                    on_token(token)
                if chunk.get("done"):
                    self._log_timing("thinking", chunk)
                    break
        except requests.exceptions.ConnectionError:
            self._last_unreachable_time = time.time()
            return {"error": "Can't reach Ollama — is it running on localhost:11434?", "text": full_text}
        except requests.exceptions.Timeout:
            # Thinking is cosmetic -- a timeout here shouldn't stop the turn,
            # just means the user didn't get the live-text preamble this time.
            self._last_unreachable_time = time.time()
            return {"error": "timeout", "text": full_text}
        except Exception as e:
            if self._cancelled:
                return {"error": "Stopped.", "text": full_text}
            return {"error": f"Unexpected error: {e}", "text": full_text}
        finally:
            self._thinking_session = None

        return {"text": full_text}

    def classify(self, user_prompt: str, history: Optional[List[Dict]] = None) -> Dict[str, Any]:
        """
        Two-tier classification only -- no thinking/response text anymore,
        those come from stream_thinking() instead. Returns {"intent": name}
        or {"error": ...}.

        Both tiers now receive history (see the fix note below) -- an
        earlier version deliberately withheld it from tier-1 on the
        assumption that picking a category never depends on prior turns.
        That assumption broke as soon as chaining was added: "make a
        folder called Homework and then open it" splits into two
        segments, and the second segment ("open it") is classified
        completely on its own. With zero history, "open it" has no way to
        know "it" refers to a folder that was just created rather than,
        say, an application to launch -- and CATEGORIES's own APP_CONTROL
        description ("Opening/launching an application") reads as an
        equally or more plausible match for that bare phrase without
        context. This was reproduced directly: that ambiguity, not a
        made-up "hallucination," is what sent a real chained request down
        the wrong path. History is capped at the last 2 turns
        (self.history, see _commit_history) specifically so this fix
        doesn't reopen the original prompt-processing cost concern by
        passing a large or growing amount of context on every call.
        """
        self._cancelled = False

        # Fast-fail path -- see _UNREACHABLE_RETRY_SECONDS' docstring
        # above. Only skips the NETWORK ATTEMPT; the return value is
        # identical in shape to what a real ConnectionError produces, so
        # every existing caller (_process_single_request's `"error" in
        # llm_result` check) behaves exactly as if the attempt had been
        # made and failed the same way it did last time.
        if self._last_unreachable_time is not None:
            if (time.time() - self._last_unreachable_time) < self._UNREACHABLE_RETRY_SECONDS:
                return {"error": "Can't reach Ollama — is it running on localhost:11434?"}

        # Tier 1: category. Now receives history too -- see docstring above
        # for why the earlier "tier-1 never needs history" assumption broke.
        cat_result = self._call(_build_category_prompt(), user_prompt, _CATEGORY_SCHEMA, history=history)
        if "error" in cat_result:
            return cat_result

        category = cat_result.get("category")
        if category not in ACTIVE_CATEGORY_NAMES:
            # Defense in depth: even schema-constrained decoding isn't
            # infallible on every runtime/model combo, so never trust it blindly.
            category = "CHAT"

        if category == "CHAT":
            return {"intent": "CHAT"}

        if category == "ASK_CONTEXT":
            return {"intent": "ASK_CONTEXT"}

        if self._cancelled:
            return {"error": "Stopped."}

        # Categories with exactly one command (GENERATE, now that the old
        # multi-command categories are graph-only -- see categories.py's
        # module docstring) have nothing left to disambiguate. Skip the
        # tier-2 round trip entirely and return that command directly.
        names_in_cat = commands_in_category(category)
        if len(names_in_cat) == 1:
            return {"intent": names_in_cat[0]}

        # Tier 2: command within that category.
        cmd_result = self._call(
            _build_command_prompt(category), user_prompt, _command_schema(category), history
        )
        if "error" in cmd_result:
            return cmd_result

        command = cmd_result.get("command")
        valid_commands = commands_in_category(category)
        if command not in valid_commands:
            # Same defense-in-depth as above, scoped to this category.
            return {"intent": "CHAT"}

        return {"intent": command}


class ToolDispatcher:
    """Runs the API-kind intents (weather/search/time/location)."""

    def __init__(self):
        self.weather = WeatherAPI()
        self.search  = WebSearchAPI()
        self.time    = TimeAPI()
        self.location = LocationAPI()
        self.fileconvert = FileConvertAPI()
        self.videodownload = VideoDownloadAPI()
        self.fileorganizer = FileOrganizerAPI()
        self.filegrouping = FileGroupingAPI()
        self._apis = {"weather": self.weather, "websearch": self.search,
                      "time": self.time, "location": self.location,
                      "fileconvert": self.fileconvert,
                      "videodownload": self.videodownload,
                      "fileorganizer": self.fileorganizer,
                      "filegrouping": self.filegrouping}

    def call(self, meta: Dict, slots: Dict[str, str]) -> str:
        api = self._apis.get(meta["api"])
        action = getattr(api, meta["action"], None)
        if not action:
            return f"Unknown API action: {meta['api']}.{meta['action']}"
        try:
            return action(**slots)
        except TypeError:
            # Slot dict may include keys the method doesn't take (e.g. empty
            # optional city) — filter down to what the callable accepts.
            import inspect
            accepted = set(inspect.signature(action).parameters)
            filtered = {k: v for k, v in slots.items() if k in accepted}
            return action(**filtered)


# ─── Message chaining ─────────────────────────────────────────────────────
#
# Splits a single user message into multiple sub-requests on unambiguous,
# literal conjunction boundaries the user themselves typed -- "and then",
# "then", ", and", ";" -- so something like "make a folder called Homework
# and then open it" becomes two independent single-intent turns instead of
# one intent swallowing (or dropping) part of the sentence.
#
# Deliberately NOT a general planner: there's no model call that decides
# how to decompose the request, no inferred steps beyond what's literally
# separated in the user's own text. Splitting is pure Python string
# splitting, same "never let the model invent structure" rule that governs
# slot extraction everywhere else in this app. Each resulting segment goes
# through the EXACT SAME single-intent classify -> extract_slots ->
# dispatch pipeline as any other message -- nothing about that pipeline
# changes, so all its existing guarantees (schema-constrained picks,
# regex-only slots, sandboxed execution) hold per-segment.
_CHAIN_SPLIT_RE = re.compile(
    r"\s*(?:,?\s*and\s+then\s+|,?\s*then\s+|,?\s+and\s+|;\s*)\s*",
    re.IGNORECASE,
)

# Guardrails: chaining is for genuinely short, obvious multi-step asks
# ("make a folder and open it"), not an open-ended batch queue. Keeping a
# hard cap means a pathological input (a run-on sentence full of commas)
# can't turn one message into a huge uncontrolled sequence of actions with
# no confirmation step anywhere in this app's design.
_MAX_CHAIN_SEGMENTS = 4

# Intents whose slot value is deliberately pulled from the user's own text
# by extract_slots(), never from graph vocabulary -- an app/process/service
# name. See _segment_is_viable()'s BETA 0.3.14 addition for why a
# below-threshold classify_or_ask() candidate is trusted for exactly these,
# and only these.
_NAME_FROM_OUTSIDE_VOCAB_INTENTS = {
    "LAUNCH_APP", "KILL_PROCESS", "WAIT_FOR_PROCESS", "FIND_PROCESS", "FIND_SERVICE",
    # Added alongside the casual-phrasing expansion (graph_source_data/
    # tier_a_phrasings.py): MAKE_FOLDER's target name has the exact same
    # "pulled from the user's own text, never from graph vocabulary"
    # property as LAUNCH_APP's app name above -- confirmed via the real
    # live-transcript case in test_chain_split_viability.py
    # (TestCommaBeforeThenIsHandled): 'Create a folder named "python"'
    # candidates MAKE_FOLDER at classify_or_ask() but can't clear
    # classify()'s threshold no matter how MAKE_FOLDER's corpus is tuned,
    # because "python" (or any real folder name) is inherently OOV, and
    # widening MAKE_FOLDER's corpus to try to cover it dilutes the
    # command's own vector for every OTHER phrasing via L2 normalization
    # (see that file's MAKE_FOLDER comment for the concrete numbers).
    # Verified this doesn't reopen the BETA 0.3.11 false-positive
    # ("copy a.txt and b.txt to D drive" splitting into a bogus second
    # DISK_USAGE segment): that segment candidates DISK_USAGE, not
    # MAKE_FOLDER, so this addition doesn't touch it either way.
    "MAKE_FOLDER",
}

# Intents that change what's actually on disk under the sandbox roots --
# used to invalidate extractor.py's FileIndex right after dispatch, the
# same "TOKI already knows when it wrote something, no need to guess or
# poll" reasoning as AppController.invalidate_app_cache() (see
# app_control.py). MOVE_ITEM/RENAME_ITEM change two things at once (an old
# path stops existing, a new one appears) but still only need ONE
# invalidate() call -- the whole index gets rebuilt on next use either way.
_WRITE_INTENTS = {"MAKE_FOLDER", "MAKE_FILE", "DELETE_ITEM", "RENAME_ITEM",
                   "MOVE_ITEM", "COPY_ITEM", "GENERATE_FILE"}


def _split_chain(user_prompt: str) -> List[str]:
    """
    Returns a list of 1+ segments. A message with no recognizable
    conjunction boundary returns a single-item list containing the
    original message unchanged -- the overwhelmingly common case, and it
    must behave identically to how process_request always worked before
    chaining existed.
    """
    parts = [p.strip() for p in _CHAIN_SPLIT_RE.split(user_prompt) if p.strip()]
    if len(parts) <= 1:
        return [user_prompt]
    return parts[:_MAX_CHAIN_SEGMENTS]


def _segment_is_viable(segment: str, graph_router: Optional["GraphRouter"]) -> bool:
    """A split segment only counts as a real, independent instruction if
    the graph gives it a CONFIDENT hit (classify()) -- a below-threshold
    "candidate" is deliberately no longer good enough here (BETA 0.3.11,
    see below for why).

    Confirmed live, the exact case this originally caught: `_split_chain`
    turns "make a file called things, and stuff.txt" (one filename with a
    comma in it) into ["make a file called things", "stuff.txt"] --
    "stuff.txt" alone has zero graph signal at all, unlike a genuine
    chained instruction like "open it" or "search for cats".

    BETA 0.3.11 tightening: once bare "and" (no comma, no "then") became
    a split boundary to fix the still-open "take a screenshot and also
    empty the recycle bin" miss (see this file's BETA 0.3.9 STATUS.md
    entry, which fixed the adjacent ",then" comma bug but explicitly left
    plain "and" unfixed), the OLD candidate-accepting version of this
    check let real two-slot commands get wrongly split. Verified against
    the live graph (toki_graph_db, this package): "copy a.txt and b.txt
    to D drive" splits into "copy a.txt" (a real classify() hit --
    COPY_ITEM) and "b.txt to D drive" (classify() misses, but
    classify_or_ask() still hands back a below-threshold "candidate":
    DISK_USAGE) -- the old code accepted that candidate as "viable" and
    would have wrongly chained a single copy command into two dispatches.
    Dropping the candidate fallback closes that: every parametrized
    "genuine chain" case in tests/test_chain_split_viability.py still
    gets a real classify() hit on both segments (re-verified against this
    package's live graph, not assumed), so nothing legitimate stops
    splitting -- only the below-threshold guesses that were never
    trustworthy enough to dispatch on anyway.

    Known remaining gap, not fixed by this change: a segment pair that
    BOTH happen to score a confident (if coincidental) classify() hit --
    e.g. "find files named report and export.csv" splits into two
    fragments that each independently graph-hit a real intent
    (FIND_DUPLICATE_FILES, EXPORT_FOLDER_LISTING_CSV) even though a human
    would likely read this as one query for a file literally named
    "report and export.csv". No signal available at the segment-viability
    layer distinguishes this from a genuine two-command chain; catching it
    would need either scoring the whole unsplit sentence as a competing
    candidate (not exposed by classify()/classify_or_ask() today) or
    tightening CONFIDENCE_THRESHOLD itself for short segments -- both out
    of scope for this fix. Not verified against real usage, only
    adversarial testing -- worth a follow-up STATUS.md item if it ever
    shows up live.

    Returns True (don't block the split) if graph_router is None --
    same fail-open posture as everywhere else in this file; this check
    is a refinement on TOP of the graph's own signal, not a replacement
    for it, so it can't do anything useful without a working graph either.

    BETA 0.3.14 addition -- fixes the known "close chrome and open
    notepad" gap documented above without reopening the copy/rename
    false-positive BETA 0.3.11 fixed:

    A real classify() hit is still required for everything EXCEPT the
    small, explicit set of intents in _NAME_FROM_OUTSIDE_VOCAB_INTENTS,
    whose whole design point is that their target (an app/process/
    service name) is deliberately never part of the graph's own
    vocabulary -- extract_slots() pulls it straight from the user's own
    text, never from the classifier. For exactly those intents, a
    below-threshold classify_or_ask() CANDIDATE is trusted as viable,
    because the thing making it "below threshold" is structural (the
    target word can never score, by design) rather than genuine
    ambiguity about what command was meant.

    Verified directly against this package's real shipped graph (not
    assumed): 'close chrome', 'stop chrome', 'start notepad', 'close
    notepad', 'close discord' all resolve to a LAUNCH_APP or
    KILL_PROCESS candidate -- exactly the two intents this whitelist
    covers. The protected false-positive case is unaffected: 'b.txt to
    D drive' (from "copy a.txt and b.txt to D drive") candidates as
    DISK_USAGE, which is NOT in the whitelist, so it's still correctly
    rejected. 'export.csv' (from "find files named report and
    export.csv") already gets a real classify() hit on its own, so the
    still-open ambiguous-"and" xfail above is untouched either way --
    this fix only ever widens acceptance for the whitelisted intents,
    never loosens the real-hit requirement for anything else.

    App-control intents (CLICK_ELEMENT and friends) are deliberately
    NOT in this whitelist yet -- that surface hasn't had its own
    live-Windows/adversarial pass, so it stays exactly as conservative
    as before rather than being widened on the strength of this
    unrelated fix.
    """
    if graph_router is None:
        return True
    if graph_router.classify(segment) is not None:
        return True
    candidate = graph_router.classify_or_ask(segment).get("candidate")
    return candidate in _NAME_FROM_OUTSIDE_VOCAB_INTENTS


def _split_chain_if_viable(user_prompt: str, graph_router: Optional["GraphRouter"]) -> List[str]:
    """Wraps _split_chain() with the viability check above: the multi-
    segment split is only used if EVERY resulting segment independently
    looks like a real command. If even one segment doesn't (e.g. it's a
    filename fragment, not a second instruction), the whole split is
    discarded and the ORIGINAL, unsplit message is used instead -- same
    "never guess, fail to the safer default" principle as every other
    fallback in this file. A false NEGATIVE here (a genuine chain that
    gets treated as one segment) just means that message goes through
    classification as a single whole instead of being split -- usually
    an honest ASK_CONTEXT/miss, never a wrong dispatch. A false POSITIVE
    (a bad split that gets accepted) is the actual failure mode worth
    avoiding, since each accepted segment gets dispatched for real."""
    segments = _split_chain(user_prompt)
    if len(segments) == 1:
        return segments
    if all(_segment_is_viable(seg, graph_router) for seg in segments):
        return segments
    return [user_prompt]


class WindowsAIAssistant:
    """Ties classification, slot-filling, generation, and execution together for the UI."""

    def __init__(self, model_name: str = "phi4-mini"):
        self.router = OllamaRouter(model_name=model_name)
        # Graph is the FIRST attempt at classification now -- see
        # _process_single_request(), where self.router.classify() is
        # only called on a graph miss. Kept as a separate object
        # (not merged into OllamaRouter) so a graph load failure can't
        # take down classification entirely -- see the try/except
        # around its construction below.
        try:
            self.graph_router = GraphRouter()
        except Exception:
            # Graph database missing, corrupt, or kuzu not installed --
            # fail open to LLM-only classification (today's v2.11
            # behavior) rather than crashing the whole app on startup.
            self.graph_router = None
        # windows_command_library matching lives in its OWN graph now --
        # see wcl_resolver.py's module docstring for why this replaced
        # tiers A2/B of the old toki_graph_db. Same fail-open pattern.
        try:
            self.wcl_resolver = WCLResolver()
            if self.wcl_resolver.conn is None:
                self.wcl_resolver = None
        except Exception:
            self.wcl_resolver = None
        self.dispatcher = ToolDispatcher()
        self.generator = FileGenerator(model_name=model_name)
        self.app_controller = AppController()
        # In-process only -- see scheduler.py/condition_checker.py module
        # docstrings for the explicit scope boundary (doesn't survive
        # TOKI closing; no Windows Task Scheduler integration).
        self.scheduler = ScheduledCommandManager()
        self.condition_poller = ConditionPoller()
        self.history: List[Dict] = []
        self._current_cmd: Optional[RunningCommand] = None
        # Set when a turn ended by asking the user a fixed follow-up
        # question (a required slot was missing). The NEXT process_request()
        # call is then treated as the answer to that question, not a new
        # message to classify -- see _resume_pending() below.
        self._pending: Optional[Dict[str, str]] = None
        # BETA 0.3.38: a SEPARATE pending state from self._pending above --
        # that one is "waiting for a missing SLOT VALUE" (which name? which
        # path?). This one is "the intent and every slot are already known,
        # but this is a caution/destructive WCL command, so pause for a
        # yes/no before actually running it." Mutually exclusive with
        # self._pending in practice (a turn either ends by asking for a
        # missing value OR asking for a go-ahead, never both), but kept as
        # two separate fields rather than one overloaded one so each stays
        # simple to reason about on its own. See _ask_for_confirmation()/
        # _resume_pending_confirmation() below.
        self._pending_confirmation: Optional[Dict[str, Any]] = None
        # Set whenever classify_or_ask() asked a clarifying question about
        # specific unknown words -- holds until the NEXT message, which is
        # read as the user's 👍/👎 (or plain reply) to that question. See
        # log_graph_ask()/confirm_graph_ask() below for the staging-DB
        # write this feeds.
        self._pending_graph_ask: Optional[Dict[str, Any]] = None
        # BETA 0.3.49: set when a bare "start/stop recording" couldn't be
        # resolved (start: genuinely no text signal left; stop: both macro
        # and dictation happened to be active at once) -- see
        # extractor.py's looks_like_ambiguous_start_recording()/
        # looks_like_ambiguous_stop_recording() docstrings. Holds until the
        # next message, read as the answer to that one question. Its own
        # dedicated pending state, same reasoning as _pending_graph_ask
        # just above having its own: different question shape, different
        # resume logic, kept separate rather than overloading _pending's
        # generic slot-filling contract.
        self._pending_recording_choice: Optional[Dict[str, Any]] = None
        # Set right after TOKI itself successfully creates/renames/moves/
        # copies/generates something -- see _remember_touched() below. This
        # is what "it"/"that"/"the folder you just made" resolve against
        # (extractor.resolve_anaphoric_target) instead of either being
        # fuzzy-matched against the whole raw sentence or crashing
        # PowerShell on a literal-sentence path. Deliberately session-only,
        # in-memory, no persistence -- same "TOKI already knows when it
        # wrote something" reasoning as FileIndex/AppController's caches.
        self._last_touched: Optional[Dict[str, str]] = None
        self._prime_caches_in_background()
        # Starts app_control.py's foreground-window fix (see
        # foreground_tracker.py's module docstring) as early as possible --
        # the whole point is to have already observed the real window BEFORE
        # the user's first command shifts OS focus to TOKI itself. No-op on
        # non-Windows platforms; idempotent, so a second WindowsAIAssistant
        # in the same process (e.g. across tests) doesn't spin up a second
        # competing thread. foreground_tracker.start() already fails soft
        # internally, but wrapped here too, same defense-in-depth posture
        # as _prime_caches_in_background's own try/excepts -- this is a
        # best-effort UX fix, never something that should be able to
        # prevent WindowsAIAssistant from constructing at all.
        try:
            foreground_tracker.start()
        except Exception:
            pass

    def _prime_caches_in_background(self) -> None:
        """Warms the three "fetch once, reuse all session" caches
        (apis.py's location_cache, AppController's installed-app list,
        extractor.py's FileIndex) right away instead of leaving them
        lazy -- previously all three only populated on whatever request
        happened to need them first, so a user's FIRST app-launch or
        file-open turn silently paid the full fetch cost (a Get-StartApps
        subprocess call, a full sandbox os.walk, an ipinfo.io round
        trip) that every later turn gets for free. apis.py's
        LocationAPI.get_raw_location() docstring already anticipated
        this ("for callers that need... e.g. showing a short status on
        startup once the fetch completes") but nothing ever actually
        called it at startup until now.

        Each cache gets its OWN daemon thread rather than one thread
        doing all three sequentially -- they're three independent I/O
        waits (network, subprocess, disk), so running them in parallel
        means the slowest one (typically the network location lookup)
        doesn't hold up the other two. Daemon threads: this must never
        keep the process alive on its own or block shutdown waiting for
        a slow/hung network call.

        Deliberately fire-and-forget: every one of these three already
        fails soft internally (never raises, never caches a failure
        forever -- see each cache's own docstring), and every real call
        site already handles "cache came back empty" today. If priming
        hasn't finished (or failed) by the time the user's first real
        request needs one of these, that request just pays the normal
        first-use fetch cost exactly like before this change -- priming
        can only make things faster, never break anything if it's slow
        or unlucky.
        """
        def _prime_location():
            try:
                location_cache.get()
            except Exception:
                pass

        def _prime_apps():
            try:
                self.app_controller.prime_app_cache()
            except Exception:
                pass

        def _prime_files():
            try:
                file_index.get_entries()
            except Exception:
                pass

        for target in (_prime_location, _prime_apps, _prime_files):
            threading.Thread(target=target, daemon=True).start()

    def _remember_touched(self, intent: str, slots: Dict[str, Any]) -> None:
        """Records the path this dispatch just created/produced, so a
        follow-up "delete it" / "open it" / "the folder you just made" has
        something concrete to resolve against. Only called for intents
        that leave behind exactly one new/changed path the user would
        plausibly refer back to -- DELETE_ITEM is deliberately excluded
        (nothing left to refer back to once it's gone), and MOVE/RENAME
        record the item's NEW location, not its old one."""
        path = None
        if intent in ("MAKE_FOLDER", "MAKE_FILE") :
            path = slots.get("path")
        elif intent == "RENAME_ITEM":
            # New path is the old directory + new_name, not slots["path"]
            # (which is still the OLD path/name at dispatch time).
            old_path, new_name = slots.get("path"), slots.get("new_name")
            if old_path and new_name:
                path = ntpath.normpath(ntpath.join(ntpath.dirname(old_path), new_name))
        elif intent in ("MOVE_ITEM", "COPY_ITEM"):
            path = slots.get("dest")
        elif intent == "GENERATE_FILE":
            path = slots.get("path")
        if path:
            self._last_touched = {"path": path}

    def stop(self):
        """Stop button — kills whichever call/process is in flight."""
        self.router.cancel()
        self.generator.cancel()
        if self._current_cmd:
            self._current_cmd.stop()

    def shutdown(self):
        """Release both kuzu database connections cleanly on app exit.

        Found via the project owner's own fix to GraphRouter.close()
        (BETA 0.3.16, missing self.db.close() alongside
        self.conn.close() -- closing the Connection alone doesn't
        release kuzu's file lock). That fix only helps if something
        actually calls .close() -- nothing did, anywhere in the app
        itself, before this. In the normal case this was harmless
        (Python's process-exit cleanup releases the OS-level lock when
        main.py terminates normally), but an abnormal exit (crash,
        force-quit, a hung thread) could leave a stale lock with no
        cleanup path to prevent it. Guards for None on both, since
        graph_router/wcl_resolver already fail open to None per their
        own design (missing kuzu install, corrupt/missing database
        file, etc.) -- see their own __init__ docstrings."""
        if self.graph_router is not None:
            self.graph_router.close()
        if self.wcl_resolver is not None:
            self.wcl_resolver.close()
        # Cancel any pending scheduled commands / condition watches so a
        # daemon timer can't fire in the instant between the window
        # closing and the process actually dying -- see scheduler.py's
        # and condition_checker.py's own shutdown() docstrings for why
        # daemon=True alone isn't quite enough on its own.
        self.scheduler.shutdown()
        self.condition_poller.shutdown()
        foreground_tracker.stop()

    def _start_thinking(
        self, user_prompt: str, decision_context: str,
        on_thinking_token: Optional[Callable[[str], None]],
        history: Optional[List[Dict]] = None,
        expected_values: Optional[List[str]] = None,
        fallback_meta: Optional[Dict[str, Any]] = None,
        fallback_slots: Optional[Dict[str, Any]] = None,
    ) -> Optional["_ThinkingHandle"]:
        """
        PERFORMANCE: kicks off stream_thinking() on a background thread and
        returns immediately, instead of blocking the calling thread until
        the whole thinking sentence has streamed. The decision (category +
        command) is already fully resolved by the time this is called, so
        thinking has everything it needs the moment it starts -- there's no
        reason the actual PowerShell/API dispatch that follows has to wait
        for the thinking text to finish narrating it first. Callers start
        this, do their dispatch work, then call .join() on the handle when
        they actually need thinking_text (e.g. to build the returned dict).

        `history` defaults to None (unchanged behavior for every action
        -narration call site) and is only passed by the CHAT call site --
        see stream_thinking()'s docstring for why.

        expected_values/fallback_meta/fallback_slots (all default None):
        when a caller has real, already-resolved value(s) worth checking
        (see _narration_values()), tokens are BUFFERED instead of streamed
        live -- the full sentence is checked once complete
        (_is_narration_grounded()) and silently replaced with a
        deterministic, guaranteed-correct sentence (_fallback_narration())
        if it doesn't mention any of them, or if stream_thinking() failed
        outright (Ollama unreachable -- previously this silently showed an
        EMPTY narration instead). This trades away live token-by-token
        display for exactly these calls: the ones where getting a specific
        name wrong is a real, visible "why did TOKI say that" trust
        problem, not a cosmetic one. Calls with nothing specific to check
        (expected_values left None/empty -- CHAT, and roughly a third of
        TOKI's intents that take no slots at all) are completely
        unaffected and still stream live, exactly as before.

        Both STATUS.md (BETA 0.3.3) and this file's own api-kind docstring
        independently concluded that prompting alone ("don't invent a
        value") doesn't reliably stop a small local model from doing
        exactly that under free-text generation. Decision_context now
        carrying the real value (see _decision_context()) and the
        anti-parroting rewrite of _build_thinking_system_prompt() both cut
        how often this triggers, but this is the structural backstop for
        when it still isn't followed -- the user never sees the ungrounded
        version either way.

        Safe to run concurrently with the dispatch call: OllamaRouter uses
        separate _classify_session/_thinking_session attributes precisely
        so a concurrent stream_thinking() call can't race with anything
        classify() touches -- classify() has already finished by this point
        anyway, but dispatch's PowerShell/API calls don't touch the router
        at all, so there's no shared state between this thread and dispatch.
        """
        if on_thinking_token is None:
            return None

        import threading
        result: Dict[str, Any] = {}

        if fallback_meta is None:
            def _run():
                result.update(self.router.stream_thinking(user_prompt, decision_context, on_thinking_token, history))
        else:
            def _run():
                buffered: List[str] = []
                outcome = self.router.stream_thinking(user_prompt, decision_context, buffered.append, history)
                text = "".join(buffered)
                if outcome.get("error") or not _is_narration_grounded(text, expected_values or []):
                    text = _fallback_narration(fallback_meta, fallback_slots or {})
                result["text"] = text
                on_thinking_token(text)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return _ThinkingHandle(t, result)

    def _run_thinking(
        self, user_prompt: str, decision_context: str,
        on_thinking_token: Optional[Callable[[str], None]],
        history: Optional[List[Dict]] = None,
    ) -> str:
        """
        Blocking convenience wrapper around _start_thinking()+join(), for the
        call sites that genuinely have nothing to overlap it with (CHAT and
        the missing-slot-question path both need thinking_text immediately
        to build their response, with no dispatch work to run in parallel).
        """
        handle = self._start_thinking(user_prompt, decision_context, on_thinking_token, history)
        return handle.join() if handle else ""

    def _check_destructive_shadow(self, intent: str, user_prompt: str) -> Optional[str]:
        """priority.md #11: returns a clarifying question if `intent` (a
        Tier A graph hit) is shadowing a genuinely destructive WCL
        command for this same query, else None. See the call site's
        comment in process_request() for the full rationale, and
        tier_a_wcl_map.py for how equivalence is decided.

        BETA 0.3.27 fix: this used to only fire on a RESOLVED WCL match --
        confirmed live, "clean temp files" / "wipe the temp files" both
        resolve AMBIGUOUS (tier 1, two literal alias matches: Clear-
        TempFiles and "Clean Temp Files", both destructive), not RESOLVED,
        even though a genuinely destructive command is sitting right there
        in the candidate list -- so the guard never got a chance to see
        it and "clean temp files" silently fell through to whatever Tier A
        intent won instead. Root cause was actually one layer down:
        wcl_resolver.resolve()'s AMBIGUOUS branches were dropping
        danger_level from each candidate tuple entirely (fixed in
        wcl_resolver.py to return (name, syntax, danger_level) instead of
        just (name, syntax)) -- this method now reads that third field.

        Still deliberately conservative: only fires when a RESOLVED result
        (unchanged) OR an AMBIGUOUS candidate list contains at least one
        destructive command. A WCL miss/UNRESOLVED, or an AMBIGUOUS list
        with no destructive candidate at all, still means there's nothing
        concrete to warn about, so Tier A's answer stands unchallenged.
        Deliberately NOT extended to "caution"-level WCL overlaps (RESOLVED
        or AMBIGUOUS) -- see the original scoping comment at this method's
        call site for why that would just be noise, not a safety gap.
        """
        if self.wcl_resolver is None:
            return None
        wcl_result = self.wcl_resolver.resolve(user_prompt)
        status = wcl_result.get("status")

        if status == "RESOLVED":
            if wcl_result.get("danger_level") != "destructive":
                return None
            wcl_cmdlet = wcl_result.get("command") or ""
            if is_equivalent(intent, wcl_cmdlet):
                return None  # same real action, just cross-listed -- not a shadow
            return self._destructive_shadow_question(intent, user_prompt, wcl_cmdlet)

        if status == "AMBIGUOUS":
            # Each candidate is (name, syntax, danger_level) -- see
            # wcl_resolver.py's fix above. Only the FIRST destructive
            # candidate that isn't a known equivalent of Tier A's own pick
            # is used for the question text; if several destructive
            # candidates exist, naming all of them would make the
            # question harder to read, not clearer, and the question
            # already tells the user to rephrase if none of these match
            # what they meant.
            for candidate in wcl_result.get("candidates", []):
                if len(candidate) < 3:
                    continue  # defensive: malformed/older-shape candidate, skip rather than crash
                name, _syntax, danger_level = candidate[0], candidate[1], candidate[2]
                if danger_level != "destructive":
                    continue
                if is_equivalent(intent, name):
                    continue
                return self._destructive_shadow_question(intent, user_prompt, name)
            return None

        # UNRESOLVED (or any other status): nothing concrete to warn
        # about, so Tier A's answer stands unchallenged -- same as always.
        return None

    def _destructive_shadow_question(self, intent: str, user_prompt: str, wcl_cmdlet: str) -> str:
        """Builds the actual clarifying question text -- shared by both
        the RESOLVED and AMBIGUOUS branches of _check_destructive_shadow()
        above so the two paths can't drift into differently-worded
        questions for what is, from the user's point of view, the exact
        same situation."""
        tier_a_description = _intent_meta(intent).get("description", intent)
        return (
            f"Before I do that -- \"{user_prompt}\" could mean two different "
            f"things: {tier_a_description.lower()} (what I'd normally do), "
            f"or run {wcl_cmdlet}, which is a destructive Windows command. "
            f"Which one did you mean? You can also rephrase to be specific."
        )

    def _commit_history(self, user_prompt: str, assistant_note: str):
        """Never poison history with a turn that failed — only call this
        once the whole turn's outcome (including any error text) is known.

        PERFORMANCE: capped at 4 entries (2 exchanges), down from 8. Only
        tier-2 classification uses history at all (see classify()'s docstring),
        and it only needs enough to resolve something like "stop it" referring
        to the immediately preceding turn -- not a long conversational tail.
        Smaller history means less prompt-processing on every tier-2 call.
        """
        self.history.append({"role": "user", "content": user_prompt})
        self.history.append({"role": "assistant", "content": assistant_note})
        if len(self.history) > 4:
            self.history[:] = self.history[-4:]

    def process_request(
        self,
        user_prompt: str,
        on_output: Callable[[str], None],
        on_done: Callable[[int], None],
        on_thinking_token: Optional[Callable[[str], None]] = None,
        on_generate_token: Optional[Callable[[str], None]] = None,
        on_generate_done: Optional[Callable[[Optional[str], Optional[str]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Public entry point. Splits the message into 1+ segments on literal
        conjunction boundaries (_split_chain -- see its docstring for why
        this is deliberately not a general planner), then runs EACH segment
        through _process_single_request() in sequence, exactly as if it had
        been typed as its own separate message.

        The split is only used if EVERY resulting segment independently
        looks like a real command to the graph (_split_chain_if_viable) --
        otherwise the whole split is discarded and the original message is
        classified as ONE segment instead. Fixes a real false-split bug:
        "make a file called things, and stuff.txt" (one filename with a
        comma in it) used to become ["make a file called things",
        "stuff.txt"] on the old unconditional regex split -- "stuff.txt"
        alone has no graph signal at all, unlike a genuine second
        instruction, so it's now rejected and the message is treated as
        one segment.

        Single-segment case (the overwhelming majority of messages) behaves
        completely unchanged from before chaining existed -- this just
        calls straight through to _process_single_request() with no
        merging logic at all.

        Multi-segment case: runs strictly in order (step 2 never starts
        before step 1's dispatch has returned), since later steps may
        depend on earlier ones actually finishing (e.g. "make a folder
        called Homework and then open it" needs the folder to exist before
        OPEN_ITEM's path check can succeed). If any segment hits an error
        or ends up asking a follow-up question (self._pending gets set),
        the chain stops there rather than continuing on guesswork -- the
        remaining segments are simply not attempted, same "never guess"
        principle as everywhere else in this app. The returned dict's
        "steps" key lists every segment's own result dict, in order, so
        the UI can show a pill + narration + result per step instead of
        merging them into one undifferentiated blob.
        """
        segments = _split_chain_if_viable(user_prompt, self.graph_router)

        if len(segments) == 1:
            # No chaining involved -- identical to pre-chaining behavior.
            return self._process_single_request(
                segments[0], on_output, on_done,
                on_thinking_token, on_generate_token, on_generate_done,
            )

        steps: List[Dict[str, Any]] = []
        for segment in segments:
            step_result = self._process_single_request(
                segment, on_output, on_done,
                on_thinking_token, on_generate_token, on_generate_done,
            )
            step_result["segment_text"] = segment
            steps.append(step_result)

            if "error" in step_result:
                break
            # A follow-up question means this segment couldn't be fully
            # resolved from its own text -- stop the chain here rather than
            # attempting later segments out of order or guessing the
            # missing detail. self._pending is already set by the single
            # -turn call above, so the user's next message answers it
            # normally; the remaining segments are simply not run.
            if self._pending is not None:
                break

        # The UI reads "steps" to render one pill+narration+result block per
        # segment. Top-level "response"/"kind" mirror the LAST step's own
        # values so any caller that only looks at the top level (instead of
        # walking "steps") still gets a sane, non-empty answer rather than
        # nothing.
        last = steps[-1]
        merged = dict(last)
        merged["steps"] = steps
        merged["chained"] = True
        return merged

    def _process_single_request(
        self,
        user_prompt: str,
        on_output: Callable[[str], None],
        on_done: Callable[[int], None],
        on_thinking_token: Optional[Callable[[str], None]] = None,
        on_generate_token: Optional[Callable[[str], None]] = None,
        on_generate_done: Optional[Callable[[Optional[str], Optional[str]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Call order per turn (SEQUENTIAL -- see v2.3->v2.4 note at the top of
        this file for why this is no longer two parallel threads):

          0a. If self._pending_confirmation is set (BETA 0.3.38), a previous
              turn ended by asking "run this? (Enter to confirm)" for a
              caution/destructive WCL command -- this message is that
              answer. See _resume_pending_confirmation() below.
          0a2. Else if self._pending_graph_ask is set (BETA 0.3.40), a
              previous turn ended by asking a graph vocabulary clarifying
              question ("does X mean you want to Y?") -- this message is
              that answer. See _resume_pending_graph_ask() below.
          0b. Else if self._pending is set, a previous turn ended by asking a
             fixed follow-up question and THIS message is the answer --
             skip classification entirely and resolve the missing slot
             directly (extractor.resolve_missing_slot).
          1. Otherwise: Tier 1 + Tier 2 classify() (schema-constrained,
             unchanged) decide the intent.
          2. extract_slots() (plain regex, unchanged) fills what it can from
             the user's own text. If a required slot is still missing, a
             fixed question is asked and the turn pauses in self._pending --
             the model is never asked to guess or invent the value.
          2b. If the resolved intent is a WCL command with danger_level
              "caution" or "destructive" (BETA 0.3.38), and every slot WAS
              found, don't dispatch yet -- ask for a plain Enter/"yes" to
              confirm first (self._pending_confirmation), showing exactly
              what would run. "safe" commands are unaffected and still
              dispatch immediately, same as before this session.
          3. stream_thinking() runs LAST, told what was actually decided (or
             that a question is about to be asked), and narrates that in one
             streamed sentence.
          4. Dispatch: slot-fill'd command executes, or GENERATE_FILE routes
             to generator.py (currently unreachable -- see
             _DISABLED_CATEGORIES; on_generate_* kept for when it's turned
             back on).

        PowerShell output streams via on_output/on_done (unchanged).
        Returns a dict describing what happened, for the UI's chat bubble.
        """
        if self._pending_confirmation is not None:
            return self._resume_pending_confirmation(
                user_prompt, on_output, on_done, on_thinking_token,
                on_generate_token, on_generate_done,
            )

        if self._pending_graph_ask is not None:
            return self._resume_pending_graph_ask(
                user_prompt, on_output, on_done, on_thinking_token,
                on_generate_token, on_generate_done,
            )

        if self._pending_recording_choice is not None:
            return self._resume_recording_choice(
                user_prompt, on_output, on_done, on_thinking_token,
                on_generate_token, on_generate_done,
            )

        if self._pending is not None:
            return self._resume_pending(
                user_prompt, on_output, on_done, on_thinking_token,
                on_generate_token, on_generate_done,
            )

        # ── Canned greeting/closing reply (latency fix) ─────────────────
        # Runs before EVERYTHING else, including the scheduling/conditional
        # pre-checks below -- a pure "hey"/"thanks" can never plausibly be
        # a schedule or conditional request, so there's no ordering risk in
        # checking this first. Skips BOTH the classify() LLM call and the
        # thinking LLM call entirely for these exact phrasings -- see
        # extractor.py's canned_reply() docstring for the measured
        # prompt_eval cost this avoids and why the match set is
        # deliberately narrow (real content alongside a greeting word
        # never matches, falls through to the real pipeline unchanged).
        canned = canned_reply(user_prompt)
        if canned is not None:
            self._commit_history(user_prompt, canned)
            return {"thinking": "", "response": canned, "kind": "chat"}

        # ── Macro trigger / "start seeing" / "stop seeing" pre-checks ────
        # Run BEFORE the scheduling/conditional pre-check below, same
        # reasoning as that block's own comment: these have their own
        # dedicated shape (a single bare saved-macro word, or a fixed
        # "start/stop seeing" phrasing) that has nothing to do with time
        # expressions or conditionals, so ordering between the two groups
        # doesn't matter -- neither can steal the other's input. See
        # macro_recorder.py's module docstring for why this is a pre-check
        # here instead of graph_router.py Tier A intents (confirmed live
        # this session: putting "start"/"stop"-keyed phrasings in the
        # graph measurably distorts LAUNCH_APP/KILL_PROCESS's own scoring
        # on those same extremely common words).
        bare_word = user_prompt.strip().lower()
        if bare_word and " " not in bare_word:
            from macro_recorder import list_macros
            if bare_word in list_macros():
                slots = {"macro_name": bare_word}
                return self._handle_missing_or_dispatch(
                    "RUN_MACRO", user_prompt, slots,
                    on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
                )

        if looks_like_start_seeing(user_prompt):
            slots = extract_slots("START_SEEING", user_prompt)
            return self._handle_missing_or_dispatch(
                "START_SEEING", user_prompt, slots,
                on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
            )

        if looks_like_stop_seeing(user_prompt):
            slots = extract_slots("STOP_SEEING", user_prompt)
            return self._handle_missing_or_dispatch(
                "STOP_SEEING", user_prompt, slots,
                on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
            )

        # ── "start listening" / "stop listening" pre-checks ──────────────
        # Same pre-check shape and same reasoning as start/stop seeing
        # just above (see looks_like_start_listening()'s own docstring in
        # extractor.py) -- checked right alongside that block since both
        # are "fixed phrasing, never graph vocabulary" cases.
        if looks_like_start_listening(user_prompt):
            slots = extract_slots("START_LISTENING", user_prompt)
            return self._handle_missing_or_dispatch(
                "START_LISTENING", user_prompt, slots,
                on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
            )

        if looks_like_stop_listening(user_prompt):
            slots = extract_slots("STOP_LISTENING", user_prompt)
            return self._handle_missing_or_dispatch(
                "STOP_LISTENING", user_prompt, slots,
                on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
            )

        # ── "function" pre-check (routes straight to GENERATE_FILE) ──────
        # BETA 0.3.56: bypasses Tier A's graph scoring entirely for any
        # message mentioning "function" -- see extractor.py's
        # looks_like_function_creation() docstring for the full
        # reasoning (this closes STATUS.md's 0.3.55 "not yet fixed"
        # flag: "create a function called calculator" scoring below
        # CONFIDENCE_THRESHOLD because a specific name dilutes the
        # query's own TF-IDF vector). Same pre-check shape as the
        # start/stop seeing/listening checks just above -- GENERATE_FILE
        # has "slots": [] by design, so extract_slots() here always
        # returns {} (never None, never triggers the missing-slot ask
        # path), and _dispatch()'s own extract_explicit_name() check
        # (BETA 0.3.55) still runs normally on the other side of this --
        # a bare "create a function" with no name still asks "what
        # should I name it?" exactly as it already does for every other
        # GENERATE_FILE request reached the normal way.
        if looks_like_function_creation(user_prompt):
            slots = extract_slots("GENERATE_FILE", user_prompt)
            return self._handle_missing_or_dispatch(
                "GENERATE_FILE", user_prompt, slots,
                on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
            )

        # ── Ambiguous bare "start/stop recording" pre-checks ─────────────
        # Only ever reached once BOTH the seeing/watching AND listening/
        # dictating checks above already returned False -- see
        # extractor.py's looks_like_ambiguous_start_recording()/
        # looks_like_ambiguous_stop_recording() docstrings for why this is
        # the genuinely irreducible leftover case, not a weaker version of
        # the checks above.
        if looks_like_ambiguous_stop_recording(user_prompt):
            # Runtime state actually answers this one, unlike "start" --
            # by the time someone says "stop," something real either is or
            # isn't currently running, which is information text alone
            # never has. Only asks if BOTH happen to be active at once
            # (unusual, but not prevented anywhere today) or NEITHER is
            # (nothing to stop -- say so plainly rather than guessing).
            macro_active = self.app_controller._active_recorder is not None
            dictation_active = self.app_controller._active_dictation is not None
            if macro_active and not dictation_active:
                slots = extract_slots("STOP_SEEING", user_prompt)
                return self._handle_missing_or_dispatch(
                    "STOP_SEEING", user_prompt, slots,
                    on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
                )
            if dictation_active and not macro_active:
                slots = extract_slots("STOP_LISTENING", user_prompt)
                return self._handle_missing_or_dispatch(
                    "STOP_LISTENING", user_prompt, slots,
                    on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
                )
            if not macro_active and not dictation_active:
                msg = "Nothing's currently recording -- nothing to stop."
                self._commit_history(user_prompt, msg)
                return {"thinking": "", "response": msg, "kind": "chat"}
            # Both active at once -- the one real case text can't resolve.
            self._pending_recording_choice = {"mode": "stop", "original_text": user_prompt}
            question = ("Both a macro recording and dictation are running -- "
                        "stop the macro, or stop dictation?")
            self._commit_history(user_prompt, question)
            return {"thinking": "", "response": question, "kind": "chat"}

        if looks_like_ambiguous_start_recording(user_prompt):
            # Nothing's running yet to check state against -- this is the
            # genuinely irreducible case, see extractor.py's
            # looks_like_ambiguous_start_recording() docstring. Ask once
            # rather than guess either way.
            self._pending_recording_choice = {"mode": "start", "original_text": user_prompt}
            question = ("Recording clicks to save as a macro, or recording/dictating "
                        "what you say?")
            self._commit_history(user_prompt, question)
            return {"thinking": "", "response": question, "kind": "chat"}

        # ── Scheduling / conditional pre-check ──────────────────────────
        # Runs BEFORE both the override parser and graph classification.
        # This has to happen first, not after a graph miss, because the
        # graph's content-word matching has no concept of time or
        # conditional structure and will happily (wrongly) match part of
        # a time/conditional message to an unrelated straight command --
        # confirmed live: "open notepad at 3pm" used to silently match
        # OPEN_ITEM and fire immediately, dropping "at 3pm" entirely. See
        # extractor.py's find_time_expression()/looks_conditional()
        # docstrings for the full rationale. A message with neither shape
        # falls through completely unchanged to the override/graph/LLM
        # pipeline below -- this can never steal an ordinary command.
        # BETA 0.3.48: SET_TIMER is checked FIRST, before the generic
        # SCHEDULE_COMMAND branch below -- a bare "set a timer for 10
        # minutes" / "remind me in 20 minutes" has no real command
        # attached to it, and letting SCHEDULE_COMMAND claim it first
        # would store "remind me" as command_text and silently
        # web-search that exact phrase the moment the timer fires (see
        # extractor.py's looks_like_bare_timer() docstring for how this
        # was found). Only messages matching that specific bare-timer
        # shape are diverted here; anything with a real command attached
        # ("shut down in 10 minutes") still falls through to
        # SCHEDULE_COMMAND unchanged, since looks_like_bare_timer()
        # returns None for those.
        bare_timer = looks_like_bare_timer(user_prompt)
        if bare_timer:
            slots = extract_slots("SET_TIMER", user_prompt)
            return self._handle_missing_or_dispatch(
                "SET_TIMER", user_prompt, slots,
                on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
            )

        time_match = find_time_expression(user_prompt)
        if time_match:
            slots = extract_slots("SCHEDULE_COMMAND", user_prompt)
            return self._handle_missing_or_dispatch(
                "SCHEDULE_COMMAND", user_prompt, slots,
                on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
            )

        if looks_conditional(user_prompt):
            slots = extract_slots("CONDITIONAL_COMMAND", user_prompt)
            return self._handle_missing_or_dispatch(
                "CONDITIONAL_COMMAND", user_prompt, slots,
                on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
            )

        if looks_like_cancel_scheduled(user_prompt):
            slots = extract_slots("CANCEL_SCHEDULED", user_prompt)
            return self._handle_missing_or_dispatch(
                "CANCEL_SCHEDULED", user_prompt, slots,
                on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
            )

        override = parse_command_override(user_prompt)
        if override:
            intent, remainder = override
            meta = _intent_meta(intent)
            # CHAT and ASK_CONTEXT have no slots/dispatch path of their own --
            # an override to either doesn't really mean anything, so just
            # fall through to normal classification rather than special-casing it.
            if intent not in ("CHAT", "ASK_CONTEXT"):
                slots = extract_slots(
                    intent, remainder or user_prompt,
                    wcl_variables=meta.get("slots") if intent.startswith("WCL_") else None,
                ) if meta["kind"] != "generate" else {}
                if slots is None:
                    question = MISSING_SLOT_QUESTIONS.get(intent, "Could you give me a bit more detail?")
                    self._pending = {"intent": intent, "original_text": remainder or user_prompt}
                    self._commit_history(user_prompt, question)
                    return {"thinking": "", "response": question, "kind": "chat"}

                context = _decision_context(meta, slots)
                return self._dispatch_or_confirm(
                    intent, user_prompt, slots, context,
                    on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
                )

        # GRAPH FIRST: a deterministic phrasing/word-overlap match against
        # TOKI's own 59 intents, no model call at all. On a miss, try the
        # windows_command_library resolver (separate graph, tiered exact/
        # synonym/fuzzy matching -- see wcl_resolver.py) before ever
        # falling through to the LLM. Only a RESOLVED result with zero
        # remaining {variables} is safe to auto-dispatch -- AMBIGUOUS,
        # UNRESOLVED, and RESOLVED-but-needs-slot-filling all fall through
        # to OllamaRouter.classify() exactly like any other miss.
        classification = None
        if self.graph_router is not None:
            classification = self.graph_router.classify(user_prompt)

        # priority.md #11 fix: a confident Tier A hit above is a WORD-
        # OVERLAP match only -- it has no idea whether a completely
        # different, genuinely destructive WCL command is what the user
        # actually meant (confirmed live: "wipe disk 2" -> DISK_USAGE,
        # "bitlocker lock mount point D" -> LOCK_WORKSTATION, "disable
        # dedup volume on E" -> VOLUME_UP -- all silently wrong, all
        # destructive-adjacent). Before trusting a Tier A hit, cross-
        # check wcl_resolver.py (same cheap local graph lookup, no LLM
        # call, so this doesn't touch the graph-first/LLM-last latency
        # goal) for a RESOLVED, danger_level=="destructive" match on the
        # SAME query. If one exists and its cmdlet ISN'T a known
        # equivalent of Tier A's pick (see tier_a_wcl_map.py -- e.g.
        # KILL_PROCESS/Stop-Process legitimately IS the same real action,
        # not a shadow), this is genuine shadowing: don't dispatch Tier
        # A's answer, ask instead. Scoped to danger_level=="destructive"
        # only (not "caution") on purpose -- "caution"-level overlaps
        # (e.g. COPY_ITEM vs WCL's own "caution"-rated Copy-Item, which
        # IS the same action anyway) are common and would just be noise;
        # destructive is where a wrong silent answer actually matters.
        if classification is not None and self.wcl_resolver is not None:
            shadow_question = self._check_destructive_shadow(classification["intent"], user_prompt)
            if shadow_question is not None:
                self._commit_history(user_prompt, shadow_question)
                return {"thinking": "", "response": shadow_question, "kind": "chat"}

        if classification is None and self.wcl_resolver is not None:
            wcl_result = self.wcl_resolver.resolve(user_prompt)
            if wcl_result["status"] == "RESOLVED":
                var_names = re.findall(r"\{(\w+)\}", wcl_result["syntax"])
                is_safe = wcl_result.get("danger_level") == "safe"
                # BETA 0.3.35 fix -- CONFIRMED LIVE, serious: the zero-
                # variable branch here ("not var_names") had NO
                # danger_level check at all, unlike the single-variable
                # branch right next to it. Traced this all the way through
                # to _dispatch(): extract_slots() falls through to its
                # universal "no slots needed for this intent -> return {}"
                # default for any WCL_ intent with wcl_variables == [],
                # which is not None, so process_request() goes straight to
                # _dispatch() with ZERO further danger_level check anywhere
                # else in the file -- confirmed by reading _dispatch()
                # itself and grep'ing every use of danger_level in this
                # file. Concretely: "run diskpart" (diskpart is
                # danger_level "destructive", zero variables) resolves
                # RESOLVED, MISSES Tier A entirely (so
                # _check_destructive_shadow() never even runs -- that
                # guard only fires when Tier A ALSO produced a
                # classification to compare against), and would have been
                # silently auto-dispatched with no confirmation of any
                # kind. Same gap for all 8 zero-variable "caution" commands
                # and the other 4 zero-variable "destructive" ones
                # (Clear-TempFiles/"Clean Temp Files"/regedit/restart --
                # confirmed those specific 4 phrasings happen to resolve
                # AMBIGUOUS rather than RESOLVED given the CURRENT alias
                # data, which is WHY they weren't already caught, not
                # because anything actually prevented them -- a future
                # alias addition to disambiguate any of them would have
                # silently reopened this exact hole).
                #
                # Fix: require danger_level == "safe" for the zero-
                # variable case too, exactly like the single-variable
                # case already does. A zero-variable "caution"/
                # "destructive" WCL command now falls all the way through
                # to the LLM router like any other miss -- it is NOT
                # given a shadow-guard question or any other special
                # handling here; it just stops being silently
                # auto-dispatched. Improving what happens to it next
                # (asking, or checking it some other way) is a separate,
                # later decision, not bundled into this fix.
                #
                # BETA 0.3.37: extended to 2-variable "safe" commands too
                # (extract_slots()/_extract_wcl_slots_pair() in
                # extractor.py does the actual quote-pair/"X to Y"
                # extraction). Made safe to extend ONLY because of the
                # SAME session's _ensure_quoted_placeholders() fix in
                # _dispatch() below -- before that fix, extending
                # auto-dispatch eligibility to more commands would have
                # meant extending a command-injection-vulnerable surface,
                # not just a routing-correctness one. 3+ variable
                # commands remain out of scope (7 of them, all
                # "destructive" anyway -- would be blocked by `is_safe`
                # regardless even if extract_slots() supported them).
                #
                # BETA 0.3.38: "eligible" here no longer requires
                # danger_level == "safe" at all -- it now means "we're
                # willing to ATTEMPT this command" (right variable count,
                # no code-like variable), regardless of danger_level.
                # What differs by danger_level is what happens once slots
                # are actually found: "safe" still dispatches immediately
                # (unchanged); "caution"/"destructive" now pause for an
                # explicit go-ahead first (see _ask_for_confirmation() /
                # the dispatch-time branch a few hundred lines down) --
                # they used to fall all the way through to the LLM
                # instead, silently never running at all. This is a
                # DELIBERATE, requested product decision (a quick
                # confirmation step unlocks the caution/destructive WCL
                # library instead of leaving it permanently unreachable),
                # not a relaxation of the injection-safety work above --
                # _ensure_quoted_placeholders()/_escape_ps_slot() and the
                # code-like-variable blocklist apply identically
                # regardless of danger_level, so this doesn't reopen
                # anything BETA 0.3.37 closed.
                if var_names:
                    eligible = (
                        len(var_names) in (1, 2)
                        and not any(_is_wcl_code_like_var(v) for v in var_names)
                    )
                else:
                    eligible = True
                if eligible:
                    # A single- or 2-variable command the graph resolved
                    # with full confidence, or a zero-variable command --
                    # at ANY danger_level (BETA 0.3.38). Slot value still
                    # has to be found in extract_slots() below like any
                    # other intent; a miss there falls through to a
                    # normal missing-slot question, same as everything
                    # else -- this block only decides ELIGIBILITY to try,
                    # never guarantees an immediate dispatch (see
                    # wcl_danger_level below for what actually gates
                    # that). ALL multi-var (3+) commands deliberately
                    # still fall through past this whole block untouched
                    # -- see extract_slots()'s _extract_wcl_slots()
                    # docstring for why that split is where it is.
                    wcl_intent = f"WCL_{wcl_result['command']}"
                    WCL_COMMANDS[wcl_intent] = {
                        "description": f"Run {wcl_result['command']}",
                        "kind": "powershell",
                        "template": wcl_result["syntax"],
                        "slots": var_names,
                        "reversible": is_safe,
                        "wcl_category": wcl_result.get("category", ""),
                        # BETA 0.3.38: carried through so the dispatch-time
                        # branch (a few hundred lines down) can decide
                        # confirm-first vs. dispatch-immediately without
                        # re-querying wcl_resolver.
                        "wcl_danger_level": wcl_result.get("danger_level"),
                        # Tier 2 (wcl_resolver.py) already isolated the
                        # exact value when it stripped a trailing alias
                        # match off the raw query (e.g. "view more
                        # notes.txt" -> alias "view more" + value
                        # "notes.txt") -- pass it through so extract_slots()
                        # doesn't have to re-guess the same boundary from
                        # scratch via generic pattern matching, which has
                        # no way to know "view more" is the command's own
                        # name and not part of the value. None for tier 1/
                        # 3/5 matches, which never needed to strip anything
                        # (the whole query WAS the alias).
                        "wcl_stripped_value": wcl_result.get("stripped_value"),
                    }
                    classification = {"intent": wcl_intent}
            # AMBIGUOUS / UNRESOLVED / resolved-with-multiple-variables /
            # resolved-with-one-variable-but-not-danger_level=="safe": no
            # safe single answer to auto-dispatch, so just fall through
            # below -- same as a plain graph miss always has.

        # On a graph + WCL miss, fall through to the LLM to decide
        # CHAT / GENERATE / ASK_CONTEXT / a command -- this is the
        # documented architecture (see README/STATUS.md) and was
        # previously short-circuited here by a "testing phase" change
        # that was never reverted: it routed EVERY graph/WCL miss straight
        # to GraphRouter.classify_or_ask() instead, which only ever
        # produces a command guess or a "what are you trying to do with
        # X" question -- it has no CHAT/GENERATE concept at all. That
        # broke plain conversation outright: confirmed directly, "hi how
        # is it going" produced "does 'going','hi' mean you want to count
        # files?" instead of a normal reply, because graph_router has no
        # way to recognize a message as conversational -- it can only
        # score command-vocabulary overlap, and CHAT/GENERATE were never
        # in its graph to begin with (see NON_GRAPH_CATEGORIES in
        # graph_router.py). The old in-code comment claiming "CHAT/
        # GENERATE... still reach OllamaRouter normally, a few lines
        # below this block" was simply wrong about what the code actually
        # did once this branch was reached.
        #
        # Fix: call the LLM first, same as documented. The graph-based
        # classify_or_ask()/staging mechanism (real, deliberately built --
        # see vocab_staging.py) is kept as a genuine fail-open fallback,
        # used only if the LLM call itself errors (e.g. Ollama isn't
        # running) -- same fail-open pattern used everywhere else in this
        # file for graph_router/wcl_resolver construction failures.
        if classification is None:
            # Caught by the existing test suite while validating the
            # BETA 0.3.47 change below (test_wcl_slot_filling_integration.py):
            # a WCL match that's RESOLVED or AMBIGUOUS but genuinely can't
            # be auto-dispatched (3+ variables, a destructive command the
            # user just cancelled, etc.) is NOT the same situation as a
            # real graph+WCL miss -- wcl_resolver found a real, specific
            # command here, it just can't safely fill it in yet (see the
            # "still the open follow-up milestone" comment a few dozen
            # lines up). Defaulting THAT straight to a web search would
            # mean literally googling the user's raw system command text
            # instead of honestly saying slot-filling for it isn't wired
            # up yet -- worse than the old behavior, not better. Only a
            # genuine total miss (UNRESOLVED, or no WCL resolver at all)
            # falls through to the search-first fallback below.
            wcl_had_real_candidate = (
                self.wcl_resolver is not None
                and wcl_result["status"] in ("RESOLVED", "AMBIGUOUS")
            )
            if wcl_had_real_candidate:
                msg = "I found a matching command but can't safely fill in all its details yet -- try rephrasing it more directly."
                self._commit_history(user_prompt, msg)
                return {"thinking": "", "response": msg, "kind": "chat"}

            # BETA 0.3.47: Ollama is now the RARE path (project owner has
            # mostly retired it -- only kept as a specific fallback for
            # whenever it happens to be reachable), not the default one
            # this whole block was written against. The old pre-LLM gate
            # below used to short-circuit straight to a graph-guess
            # clarifying question ("does X mean Y?") for ANY below-
            # threshold candidate, BEFORE ever trying Ollama, specifically
            # to stop Ollama's free-text CHAT/GENERATE call from
            # fabricating a false "Done, I've terminated..." narration for
            # command-shaped text (see git history / STATUS.md for the
            # original "kill notepad.exe" bug this fixed). That protection
            # only matters when Ollama is actually the one being asked to
            # narrate -- if it's unreachable anyway there's no fabrication
            # risk to guard against.
            #
            # Root cause of the NEW bug this replaces (confirmed live,
            # reproduced directly against the shipped graph): with Ollama
            # down, this gate fired on nearly every plain question, not
            # just real command near-misses, because graph_router's
            # bag-of-words scoring cannot tell "kill notepad.exe" (real
            # KILL_PROCESS candidate, confidence 0.172) apart from "what
            # is the capital of mexico" (bogus LIST_FILES candidate,
            # confidence 0.381, driven entirely by the filler word "what")
            # or "what does mexico and capital mean" (bogus PATH_EXISTS
            # candidate, confidence 0.315, off nothing but "does"). Tried
            # three different automatic ways to separate real near-misses
            # from this -- raising CONFIDENCE_THRESHOLD for the ask path,
            # requiring the matched word have high idf, requiring a
            # minimum query-word coverage ratio -- and checked each against
            # the real graph: none of them cleanly separates the two
            # (the bogus cases scored HIGHER on every metric than some of
            # the genuine documented near-misses like "kill notepad.exe").
            # This is an inherent limit of scoring word-overlap with no
            # actual language understanding, not a tunable bug -- which is
            # exactly why this used to lean on Ollama for it.
            #
            # New behavior: try Ollama first, unconditionally, same as
            # this method's own comment above already says is the
            # documented design. If it's reachable, its real judgment
            # (CHAT/GENERATE/ASK_CONTEXT/a command) is used exactly as
            # before -- this IS the "specific fallback" case. If it's not
            # reachable (now the common case), don't guess a specific
            # wrong command out loud -- default to SEARCH_WEB with the
            # user's own text as the query. The graph's below-threshold
            # candidate (if any) is still logged into vocab_staging.jsonl
            # so the 👍/👎 vocabulary-learning loop doesn't lose data, it
            # just no longer interrupts the user with a guess about it.
            llm_result = self.router.classify(user_prompt, history=self.history)
            if "error" not in llm_result:
                classification = llm_result
            else:
                ask_result = (
                    self.graph_router.classify_or_ask(user_prompt)
                    if self.graph_router is not None
                    else {"ask": "", "unknown_words": []}
                )
                if "intent" in ask_result:
                    classification = ask_result
                else:
                    unknown_words = ask_result.get("unknown_words", [])
                    candidate = ask_result.get("candidate")
                    if unknown_words:
                        log_graph_ask(user_prompt, unknown_words, candidate)

                    # BETA 0.3.48: before defaulting to a raw-text search,
                    # take one real, deterministic look at whether this is
                    # actually an app-launch request the graph just didn't
                    # have the vocabulary for -- e.g. "pull up obs",
                    # "yo get discord going", any phrasing that never put
                    # "open/launch/start/run" anywhere near a name the
                    # TF-IDF vocabulary recognizes. This is NOT another
                    # fuzzy text-similarity guess (see graph_router.py's
                    # CONFIDENCE_THRESHOLD comment for why that approach
                    # has a hard ceiling) -- it's a ground-truth check
                    # against what's actually installed (Get-StartApps via
                    # app_controller.app_exists), reusing the exact same
                    # cascade (resolve_open_target) the LAUNCH_APP/OPEN_ITEM
                    # dispatch path below already trusts, just run earlier.
                    # Skipped entirely if the user already used the
                    # explicit ''/"" convention -- same as every other
                    # resolve_open_target() call site, that convention
                    # means "don't second-guess me" and should still win.
                    resolved_open = (
                        None if has_explicit_open_convention(user_prompt)
                        else resolve_open_target(user_prompt, self.app_controller.app_exists)
                    )
                    if resolved_open is not None:
                        # Real app (or real file/folder) found -- treat
                        # this exactly like a confident graph/LLM hit and
                        # fall through to normal dispatch below. The
                        # LAUNCH_APP/OPEN_ITEM cascade a few dozen lines
                        # down re-resolves the same target via the same
                        # cache (cheap -- see app_control.py's indefinite
                        # per-process cache), so no slot data needs to be
                        # threaded through here; it's the exact same
                        # re-resolve-on-commit pattern that cascade already
                        # uses for its own retry-once-on-miss path.
                        classification = {"intent": resolved_open["intent"]}
                    else:
                        # Run the search directly rather than synthesizing a
                        # {"intent": "SEARCH_WEB"} classification and letting it
                        # fall through the normal dispatch pipeline -- caught by
                        # the existing test suite: that pipeline's "did an
                        # intent get dispatched" check is also how the
                        # confirmation-cancel flow (_resume_pending_confirmation,
                        # "anything but yes cancels silently") verifies nothing
                        # ran. A cancel reply like "no thanks" has no graph/WCL
                        # match either, so it used to land right here too -- it
                        # should stay a true no-op, not turn into an actual
                        # Chrome window opening a search for "no thanks".
                        search_query = user_prompt.strip()
                        if search_query:
                            result_text = self.dispatcher.search.search(query=search_query)
                            self._commit_history(user_prompt, result_text)
                            return {"thinking": "", "response": result_text, "kind": "api",
                                    "intent": "SEARCH_WEB", "result": result_text}
                        msg = "I didn't catch that -- can you rephrase?"
                        self._commit_history(user_prompt, msg)
                        return {"thinking": "", "response": msg, "kind": "chat"}
                        # classification is guaranteed non-None past this
                        # point -- either resolved_open set it just above,
                        # or one of the two returns right above this
                        # comment already exited the function.

        if "error" in classification:
            return {"error": classification["error"]}

        intent = classification["intent"]

        # The actual fix for "asked it to open an app, it tried to open a
        # folder instead": the graph/LLM classification above makes ONE
        # one-shot guess between LAUNCH_APP and OPEN_ITEM based on word-
        # overlap against a fixed phrasing list, which misfires for any
        # app name that wasn't specifically added to that list (confirmed
        # live: "steam"/"vscode"/"obs" all lost to OPEN_ITEM, which only
        # knows how to look on Desktop/D:\). Don't trust that guess for
        # either intent -- run a real, deterministic cascade instead: does
        # an app by this name actually exist (Get-StartApps-backed, see
        # app_control.py)? If not, does a file/folder by this name
        # actually exist in the sandbox? Whichever real answer wins
        # OVERRIDES the classifier's guess. resolve_open_target() already
        # skips itself (returns None immediately) if the user was
        # explicit via the '' / "" quote convention, so an explicit
        # instruction is never second-guessed here.
        open_target_slots: Optional[Dict[str, Any]] = None
        if intent in ("LAUNCH_APP", "OPEN_ITEM") and not has_explicit_open_convention(user_prompt):
            resolved_open = resolve_open_target(user_prompt, self.app_controller.app_exists)
            if resolved_open is None:
                # First look missed -- before concluding "doesn't exist,"
                # force a real rescan of both caches and try exactly once
                # more. Covers the case where the target was installed/
                # created earlier THIS session, after the caches were last
                # populated (Get-StartApps and FileIndex are both "fetch
                # once, reuse" -- see their own docstrings -- so neither
                # would notice on its own). Deliberately only one retry,
                # only on a miss: a confident first-try hit never pays this
                # cost, and a second miss after a genuine rescan means the
                # cascade's "ask, don't guess" behavior below is correct,
                # not stale.
                self.app_controller.invalidate_app_cache()
                file_index.invalidate()
                resolved_open = resolve_open_target(user_prompt, self.app_controller.app_exists)
            if resolved_open is not None:
                intent = resolved_open["intent"]
                open_target_slots = {k: v for k, v in resolved_open.items() if k != "intent"}
            elif is_anaphoric_reference(user_prompt) and resolve_anaphoric_target("OPEN_ITEM", self._last_touched):
                # "now open it" / "open that" -- the cascade above found no
                # real app AND no real file/folder by whatever name it
                # extracted, but the message itself is a back-reference,
                # not a real name at all (see extract_open_target_name's
                # own guard, which is exactly why this branch is reachable:
                # it now correctly returns None for a bare "it" instead of
                # handing the cascade the whole raw sentence to fuzzy-match
                # against real files). Resolve against the last thing TOKI
                # itself created/touched this session instead of asking a
                # question the user will find confusing ("didn't I just
                # tell you?") -- bounded and safe, see
                # resolve_anaphoric_target()'s own docstring for why.
                intent = "OPEN_ITEM"
                open_target_slots = resolve_anaphoric_target("OPEN_ITEM", self._last_touched)
            else:
                # Neither a real app nor a real file/folder by this name
                # exists, and there's nothing recent to resolve a pronoun
                # against either -- ask, don't silently commit to whichever
                # the one-shot classifier happened to guess (that guess is
                # exactly the bug this cascade replaces). Only reachable
                # here because has_explicit_open_convention() already
                # confirmed the user did NOT use the '' / "" literal
                # convention -- an explicit quote skips this whole block
                # and falls through to the original classified intent's
                # own extract_slots() a few lines down, completely
                # unchanged. (Caught directly while testing: conflating
                # "cascade found nothing" with "user was explicit" here
                # used to make an explicit "open 'SomeApp'" incorrectly
                # ask instead of dispatching, even with a matching app.)
                note = (
                    "I couldn't find an app or a file/folder by that name -- "
                    "could you double-check it?"
                )
                self._commit_history(user_prompt, note)
                return {"thinking": "", "response": note, "kind": "chat"}

        meta = _intent_meta(intent)

        if meta["kind"] == "chat":
            thinking_text = self._run_thinking(
                user_prompt,
                "The user is just chatting with you -- respond naturally and conversationally. "
                "If they're asking about something that just happened, answer based on the "
                "conversation history, not a guess.",
                on_thinking_token,
                history=self.history,
            )
            self._commit_history(user_prompt, thinking_text or "(chat)")
            return {"thinking": thinking_text, "response": thinking_text, "kind": "chat"}

        if meta["kind"] == "ask_context":
            # Distinct from CHAT: the user IS asking for something real, but
            # a required detail is missing and unknown (unlike the
            # MISSING_SLOT_QUESTIONS path below, which only fires once a
            # specific intent/slot has already been decided). This fires
            # earlier, when even the intent itself can't be pinned down yet.
            thinking_text = self._run_thinking(
                user_prompt,
                "The user wants something done or wants to know something, but left out "
                "a detail you need before you can tell what -- don't guess, don't just "
                "chat. Ask ONE short, specific question about exactly what's missing.",
                on_thinking_token,
                history=self.history,
            )
            self._commit_history(user_prompt, thinking_text or "(asked for more context)")
            return {"thinking": thinking_text, "response": thinking_text, "kind": "chat"}

        slots = (
            open_target_slots if open_target_slots is not None
            else (extract_slots(
                intent, user_prompt,
                wcl_variables=meta.get("slots") if intent.startswith("WCL_") else None,
                wcl_stripped_value=meta.get("wcl_stripped_value") if intent.startswith("WCL_") else None,
            ) if meta["kind"] != "generate" else {})
        )

        if slots is None and intent in ANAPHORA_ELIGIBLE_INTENTS and is_anaphoric_reference(user_prompt):
            # "delete it" / "read that" -- extract_slots() found no
            # extractable name (correctly -- there isn't one), but the
            # message is a back-reference. Same resolution as the
            # OPEN_ITEM cascade above: use the last thing TOKI itself
            # created/touched this session instead of asking a question,
            # if there's something real to resolve against.
            slots = resolve_anaphoric_target(intent, self._last_touched)

        if slots is None:
            # Required detail missing — ask a FIXED question, don't guess
            # and don't ask the model to improvise one.
            question = MISSING_SLOT_QUESTIONS.get(intent, "Could you give me a bit more detail?")
            self._pending = {"intent": intent, "original_text": user_prompt}
            self._commit_history(user_prompt, question)
            return {"thinking": "", "response": question, "kind": "chat"}

        context = _decision_context(meta, slots)
        # PERFORMANCE: for generate/app_control/powershell, _dispatch starts
        # thinking immediately and lets it overlap with the real work below.
        # api-kind intents are the one exception -- see _dispatch()'s
        # docstring for why those now run the real call first and ground
        # thinking in the actual result instead of overlapping.
        return self._dispatch_or_confirm(
            intent, user_prompt, slots, context,
            on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
        )

    # BETA 0.3.40: closes the loop _pending_graph_ask always started but
    # nothing ever finished. classify_or_ask()'s "ask" branch and
    # log_graph_ask() were being reached correctly (staged rows exist in
    # vocab_staging.jsonl to prove it) but confirm_pending_graph_ask()/
    # reject_pending_graph_ask() below had ZERO callers anywhere in the
    # app -- not main_widget.py (no chat window, no thumbs buttons, per
    # its own module docstring), not the old app.py this replaced either
    # (grepped the whole tree; the only other reference is a unit test
    # manually poking the attribute). Every clarifying question the graph
    # ever asked sat in self._pending_graph_ask forever, and because
    # _process_single_request never checked it (unlike the structurally
    # identical self._pending_confirmation / self._pending), the user's
    # actual next message -- a real command, or their yes/no answer --
    # was reclassified from scratch instead, which is the "broke my
    # script" symptom: TOKI silently ignored the fact that it had a
    # question outstanding at all.
    #
    # Same fix pattern as _resume_pending_confirmation() below (BETA
    # 0.3.38): a plain-text yes/no answers the question. This works
    # identically for typed widget input and voice, since both funnel
    # through process_request() -- no widget UI changes needed.
    _GRAPH_ASK_YES_WORDS = {
        "y", "yes", "yeah", "yep", "yup", "correct", "right", "thats it", "that's it",
    }
    _GRAPH_ASK_NO_WORDS = {
        "n", "no", "nope", "nah", "wrong", "not quite", "no thats not it", "no that's not it",
    }

    def _resume_pending_graph_ask(
        self,
        user_reply: str,
        on_output: Callable[[str], None],
        on_done: Callable[[int], None],
        on_thinking_token: Optional[Callable[[str], None]],
        on_generate_token: Optional[Callable[[str], None]],
        on_generate_done: Optional[Callable[[Optional[str], Optional[str]], None]],
    ) -> Dict[str, Any]:
        """Handles the message that answers a graph clarifying question
        (classify_or_ask's {"ask": ...} branch, staged via log_graph_ask()
        in _process_single_request above).

        By design (see vocab_staging.py's docstring), this ONLY resolves
        the staging-DB vocabulary record -- confirmed/rejected rows still
        need a manual promotion pass into toki_graph_db before they
        actually change how the graph classifies anything next time.
        A "yes" here deliberately does NOT auto-dispatch the guessed
        command: this loop was built to teach the graph vocabulary, not
        to be a second, unverified attempt at running an action the graph
        wasn't confident about in the first place. If you want confirmed
        guesses to also execute immediately, that's a real, separate
        product decision -- flag it and it can be added as its own
        explicit branch here, not folded in silently.

        A reply that ISN'T a recognizable yes/no is NOT swallowed or
        guessed at -- the staged words are marked rejected (the guess
        plainly wasn't confirmed) and the message is reprocessed as a
        brand-new turn, exactly like _resume_pending_confirmation()'s own
        fallthrough for a non-confirm reply. This is what fixes the old
        breakage: a real follow-up command typed/spoken right after a
        clarifying question now actually runs, instead of vanishing.
        """
        pending = self._pending_graph_ask
        self._pending_graph_ask = None
        reply_norm = user_reply.strip().lower()

        if reply_norm in self._GRAPH_ASK_YES_WORDS:
            if pending and pending.get("staged_ids"):
                confirm_graph_ask(pending["staged_ids"])
            note = "Got it, thanks."
            self._commit_history(user_reply, note)
            return {"thinking": "", "response": note, "kind": "chat"}

        if reply_norm in self._GRAPH_ASK_NO_WORDS:
            if pending and pending.get("staged_ids"):
                reject_graph_ask(pending["staged_ids"])
            note = "Got it, my mistake."
            self._commit_history(user_reply, note)
            return {"thinking": "", "response": note, "kind": "chat"}

        # Not a yes/no answer: the guess wasn't confirmed, so stage it as
        # rejected, then treat this message as brand new -- it may well be
        # the user's actual next command, not an answer at all.
        if pending and pending.get("staged_ids"):
            reject_graph_ask(pending["staged_ids"])
        return self._process_single_request(
            user_reply, on_output, on_done, on_thinking_token,
            on_generate_token, on_generate_done,
        )

    def confirm_pending_graph_ask(self) -> bool:
        """Wire to the UI's 👍 (like) button, shown on the response bubble
        for a graph clarifying question (kind='chat' responses that came
        from classify_or_ask's {"ask": ...} branch above). Marks every
        word from that question 'confirmed' in the staging file -- this
        does NOT touch toki_graph_db; a human still promotes confirmed
        rows into the real graph by hand (see vocab_staging.py's
        docstring). Returns False if there's no pending graph ask to
        confirm (e.g. the button was shown on the wrong turn / stale UI
        state)."""
        pending = self._pending_graph_ask
        self._pending_graph_ask = None
        if pending is None or not pending.get("staged_ids"):
            return False
        confirm_graph_ask(pending["staged_ids"])
        return True

    def reject_pending_graph_ask(self) -> bool:
        """Wire to the UI's 👎 (dislike) button on the same response. Marks
        the staged words 'rejected' so a review pass skips them. Also the
        right hook for TOKI's EXISTING general-purpose dislike button
        (flagging any wrong response) when it happens to land on a graph
        clarifying question -- same staging file either way."""
        pending = self._pending_graph_ask
        self._pending_graph_ask = None
        if pending is None or not pending.get("staged_ids"):
            return False
        reject_graph_ask(pending["staged_ids"])
        return True

    # BETA 0.3.49: resolves the one question _pending_recording_choice
    # ever asks -- see extractor.py's looks_like_ambiguous_start_recording()/
    # looks_like_ambiguous_stop_recording() docstrings for why this
    # question exists at all instead of another guessing attempt. Reply
    # matching is deliberately a small curated keyword set, same posture
    # as _GRAPH_ASK_YES_WORDS/_GRAPH_ASK_NO_WORDS just above and
    # synonyms.py's fixed table elsewhere -- this is picking one of
    # exactly two named options, not open-ended classification, so a
    # keyword table is the right tool here, not a ceiling like it is for
    # open text (see graph_router.py's CONFIDENCE_THRESHOLD docstring).
    _RECORDING_CHOICE_MACRO_WORDS = {
        "macro", "clicks", "click", "clicking", "actions", "seeing", "watching", "1", "first",
    }
    _RECORDING_CHOICE_VOICE_WORDS = {
        "say", "speak", "talk", "voice", "dictation", "dictate", "dictating",
        "listening", "listen", "2", "second",
    }

    def _resume_recording_choice(
        self,
        user_reply: str,
        on_output: Callable[[str], None],
        on_done: Callable[[int], None],
        on_thinking_token: Optional[Callable[[str], None]],
        on_generate_token: Optional[Callable[[str], None]],
        on_generate_done: Optional[Callable[[Optional[str], Optional[str]], None]],
    ) -> Dict[str, Any]:
        """Handles the reply to the one ambiguous-recording question
        _process_single_request ever asks (see the "start"/"stop" checks
        there). A reply that doesn't clearly pick one is NOT guessed at --
        same fallthrough shape as _resume_pending_graph_ask() just above:
        treat it as a brand-new message instead of swallowing what might
        be the user's real next command."""
        pending = self._pending_recording_choice
        self._pending_recording_choice = None
        reply_words = set(re.findall(r"[a-z0-9']+", user_reply.strip().lower()))

        wants_macro = bool(reply_words & self._RECORDING_CHOICE_MACRO_WORDS)
        wants_voice = bool(reply_words & self._RECORDING_CHOICE_VOICE_WORDS)

        if wants_macro and not wants_voice:
            intent = "STOP_SEEING" if pending["mode"] == "stop" else "START_SEEING"
        elif wants_voice and not wants_macro:
            intent = "STOP_LISTENING" if pending["mode"] == "stop" else "START_LISTENING"
        else:
            # Neither, or both, keywords present -- not a clean pick.
            # Same principle as every other ambiguous-reply fallthrough in
            # this file: don't guess, treat as a fresh message.
            return self._process_single_request(
                user_reply, on_output, on_done, on_thinking_token,
                on_generate_token, on_generate_done,
            )

        slots = extract_slots(intent, pending["original_text"])
        return self._handle_missing_or_dispatch(
            intent, pending["original_text"], slots,
            on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
        )

    # BETA 0.3.38: caution/destructive WCL command confirmation.
    #
    # Deliberately minimal, matching the actual product decision made in
    # chat rather than building an elaborate dialog: a plain-text chat
    # question showing the description AND the exact command that would
    # run (transparency costs nothing extra here, and matters a lot more
    # for a destructive command than a safe one), and ANY of a bare
    # Enter / a short confirm word answers "yes" -- anything else cancels
    # and that message is processed as an ordinary new turn, not treated
    # as an error. Uses "kind": "chat" for the question so it renders
    # through the exact same, already-tested code path as every other
    # plain-text response (app.py's kind == "chat" branch) -- no new UI
    # code needed, no risk of it rendering differently or being missed by
    # a kind the UI doesn't specially handle.
    _CONFIRMATION_WORDS = {
        "", "y", "yes", "yeah", "yep", "confirm", "ok", "okay", "run it", "do it",
    }

    def _dispatch_or_confirm(
        self,
        intent: str,
        user_prompt: str,
        slots: Dict[str, str],
        context: str,
        on_output: Callable[[str], None],
        on_done: Callable[[int], None],
        on_thinking_token: Optional[Callable[[str], None]],
        on_generate_token: Optional[Callable[[str], None]],
        on_generate_done: Optional[Callable[[Optional[str], Optional[str]], None]],
        skip_generate_name_check: bool = False,
    ) -> Dict[str, Any]:
        """BETA 0.3.38: the ONE place every dispatch-ready call site in
        this file routes through (instead of calling self._dispatch()
        directly) -- deliberately a single choke point so the caution/
        destructive confirmation check can't be missed at any individual
        call site (there are 4 of them: the main classify path, the
        //override path, _resume_pending(), and
        _handle_missing_or_dispatch()). The ONE exception is
        _resume_pending_confirmation()'s own dispatch call after a YES --
        that one calls self._dispatch() directly on purpose, since a
        confirmation was already just given; routing it back through here
        would ask again.

        skip_generate_name_check (BETA 0.3.55): forwarded straight to
        _dispatch() -- see that parameter's own docstring there. Only
        ever True from _resume_pending()'s GENERATE_FILE branch, after
        the "what should I name it?" question has already been asked and
        answered (or explicitly skipped) once this turn.
        """
        meta = _intent_meta(intent)
        if meta.get("wcl_danger_level") in ("caution", "destructive"):
            return self._ask_for_confirmation(intent, user_prompt, slots, context, meta)
        return self._dispatch(
            intent, user_prompt, slots, context,
            on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
            skip_generate_name_check=skip_generate_name_check,
        )

    def _ask_for_confirmation(
        self, intent: str, user_prompt: str, slots: Dict[str, str], context: str, meta: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Called instead of _dispatch() when meta["wcl_danger_level"] is
        "caution" or "destructive" and every slot was successfully found.
        Builds the actual command via the SAME _build_powershell_command()
        _dispatch() itself uses (see that function's docstring for why
        this matters -- the preview and the real thing can never drift
        apart), stores everything _dispatch() will need in
        self._pending_confirmation, and asks a plain question."""
        try:
            preview_command = _build_powershell_command(meta, slots)
        except (KeyError, ValueError, IndexError) as e:
            note = f"Done. (missing a detail: {e})"
            self._commit_history(user_prompt, note)
            return {"thinking": "", "response": note, "kind": "chat"}

        self._pending_confirmation = {
            "intent": intent, "user_prompt": user_prompt, "slots": slots, "context": context,
        }
        danger_word = "destructive" if meta.get("wcl_danger_level") == "destructive" else "a caution-level"
        question = (
            f"This is {danger_word} command -- {meta.get('description', intent)}:\n"
            f"    {preview_command}\n"
            f"Press Enter to run it, or type anything else to skip."
        )
        self._commit_history(user_prompt, question)
        return {"thinking": "", "response": question, "kind": "chat"}

    def _resume_pending_confirmation(
        self,
        user_reply: str,
        on_output: Callable[[str], None],
        on_done: Callable[[int], None],
        on_thinking_token: Optional[Callable[[str], None]],
        on_generate_token: Optional[Callable[[str], None]],
        on_generate_done: Optional[Callable[[Optional[str], Optional[str]], None]],
    ) -> Dict[str, Any]:
        """Handles the message that answers a pending confirmation
        question. Never re-classifies on a YES -- exactly like
        _resume_pending() below, the intent and slots were already
        decided last turn."""
        pending = self._pending_confirmation
        self._pending_confirmation = None
        if user_reply.strip().lower() in self._CONFIRMATION_WORDS:
            return self._dispatch(
                pending["intent"], pending["user_prompt"], pending["slots"], pending["context"],
                on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
            )
        # Anything else cancels -- silently, no separate "cancelled" note
        # first. self._pending_confirmation is already cleared above, so
        # this is a plain, ordinary, non-recursive-loop-risking turn, and
        # it keeps a "never mind" reply feeling like normal conversation
        # rather than a modal dialog that has to be dismissed before
        # anything else can happen.
        return self.process_request(
            user_reply, on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
        )

    def _resume_pending(
        self,
        user_answer: str,
        on_output: Callable[[str], None],
        on_done: Callable[[int], None],
        on_thinking_token: Optional[Callable[[str], None]],
        on_generate_token: Optional[Callable[[str], None]],
        on_generate_done: Optional[Callable[[Optional[str], Optional[str]], None]],
    ) -> Dict[str, Any]:
        """Handles the message that answers a pending follow-up question.
        Never re-classifies -- the intent was already decided last turn,
        only the missing slot value was outstanding."""
        pending = self._pending
        self._pending = None
        intent = pending["intent"]
        original_text = pending["original_text"]
        meta = _intent_meta(intent)

        if intent == "GENERATE_FILE":
            # BETA 0.3.55: GENERATE_FILE has "slots": [] (see its INTENTS
            # entry's own comment for why -- it never goes through
            # extract_slots()/resolve_missing_slot() like a template
            # intent does), so it can't reuse the generic slot-resume path
            # below at all. The "answer" here is free text meant to be
            # merged back into the ORIGINAL description, not a dict of
            # named slot values -- generator.extract_explicit_name() reads
            # straight from that merged text later, the same way it reads
            # any other GENERATE_FILE request.
            answer = _strip_answer_filler(user_answer).strip()
            if answer.lower() in GENERATE_FILE_SKIP_NAME_ANSWERS or not answer:
                # Explicit opt-out (or an empty/unusable reply -- treated
                # the same as skip rather than re-asking forever, since
                # this question already has a stated "or say 'skip'"
                # escape hatch unlike every other MISSING_SLOT_QUESTIONS
                # entry) -- proceed with generator.py's own generic
                # default name, unchanged original text.
                final_prompt = original_text
            else:
                final_prompt = f"{original_text} called {answer}"
            context = _decision_context(meta, {})
            return self._dispatch_or_confirm(
                intent, final_prompt, {}, context,
                on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
                skip_generate_name_check=True,
            )

        slots = resolve_missing_slot(
            intent, original_text, user_answer,
            wcl_variables=meta.get("slots") if intent.startswith("WCL_") else None,
        )
        if slots is None and intent in ANAPHORA_ELIGIBLE_INTENTS and is_anaphoric_reference(user_answer):
            # Answered "the one you just made" / "that" instead of a bare
            # name -- resolve_missing_slot() correctly rejects that as a
            # literal name (same _looks_like_real_name guard as everywhere
            # else), but it's not actually an unusable answer, just an
            # anaphoric one. Try last-touched before re-asking.
            slots = resolve_anaphoric_target(intent, self._last_touched)
        if slots is None:
            # Still not usable (e.g. empty reply, or a two-part answer
            # missing its second half) -- ask again rather than guessing.
            question = MISSING_SLOT_QUESTIONS.get(intent, "Could you clarify that?")
            self._pending = pending
            note = f"I still didn't catch that. {question}"
            self._commit_history(user_answer, note)
            return {"thinking": "", "response": note, "kind": "chat"}

        context = _decision_context(meta, slots)
        return self._dispatch_or_confirm(
            intent, original_text, slots, context,
            on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
        )

    def _handle_missing_or_dispatch(
        self,
        intent: str,
        user_prompt: str,
        slots: Optional[Dict[str, str]],
        on_output: Callable[[str], None],
        on_done: Callable[[int], None],
        on_thinking_token: Optional[Callable[[str], None]],
        on_generate_token: Optional[Callable[[str], None]],
        on_generate_done: Optional[Callable[[Optional[str], Optional[str]], None]],
    ) -> Dict[str, Any]:
        """Shared by the time/conditional pre-checks in
        _process_single_request(): same "ask a fixed question on a
        slot-extraction miss, dispatch on a hit" shape used by the
        override branch and the main classify->extract_slots path, pulled
        out once here instead of copy-pasted per pre-check. If slots is
        None, sets self._pending exactly like every other missing-slot
        case in this file, so the user's next message resolves it via
        _resume_pending() -> resolve_missing_slot(), the same existing
        machinery as any other intent."""
        meta = _intent_meta(intent)
        if slots is None:
            question = MISSING_SLOT_QUESTIONS.get(intent, "Could you give me a bit more detail?")
            self._pending = {"intent": intent, "original_text": user_prompt}
            self._commit_history(user_prompt, question)
            return {"thinking": "", "response": question, "kind": "chat"}

        context = _decision_context(meta, slots)
        return self._dispatch_or_confirm(
            intent, user_prompt, slots, context,
            on_output, on_done, on_thinking_token, on_generate_token, on_generate_done,
        )

    def _dispatch(
        self,
        intent: str,
        user_prompt: str,
        slots: Dict[str, str],
        context: str,
        on_output: Callable[[str], None],
        on_done: Callable[[int], None],
        on_thinking_token: Optional[Callable[[str], None]],
        on_generate_token: Optional[Callable[[str], None]],
        on_generate_done: Optional[Callable[[Optional[str], Optional[str]], None]],
        skip_generate_name_check: bool = False,
    ) -> Dict[str, Any]:
        """
        Runs the already-decided, already-slot-filled intent. Shared by both
        the normal path and the resumed-pending-question path. Takes the
        raw decision `context` string (not a pre-started thinking handle)
        so each branch below can decide FOR ITSELF whether it's safe to
        start narrating before the real work finishes.

        PERFORMANCE vs CORRECTNESS, per kind:
          - generate/app_control/powershell: thinking starts immediately and
            overlaps with the real work (a background thread streaming the
            narration while PowerShell/UI-Automation/generation runs) --
            same as before. These kinds don't ask the model to state a
            specific fact it doesn't have yet, so overlapping is safe.
          - api (weather/search/time/location): this is exactly the kind
            that WAS overlapped before and produced the fabricated-weather /
            fabricated-narration bug -- giving the model a context like
            "You've decided to check the current weather" with no number in
            it, then asking it to "narrate what you're doing" in one
            natural sentence, reliably got completed as if it already knew
            the answer (small models under free-text generation do this).
            A stronger prompt instruction alone did NOT fix this in
            practice (confirmed live), because the model still has the
            user's original question sitting right there in the same
            call and nothing forces it to leave the blank unfilled.
            The actual fix is structural, not another prompt tweak: the
            real API call now runs FIRST, and thinking only starts
            afterward, told the REAL result as part of its context. There
            is nothing left for it to invent -- it's now describing a
            fact it was actually handed, not the fact it's about to go
            get. This costs the overlap latency for API calls specifically
            (typically well under a second for Open-Meteo/Wikipedia), which
            is the right trade for a call whose entire purpose is stating a
            specific piece of information.
        """
        meta = _intent_meta(intent)

        if meta["kind"] == "generate":
            # BETA 0.3.55: ask for a name instead of silently defaulting to
            # "generated_file.txt" when the request has no "called X"/
            # "named X" clause at all -- see MISSING_SLOT_QUESTIONS'
            # GENERATE_FILE entry and generator.extract_explicit_name()'s
            # own docstring for the full bug this closes (every other
            # file-creating intent already asks on a miss; this one
            # silently didn't, which is what made a cut-off/never-spoken
            # name invisible instead of a clear follow-up question).
            # skip_generate_name_check is True only when _resume_pending()
            # is calling back in after that question was already asked
            # (and either answered or explicitly skipped) -- checking
            # again here would just ask a second time.
            if not skip_generate_name_check and extract_explicit_name(user_prompt) is None:
                question = MISSING_SLOT_QUESTIONS.get(intent, "What should I name it?")
                self._pending = {"intent": intent, "original_text": user_prompt}
                self._commit_history(user_prompt, question)
                return {"thinking": "", "response": question, "kind": "chat"}

            thinking_handle = self._start_thinking(
                user_prompt, context, on_thinking_token,
                expected_values=_narration_values(slots), fallback_meta=meta, fallback_slots=slots,
            )

            def join_thinking() -> str:
                return thinking_handle.join() if thinking_handle else ""

            if on_generate_token is None or on_generate_done is None:
                # Caller (UI) didn't wire up the generation callbacks --
                # fail loudly rather than silently doing nothing.
                thinking_text = join_thinking()
                note = f"{thinking_text} (file generation isn't wired up in this UI yet)"
                self._commit_history(user_prompt, note)
                return {"thinking": thinking_text, "response": note, "kind": "chat"}

            def wrapped_done(path: Optional[str], error: Optional[str]):
                # thinking_handle is joined once, lazily, the first time
                # either branch of wrapped_done actually needs the text --
                # by the time generation finishes this has almost certainly
                # already completed, so this join is normally instant.
                thinking_text = join_thinking()
                # Natural-language note, not a bracket tag -- see
                # _commit_history's docstring.
                if path:
                    self._remember_touched("GENERATE_FILE", {"path": path})
                    self._commit_history(user_prompt, f"{thinking_text} (saved to {path})")
                else:
                    self._commit_history(user_prompt, f"{thinking_text} (generation failed: {error})")
                on_generate_done(path, error)

            self.generator.generate_and_save(user_prompt, on_generate_token, wrapped_done)
            # generate_and_save is itself a blocking streamed call, so by the
            # time we get here thinking has certainly finished -- join is instant.
            thinking_text = join_thinking()
            return {"thinking": thinking_text, "response": thinking_text, "kind": "generate", "intent": intent}

        if meta["kind"] == "api":
            # apis.py's own methods already return a complete, correct,
            # human-readable sentence (e.g. "Lahore: 22°C, wind 10 km/h")
            # -- there was never anything left for an LLM call to add here
            # except latency and a chance to paraphrase it wrong. Use the
            # real result directly.
            result = self.dispatcher.call(meta, slots)
            if is_api_failure(result):
                thinking_text = f"Hmm, that didn't work. {result}"
                self._commit_history(user_prompt, thinking_text)
                return {"thinking": thinking_text, "response": thinking_text, "kind": "api",
                         "intent": intent, "result": result}
            self._commit_history(user_prompt, result)
            return {"thinking": result, "response": result, "kind": "api",
                     "intent": intent, "result": result}

        if meta["kind"] == "app_control":
            # No narration LLM call here either -- same reasoning as the
            # powershell branch above.
            action_name = meta["action"]
            extra_args = meta.get("extra_args", {})
            action_fn = getattr(self.app_controller, action_name, None)
            if action_fn is None:
                note = f"Done. (internal error: unknown app_control action {action_name})"
                self._commit_history(user_prompt, note)
                return {"thinking": "", "response": note, "kind": "chat"}

            result = action_fn(**slots, **extra_args)

            # Click-to-teach hook (this session): a clean resolve_target()
            # miss on click/double-click/right-click surfaces as this exact
            # sentinel string (see app_control.py's click()) -- not for
            # type_text() yet, scoped to clicks only per this session's
            # discussion ("when it can't find a button"). Offer to learn it
            # from the user's own next click instead of just failing.
            if action_name == "click" and isinstance(result, str) \
                    and result.startswith("Couldn't confidently find"):
                target_description = slots.get("target_description", "")
                result = self.app_controller.teach_from_next_click(target_description)

            # BUG FIX (this session), later NARROWED (BETA 0.3.40): the
            # first version of this fix surfaced `result` as "response"
            # unconditionally for every app_control action, replacing what
            # used to be a hardcoded "Done." no matter what happened. That
            # was necessary for the click-to-teach flow above -- "I
            # couldn't find that -- click it yourself and I'll remember
            # it" genuinely needs to reach the screen, not vanish behind
            # "Done." -- and for start_seeing/stop_seeing_and_save/teach/
            # list_installed_apps/run_macro, whose whole return value IS
            # the actual information the user asked for. But it also swept
            # up the plain "it worked" case for the everyday actions
            # (LAUNCH_APP/CLICK_ELEMENT/TYPE_TEXT) -- "Clicked \"Save\" at
            # (50, 15)." on a successful click is not new information,
            # it's the exact same action restated in different words,
            # which is precisely the "assistant repeating vaguely back at
            # me" behavior "Done." exists to avoid for normal commands.
            #
            # Fix: these three methods' SUCCESS strings are fixed,
            # entirely known templates (see app_control.py's launch_app/
            # click/type_text) -- a result starting with one of them is a
            # plain success with nothing new to say, so it collapses back
            # to "Done.". Anything else -- a failure message, an
            # instruction, a report, or any other action's result -- is
            # real information and is still surfaced exactly as before.
            # No action-name special-casing needed: the teach_from_next_click
            # substitution above already replaces `result` with its own
            # message, which never matches these prefixes, so it's
            # surfaced correctly without extra logic here.
            thinking_text = "Done."
            success_restatement = isinstance(result, str) and result.startswith(
                ("Launching ", "Clicked \"", "Typed into \"")
            )
            if success_restatement:
                response_text = thinking_text
            else:
                response_text = result if isinstance(result, str) and result else thinking_text
            note = f"{thinking_text} (result: {result})" if result else thinking_text
            self._commit_history(user_prompt, note)
            return {"thinking": thinking_text, "response": response_text, "kind": "app_control",
                     "intent": intent, "result": result}

        if meta["kind"] == "powershell":
            try:
                command = _build_powershell_command(meta, slots)
            except (KeyError, ValueError, IndexError) as e:
                note = f"Done. (missing a detail: {e})"
                self._commit_history(user_prompt, note)
                return {"thinking": "", "response": note, "kind": "chat"}

            # No narration LLM call for pure command dispatches -- per
            # product decision, real usage of this feature is "get things
            # done", not conversation, and the graph/WCL classification
            # that got us here was already zero-LLM and fully confident.
            # A bare "Done." costs nothing and needs no Ollama round trip.
            if intent in _WRITE_INTENTS:
                file_index.invalidate()
                self._remember_touched(intent, slots)

            self._current_cmd = RunningCommand(command, on_output, on_done)
            self._current_cmd.start()
            thinking_text = "Done."
            self._commit_history(user_prompt, thinking_text)
            return {"thinking": thinking_text, "response": thinking_text, "kind": "powershell",
                     "intent": intent, "command": command}

        if meta["kind"] == "schedule":
            command_text = slots["command_text"]
            delay_seconds = float(slots["delay_seconds"])
            delay_label = format_delay(delay_seconds)

            def _on_fire():
                # Re-runs the inner command through the FULL normal
                # pipeline (classify -> extract_slots -> dispatch), not a
                # raw shell call -- so "shut down" scheduled for later
                # gets exactly the same handling (including a missing-
                # slot question, if the phrasing genuinely needs one) as
                # if the user had typed it fresh at that moment. Uses a
                # fresh, throwaway self._pending-independent call: if the
                # scheduled text itself needs a follow-up question, that
                # question is appended to history as a chat note rather
                # than silently dropped, since there's no live user turn
                # to attach a NEW self._pending to at fire-time.
                try:
                    result = self.process_request(
                        command_text, on_output, on_done,
                        on_thinking_token=None, on_generate_token=None, on_generate_done=None,
                    )
                    note = f"[Scheduled] {command_text} -- {result.get('response', 'done')} ({result.get('intent', '')})"
                except Exception as e:
                    note = f"[Scheduled] {command_text} -- failed: {e}"
                self._commit_history(f"(scheduled) {command_text}", note)

            try:
                item = self.scheduler.schedule(delay_seconds, command_text, _on_fire)
            except SchedulerFullError as e:
                note = f"Done. Can't schedule that: {e}"
                self._commit_history(user_prompt, note)
                return {"thinking": "", "response": note, "kind": "chat"}

            note = (f"Done. Scheduled as {item.id}: \"{command_text}\" {delay_label}. "
                    f"Say \"cancel {item.id}\" to cancel it.")
            self._commit_history(user_prompt, note)
            return {"thinking": "Done.", "response": note, "kind": "schedule",
                     "intent": intent, "schedule_id": item.id}

        if meta["kind"] == "timer":
            # Deliberately NOT the "schedule" branch's pattern of
            # re-running some text through process_request at fire time
            # -- there's no command here to re-run (see intents.py's
            # SET_TIMER entry / extractor.py's looks_like_bare_timer()
            # docstring for why that's exactly the bug this intent
            # exists to avoid). Fire-time action is just a plain
            # notification committed to history, same visibility model
            # "schedule" already uses (no separate toast/tray plumbing --
            # this app has no cross-cutting push-notification channel
            # today, so this matches the one that already exists rather
            # than inventing a new one).
            delay_seconds = float(slots["delay_seconds"])
            label = (slots.get("label") or "").strip()
            delay_label = format_delay(delay_seconds)
            display = f"Timer ({label})" if label else "Timer"

            def _on_fire():
                note = f"⏰ {display} is up!" if not label else f"⏰ Timer's up -- {label}"
                self._commit_history(f"(timer) {display}", note)

            try:
                item = self.scheduler.schedule(delay_seconds, display, _on_fire)
            except SchedulerFullError as e:
                note = f"Done. Can't set that timer: {e}"
                self._commit_history(user_prompt, note)
                return {"thinking": "", "response": note, "kind": "chat"}

            note = (f"Done. Timer set as {item.id}, {delay_label}"
                    + (f" -- I'll remind you: {label}" if label else "") + ". "
                    f"Say \"cancel {item.id}\" to cancel it.")
            self._commit_history(user_prompt, note)
            return {"thinking": "Done.", "response": note, "kind": "timer",
                     "intent": intent, "schedule_id": item.id}

        if meta["kind"] == "cancel_scheduled":
            ref = slots["ref"]
            cancelled_schedule = self.scheduler.cancel(ref)
            cancelled_condition = None if cancelled_schedule else self.condition_poller.cancel(ref)
            if cancelled_schedule:
                note = f"Done. Cancelled {cancelled_schedule.id}: \"{cancelled_schedule.description}\"."
            elif cancelled_condition:
                note = f"Done. Stopped watching {cancelled_condition.id}: \"{cancelled_condition.description}\"."
            else:
                active_s = [it.id for it in self.scheduler.list_active()]
                active_c = [it.id for it in self.condition_poller.list_active()]
                available = ", ".join(active_s + active_c) or "none"
                note = f"Done. Couldn't find \"{ref}\" to cancel. Currently active: {available}."
            self._commit_history(user_prompt, note)
            return {"thinking": "Done.", "response": note, "kind": "cancel_scheduled", "intent": intent}

        if meta["kind"] == "conditional":
            condition_and_action = slots.get("condition_and_action", "") if slots else ""
            checker = match_condition(condition_and_action)
            if checker is None:
                supported = ", ".join(CHECKABLE_CONDITIONS_SUMMARY)
                note = (f"Done. I can't monitor \"{condition_and_action}\" yet -- "
                        f"right now I can only watch for: {supported}.")
                self._commit_history(user_prompt, note)
                return {"thinking": "", "response": note, "kind": "chat"}

            action_text = condition_and_action

            def _on_true():
                try:
                    result = self.process_request(
                        action_text, on_output, on_done,
                        on_thinking_token=None, on_generate_token=None, on_generate_done=None,
                    )
                    note = f"[Condition met] {condition_and_action} -- {result.get('response', 'done')} ({result.get('intent', '')})"
                except Exception as e:
                    note = f"[Condition met] {condition_and_action} -- failed: {e}"
                self._commit_history(f"(condition fired) {condition_and_action}", note)

            def _on_error(err: str):
                self._commit_history(
                    f"(condition check failed) {condition_and_action}",
                    f"[Watch stopped] {condition_and_action} -- {err}",
                )

            def _on_timeout():
                self._commit_history(
                    f"(condition timed out) {condition_and_action}",
                    f"[Watch timed out] \"{condition_and_action}\" never became true within the watch window.",
                )

            try:
                item = self.condition_poller.start(checker, condition_and_action, _on_true, _on_error, _on_timeout)
            except RuntimeError as e:
                note = f"Done. Can't start watching that: {e}"
                self._commit_history(user_prompt, note)
                return {"thinking": "", "response": note, "kind": "chat"}

            note = (f"Done. Watching as {item.id}: \"{condition_and_action}\". "
                    f"Say \"cancel {item.id}\" to stop watching.")
            self._commit_history(user_prompt, note)
            return {"thinking": "Done.", "response": note, "kind": "conditional",
                     "intent": intent, "watch_id": item.id}

        # Defensive fallback for any kind not handled above (should be
        # unreachable given intents.py's closed set of "kind" values) --
        # blocking is fine here since there's no dispatch work to overlap.
        thinking_text = self._run_thinking(user_prompt, context, on_thinking_token)
        return {"thinking": thinking_text, "response": thinking_text, "kind": "chat"}

    def set_model(self, name: str):
        self.router.model_name = name
        self.generator.model_name = name

    def get_model(self) -> str:
        return self.router.model_name

    def graph_status(self) -> str:
        """Surfaces whether the fast, reliable graph-router path is
        actually live, or whether construction failed silently and every
        message is falling through to LLM-only classification -- see
        __init__'s try/except around GraphRouter(). This was previously
        invisible anywhere in the UI: a graph load failure produced no
        error, no log, nothing -- just a silent, permanent downgrade to
        the small local model classifying everything on its own, history
        and all. Call this once after construction and show it somewhere
        the user will actually see it."""
        parts = []
        parts.append("graph: on" if self.graph_router is not None else "graph: OFF (LLM-only)")
        parts.append("wcl: on" if self.wcl_resolver is not None else "wcl: off")
        return ", ".join(parts)
