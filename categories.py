"""
categories.py — the category layer for the LLM's classify() fallback.

── Why this got smaller ────────────────────────────────────────────────────
Originally 8 categories (FILESYSTEM/PROCESS/SYSTEM/INFO/GENERATE/
APP_CONTROL/CHAT/ASK_CONTEXT), because the LLM had to be ABLE to route to
any of TOKI's 59 intents itself. That's no longer true: graph_router.py now
runs FIRST, unconditionally, on every message, and matches across ALL
dispatchable commands (TOKI's original 59 + 284 windows_command_library
zero-variable commands) in one flat pass, with no category pre-filter of
its own (see graph_router.py's _best_command). A hit there means zero LLM
calls happen at all for that turn.

So the LLM's classify() is only ever reached on a GRAPH MISS -- and a graph
miss on something shell/filesystem/process/system/app-control/info-shaped
isn't a signal to re-attempt that same classification with the LLM
instead (that's the OLD design, and it's exactly the redundant tier-2 call
this rework removes); it's a signal the graph's fuzzy matching wasn't
confident enough, which per design falls through to ASK_CONTEXT so the
user gets asked to rephrase, rather than letting the LLM guess a shell
command it might get wrong with real side effects. See graph_router.py's
CONFIDENCE_THRESHOLD docstring for why false-positive dispatch is treated
as worse than an extra clarifying question.

That leaves exactly three things the LLM's tier-1 call ever needs to
decide between:
  - CHAT          -- small talk, no action/lookup implied
  - GENERATE      -- write new file content (the one intent, GENERATE_FILE,
                     that was NEVER in the graph -- it has no fixed
                     template, so there's nothing for the graph to match)
  - ASK_CONTEXT   -- wants something done/looked up, but unclear what (this
                     is ALSO where a graph miss on a real shell request
                     ends up, since there's no separate "try again but
                     smarter" tier for that)

── Tier 2 ───────────────────────────────────────────────────────────────────
GENERATE maps to exactly one intent (GENERATE_FILE), so classify() skips
the second LLM call entirely for it now (see orchestrator.py's classify())
-- there is nothing left to disambiguate within a 1-command category. CHAT
and ASK_CONTEXT never had a tier 2. So the tier-2 LLM call this file used
to support (_build_command_prompt, commands_in_category as a multi-command
lookup) is effectively retired; commands_in_category() is kept only
because validate_category_map() still uses it to sanity-check that no
listed category is empty.

This file does NOT redefine any intent -- it only maps CHAT/ASK_CONTEXT/
GENERATE_FILE to a category. Every other intent (all 59 original ones +
284 windows_command_library ones) is intentionally UNMAPPED here now --
they're graph-only, never shown to or picked by the LLM. See
validate_category_map()'s docstring for how it checks that split is
consistent instead of requiring full coverage like it used to.
"""

from typing import Dict, List

# ── Category definitions ──────────────────────────────────────────────────
# Shown to the model in call 1's system prompt. Keep descriptions short and
# mutually distinguishable — overlapping descriptions is what causes
# misclassification, not model weakness.
CATEGORIES: Dict[str, str] = {
    "CHAT": (
        "Greetings, small talk, casual conversation, thanks, or anything that "
        "isn't a clear request to look something up or take an action on the computer. "
        "If the message doesn't CLEARLY ask for a specific fact or action, use this."
    ),
    "GENERATE": (
        "Writing new TEXT CONTENT from scratch that doesn't exist yet -- an actual "
        "script's code, a document's prose, etc. Requires something to actually WRITE, "
        "not just a name. Do NOT use this for creating an empty folder or empty file, "
        "even if its name mentions a language or file type (e.g. 'make a folder named "
        "python' is creating a folder, NOT writing Python code -- that's ASK_CONTEXT, "
        "not GENERATE)."
    ),
    "ASK_CONTEXT": (
        "The user clearly wants something done or wants a specific fact looked up, but "
        "either left out a detail needed to tell what (e.g. 'delete it' with no 'it' "
        "named), OR it's a reasonable-sounding request TOKI just doesn't have a confident "
        "match for. Use this INSTEAD of CHAT whenever they're trying to get something "
        "done or looked up but you can't tell exactly what/how. Do NOT use this for "
        "genuine greetings or small talk -- that's CHAT."
    ),
}
CATEGORY_NAMES: List[str] = list(CATEGORIES.keys())


# ── Intent -> category map ──────────────────────────────────────────────────
# Only the three LLM-reachable intents live here on purpose -- see module
# docstring. Everything else (FILESYSTEM/PROCESS/SYSTEM/INFO/APP_CONTROL-
# shaped intents, both TOKI's original 59 and the windows_command_library
# additions) is graph-only: reachable through graph_router.py's flat match,
# never through this category map or the LLM's tier-1/tier-2 calls.
INTENT_CATEGORY: Dict[str, str] = {
    "CHAT": "CHAT",
    "ASK_CONTEXT": "ASK_CONTEXT",
    "GENERATE_FILE": "GENERATE",
}

# The graph-only intents that are DELIBERATELY unmapped above -- kept here
# just so validate_category_map() can tell "intentionally graph-only" apart
# from "someone forgot to map a new LLM-reachable intent," which is the
# actual failure mode worth catching at startup now.
GRAPH_ONLY_CATEGORIES = {"FILESYSTEM", "PROCESS", "SYSTEM", "INFO", "APP_CONTROL"}


def commands_in_category(category: str) -> List[str]:
    """All intent names that map to the given category, in stable order."""
    return [name for name, cat in INTENT_CATEGORY.items() if cat == category]


def validate_category_map(all_intent_names: List[str]) -> None:
    """
    Defense in depth, updated for the graph-first design: this used to
    require EVERY intent to have a category (because the LLM had to be
    able to reach any of them). Now most intents are graph-only by design
    (see module docstring), so the only things worth catching at startup
    are:

      1. A mapped intent that no longer exists in INTENTS (stale mapping).
      2. A listed CATEGORY with zero commands mapped to it (the model
         could pick it and have nothing to choose from next).

    Deliberately NOT checking "every intent has a category" anymore --
    that's now a false positive by design (the 340 graph-only intents are
    supposed to be unmapped here).
    """
    mapped = set(INTENT_CATEGORY)
    actual = set(all_intent_names)

    stale = mapped - actual
    if stale:
        raise ValueError(
            f"categories.py: these mapped intents no longer exist in INTENTS: "
            f"{sorted(stale)}. Remove them from INTENT_CATEGORY."
        )

    empty_categories = [c for c in CATEGORY_NAMES if not commands_in_category(c)]
    if empty_categories:
        raise ValueError(
            f"categories.py: these categories have zero commands mapped to them: "
            f"{empty_categories}. The model could pick one and have nothing to choose from next."
        )
