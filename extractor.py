"""
extractor.py — fills intent slots from the user's raw text, deterministically.

The model only ever picks an intent word (see intents.py). It never sees or
produces variable values. This module is what actually pulls "that folder
called Homework" out of "make a folder called Homework on my desktop" — via
plain regex/heuristics, not another model call. This is the piece that
removes the whole "model invents a wrong variable shape" failure class we
kept hitting before.
"""

import json
import ntpath
import os
import re
import subprocess
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

# Directory this file lives in -- the installed app root. The sandbox
# config (if the installer wrote one) lives alongside it at
# config/sandbox_config.json, never bundled with the app itself.
_APP_ROOT = os.path.dirname(os.path.abspath(__file__))
_SANDBOX_CONFIG_PATH = os.path.join(_APP_ROOT, "config", "sandbox_config.json")


# ─── Scheduled/delayed commands ("do X in N minutes" / "do X at HH:MM") ──────
#
# This runs BEFORE the graph router gets a chance at the message (see
# orchestrator.py's _process_single_request) -- NOT after a graph miss.
# Reason, confirmed directly against a real bug: graph_router.classify()
# matches on content-word overlap with no concept of time at all, so
# "open notepad at 3pm" used to silently match OPEN_ITEM and fire
# immediately, quietly dropping "at 3pm" instead of either scheduling it
# or admitting it didn't understand the timing. Checking for a time
# expression FIRST, before any classification happens, means a genuine
# time-bearing message never reaches a handler that has no idea what to
# do with the time part.
#
# Deliberately narrow: only fires on an explicit relative ("in N
# minutes/hours/seconds") or absolute ("at H[:MM][am/pm]") time
# expression. A message with no such expression falls through to normal
# classification completely unchanged -- this can't accidentally steal
# a plain "open notepad" just because it can't rule out timing.

# "for" added alongside "in" -- BETA 0.3.48 fix, confirmed live: "set a
# timer for 10 minutes" is the single most natural way to phrase a timer
# request, and it used to match NOTHING here (only "in N minutes" did),
# so it fell all the way through to a raw graph/LLM miss instead of ever
# reaching the scheduling path at all. "for" only ever appears in this
# duration-preposition role in real phrasings ("for 10 minutes", "for an
# hour") -- it isn't a word find_time_expression's caller needs to keep
# for anything else, so widening the regex carries no ambiguity risk the
# way a bare word-overlap match would.
# BETA 0.3.49 fix: the amount before the unit only ever accepted digits
# (\d+), so word-form durations -- "for an hour", "in a minute" -- never
# matched at all, even though they're at least as natural as "for 10
# minutes". Confirmed live: "set a timer for an hour" didn't even reach
# the scheduling pre-check, it fell straight through to a normal graph
# miss and silently web-searched the whole sentence -- the exact bug
# class SET_TIMER (BETA 0.3.48) was written to eliminate, just via a
# different gap. "a"/"an" here only ever mean the single-unit amount
# (matching how a person says "for a minute" == "for 1 minute" == "for
# one minute"), not the article on a following noun -- \b anchors on
# both sides plus the required unit word right after keep this from
# matching stray "a"/"an" elsewhere in a sentence.
_RELATIVE_TIME_RE = re.compile(
    r"\b(?:in|for)\s+(\d+|a|an)\s*(second|sec|minute|min|hour|hr)s?\b",
    re.IGNORECASE,
)
_ABSOLUTE_TIME_RE = re.compile(
    r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
    re.IGNORECASE,
)

# BETA 0.3.49 fix: "set a 10 minute timer" / "a 10 minute alarm" has no
# "in"/"for" preposition at all -- the duration sits directly in front of
# the timer/alarm/reminder noun instead. _RELATIVE_TIME_RE requires
# in/for, so this shape missed find_time_expression() entirely (not even
# a scheduling attempt -- straight to a plain graph/LLM miss). Rather
# than making the preposition optional everywhere (which would make
# ordinary sentences like "the movie is 90 minutes long" look like a
# scheduling request), this only fires when the duration is immediately
# followed by one of the timer trigger nouns -- same narrow, curated-
# phrase-set posture as _BARE_TIMER_REMAINDER_RE below, not a general
# "number + unit anywhere" detector. The trigger noun itself is matched
# via lookahead (not consumed), so it stays in the remainder for
# _BARE_TIMER_REMAINDER_RE to recognize afterward.
_BARE_DURATION_BEFORE_TIMER_RE = re.compile(
    r"\b(\d+|a|an)\s*(second|sec|minute|min|hour|hr)s?\s+"
    r"(?=timer\b|alarm\b|reminder\b)",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "second": 1, "sec": 1,
    "minute": 60, "min": 60,
    "hour": 3600, "hr": 3600,
}


def _parse_amount(raw: str) -> int:
    """'10' -> 10, 'a'/'an' -> 1 (word-form single unit, e.g. 'a minute')."""
    if raw.lower() in ("a", "an"):
        return 1
    return int(raw)


def find_time_expression(text: str) -> Optional[Tuple[float, str, str]]:
    """Looks for a relative or absolute time expression anywhere in text.

    Returns (delay_seconds, matched_span_text, remainder_text) on a hit,
    or None if no time expression is present -- caller treats None as
    "not a scheduling request at all", not as a failed schedule attempt.

    remainder_text is the original text with the matched time expression
    removed, so the caller can classify what's LEFT as the actual command
    to schedule (e.g. "shut down in 10 minutes" -> remainder "shut down").

    Absolute times in the past today (e.g. it's 4pm and the user says
    "at 3pm") are rolled forward to tomorrow -- matches the ordinary human
    reading of "at 3pm" as a future request, never silently scheduled for
    a time already gone.
    """
    m = _RELATIVE_TIME_RE.search(text)
    if m:
        amount = _parse_amount(m.group(1))
        unit = m.group(2).lower()
        delay = amount * _UNIT_SECONDS[unit]
        remainder = (text[:m.start()] + text[m.end():]).strip()
        remainder = re.sub(r"\s{2,}", " ", remainder).strip(" ,.")
        return delay, m.group(0), remainder

    m = _BARE_DURATION_BEFORE_TIMER_RE.search(text)
    if m:
        amount = _parse_amount(m.group(1))
        unit = m.group(2).lower()
        delay = amount * _UNIT_SECONDS[unit]
        remainder = (text[:m.start()] + text[m.end():]).strip()
        remainder = re.sub(r"\s{2,}", " ", remainder).strip(" ,.")
        return delay, m.group(0), remainder

    m = _ABSOLUTE_TIME_RE.search(text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = (m.group(3) or "").lower()
        if hour > 23 or minute > 59:
            return None  # not a real time -- e.g. "at 99" isn't a clock time
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        elif not ampm and hour <= 12:
            # No am/pm given and an ambiguous 1-12 hour -- assume the NEXT
            # occurrence of that hour (could be am or pm), same principle
            # as a human hearing "at 3" with no other context: nearest
            # future 3 o'clock, not a guess about which one was "meant".
            pass
        now = datetime.now()
        candidate = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        delay = (candidate - now).total_seconds()
        remainder = (text[:m.start()] + text[m.end():]).strip()
        remainder = re.sub(r"\s{2,}", " ", remainder).strip(" ,.")
        return delay, m.group(0), remainder

    return None


# BETA 0.3.48: a bare "set a timer for 10 minutes" / "remind me in 20
# minutes" has no real dispatchable command in it -- there's nothing to
# RUN when it fires, just something to tell the user. Before this,
# find_time_expression()'s remainder ("set a timer for", "remind me",
# "wake me up") got treated as SCHEDULE_COMMAND's command_text and
# blindly re-run through the full classify/dispatch pipeline at fire
# time (see orchestrator.py's _on_fire) -- which has no more idea what
# "remind me" means THEN than it does right now, so it silently
# defaulted to web-searching the phrase "remind me" the moment the timer
# actually went off. Confirmed directly against extract_slots().
#
# This is deliberately a small, explicit phrase set (same "curated
# table, not a classifier" posture as synonyms.py) rather than an
# attempt to detect "any remainder that isn't a real command" generally
# -- that's exactly the open-set problem graph_router.py's
# CONFIDENCE_THRESHOLD comment already documents as unsolvable by
# pattern-matching alone. This only needs to catch the specific, narrow,
# extremely common shape of "there's a timer word and nothing else
# actionable," not classify arbitrary remainders.
# BETA 0.3.49 fix: the leading article group only accepted "a"/"the"
# (missing "an" -- "an alarm" doesn't match a|the), and the only accepted
# lead-in verb was "set" -- "give me a reminder" isn't shaped like
# anything this regex recognizes at all, so it fell through to the exact
# 0.3.48 bug (remainder "give me a reminder" gets scheduled and re-run
# verbatim at fire time) instead of being caught as a bare timer.
# Confirmed live for both gaps. "give me" is added as a second accepted
# lead-in alongside "set" -- still a small, curated set of real phrasings
# (same posture as the rest of this table), not a general verb-detector.
_BARE_TIMER_REMAINDER_RE = re.compile(
    r"^(?:(?:set|give\s+me)\s+(?:a|an|the)?\s*)?(?:a|an|the)?\s*"
    r"(timer|reminder|alarm|remind\s*me|wake\s*me(\s*up)?|notify\s*me|alert\s*me)$",
    re.IGNORECASE,
)


def looks_like_bare_timer(text: str) -> Optional[Tuple[float, str]]:
    """Returns (delay_seconds, label) if `text` is a bare timer/reminder
    request with no real command attached (e.g. "set a timer for 10
    minutes", "remind me in 20 minutes"), else None. `label` is whatever
    was left after stripping the trigger phrase itself (e.g. "check the
    oven" from "remind me in 10 minutes to check the oven") -- used only
    for the notification text, never re-executed as a command, which is
    the actual fix (see module comment above)."""
    found = find_time_expression(text)
    if not found:
        return None
    delay_seconds, _span, remainder = found
    remainder = re.sub(r"^(please\s+|can you\s+|could you\s+)", "", remainder, flags=re.IGNORECASE).strip()
    # NOTE: an EMPTY remainder ("in 10 minutes" with nothing else at all)
    # deliberately does NOT count as a bare timer, even though it "has
    # nothing to run" same as the cases below -- there's no actual
    # timer/reminder/alarm word anywhere, so this is exactly as likely to
    # be a truncated real command (a slot-extraction miss on something
    # like "shut down in 10 minutes" typed oddly) as it is a timer
    # request. Confirmed by test_scheduling_and_conditionals.py's
    # existing test_bare_time_expression_asks_instead_of_guessing: that
    # ambiguous case is SUPPOSED to fall through to SCHEDULE_COMMAND and
    # ask "what should I do, and when?", not silently guess either way.
    if remainder and _BARE_TIMER_REMAINDER_RE.match(remainder):
        return delay_seconds, ""
    # "remind me to check the oven" / "remind me to walk the dog" --
    # trigger phrase PLUS a real label to show back at fire time.
    m = re.match(
        r"^(?:remind\s*me|notify\s*me|alert\s*me)\s+(?:to\s+)?(.+)$",
        remainder, re.IGNORECASE,
    )
    if m:
        return delay_seconds, m.group(1).strip()
    return None


def format_delay(seconds: float) -> str:
    """Human-friendly rendering for narration/history, e.g. 'in 10 minutes'."""
    seconds = int(round(seconds))
    if seconds < 90:
        return f"in {seconds} second{'s' if seconds != 1 else ''}"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"in {minutes} minute{'s' if minutes != 1 else ''}"
    hours = round(seconds / 3600)
    return f"in {hours} hour{'s' if hours != 1 else ''}"


# ─── Conditional commands ("if X then Y" / "if X, Y") ────────────────────────
#
# Same "run before the graph" placement as time expressions, and for the
# same reason: the graph has no concept of conditional structure, so
# "if battery is low show battery status" was silently matching
# BATTERY_STATUS on content-word overlap and would fire UNCONDITIONALLY --
# worse than a miss, since it looks like correct handling of the
# conditional while actually ignoring the "if" entirely.
#
# Per product decision: TOKI does NOT try to guess which system check a
# vague condition maps to. It always asks the user to spell out the exact
# condition and action in plain terms, using the SAME "never guess, ask a
# fixed question" principle as every missing-slot case elsewhere in this
# app (see MISSING_SLOT_QUESTIONS). This function's only job is to detect
# that a conditional was ATTEMPTED at all -- resolving what it means is
# deliberately left to the user's clarifying answer, not to this file.
#
# BUG FOUND AND FIXED before shipping: the first version of this regex
# required an explicit "," or "then" separator between the condition and
# the action (r"^if\s+.+?(?:,|\bthen\b)\s*.+"). Tested directly against
# "if wifi is off turn it on" -- a completely natural phrasing with NO
# comma and NO "then" -- and it returned False, silently failing to
# detect the single most obvious real-world conditional phrasing. Fixed
# by dropping the separator requirement entirely: matching "starts with
# if" is enough, since (per the design above) this function was never
# meant to parse the condition/action boundary anyway -- that always goes
# through the follow-up question regardless of how confident a stricter
# regex might have been. A looser shape check here costs nothing, since
# ambiguity beyond "this looks like an if-statement" is handled the exact
# same way either way: ask.
_CONDITIONAL_RE = re.compile(r"^\s*if\s+\S+", re.IGNORECASE)


def looks_conditional(text: str) -> bool:
    """True if text has the shape of an 'if X then Y' / 'if X, Y' request.
    Deliberately shape-only -- never tries to parse what X or Y actually
    means; that's the whole point of always asking for plain-language
    clarification instead of guessing (see module note above)."""
    return bool(_CONDITIONAL_RE.match(text.strip()))


# ─── Cancelling a scheduled/watched item ─────────────────────────────────────
#
# Deliberately narrow trigger: only the word "cancel" (never "stop" or
# "close" or "end"), combined with either an explicit S<n>/C<n> reference
# or the words "scheduled"/"reminder"/"timer"/"watching"/"condition". This
# avoids colliding with KILL_PROCESS ("stop chrome") or any other existing
# intent that already legitimately uses "stop"/"close"/"end" -- "cancel"
# alone is not currently used as a trigger word anywhere else in this
# codebase (confirmed by grep across intents.py/intents_extended.py before
# adding this), so this can't steal an existing command's phrasing.
_CANCEL_SCHEDULED_RE = re.compile(
    r"\bcancel\b.*\b(s\d+|c\d+|scheduled|reminder|timer|watching|condition)\b",
    re.IGNORECASE,
)


def looks_like_cancel_scheduled(text: str) -> bool:
    return bool(_CANCEL_SCHEDULED_RE.search(text.strip()))


# ─── "start seeing" / "stop seeing" macro recording triggers ────────────────
#
# Deliberately handled as their OWN pre-check, exactly like the scheduling/
# conditional checks above, and NOT as ordinary graph_router.py Tier A
# intents. Confirmed live during this session that putting them in the
# graph instead breaks things: "start"/"stop" are extremely common words
# already load-bearing for LAUNCH_APP/KILL_PROCESS, and even a small,
# carefully-diluted phrasing set for START_SEEING/STOP_SEEING measurably
# shifted those two words' TF-IDF weight enough to misroute "start notepad"
# away from LAUNCH_APP. A dedicated regex pre-check has no shared
# vocabulary with the graph at all, so it can't cause that class of
# collision -- same reasoning as looks_like_cancel_scheduled() above
# avoiding "stop"/"close"/"end" for the same reason.
#
# BETA 0.3.49: "recording" removed from the bare/unqualified branch below.
# Root cause of a real bug (confirmed): this used to be
# r"\b(start|begin)\b.*\b(seeing|watching|recording)\b|..." -- which means
# a completely bare "start recording" ALWAYS matched here unconditionally,
# silently claiming it for macro capture, even when someone meant dictation
# ("start recording what I say"). "seeing"/"watching" stay unqualified --
# START_LISTENING never uses those words, so there's no genuine ambiguity
# there. "recording" only counts as a macro trigger now when paired with
# its own object (do/click/type), mirroring the second branch that already
# existed. The genuinely bare, no-object "start recording" case is now
# caught separately by looks_like_ambiguous_start_recording() below and
# asked about instead of silently guessed -- see that function's docstring.
# BETA 0.3.49: added a "my clicks" branch. Confirmed live: "start
# recording my clicks" -- despite "clicks" being an unambiguous macro
# signal on its own -- fell through to looks_like_ambiguous_start_
# recording() and asked a question that had only one sane answer,
# instead of resolving deterministically, because the existing
# "(everything|what) I (do|click|type)" branch only recognized "what I
# click", not "my clicks". "clicks"/"my clicks" never means dictation, so
# this can resolve directly without asking.
_START_SEEING_RE = re.compile(
    r"\b(start|begin)\b.*\b(seeing|watching)\b|"
    r"\b(start|begin)\b.*\brecording\b.*\b(everything|what)\s+i\s+(do|click|type)\b|"
    r"\b(watch|record)\b.*\b(everything|what)\s+i\s+(do|click|type)\b|"
    r"\brecord(ing)?\b.*\bmy\s+clicks?\b",
    re.IGNORECASE,
)

# BETA 0.3.49: same "recording" carve-out as _START_SEEING_RE above, and
# for the same reason -- a bare "stop recording" no longer assumes macro.
# Runtime state (is a macro recorder actually active? is dictation?) is a
# much better disambiguator than text for the STOP case specifically,
# since by the time someone says "stop," something is actually running --
# see looks_like_ambiguous_stop_recording() / orchestrator.py's use of
# app_controller._active_recorder / _active_dictation.
_STOP_SEEING_RE = re.compile(
    r"\bstop\b.*\b(seeing|watching)\b|"
    r"\bsave\b.*\bmacro\b|"
    r"\b(that'?s|thats)\s+(it|everything)\b.*\bsave\b",
    re.IGNORECASE,
)


def looks_like_start_seeing(text: str) -> bool:
    return bool(_START_SEEING_RE.search(text.strip()))


def looks_like_stop_seeing(text: str) -> bool:
    return bool(_STOP_SEEING_RE.search(text.strip()))


# Same reasoning/precedent as _START_SEEING_RE/_STOP_SEEING_RE just above:
# "start"/"stop" are extremely common words that would distort
# LAUNCH_APP/KILL_PROCESS's own Tier A graph scoring if "start listening"-
# shaped phrasings were added there instead -- confirmed live for the
# seeing/watching case, and "listening" carries the exact same risk (it's
# arguably worse: "listen" is closer to ordinary conversational English
# than "seeing"/"watching" already are). Same fix: a dedicated pre-check
# here, bypassing graph_router.py's Tier A entirely, same as start/stop
# seeing above.
#
# BETA 0.3.49: added the "record(ing) ... what/everything I say" branch,
# mirroring _START_SEEING_RE's "record(ing) ... what I do/click" branch --
# same real object-word disambiguation, just the voice side of it. Bare
# "recording" alone still does NOT match here (never did) -- only when
# "say" is the stated object.
#
# Fixed live bug (confirmed): the "record ... record(ing) ..." branch
# required TWO separate record-root words in the sentence (one for its
# own opening (start|begin|record) group, another for the literal
# "record(ing)?" right after) -- so a bare "record what I say" /
# "record everything I say", which only contains ONE "record" word,
# matched neither this branch nor the "start|begin ... everything/what I
# say" branch (no start/begin present either), and fell all the way
# through to a plain miss. Replaced that double-counting branch with a
# single-occurrence "record(ing)? ... (everything|what) I say" branch --
# it still matches "start recording everything I say" (start/begin is
# just not required by this branch, "record" alone is) without requiring
# the word to appear twice.
_START_LISTENING_RE = re.compile(
    r"\b(start|begin)\b.*\b(listening|dictating|dictation)\b|"
    r"\bstart\s+listening\b|"
    r"\brecord(ing)?\b.*\b(everything|what)\s+i\s+say\b|"
    r"\b(start|begin)\b.*\b(everything|what)\s+i\s+say\b",
    re.IGNORECASE,
)

_STOP_LISTENING_RE = re.compile(
    r"\bstop\b.*\b(listening|dictating|dictation)\b",
    re.IGNORECASE,
)


def looks_like_start_listening(text: str) -> bool:
    return bool(_START_LISTENING_RE.search(text.strip()))


def looks_like_stop_listening(text: str) -> bool:
    return bool(_STOP_LISTENING_RE.search(text.strip()))


# BETA 0.3.56: "function" is a near-unambiguous signal for GENERATE_FILE
# in this app's vocabulary -- nothing else TOKI does has any other use
# for that specific word. Confirmed live, matching STATUS.md's own
# 0.3.55 disclosure: "create a function called calculator" scored only
# 0.285 against graph_router.py's 0.5 CONFIDENCE_THRESHOLD for
# GENERATE_FILE, because the query's own TF-IDF vector gets diluted by
# "calculator" (or any specific name) not appearing in ANY phrasing's
# vocabulary -- a name necessarily does this to some degree, and no
# amount of corpus-tuning fully closes it (see that entry's own "not yet
# fixed" note, which flagged exactly this as the architecturally cleaner
# fix and deferred it pending review).
#
# Rather than keep fighting the corpus, this is a dedicated pre-check
# that bypasses Tier A's graph scoring entirely for anything mentioning
# "function" -- same "fixed phrase-shape, not a classifier" posture this
# file already uses for looks_like_start_seeing()/looks_like_bare_timer()
# above, and the same underlying reasoning as MAKE_FOLDER's own
# _NAME_FROM_OUTSIDE_VOCAB_INTENTS whitelist in orchestrator.py: a
# user-supplied name is structurally unpredictable text no amount of
# training phrasings can vocabulary-cover.
#
# Deliberately narrow to exactly the word "function" -- doesn't touch
# "folder"/"file"/"script"/"program" at all. Those already have real,
# working graph vocabulary and their own established intents (MAKE_FOLDER,
# MAKE_FILE, GENERATE_FILE's own "script"/"program"/"code" phrasings),
# so a plain "make a folder called Homework" or "write a script for this"
# is completely unaffected by this check and keeps routing exactly as it
# already does. Only "function" gets this treatment, per the explicit
# ask: function creation should be near-exclusive to GENERATE_FILE,
# something as specific as a folder should not be swept in.
_GENERATE_FUNCTION_RE = re.compile(r"\bfunction\b", re.IGNORECASE)

# BETA 0.3.56 follow-up: "function" alone isn't actually unambiguous --
# a real file can be literally named "function" ("open function.py",
# "delete the file called function", "rename function.py to helper.py").
# looks_like_function_creation() below must NOT steal those away from
# DELETE_ITEM/READ_FILE/OPEN_ITEM/RENAME_ITEM/etc. -- that would be the
# exact same class of bug this whole file's history keeps finding
# (misrouting a legitimate file-target intent). Two signals distinguish
# "function" the code-generation noun from "function" the literal
# filename:
#
# 1. A real creation verb (write/create/make/build/generate/code)
#    anywhere in the message -- confidently means the request IS about
#    generating code, whatever else is in the sentence. Always wins.
# 2. Absent that, "function" reads as an existing FILE being targeted
#    when it's either (a) immediately followed by a file extension
#    (function.py, function.txt, ...), (b) quoted as a literal name, or
#    (c) the message opens with one of the same file-management verbs
#    _BARE_PATH_LEADING_VERB_RE already recognizes for exactly this
#    purpose elsewhere in this file. Any of those, with no creation verb
#    present, means this isn't a generation request -- fall through to
#    normal routing instead.
_FUNCTION_CREATION_VERB_RE = re.compile(
    r"\b(write|create|make|build|generate|code)\b", re.IGNORECASE,
)
_FUNCTION_AS_EXISTING_FILE_RE = re.compile(
    r"\bfunction\.[a-z0-9]{1,6}\b|"           # function.py, function.txt, ...
    r"['\"]function['\"]|"                    # a quoted literal name
    r"^(?:please\s+)?(?:delete|remove|erase|read|open|view|show|display|"
    r"list|find|search|rename|move|copy)\b.*\bfunction\b",
    re.IGNORECASE,
)

# BUGFIX (found live while re-verifying 0.3.56, not caught by its own test
# suite): the header comment above claims "doesn't touch folder/file/
# script/program at all", but the code never actually enforced that once a
# creation verb was present -- "make a folder called function" ALSO
# contains a creation verb ("make"), so rule 1 ("a creation verb always
# wins") fired and stole it into GENERATE_FILE, exactly the misroute the
# comment says can't happen. Confirmed live end-to-end via
# _process_single_request(): router.classify() was never even called,
# generate_and_save("make a folder called function") was. The existing
# regression test (test_extractor.py) only checked "make a folder called
# Homework" -- which never contains the word "function" at all, so it
# could never have caught this.
#
# Fix: when "function" is clearly being used as the NAME of some other
# explicitly-typed thing (a folder/file/script/program/directory), that
# explicit type wins over the generic creation-verb rule, full stop --
# "make a folder called function" is a folder request; the fact that
# "make" is also a creation verb doesn't change what's being created.
# Two shapes catch this:
#   1. "called/named function" (or a quoted "function") alongside any of
#      those five type words anywhere in the message -- "function" is
#      the *name*, the other word is the *type*.
#   2. "function" immediately followed by one of those type words
#      ("function folder", "function script") -- same relationship,
#      reversed word order.
# This check runs BEFORE the creation-verb check, so it overrides rule 1
# rather than being overridden by it.
_OTHER_CREATION_TARGET_RE = re.compile(
    r"\b(?:folder|file|script|program|directory)\b", re.IGNORECASE,
)
_FUNCTION_AS_NAME_OF_OTHER_TARGET_RE = re.compile(
    r"\b(?:called|named)\s+['\"]?function['\"]?\b|"   # "called/named function"
    r"['\"]function['\"]|"                             # quoted "function"
    r"\bfunction\s+(?:folder|file|script|program|directory)\b|"  # "function folder"
    r"\b(?:folder|file|script|program|directory)\s+function\b",  # "folder function"
    re.IGNORECASE,
)


def looks_like_function_creation(text: str) -> bool:
    stripped = text.strip()
    if not _GENERATE_FUNCTION_RE.search(stripped):
        return False
    if (_FUNCTION_AS_NAME_OF_OTHER_TARGET_RE.search(stripped)
            and _OTHER_CREATION_TARGET_RE.search(stripped)):
        return False
    if _FUNCTION_AS_EXISTING_FILE_RE.search(stripped) and not _FUNCTION_CREATION_VERB_RE.search(stripped):
        return False
    return True


# BETA 0.3.49: the genuinely irreducible case -- "start recording" (or
# "begin recording") with NO companion word telling us which of the two
# real features it means. This is deliberately NOT another attempt to
# guess harder (see graph_router.py's CONFIDENCE_THRESHOLD docstring, and
# STATUS.md's 0.3.47/0.3.48 entries, for two separate confirmations this
# session that closed-vocabulary heuristics have a real ceiling on this
# exact class of problem). "Recording clicks for a macro" and "recording
# what I say" are both completely valid readings of the bare phrase --
# there's no text-level signal left to lean on once seeing/watching/
# listening/dictating/say/do/click/type are all absent, so the honest
# answer is to ask once, the same "false-positive dispatch is worse than
# an extra clarifying question" principle categories.py already documents
# for the graph/LLM tier.
#
# Deliberately checked by the caller AFTER looks_like_start_seeing() and
# looks_like_start_listening() both return False, so it only ever fires on
# the true leftover case -- it can never steal a phrasing that already had
# a real answer.
# BETA 0.3.49: widened to also catch a bare "record ..." with no leading
# "start"/"begin" at all (e.g. "record my screen"). Confirmed live: this
# used to require "start"/"begin" literally in the text, so a bare
# imperative fell through to a raw miss (silent web search) instead of
# reaching this "ask once" fallback -- the exact failure mode this
# function exists to prevent. Safe to widen: this check only ever runs
# (see orchestrator.py's call site) AFTER looks_like_start_seeing() and
# looks_like_start_listening() have both already returned False, so by
# the time bare "record"/"recording" reaches here, any phrasing with a
# real do/click/type/clicks/say object has already been resolved
# deterministically above -- nothing this widening newly matches was ever
# a genuinely resolvable case.
_AMBIGUOUS_START_RECORDING_RE = re.compile(
    r"\b(start|begin)\b.*\brecording\b|\brecord(ing)?\b",
    re.IGNORECASE,
)


def looks_like_ambiguous_start_recording(text: str) -> bool:
    return bool(_AMBIGUOUS_START_RECORDING_RE.search(text.strip()))


# Mirror of the above for "stop" -- but see orchestrator.py's actual use
# of this: by the time someone says "stop," something is (or isn't)
# genuinely running, which is real information a "start" check never has.
# The orchestrator checks this ONLY as a last resort, after checking
# app_controller's actual _active_recorder / _active_dictation state --
# this regex exists just to recognize the bare "stop recording" shape in
# the first place, not to resolve which one.
_AMBIGUOUS_STOP_RECORDING_RE = re.compile(
    r"\bstop\b.*\brecording\b",
    re.IGNORECASE,
)


def looks_like_ambiguous_stop_recording(text: str) -> bool:
    return bool(_AMBIGUOUS_STOP_RECORDING_RE.search(text.strip()))



# ─── Sandbox roots ────────────────────────────────────────────────────────────

_desktop_path_cache: Optional[str] = None


def _resolve_real_desktop_path() -> str:
    """
    %USERPROFILE%\\Desktop is NOT reliably the actual Desktop folder — if
    OneDrive has ever backed up/redirected Desktop, Windows silently creates
    a NEW active Desktop at %USERPROFILE%\\OneDrive\\Desktop (or similar)
    while the old %USERPROFILE%\\Desktop folder can still exist untouched
    with stale contents. That mismatch is exactly what caused files to be
    created/listed in the wrong place while Explorer showed something else.

    The only reliable way to get the CURRENT active Desktop path is to ask
    Windows itself via the .NET known-folder API, which correctly follows
    OneDrive/Group Policy redirection.
    """
    global _desktop_path_cache
    if _desktop_path_cache is not None:
        return _desktop_path_cache

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, timeout=5,
        )
        path = result.stdout.strip()
        if path:
            _desktop_path_cache = ntpath.normpath(path)
            return _desktop_path_cache
    except Exception:
        pass

    # Fallback (also what lets this run/test on non-Windows dev machines) —
    # only used if PowerShell itself couldn't be reached at all.
    userprofile = os.environ.get("USERPROFILE", r"C:\Users\Default")
    _desktop_path_cache = ntpath.normpath(ntpath.join(userprofile, "Desktop"))
    return _desktop_path_cache


def _load_configured_roots() -> Optional[List[str]]:
    """
    Reads the installer-provided allow-list at config/sandbox_config.json,
    e.g. {"roots": ["D:\\", "C:\\Users\\Alex\\Documents\\TOKI"]}. Returns
    None (not an empty list) on anything short of a clean, non-empty,
    well-formed read, so the caller can fall back to the hardcoded
    default cleanly instead of accidentally sandboxing to nothing.
    """
    try:
        with open(_SANDBOX_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        roots = data.get("roots")
        if not isinstance(roots, list) or not roots:
            return None
        cleaned = [ntpath.normpath(r) for r in roots if isinstance(r, str) and r.strip()]
        return cleaned or None
    except Exception:
        # Missing file, bad JSON, wrong shape -- all treated the same:
        # fall back to the default below. This file is entirely optional.
        return None


def get_desktop_root() -> str:
    """
    The real Desktop path specifically -- used by anything that wants
    "put this on the Desktop" regardless of what else is in the sandbox
    allow-list (e.g. video_downloader's default download folder). NOT
    the same as "the second entry in get_sandbox_roots()" -- that
    assumption broke once the roots list became installer-configurable
    and Desktop's position in it is no longer guaranteed.
    """
    return _resolve_real_desktop_path()


def get_sandbox_roots() -> List[str]:
    """
    The set of trees TOKI is allowed to read/write. Defaults to the
    non-Windows data drive (D:\\) and the user's ACTUAL Desktop (resolved
    via Windows' known-folder API, not assumed from %USERPROFILE%) --
    but if the installer collected a different allow-list from the user
    (config/sandbox_config.json), that list is used instead. Everything
    outside the active roots is off-limits — no app-launching, no
    System32, no Program Files.

    Uses ntpath explicitly (not os.path) since this app only ever runs on
    Windows, regardless of what platform it's developed/tested on.
    """
    configured = _load_configured_roots()
    if configured:
        return configured
    return [ntpath.normpath(r"D:\\"), _resolve_real_desktop_path()]


def is_within_sandbox(path: str) -> bool:
    """Reject anything that resolves outside the sandbox roots, including via '..' traversal.

    BETA 0.3.44 fix: root strings from get_sandbox_roots() are now
    ntpath-normalized here too, not just the path being checked. Never
    surfaced before because production's own get_sandbox_roots() already
    returns pre-normalized Windows-style strings (ntpath.normpath(r"D:\\")
    and _resolve_real_desktop_path()'s own Windows API result) -- but any
    test (or future caller) that monkeypatches get_sandbox_roots() to a
    raw POSIX-style path (e.g. a pytest tmp_path fixture, the same
    pattern tests/test_file_index.py's own `sandbox` fixture already
    uses for FileIndex) silently failed every check: ntpath.normpath()
    turns "/tmp/x/Desktop" into "\\tmp\\x\\desktop" for the PATH being
    checked, but the un-normalized ROOT string still had forward
    slashes, so the two never matched. Normalizing both sides the same
    way fixes that -- a no-op on already-normalized production roots,
    real for anything else.
    """
    try:
        resolved = ntpath.normpath(path).lower()
    except Exception:
        return False
    for root in get_sandbox_roots():
        try:
            root_stripped = ntpath.normpath(root).lower().rstrip("\\")
        except Exception:
            continue
        if resolved == root_stripped or resolved.startswith(root_stripped + "\\"):
            return True
    return False


# ─── File index ───────────────────────────────────────────────────────────────
#
# Same "fetch once at startup, cache, fail soft" pattern as apis.py's
# LocationCache and app_control.py's AppController._get_installed_apps --
# but for the sandbox's actual file/folder contents, not IP geolocation or
# the Start Menu. This is what resolve_open_target() (below) needed and
# didn't have: step 2 of that cascade only ever checked ONE resolved path
# for existence, so "open <name>" only worked if the name matched a path
# exactly -- no fuzzy matching against what's actually on disk, unlike step
# 1's app check, which already fuzzy-matches via _score_app_match.
#
# Deliberately whole-tree, not lazy-per-directory: the sandbox is only two
# roots (D:\ and the real Desktop, see get_sandbox_roots() above), not the
# whole disk, so a full recursive scandir is cheap (a few hundred ms even
# for tens of thousands of entries) and gives fuzzy matching something real
# to search across, the same way Get-StartApps gives the app cascade a real
# list instead of one exact-path guess.
class FileIndex:
    """Indexes every file/folder under the sandbox roots once, then serves
    fast in-memory lookups against that snapshot for the rest of the
    session. Call invalidate() (or refresh_path()) after TOKI performs a
    write of its own (create/delete/rename/move) so the index doesn't go
    stale from its own actions -- TOKI already knows exactly when that
    happens, so there's no need to guess or poll for external changes."""

    def __init__(self):
        self._entries: Optional[List[Dict[str, str]]] = None

    def _scan(self) -> List[Dict[str, str]]:
        """Returns [] (not None) on any failure -- a missing/unreadable
        root, a permissions error partway through a subtree, etc. -- so
        every caller can treat "nothing indexed" and "couldn't index"
        identically, same collapsing-of-failure-modes reasoning as
        AppController._get_installed_apps()."""
        entries: List[Dict[str, str]] = []
        for root in get_sandbox_roots():
            try:
                for dirpath, dirnames, filenames in os.walk(root):
                    for d in dirnames:
                        entries.append({
                            "name": d,
                            "path": ntpath.normpath(ntpath.join(dirpath, d)),
                            "is_dir": True,
                        })
                    for f in filenames:
                        entries.append({
                            "name": f,
                            "path": ntpath.normpath(ntpath.join(dirpath, f)),
                            "is_dir": False,
                        })
            except Exception:
                # Fail soft per-root -- a problem under D:\ shouldn't
                # discard whatever Desktop already scanned fine.
                continue
        return entries

    def get_entries(self) -> List[Dict[str, str]]:
        if self._entries is None:
            self._entries = self._scan()
        return self._entries

    def invalidate(self) -> None:
        """Force a full rescan on next use. Cheap enough (see class
        docstring) to call this after any write TOKI performs, rather than
        trying to patch the in-memory list incrementally."""
        self._entries = None

    def find_best_match(self, name: str) -> Optional[Dict[str, str]]:
        """Fuzzy existence check against real indexed entries, same scoring
        function app_control.py already uses for installed apps
        (_score_app_match is generic name-vs-name scoring, nothing
        app-specific in it) -- so "open my resme" can still find
        "resume.docx" the same way "open vscode" already finds "Visual
        Studio Code", instead of requiring an exact filename match.

        Extra guard beyond _APP_MATCH_THRESHOLD, specific to files: a
        short query like "vscode" can score HIGHER against a short,
        generic single-word file/folder name (e.g. a folder literally
        named "Code", 0.917) than against the app it actually means
        ("Google Chrome" only scores 0.875 for "chrome") -- because a
        short file name carries much less disambiguating signal than a
        full app display name does. Found live: this is the likely
        mechanism behind an early bug report where an app query fell
        through to a wrong file match once the app check itself missed.
        Guarded here, not in _score_app_match, since that function stays
        correct for its primary job (app matching, where this specific
        risk doesn't arise the same way) -- reject a match whose own
        name is shorter than the QUERY (both with any file extension
        stripped first, so "resume.docx" the query and "resume.docx"
        the match compare on the same basis, not 11 vs 6 chars).
        Checked against real cases: the risky match (vscode -> "Code")
        has the matched stem shorter than the query (4 < 6); every
        legitimate case checked (chrome -> "Google Chrome", calc ->
        "Calculator", obs -> "OBS Studio", vscode -> "Visual Studio
        Code", homework -> "Homework", resume(.docx) -> "resume.docx")
        has the matched stem at least as long as the query stem -- an
        abbreviation query reasonably stands for something LONGER,
        never something shorter, so this cleanly separates the two.
        (First version of this fix compared the stripped match against
        the RAW query including its extension, which wrongly rejected
        exact matches like "resume.docx" itself -- caught by testing
        before shipping, not left in.)"""
        from app_control import _score_app_match, _APP_MATCH_THRESHOLD
        entries = self.get_entries()
        if not entries:
            return None
        best_entry, best_score = None, 0.0
        for e in entries:
            s = _score_app_match(name, e["name"])
            if s > best_score:
                best_entry, best_score = e, s
        if best_entry is None or best_score < _APP_MATCH_THRESHOLD:
            return None
        matched_stem = ntpath.splitext(best_entry["name"])[0]
        query_stem = ntpath.splitext(name)[0]
        if len(matched_stem) < len(query_stem):
            return None
        return best_entry


file_index = FileIndex()


def resolve_path(raw: str, default_root: Optional[str] = None) -> Optional[str]:
    """
    Turn a user-supplied name/path into a full sandboxed path.
      - Already-absolute paths get validated against the sandbox.
      - Bare names ("Homework", "notes.txt") default to Desktop.
    Returns None if the result would fall outside the sandbox.
    """
    raw = raw.strip().strip("'\"")
    if not raw:
        return None

    if ntpath.isabs(raw) or re.match(r"^[A-Za-z]:\\", raw):
        candidate = ntpath.normpath(raw)
    else:
        root = default_root or get_desktop_root()  # Desktop by default
        candidate = ntpath.normpath(ntpath.join(root, raw))

    return candidate if is_within_sandbox(candidate) else None


# ─── Generic text-pulling helpers ─────────────────────────────────────────────

_QUOTED_RE = re.compile(r"[\"']([^\"']+)[\"']")

# NEW convention: "" is an explicit literal for a NAME/CONTENT slot (file
# name, folder name, search query, etc.) -- whatever the user puts in
# double quotes is used VERBATIM, no heuristic guessing needed at all.
# '' is reserved for an EXISTING app name (LAUNCH_APP / app_control), kept
# separate from "" specifically so a single instruction could eventually
# name both a literal AND an app unambiguously (e.g. open 'Chrome' and
# make a file called "notes.txt") without the two colliding. Both quote
# types still fall back to the old merged _QUOTED_RE / heuristic behavior
# below if the "wrong" quote type is used, so nothing that worked before
# this change stops working.
_DOUBLE_QUOTED_RE = re.compile(r'"([^"]+)"')
_SINGLE_QUOTED_RE = re.compile(r"'([^']+)'")

# Phrases that introduce the "name" of a thing, in rough priority order.
_NAME_TRIGGERS = [
    r"\bcalled\s+(.+?)(?:\s+on\s+(?:my\s+)?desktop|\s+in\s+d\b|$)",
    r"\bnamed\s+(.+?)(?:\s+on\s+(?:my\s+)?desktop|\s+in\s+d\b|$)",
    r"\btitled\s+(.+?)$",
]

_ON_D_DRIVE_RE = re.compile(r"\bin\s+d\b|\bin\s+d:|\bin\s+d\s+drive\b", re.IGNORECASE)


def _extract_name(text: str) -> Optional[str]:
    """Pull whatever the user called the thing they want to create/act on.

    Priority: explicit "" literal first (new convention -- unambiguous,
    used verbatim) -- then the old merged-quote behavior (either quote
    type, for backward compatibility with anything typed before this
    convention existed) -- then the trigger-word heuristics.
    """
    m = _DOUBLE_QUOTED_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _QUOTED_RE.search(text)
    if m:
        return m.group(1).strip()
    for pattern in _NAME_TRIGGERS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            name = m.group(1).strip().rstrip(".,!")
            if name:
                return name
    return None


# ── ORGANIZE_FILES_BY_TOPIC / GROUP_FILES_BY_EXTENSION helpers ─────────────
#
# Kept together and near each other deliberately -- these two intents look
# superficially similar in casual phrasing ("organize"/"put in a folder")
# but are handled with opposite postures: ORGANIZE_FILES_BY_TOPIC infers a
# destination from evidence (file_graph/), so it never needs to ask for
# anything up front; GROUP_FILES_BY_EXTENSION is a fully explicit
# instruction with two required slots, so a miss on either means "ask",
# never "guess". See each intent's own branch in extract_slots() below.

_ORGANIZE_INCLUDE_SUGGESTIONS_RE = re.compile(
    r"including suggestions|include suggestions|include the suggested"
    r"|suggested ones? too|be more aggressive|the maybes? too",
    re.IGNORECASE,
)

# Deliberately narrow: only "folder named X" / "folder called X", and only
# a SINGLE token for X (no spaces) -- see extract_slots()'s own
# GROUP_FILES_BY_EXTENSION branch docstring for why a genuinely reliable
# multi-word folder-name capture isn't attempted here (this file has no
# real language understanding, so extending to multi-word names risks
# swallowing trailing filler like "folder named rezero please" as the
# name itself; a miss here just means the intent falls through to None
# and the user gets asked, which is the safe outcome either way).
_GROUP_DEST_FOLDER_RE = re.compile(
    r"folder\s+(?:named|called)\s+[\"']?([A-Za-z0-9][\w\-]*)[\"']?",
    re.IGNORECASE,
)

# Known file-type words -> real extensions. Both singular and plural forms
# are listed explicitly (rather than stripping a trailing "s" in code)
# so a word like "this" or "gps" is never accidentally mangled by a
# generic singularization rule.
_EXTENSION_WORD_MAP = {
    "pdf": [".pdf"], "pdfs": [".pdf"],
    "json": [".json"],
    "image": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"],
    "images": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"],
    "photo": [".jpg", ".jpeg", ".png"], "photos": [".jpg", ".jpeg", ".png"],
    "picture": [".jpg", ".jpeg", ".png"], "pictures": [".jpg", ".jpeg", ".png"],
    "doc": [".doc", ".docx"], "docs": [".doc", ".docx"],
    "word": [".doc", ".docx"],
    "text": [".txt"], "txt": [".txt"], "txts": [".txt"],
    "excel": [".xls", ".xlsx"],
    "spreadsheet": [".xls", ".xlsx"], "spreadsheets": [".xls", ".xlsx"],
    "zip": [".zip"], "zips": [".zip"],
    "archive": [".zip", ".rar", ".7z"], "archives": [".zip", ".rar", ".7z"],
    "video": [".mp4", ".mov", ".mkv", ".avi"], "videos": [".mp4", ".mov", ".mkv", ".avi"],
    "audio": [".mp3", ".wav", ".flac", ".m4a"], "music": [".mp3", ".wav", ".flac", ".m4a"],
    "csv": [".csv"], "csvs": [".csv"],
    "powerpoint": [".ppt", ".pptx"], "ppt": [".ppt", ".pptx"], "pptx": [".pptx"],
}

# Fallback for a type-word not in the dict above, used ONLY in the shape
# "<word> file(s)" (e.g. "csv files", "log files") -- the word directly
# preceding "file(s)" becomes its own literal extension. Guarded by
# _EXTENSION_FALLBACK_STOPWORDS so generic phrasing like "all the files"
# or "these files" doesn't get misread as an extension named "the"/"these".
_GENERIC_EXT_FILES_RE = re.compile(r"\b([a-z][a-z0-9]{1,4})s?\s+files?\b", re.IGNORECASE)
_EXTENSION_FALLBACK_STOPWORDS = frozenset({
    "the", "all", "my", "these", "those", "some", "any", "loose", "other",
    "more", "new", "old", "such", "same", "rest", "remaining",
})


def _extract_extensions(text: str) -> List[str]:
    """Returns real, deduped, lowercase extensions (with leading dot)
    mentioned in `text`, in first-seen order. Empty list means "couldn't
    confidently tell which file types" -- the caller treats that as an
    extraction miss (ask), never a guess at "all files"."""
    lower = text.lower()
    found: List[str] = []
    for word in sorted(_EXTENSION_WORD_MAP, key=len, reverse=True):
        if re.search(rf"\b{re.escape(word)}\b", lower):
            for ext in _EXTENSION_WORD_MAP[word]:
                if ext not in found:
                    found.append(ext)
    for m in _GENERIC_EXT_FILES_RE.finditer(lower):
        word = m.group(1)
        if word in _EXTENSION_FALLBACK_STOPWORDS:
            continue
        ext = f".{word}"
        if ext not in found:
            found.append(ext)
    return found


def _default_root_for(text: str) -> str:
    # Desktop is the default when nothing else is said -- resolved
    # directly rather than assumed from roots list position, since a
    # user-configured allow-list may not include Desktop at all or may
    # not put it second. If "D:\" was mentioned explicitly, only honor
    # it when D:\ is actually one of the active sandbox roots; otherwise
    # fall back to the first configured root so an explicit mention
    # doesn't silently escape a custom allow-list.
    roots = get_sandbox_roots()
    if _ON_D_DRIVE_RE.search(text):
        d_drive = ntpath.normpath(r"D:\\")
        for root in roots:
            if ntpath.normpath(root).lower() == d_drive.lower():
                return root
        return roots[0]
    return get_desktop_root() if get_desktop_root() in roots else roots[0]


# BETA 0.3.27 fix -- confirmed live: the OLD single regex's drive-letter
# branch ([A-Za-z]:\\[^\s"']+) stopped at the FIRST whitespace, so any
# unquoted absolute path with a space anywhere silently truncated:
#   "read the file at D:\notes\meeting notes.txt" -> "D:\notes\meeting"
#   (dropped " notes.txt")
#   "delete D:\old files\draft v2.docx" -> "D:\old"
#   (dropped almost everything)
# Quoting worked around it, but nothing forced the user to quote, and the
# truncated path then got resolved/read/deleted with no error surfaced --
# just silently operating on a path that doesn't exist.
#
# A path CAN legally contain spaces (folder/file names routinely do) but
# has no other delimiter marking where it ends -- so this is split into
# two ordered passes instead of one regex trying to do both jobs:
#
# Pass 1 (preferred): a drive-letter path that ends in a recognizable
# extension, matched NON-GREEDILY so it stops at the first dot+extension
# it finds rather than swallowing everything to the end of the line.
# Handles both repro cases above correctly (there's only one literal dot
# in either path). KNOWN remaining edge case, not fixed here: a path
# containing an earlier dot that ALSO looks like an extension (e.g. a
# folder literally named "v1.2") would still cut short at that first
# dot -- rare, and a strict improvement over the old first-WHITESPACE
# cutoff either way.
_BARE_DRIVE_PATH_WITH_EXT_RE = re.compile(r'[A-Za-z]:\\[^"\']+?\.\w{1,5}\b')

# Pass 2 (fallback): no extension anywhere (e.g. deleting/opening a
# folder, not a file) -- there's no boundary marker to anchor on, so take
# the rest of the line after the drive letter and trim a short list of
# common trailing filler words that would otherwise glue themselves onto
# the path (e.g. "delete D:\old files please" -> "D:\old files", not
# "D:\old files please").
_BARE_DRIVE_PATH_RE = re.compile(r'[A-Za-z]:\\[^"\']+$')
_TRAILING_FILLER_RE = re.compile(
    r"\s+(please|now|thanks|thank you|for me)\.?\s*$", re.IGNORECASE
)

# Bare RELATIVE filename (no drive letter), e.g. "notes.txt" inside a
# longer sentence. This regex's [\w .\-]+ character class is happy to
# include a leading VERB too (it's just letters+spaces to the regex) --
# confirmed this was ALREADY silently broken even in the trivial case,
# predating this session's fixes and not caught by any earlier test:
# _extract_bare_path("delete notes.txt") returned "delete notes.txt", not
# "notes.txt" -- a wrong (nonexistent) path handed straight to
# resolve_path() with no error ever surfaced. The known-open issue
# tracked in tests/test_extractor.py::TestKnownOpenIssues was the same
# root cause, just on a longer sentence ("delete the file version 2.0
# from my desktop" -> "delete the file version 2.0").
#
# BETA 0.3.31 fix, two layers:
#   1. Strip a small, curated leading verb+filler prefix (below) BEFORE
#      matching, so the common "delete X.ext" / "read X.ext" case just
#      works correctly instead of needing to fall through to asking.
#   2. Whatever's left still goes through _looks_like_real_name() at the
#      call site (extract_slots()) as a final safety net -- catches
#      cases that survive the strip but still look like a sentence
#      fragment rather than a real name, and asks instead of guessing.
# BETA 0.3.33 addition: also covers find/search, for FIND_FILES's
# identical over-capture bug -- confirmed live, same root cause:
# "find files.txt" -> query "find files.txt" instead of "files.txt";
# "search for report.docx" -> query "search for report.docx" instead of
# "report.docx". FIND_FILES's query is a loose search term (not a
# destructive-dispatch target), so this is lower-stakes than the
# DELETE_ITEM/READ_FILE/etc. case above, but the fix is the same
# mechanism and free to extend to it here (added "for" as an optional
# filler too, for "search for X").
_BARE_PATH_LEADING_VERB_RE = re.compile(
    r"^(?:please\s+)?"
    r"(?:delete|remove|erase|read|open|view|show|display|list|find|search)\s+"
    r"(?:for\s+)?(?:the\s+)?(?:file\s+|document\s+|folder\s+)?",
    re.IGNORECASE,
)
_BARE_FILENAME_RE = re.compile(r"[\w .\-]+\.\w{1,5}")


def _extract_bare_path(text: str) -> Optional[str]:
    """Last-resort: grab anything that looks like a filename or drive path."""
    m = _BARE_DRIVE_PATH_WITH_EXT_RE.search(text)
    if m:
        return m.group(0).strip()
    m = _BARE_DRIVE_PATH_RE.search(text)
    if m:
        candidate = _TRAILING_FILLER_RE.sub("", m.group(0)).strip().rstrip(".,")
        return candidate or None
    # A no-op if `text` doesn't start with a recognized verb -- see
    # _BARE_PATH_LEADING_VERB_RE's comment above for why this runs first.
    stripped = _BARE_PATH_LEADING_VERB_RE.sub("", text, count=1)
    m = _BARE_FILENAME_RE.search(stripped)
    return m.group(0).strip() if m else None


# ─── Plausibility guard ───────────────────────────────────────────────────────
#
# Bug this fixes: "now delete the folder you just made" (as a direct answer
# to "Which file or folder should I delete?") was being accepted as a
# LITERAL name -- resolve_missing_slot() only stripped a few fixed lead-ins
# ("call it", "name it", ...) and otherwise trusted the whole answer, so the
# whole sentence became the -LiteralPath handed to PowerShell, which
# obviously doesn't exist. Confirmed live, exact repro: Get-Item failed on
# 'C:\...\Desktop\now delete the folder you just made'. Same root shape as
# extract_open_target_name() below producing a whole raw sentence as an
# "app/file name" when its own filler-stripping doesn't recognize a word
# (e.g. a leading "now") -- both are "something was extracted, but nobody
# checked whether it actually LOOKS like a name" bugs, so both get the same
# fix here: one shared sanity check every caller runs on a candidate
# name/answer BEFORE trusting it as a path component.
#
# Deliberately conservative in the same spirit as app_control.py's
# _APP_MATCH_THRESHOLD (0.72) / _MATCH_THRESHOLD (0.55): a real filename
# that happens to contain one of these words, or that's unusually long, will
# occasionally get rejected and re-asked-about -- that's a minor annoyance.
# An accepted-when-it-shouldn't-have-been case is a wrong delete or a
# PowerShell error shown to the user -- worse by far, so ties go to
# rejecting. Anyone who genuinely wants a verb-containing name can still
# force it verbatim with the existing "" literal convention (_extract_name
# already checks that FIRST and returns before this guard ever runs).
_SENTENCE_VERBS = {
    "delete", "remove", "open", "launch", "start", "run", "make", "create",
    "rename", "move", "copy", "find", "search", "kill", "stop", "close",
    "end", "terminate", "read", "list", "empty", "clear", "lock", "mute",
    "unmute", "click", "type", "wait", "check",
}

# Bare back-reference with no concrete noun at all ("it", "that", "this
# one") -- what resolve_anaphoric_target() below exists to handle instead
# of ever being treated as a literal name.
_BARE_PRONOUN_ONLY_RE = re.compile(r"^(?:it|that|this|this one|that one)\.?$", re.IGNORECASE)


def _looks_like_real_name(candidate: str) -> bool:
    """
    Sanity check for "does this actually look like a short name/path
    component, or does it look like a leftover sentence/instruction that
    slipped through extraction?" Returns False for the latter so the
    caller can treat it as an extraction failure (ask again, or try
    anaphora resolution) instead of dispatching on it.

    Not fuzzy or inferential -- three fixed, cheap checks:
      1. Empty after stripping -> not real.
      2. Bare pronoun with no noun ("it", "that") -> not a name at all.
      3. Too long (>60 chars or >6 words) OR contains one of TOKI's own
         action verbs as a whole word -> almost certainly a whole
         sentence/instruction, not something a person would actually name
         a file/folder/process.
    """
    c = candidate.strip()
    if not c:
        return False
    if _BARE_PRONOUN_ONLY_RE.match(c):
        return False
    if len(c) > 60 or len(c.split()) > 6:
        return False
    words = re.findall(r"[a-zA-Z']+", c.lower())
    if any(w in _SENTENCE_VERBS for w in words):
        return False
    return True


# Descriptive back-reference ("the folder you just made", "the file you
# created") as well as a bare pronoun anywhere in the message ("open it",
# "now delete it", "rename that"). Deliberately broad on the bare-pronoun
# half -- see is_anaphoric_reference()'s docstring for why that's safe
# despite matching lots of unrelated "it"/"that" usages.
_ANAPHORA_BACKREF_RE = re.compile(
    r"\bthe\s+(?:file|folder|directory|thing|item|one)\s+"
    r"(?:that\s+)?(?:you\s+)?(?:just\s+|previously\s+|earlier\s+)?"
    r"(?:made|created)\b",
    re.IGNORECASE,
)
_ANAPHORA_PRONOUN_RE = re.compile(r"\b(?:it|that|this)\b", re.IGNORECASE)


def is_anaphoric_reference(text: str) -> bool:
    """
    True if the message points back at something ("it", "that", "the
    folder you just made") rather than naming a concrete target.

    Deliberately broad -- "it"/"that" anywhere in the message counts, not
    just as the sole content. This is only safe because of WHERE this gets
    called from (see orchestrator.py's _resolve_anaphora_if_possible):
    it's only ever consulted for a small, fixed set of intents that need a
    path-like slot (see ANAPHORA_ELIGIBLE_INTENTS below), and only AFTER
    extract_slots()/resolve_missing_slot() already failed to find a real
    named target from the same text. A message that both (a) genuinely
    needs a path slot, (b) has no extractable name, and (c) happens to
    contain "it"/"that" for an unrelated reason is rare enough, and the
    fallback behavior if it's wrong is bounded (it can only resolve to
    something TOKI itself just touched this session, never an arbitrary
    unrelated file) -- strictly safer than the two things it replaces:
    fuzzy-matching against the whole raw sentence, or crashing PowerShell
    on a literal-sentence path.
    """
    return bool(_ANAPHORA_BACKREF_RE.search(text) or _ANAPHORA_PRONOUN_RE.search(text))


# Intents where "it"/"that"/"the folder you just made" can be resolved
# against _last_touched (orchestrator.py) instead of asking. Deliberately
# NOT every path-taking intent -- MAKE_FOLDER/MAKE_FILE need a NEW name
# (anaphora doesn't apply to naming something that doesn't exist yet), and
# RENAME_ITEM/MOVE_ITEM/COPY_ITEM's two-slot "X to Y" shape needs its own
# follow-up (tracked in STATUS.md, not done here to keep this change
# focused and fully tested rather than sprawling into half-covered cases).
ANAPHORA_ELIGIBLE_INTENTS = frozenset({"DELETE_ITEM", "OPEN_ITEM", "READ_FILE"})


def resolve_anaphoric_target(intent: str, last_touched: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """
    Turns "it"/"that"/"the folder you just made" into real slots using
    orchestrator.py's _last_touched memory (the path TOKI itself most
    recently created/renamed/moved/copied/generated this session).
    Returns None if there's nothing remembered yet, or if the intent isn't
    one of ANAPHORA_ELIGIBLE_INTENTS -- caller falls back to the normal
    ask-a-question path in either case, exactly as before this existed.
    """
    if intent not in ANAPHORA_ELIGIBLE_INTENTS:
        return None
    if not last_touched or not last_touched.get("path"):
        return None
    return {"path": last_touched["path"]}


# ── Selected-file conversion intents ────────────────────────────────────────
#
# "this file I'm selecting" / "this image" / "the file I dragged in" all
# resolve against selection_context.py's SelectionContext (the file the
# USER most recently pointed at), NOT orchestrator._last_touched (which is
# what TOKI itself last wrote) and NOT target_memory.py (UI element clicks).
# See selection_context.py's module docstring for why these three stores
# are kept deliberately separate.
SELECTION_ELIGIBLE_INTENTS = frozenset({
    "CONVERT_SELECTED_FILE", "RESIZE_SELECTED_FILE",
    "COMPRESS_SELECTED_FILE", "EXTRACT_SELECTED_FILE",
})

# Recognized target-format words. Deliberately a closed vocabulary list,
# same "no guessing" posture as everything else here -- if the user names
# a format not on this list, _extract_target_format returns None and
# FileConvertAPI reports it couldn't tell what format was meant, rather
# than silently trying to interpret an arbitrary word as an extension.
_KNOWN_FORMAT_WORDS = {
    "text": "txt", "txt": "txt", "json": "json", "csv": "csv", "tsv": "tsv",
    "xml": "xml", "yaml": "yaml", "yml": "yml", "markdown": "md", "md": "md",
    "png": "png", "jpg": "jpg", "jpeg": "jpg", "webp": "webp", "bmp": "bmp",
    "gif": "gif", "tiff": "tiff", "ico": "ico", "ppm": "ppm", "pcx": "pcx", "tga": "tga",
    "word": "docx", "docx": "docx", "pdf": "pdf", "html": "html", "htm": "html",
    "rtf": "rtf", "odt": "odt", "zip": "zip",
    "powerpoint": "pptx", "pptx": "pptx", "epub": "epub",
    "tar": "tar", "tgz": "tgz", "tarball": "tar.gz",
    "ini": "ini", "conf": "conf", "toml": "toml",
    "mp3": "mp3", "wav": "wav", "flac": "flac", "aac": "aac", "ogg": "ogg", "m4a": "m4a",
    "mp4": "mp4", "mkv": "mkv", "webm": "webm", "mov": "mov", "avi": "avi",
}

_FORMAT_WORD_RE = re.compile(
    r"\b(?:to|into|as)\s+(?:an?\s+)?(" + "|".join(sorted(_KNOWN_FORMAT_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
# Bare ".ext" mention, e.g. "convert this to .pdf"
_DOT_EXT_RE = re.compile(r"\.([a-zA-Z]{2,5})\b")


def _extract_target_format(text: str) -> Optional[str]:
    """Pulls the target format out of phrasing like "turn this into a text
    file" / "convert to json" / "make this a .pdf". Returns a bare
    extension (no dot), or None if nothing recognizable was said -- the
    caller (FileConvertAPI) is responsible for asking rather than TOKI
    guessing."""
    m = _FORMAT_WORD_RE.search(text)
    if m:
        return _KNOWN_FORMAT_WORDS[m.group(1).lower()]
    m = _DOT_EXT_RE.search(text)
    if m and m.group(1).lower() in _KNOWN_FORMAT_WORDS.values():
        return m.group(1).lower()
    return None


_SHRINK_WORDS_RE = re.compile(r"\b(shrink|smaller|reduce|too big|too large)\b", re.IGNORECASE)
_ENLARGE_WORDS_RE = re.compile(r"\b(enlarge|bigger|larger|grow|blow up|too small)\b", re.IGNORECASE)
_PERCENT_RE = re.compile(r"\bby\s+(\d{1,3})\s*%|\b(\d{1,3})\s*%", re.IGNORECASE)
_DIMENSIONS_RE = re.compile(r"\b(\d{2,5})\s*(?:x|by|X)\s*(\d{2,5})\b")
_WIDTH_ONLY_RE = re.compile(r"\b(\d{2,5})\s*(?:px|pixels?)\s*wide\b", re.IGNORECASE)


def _extract_resize_params(text: str) -> Dict[str, str]:
    """Returns a slot dict with at most one of width/height / width+height /
    scale set. Deliberately permissive on the RETURN side (an empty dict
    is a valid, meaningful result -- FileConvertAPI's resize_file()
    defaults to a 50% shrink) since "shrink this image" alone is already
    a complete, unambiguous instruction; this only extracts MORE specific
    numbers when the user actually gave them.

    A bare percentage is direction-less on its own ("50%" could mean
    half OR double) -- _ENLARGE_WORDS_RE / _SHRINK_WORDS_RE resolve that
    from wording. Enlarge wins on a tie (mirrors resize_file()'s own
    shrink-by-default bias for the *other* direction, so an ambiguous
    "50% bigger, or maybe not" phrasing still does something sane rather
    than silently no-op'ing)."""
    dims = _DIMENSIONS_RE.search(text)
    if dims:
        return {"width": dims.group(1), "height": dims.group(2)}

    width_only = _WIDTH_ONLY_RE.search(text)
    if width_only:
        return {"width": width_only.group(1)}

    pct = _PERCENT_RE.search(text)
    if pct:
        pct_value = int(pct.group(1) or pct.group(2))
        if _ENLARGE_WORDS_RE.search(text):
            return {"scale": str(1 + pct_value / 100)}
        # Shrink wording, or bare "50%" with no direction word at all --
        # scale-as-shrink-fraction is resize_file()'s own default
        # semantics (its no-args default IS a 50% shrink), so treating
        # a direction-less percentage as "shrink to X%" matches that.
        return {"scale": str(pct_value / 100)}

    # "shrink"/"smaller"/"too big" with no number -- let resize_file()'s
    # own stated DEFAULT_SHRINK_SCALE apply; no slot needed.
    return {}


def _extract_compress_quality(text: str) -> str:
    """"compress this a lot" -> lower quality number; a bare "compress"
    uses compress_file()'s own default. Small, explicit mapping rather
    than trying to parse degree-of-compression from free text."""
    if re.search(r"\b(a lot|way|much|really)\s+(smaller|more)\b", text, re.IGNORECASE):
        return "30"
    if re.search(r"\bslightly|a (little|bit)\b", text, re.IGNORECASE):
        return "80"
    return ""


# ── Video download intents ──────────────────────────────────────────────────
#
# "download this video" / "grab that as mp3" -- url comes either straight
# out of the user's own message (a pasted link) or, for
# DOWNLOAD_PLAYING_VIDEO, from video_downloader.now_playing's UI-Automation
# read of the focused browser's address bar (resolved in apis.py, not here --
# this module stays a pure function of user_text, same contract as every
# other intent).
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

_AUDIO_ONLY_RE = re.compile(
    r"\b(just the audio|audio only|only the audio|as (?:an?\s+)?mp3|"
    r"extract (?:the\s+)?audio|convert (?:it\s+)?to (?:an?\s+)?mp3)\b",
    re.IGNORECASE,
)


def _extract_url(text: str) -> Optional[str]:
    """Pulls the first http(s) link out of free text, trimming trailing
    punctuation a sentence would naturally add around a pasted URL (a
    period ending the sentence, a closing paren/quote). Returns None if no
    link is present -- never guesses one from a bare video title."""
    m = _URL_IN_TEXT_RE.search(text)
    if not m:
        return None
    return m.group(0).rstrip(".,)\"'\u201d\u2019")


def _extract_audio_only(text: str) -> str:
    """Returns \"true\" if the user asked for audio-only, else \"\" (the
    caller/API defaults to full video) -- same empty-string-means-\"not
    stated\" convention _extract_compress_quality() above already uses."""
    return "true" if _AUDIO_ONLY_RE.search(text) else ""


def resolve_selected_file_target(intent: str, selected: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """Mirrors resolve_anaphoric_target()'s shape exactly, but resolves
    against selection_context.py's SelectionContext instead of
    orchestrator._last_touched. Returns None if there's nothing currently
    selected (caller falls back to FileConvertAPI's own "nothing is
    selected" message) or if the intent isn't selection-eligible."""
    if intent not in SELECTION_ELIGIBLE_INTENTS:
        return None
    if not selected or not selected.get("path"):
        return None
    return {"path": selected["path"]}


# BUG FOUND AND FIXED: the original pattern required the city name to
# START WITH A CAPITAL LETTER ([A-Z]...). Tested directly:
# "whats the weather in lahore" (all lowercase, a completely ordinary way
# to type a chat message) returned None -- silently falling back to
# "couldn't determine your location" even though the user explicitly
# named a city. Confirmed this was never caught before: the only existing
# tests for this (test_orchestrator.py) only ever used "Lahore"/"Karachi",
# properly capitalized. Fixed to match case-insensitively.
#
# That alone introduced a NEW problem: without the capital-letter cue to
# mark where the city name stops, greedy matching against a
# non-capitalized sentence swallows trailing words too --
# "weather in lahore please" was capturing "lahore please" as the city.
# Fixed by stripping a small set of common trailing filler words
# ("please", "right now", "today", etc.) after the match, applied
# repeatedly in case more than one stacks ("...in london right now
# please").
_CITY_RE = re.compile(r"\bin\s+([a-zA-Z][a-zA-Z\s]+?)(?:[.?!]|$)", re.IGNORECASE)
_CITY_TRAILING_FILLER_RE = re.compile(
    r"\s+(please|right now|now|today|tomorrow|thanks|thank you)$",
    re.IGNORECASE,
)


def _extract_city(text: str) -> Optional[str]:
    m = _CITY_RE.search(text)
    if not m:
        return None
    city = m.group(1).strip()
    for _ in range(3):  # strip repeatedly in case multiple filler words stack
        stripped = _CITY_TRAILING_FILLER_RE.sub("", city).strip()
        if stripped == city:
            break
        city = stripped
    return city or None


# PowerShell's Stop-Process/Wait-Process/Get-Process -Name parameter matches
# against Process.ProcessName, which NEVER includes the executable
# extension -- the real property for notepad.exe is just "notepad". A
# process-name slot that still has ".exe" (or another executable
# extension) on it will therefore silently match zero processes, even
# though the name is otherwise exactly right. Confirmed directly: "kill
# notepad.exe" -- the flagship test case used throughout this project's
# own STATUS.md for the classification-layer bugs -- would still do
# nothing once routed and dispatched correctly, because
# `Stop-Process -Name 'notepad.exe'` matches nothing. Stripping the
# extension here (once, at the slot-fill boundary) fixes it for every
# process-name-taking intent at once, the same "one place, not per-
# template" principle _escape_ps_slot() already uses in orchestrator.py.
_EXE_EXT_RE = re.compile(r"\.(exe|com|bat|cmd|scr|msi)$", re.IGNORECASE)


def _strip_exe_extension(name: str) -> str:
    stripped = _EXE_EXT_RE.sub("", name.strip()).strip()
    return stripped or name


# Strip common leading filler ("okay can you", "please", "i'll", "i want
# you to", etc.) and an "open/launch/start/run" trigger verb if present.
# Originally LAUNCH_APP-only; factored out to module level (and renamed
# without the leading underscore) so orchestrator.py's open-target cascade
# can reuse the EXACT SAME heuristic to get a candidate name BEFORE
# deciding whether that name resolves to an installed app or a file/
# folder -- using two different extraction heuristics for the same bare
# name would make the two paths disagree on what the user actually typed.
_OPEN_FILLER_LEAD = re.compile(
    r"^(okay,?\s*|ok,?\s*|sure,?\s*|alright,?\s*)?"
    r"(can|could|would|will)?\s*(you\s+)?"
    r"(please\s+)?"
    r"(i'?ll\s+|i will\s+|i want you to\s+|i'?d like to\s+)?"
    r"(open|launch|start|run)?\s*(the\s+)?",
    re.IGNORECASE,
)


def extract_open_target_name(text: str) -> Optional[str]:
    """Strips conversational filler + an open/launch/start/run trigger verb,
    leaving just the bare name of whatever the user wants opened -- doesn't
    know or care yet whether that resolves to an app, a file, or a folder.
    Run twice since filler words can stack (e.g. "okay ill open X" strips
    "okay" and "ill" in separate passes, not one)."""
    cleaned = text
    for _ in range(2):
        cleaned = _OPEN_FILLER_LEAD.sub("", cleaned).strip()
    cleaned = re.sub(
        r"\s+(for me|please|then|now|app(?:lication)?)\.?$",
        "", cleaned, flags=re.IGNORECASE,
    ).strip()
    # If nothing matched the trigger-verb/filler patterns at all, cleaned
    # may just be the original text (e.g. a bare "chrome" follow-up) --
    # that's fine, it's likely already just the name. Only fail if nothing
    # usable is left after stripping.
    cleaned = cleaned.strip("'\" ") or None
    if cleaned is None:
        return None

    # Bug this fixes: "now open it" -- _OPEN_FILLER_LEAD doesn't recognize
    # a leading "now" (only okay/ok/sure/alright + modal verbs + please +
    # i'll/i will/...), so nothing gets stripped and `cleaned` comes back
    # as the entire original sentence, "now open it". That then got run
    # straight through app-existence and fuzzy file-name matching as if it
    # were a real short name to search for -- confirmed live, this is how
    # a request to open "it" ended up resolving to a real but completely
    # unrelated folder on the machine instead of asking what "it" meant.
    # Same _looks_like_real_name() guard as resolve_missing_slot() uses,
    # applied here for the same reason: something got extracted, but
    # nothing had checked whether it actually LOOKS like a name yet.
    if not _looks_like_real_name(cleaned):
        return None
    return cleaned


def has_explicit_open_convention(text: str) -> bool:
    """True if the user used the '' (explicit app name) or "" (explicit
    file/folder name) convention anywhere in the message. Exposed
    separately from resolve_open_target() (which also checks this
    internally and returns None either way) so a caller can tell apart
    two DIFFERENT reasons resolve_open_target() might return None:
    "the user was explicit, don't run the cascade at all" (this
    function returns True) vs "the cascade ran and genuinely found
    neither an app nor a file/folder" (this function returns False).
    Conflating those two is a real bug, not a hypothetical one -- caught
    directly while wiring this in: without this check, an explicit
    "open 'SomeApp'" with no matching Get-StartApps entry incorrectly
    fell into the "ask the user" branch instead of trusting the explicit
    quote and dispatching LAUNCH_APP as intended.
    """
    return bool(_SINGLE_QUOTED_RE.search(text) or _DOUBLE_QUOTED_RE.search(text))


def resolve_open_target(
    text: str,
    app_exists_fn: Optional[Callable[[str], bool]] = None,
) -> Optional[Dict[str, str]]:
    """
    The actual fix for "asked it to open an app, it tried to open a
    folder instead": classification used to make ONE one-shot decision
    between LAUNCH_APP and OPEN_ITEM based only on word-overlap against a
    fixed, hand-seeded phrasing list -- so any app name that hadn't
    specifically been added to that list (confirmed live: "steam",
    "vscode", "obs" all lost to OPEN_ITEM, which only knows how to look
    on Desktop/D:\\) would misfire, with no way to recover once OPEN_ITEM
    had already committed to asking "which file or folder?".

    This replaces that single guess with a real, deterministic cascade,
    run BEFORE either intent's own extract_slots() commits to anything:
        1. Does an app by this name actually exist (app_exists_fn, a
           real Get-StartApps-backed check, not a guess)? -> LAUNCH_APP.
        2. Does a file/folder by this name actually exist in the sandbox?
           -> OPEN_ITEM.
        3. Neither -> None, caller asks an honest clarifying question
           instead of guessing either way.

    Deliberately skipped (returns None immediately, so the ORIGINALLY
    classified intent's own extract_slots() runs completely unchanged) if
    the user was explicit about which they meant, via this app's existing
    '' (app name literal) or "" (file/folder name literal) convention --
    an explicit quote already means "I know exactly what I want, don't
    second-guess me," so overriding it here would fight the user instead
    of helping them.

    app_exists_fn is injected (not imported directly from app_control.py)
    so this stays testable with a plain function/mock in this pure-Python
    module -- no subprocess/PowerShell needed to test the CASCADE LOGIC
    itself, only app_control.py's own app-matching needs that.
    """
    if _SINGLE_QUOTED_RE.search(text) or _DOUBLE_QUOTED_RE.search(text):
        return None

    name = extract_open_target_name(text)
    if not name:
        return None

    if app_exists_fn is not None and app_exists_fn(name):
        return {"intent": "LAUNCH_APP", "app_name": name}

    path = resolve_path(name, _default_root_for(text))
    if path and os.path.exists(path):
        return {"intent": "OPEN_ITEM", "path": path}

    # Fuzzy fallback: exact path didn't exist -- check the real indexed
    # sandbox contents before giving up (see FileIndex above). Same
    # cascade shape as the app check above it: a real, deterministic
    # lookup against actual data, not a guess.
    match = file_index.find_best_match(name)
    if match:
        return {"intent": "OPEN_ITEM", "path": match["path"]}

    return None


# ─── Per-intent slot extraction ──────────────────────────────────────────────

# ─── Generic WCL (windows_command_library) slot filler ────────────────────
#
# Phase 1 (BETA 0.3.15) of the "876 commands are look-but-don't-touch" gap
# (see STATUS.md): single-{variable} "safe" commands (298 of them).
# Phase 2 (BETA 0.3.36): 2-variable "safe" commands (61 of them) -- see
# _extract_wcl_slots_pair()'s own module comment below for exactly what
# that phase does and doesn't cover. Deliberately narrow scope, matching
# the phasing STATUS.md itself recommended rather than attempting a
# universal filler in one pass:
#
#   - 3+ variable commands (7 of them, all "destructive" anyway) are
#     still an explicitly separate, harder, not-yet-attempted phase.
#   - orchestrator.py only ever calls this for danger_level == "safe"
#     commands, at any variable count this file handles (1 or 2) --
#     "caution"/"destructive" commands still fall through to the LLM/
#     ASK_CONTEXT path exactly like today, on purpose, at EVERY variable
#     count including zero (see STATUS.md BETA 0.3.35 for why the
#     zero-variable case specifically needed its own fix, found and
#     closed separately from this scope-extension work). Confidence-
#     threshold and destructive-command gating are explicitly separate,
#     not-yet-decided product questions (see STATUS.md's "needs the
#     project owner" list) -- this filler only ever unlocks the already-
#     vetted-safe subset, never expands what's allowed to auto-dispatch
#     without confirmation.
#
# Same "never guess" posture as every other extractor in this file: if a
# confident value can't be pulled from the user's own text, return None
# and let the normal missing-slot question flow ask for it -- never
# fabricate a value to fill an unfamiliar variable name.

# Variable names (from windows_command_library.json's own `variables[].name`
# field) that clearly mean "a filesystem path" -- routed through the same
# sandboxed resolve_path() every filesystem intent already uses, so a WCL
# path-shaped command can't reach outside D:\/Desktop any more than
# MAKE_FOLDER or DELETE_ITEM can.
# BETA 0.3.37 fix -- CONFIRMED LIVE: 6 currently-shipped "safe" single-
# variable commands have a clearly path-shaped variable name that was
# NOT in this set -- Take-Screenshot's `destination`, vol's `drive`,
# Save-Help's `destination_path`, mstsc's `rdp_file`, call's
# `batch_file`, and "Backup User Profile"'s `backup_path`. Most
# seriously: `call {batch_file}` runs an arbitrary .bat/.cmd file --
# with `batch_file` not routed through resolve_path()'s sandbox, a value
# could point ANYWHERE on disk, not just under D:\/Desktop, and TOKI
# would execute whatever script is there. Expanded via a full audit of
# every variable name across the whole dataset (234 distinct names),
# not just these 6 -- see STATUS.md for the complete before/after list.
# Deliberately generous here (better to route something that ISN'T
# really a path through resolve_path() and have it safely fail to
# resolve, than to leave something that IS a path unsandboxed) --
# resolve_path() itself is the actual safety boundary; this set just
# decides which variables get routed through it.
_WCL_PATH_VAR_NAMES = {
    "path", "file_path", "folder_path", "directory_path", "directory", "folder",
    "backup_path", "batch_file", "binary_path", "cab_file", "child_path",
    "counter_file", "destination", "destination_path", "file", "file1", "file2",
    "firmware_path", "logfile", "path_value", "rdp_file", "source", "vhd_path",
}

# BETA 0.3.37 fix -- CONFIRMED LIVE, more serious than the path gap
# above: 15 currently-eligible "safe" commands (<=2 variables) have a
# variable representing literal CODE/COMMAND CONTENT, not a plain value
# -- Start-Job/Measure-Command/foreach/%'s `script_block`,
# Set-PSBreakpoint's `script`, Set-Alias/New-Alias/sal/nal's `command`,
# Set-PSReadLineKeyHandler's `function`, Open-Application's `arguments`,
# etc. _ensure_quoted_placeholders() (orchestrator.py) now guarantees
# these values can't break OUT of a string literal via shell metachars
# -- but that's not the only risk: PowerShell parameter binding for a
# [scriptblock]-typed parameter (which -ScriptBlock/-Expression are)
# can implicitly CONVERT a plain string argument into a compiled,
# EXECUTABLE scriptblock (ScriptBlock.Create() semantics) -- meaning a
# value that's perfectly safe AS A STRING LITERAL could still end up
# running as code once bound to a parameter of that type, a risk no
# amount of quoting/escaping the STRING itself can prevent. Categorical
# blocklist by variable name (substring match, deliberately broad --
# also catches command_id/command_name, which aren't actually dangerous,
# but the cost of falling through to "ask" for those is just a UX
# inconvenience, not a safety gap) rather than trying to distinguish
# "safe-shaped" from "dangerous-shaped" content after the fact.
#
# BETA 0.3.37 checkpoint 1: "condition" added -- found while auditing the
# WCL dataset for a different bug (the syntax/variables brace-escaping
# fix in the same checkpoint). `?`/`where` (both "safe") take a
# `condition` variable that gets substituted directly into a live
# `Where-Object { ... }` PowerShell scriptblock -- exactly the same
# raw-expression-injection shape as `script_block`, just under a
# different variable name that the original blocklist didn't happen to
# cover. (Add-DhcpServerv4Policy/v6Policy also use a `condition`
# variable, but those are already "destructive" and never auto-dispatch
# regardless -- this addition is what actually matters for the two
# "safe" ones.)
_WCL_CODE_LIKE_VAR_SUBSTRINGS = ("script", "command", "expression", "function", "argument", "param", "condition")


def _is_wcl_code_like_var(var_name: str) -> bool:
    return any(s in var_name.lower() for s in _WCL_CODE_LIKE_VAR_SUBSTRINGS)

# ─── BETA 0.3.36: phase 2 of the "876 commands" milestone -- 2-variable
# "safe" commands (61 of them) ────────────────────────────────────────────
#
# Deliberately a SLICE of the full milestone, not the whole thing --
# picked the most reliable, most narrowly-scoped strategies rather than
# attempting full free-text 2-slot parsing in one pass:
#
#   1. Exactly two quoted substrings in the message -> assign in order.
#      Most reliable signal there is: the user explicitly delimited both
#      values, no guessing about where one ends and the other begins.
#   2. A natural "X to/into/as Y" separator (no quotes, or the wrong
#      quote count) -> split once on the first such separator. Matches
#      the exact same word order TOKI's OWN RENAME_ITEM/MOVE_ITEM/
#      COPY_ITEM intents already use for their own two-value slots
#      elsewhere in this file -- not a new convention, the same one.
#   3. (BETA 0.3.38) A numeric hint -- a count or an explicitly-unitted
#      size -- for the specific (path/name, count) / (path/name, size)
#      shape that 1 and 2 both structurally can't catch (no quotes, no
#      to/into/as separator at all: "show me the 5 largest files"). See
#      _extract_wcl_numeric_pair()'s own module comment further below for
#      the full detail. Only tried when strategies 1 and 2 both found
#      NO signal at all (not "found a signal but failed to resolve it").
#
# In strategies 1 and 2, the LEFT value maps to var_names[0] and the
# RIGHT value maps to var_names[1] -- this matches syntax order (e.g.
# "Copy-Item -Path {source} -Destination {destination}" lists source
# first), which in turn matches how a WCL command's own template was
# written, not an assumption this extractor makes on its own. Strategy 3
# instead maps by variable NAME (which one is numeric-shaped), since
# order alone can't tell "5 largest files in D:\Projects" apart from
# "in D:\Projects show 5 largest files" the way an explicit separator or
# quote position can.
#
# BETA 0.3.38: a THIRD strategy, added -- see _extract_wcl_numeric_pair()
# below -- for the exact gap this module comment used to describe as not
# yet attempted: a numeric-hint strategy for pairs like (path, count) --
# e.g. "show me the 5 largest files" has no quote and no to/into/as
# separator at all, so strategies 1 and 2 above both miss it and it used
# to fall straight through to "ask". Only tried when NEITHER of the
# above two strategies matched at all (not "matched but failed to
# resolve" -- same "don't guess differently once the user already gave
# an explicit signal" posture strategy 1 already has for its own
# early-return).
_WCL_TWO_VALUE_SEPARATOR_RE = re.compile(r"\s+(?:to|into|as)\s+", re.IGNORECASE)
_QUOTED_RE = re.compile(r'"([^"]+)"|\'([^\']+)\'')

# ─── BETA 0.3.38: numeric-hint strategy for (path/name, count) and
# (path/name, size) pairs ──────────────────────────────────────────────
#
# Scope: only the 5 currently-eligible "safe" 2-variable WCL commands
# that actually have this shape (Get-LargestFiles, Recent Files, Old
# Files, Large Folders -- all (path, count); Find Large Files -- (path,
# size_bytes)). Detected generically by variable NAME (substring "count"
# / "size"), not by a hardcoded command list, so it also covers any
# future WCL dataset addition with the same shape without code changes --
# but the actual behavior is only ever exercised by real data through
# these 5 today.
#
# Same "never guess a value that isn't clearly signaled" posture as
# every other extractor in this file:
#   - "count"-named variable: a plain number, ideally near a keyword
#     that means "how many" ("top 5", "5 largest", "10 files") -- a
#     bare, unqualified number anywhere in the text is still accepted
#     as a last resort, but ONLY if nothing else in the pair extractor
#     already claimed a match (see the ordering in
#     _extract_wcl_slots_pair below).
#   - "size"-named variable: a number is REQUIRED to carry an explicit
#     unit (MB/GB/KB/bytes/...) -- an unqualified bare number is
#     deliberately rejected here, because "size_bytes" is a byte count
#     and a plain "500" in conversation is far more likely to mean
#     something else (a count, a percentage, ...) than "500 bytes".
#     Converted to a literal byte-count string, matching what the WCL
#     template's own `{size_bytes}` substitution expects.
#
# Whatever's left of the sentence after removing the matched numeral (or
# numeral+unit) span is handed to the SAME narrowing helpers
# (_extract_bare_path / _extract_name) and the SAME plausibility+sandbox
# gate (_resolve_single_wcl_value) that strategies 1 and 2 already use
# above -- this is a new way to FIND the two raw candidate strings, not
# a new way to trust them once found.
_WCL_COUNT_AFTER_ORDINAL_WORD_RE = re.compile(r"\b(?:top|first|last)\s+(\d+)\b", re.IGNORECASE)
_WCL_COUNT_NEAR_KEYWORD_RE = re.compile(
    r"\b(\d+)\b(?=\s*\w*\s*(?:largest|biggest|smallest|newest|oldest|recent|"
    r"old|large|files?|folders?|items?|results?)\b)",
    re.IGNORECASE,
)
# Last-resort fallback: any standalone number, but not one glued to a
# drive-letter/path separator (e.g. the "5" in "D:\5\notes.txt", a
# folder literally named "5") -- \b alone doesn't exclude that case
# since backslash/colon are non-word characters too.
_WCL_STANDALONE_INT_RE = re.compile(r"(?<![:\\/\w.])\b(\d+)\b")

_WCL_SIZE_WITH_UNIT_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(bytes?|kilobytes?|megabytes?|gigabytes?|terabytes?|kb|mb|gb|tb|b)\b",
    re.IGNORECASE,
)
_WCL_SIZE_UNIT_MULTIPLIERS = {
    "b": 1, "byte": 1,
    "kb": 1024, "kilobyte": 1024,
    "mb": 1024 ** 2, "megabyte": 1024 ** 2,
    "gb": 1024 ** 3, "gigabyte": 1024 ** 3,
    "tb": 1024 ** 4, "terabyte": 1024 ** 4,
}


def _extract_wcl_numeric_pair(var_names: List[str], text: str) -> Optional[Dict[str, str]]:
    a_name, b_name = var_names[0], var_names[1]

    numeric_name: Optional[str] = None
    is_size = False
    for name in (a_name, b_name):
        lname = name.lower()
        if "count" in lname:
            numeric_name = name
            break
        if "size" in lname:
            numeric_name = name
            is_size = True
            break
    if numeric_name is None:
        return None
    other_name = b_name if numeric_name == a_name else a_name

    if is_size:
        m = _WCL_SIZE_WITH_UNIT_RE.search(text)
        if not m:
            return None
        unit = m.group(2).lower().rstrip("s")
        multiplier = _WCL_SIZE_UNIT_MULTIPLIERS.get(unit)
        if multiplier is None:
            return None
        numeric_value = str(int(float(m.group(1)) * multiplier))
    else:
        m = (
            _WCL_COUNT_AFTER_ORDINAL_WORD_RE.search(text)
            or _WCL_COUNT_NEAR_KEYWORD_RE.search(text)
            or _WCL_STANDALONE_INT_RE.search(text)
        )
        if not m:
            return None
        numeric_value = m.group(1)

    remainder = (text[:m.start()] + " " + text[m.end():]).strip()
    if not remainder:
        return None

    if other_name in _WCL_PATH_VAR_NAMES:
        candidate = _extract_bare_path(remainder) or _extract_name(remainder)
    else:
        candidate = _extract_name(remainder)
    if not candidate:
        return None

    other_val = _resolve_single_wcl_value(other_name, candidate, text)
    if not other_val:
        return None

    return {numeric_name: numeric_value, other_name: other_val}


def _resolve_single_wcl_value(var_name: str, raw: str, full_text: str) -> Optional[str]:
    """Shared by both the single-variable filler above and the 2-variable
    one below -- same path-vs-generic-name split, same plausibility gate,
    just factored out so both call sites can't drift apart on what
    counts as a trustworthy value.

    BETA 0.3.37 fix: _looks_like_real_name() now runs FIRST,
    unconditionally, before the path-vs-generic split -- confirmed live
    that a path-shaped variable name skipped it entirely (went straight
    to resolve_path()), and resolve_path() alone doesn't judge whether a
    string LOOKS like a real path component vs. a whole garbled sentence,
    only whether it resolves inside the sandbox. Repro: 2-variable
    "copy whatever you think is best to backup.txt" split on " to " into
    "copy whatever you think is best" / "backup.txt" -- the first part,
    handed to a path-shaped variable, resolved to a real (if nonsensical)
    sandboxed path instead of being rejected as implausible.
    """
    raw = raw.strip().strip("'\"")
    if not raw or not _looks_like_real_name(raw):
        return None
    if var_name in _WCL_PATH_VAR_NAMES:
        return resolve_path(raw, _default_root_for(full_text))
    return raw


def _extract_wcl_slots_pair(var_names: List[str], text: str) -> Optional[Dict[str, str]]:
    a_name, b_name = var_names[0], var_names[1]

    # Strategy 1: exactly two quoted substrings.
    quoted = [a or b for a, b in _QUOTED_RE.findall(text)]
    if len(quoted) == 2:
        a_val = _resolve_single_wcl_value(a_name, quoted[0], text)
        b_val = _resolve_single_wcl_value(b_name, quoted[1], text)
        if a_val and b_val:
            return {a_name: a_val, b_name: b_val}
        return None  # exactly 2 quotes present but one side didn't resolve -- don't fall through to strategy 2 and guess differently

    # Strategy 2: a natural "X to/into/as Y" separator, split once.
    parts = _WCL_TWO_VALUE_SEPARATOR_RE.split(text, maxsplit=1)
    if len(parts) == 2:
        a_val = _resolve_single_wcl_value(a_name, parts[0], text)
        b_val = _resolve_single_wcl_value(b_name, parts[1], text)
        if a_val and b_val:
            return {a_name: a_val, b_name: b_val}
        return None  # an explicit to/into/as split was found -- same "don't guess differently" rule as strategy 1 above

    # Strategy 3 (BETA 0.3.38): numeric-hint pairing for (path/name, count)
    # and (path/name, size) pairs -- see the module comment above
    # _extract_wcl_numeric_pair() for exactly what this covers. Only
    # reached when NEITHER strategy 1 nor strategy 2 found their own
    # signal at all (no exactly-2-quotes, no to/into/as separator).
    return _extract_wcl_numeric_pair(var_names, text)


def _extract_wcl_slots(var_names: List[str], text: str, stripped_value: Optional[str] = None) -> Optional[Dict[str, str]]:
    # BETA 0.3.37: hard block, checked FIRST, before anything else in
    # this function -- see _is_wcl_code_like_var()'s module comment
    # above for exactly what this closes and why quoting/escaping alone
    # (_ensure_quoted_placeholders() / _escape_ps_slot() in
    # orchestrator.py) isn't sufficient protection on its own for this
    # specific class of variable. Applies regardless of variable count
    # (1 or 2) -- a command needing EITHER a code-like value at all is
    # entirely out of scope for auto-fill, full stop.
    if any(_is_wcl_code_like_var(v) for v in var_names):
        return None

    if len(var_names) == 2:
        # BETA 0.3.36: see module comment above for exactly what this
        # does and doesn't cover. stripped_value (Tier 2's own alias-
        # boundary result) isn't used here -- Tier 2 only ever isolates
        # ONE trailing value, and a 2-variable command needs the FULL
        # original text to find both quote/separator boundaries.
        return _extract_wcl_slots_pair(var_names, text)

    if len(var_names) != 1:
        # 3+ variable commands: still the explicitly separate later phase
        # (see module comment) -- never guess an ordering/split between
        # 3+ values here.
        return None

    var_name = var_names[0]

    if stripped_value:
        # The resolver's Tier 2 already found the exact boundary between
        # its own alias match and the user's real value -- use that
        # directly instead of re-running generic pattern matching against
        # the FULL sentence, which has no way to know the command's own
        # alias words ("view more", "display type of", ...) aren't part
        # of the value and would otherwise swallow the whole thing.
        # Still route through the same path-resolution/plausibility
        # checks below, just starting from a narrower, more accurate
        # candidate.
        if var_name in _WCL_PATH_VAR_NAMES:
            resolved = resolve_path(stripped_value, _default_root_for(text))
            return {var_name: resolved} if resolved else None
        if _looks_like_real_name(stripped_value):
            return {var_name: stripped_value}
        return None

    if var_name in _WCL_PATH_VAR_NAMES:
        candidate = _extract_name(text) or _extract_bare_path(text)
        if not candidate:
            return None
        resolved = resolve_path(candidate, _default_root_for(text))
        return {var_name: resolved} if resolved else None

    # Every other variable name (vm_name, service_name, server, host,
    # command_name, scope, message, uri, ... -- 230+ distinct names with
    # no small fixed vocabulary, see STATUS.md): the same generic
    # quote-first / "called"|"named"|"titled"-triggered extraction every
    # other identifier-ish slot in this file already uses, gated by the
    # same plausibility check (_looks_like_real_name) so a leftover whole
    # sentence never gets treated as a value.
    candidate = _extract_name(text)
    if candidate and _looks_like_real_name(candidate):
        return {var_name: candidate}
    return None


def extract_slots(intent: str, user_text: str, wcl_variables: Optional[List[str]] = None, wcl_stripped_value: Optional[str] = None) -> Optional[Dict[str, str]]:
    """
    Returns a dict of filled slot values, or None if a REQUIRED slot
    couldn't be confidently extracted (caller should fall back to a plain
    chat response asking for the missing detail — never guess).

    wcl_variables: only passed for "WCL_..." intents (windows_command_library
    commands with exactly one {variable} in their syntax -- see
    orchestrator.py's registration block, which only ever passes this for
    danger_level == "safe" single-variable commands). None/empty for every
    one of TOKI's own 62 hand-written intents below, which keep their
    existing per-intent regex completely untouched.

    wcl_stripped_value: the value wcl_resolver.py's Tier 2 already isolated
    by stripping a trailing alias match off the raw query (e.g. "view more
    notes.txt" -> alias "view more" + value "notes.txt") -- when present,
    used INSTEAD of re-deriving a value from the full user_text via generic
    pattern matching, which has no way to know which words are the
    command's own alias vs. the user's actual value and would otherwise
    swallow the whole sentence. None for tier 1/3/5 matches (the whole
    query WAS the alias, nothing was stripped) -- falls back to the
    existing generic extraction in that case, unchanged.
    """
    text = user_text.strip()

    if intent.startswith("WCL_") and wcl_variables:
        return _extract_wcl_slots(wcl_variables, text, wcl_stripped_value)

    if intent == "SCHEDULE_COMMAND":
        # By the time this is reached, orchestrator.py has ALREADY called
        # find_time_expression() once (to decide this is a scheduling
        # request at all -- see its own docstring for why that check runs
        # before classification). Re-running it here, on the same text,
        # gets the same (delay, matched_span, remainder) deterministically
        # -- cheap, and keeps this function's contract identical to every
        # other intent's (pure function of user_text, no hidden state).
        found = find_time_expression(text)
        if not found:
            return None
        delay_seconds, _span, remainder = found
        remainder = re.sub(r"^(please\s+|can you\s+|could you\s+)", "", remainder, flags=re.IGNORECASE).strip()
        if not remainder:
            # A bare time expression with nothing to schedule ("in 10
            # minutes" alone) isn't a usable command -- ask, don't guess.
            return None
        return {"command_text": remainder, "delay_seconds": str(delay_seconds)}

    if intent == "SET_TIMER":
        # orchestrator.py only routes here after looks_like_bare_timer()
        # already confirmed this text matches -- re-run it for the same
        # reason SCHEDULE_COMMAND above re-runs find_time_expression(),
        # keeping this function a pure re-derivation of user_text with no
        # hidden state, same contract as every other branch here.
        found = looks_like_bare_timer(text)
        if not found:
            return None
        delay_seconds, label = found
        return {"delay_seconds": str(delay_seconds), "label": label}

    if intent == "CANCEL_SCHEDULED":
        # Accepts an explicit ID ("cancel S2") or a free-text description
        # ("cancel the shutdown") -- scheduler.py's own cancel() does the
        # actual ID-vs-substring matching; this just isolates whichever
        # the user typed after the trigger word.
        m = re.search(
            r"(?:cancel|stop|remove)\s+(?:the\s+)?(?:scheduled\s+)?(.+)$",
            text, re.IGNORECASE,
        )
        if not m:
            return None
        ref = m.group(1).strip().strip("'\"")
        ref = re.sub(r"^(command|task|reminder)\s+", "", ref, flags=re.IGNORECASE).strip()
        return {"ref": ref} if ref else None

    if intent == "CONDITIONAL_COMMAND":
        # Deliberately never resolved here -- see looks_conditional()'s
        # module docstring. Detecting the shape is enough to route to this
        # intent; the ACTUAL condition/action always comes from the
        # user's plain-language answer to MISSING_SLOT_QUESTIONS, handled
        # in resolve_missing_slot() below, never guessed from the original
        # "if..." phrasing itself.
        return None

    if intent == "START_SEEING":
        # No slots needed at all -- the trigger phrase itself is the
        # whole instruction (see extractor.py's looks_like_start_seeing()
        # pre-check, which is what routes here in the first place).
        return {}

    if intent == "STOP_SEEING":
        # Same "deliberately never resolved from the trigger text itself"
        # pattern as CONDITIONAL_COMMAND just above -- there's nothing in
        # "stop seeing"/"that's it, save this" that could plausibly BE the
        # macro's name. Always falls through to MISSING_SLOT_QUESTIONS +
        # resolve_missing_slot().
        return None

    if intent == "CONVERT_SELECTED_FILE":
        fmt = _extract_target_format(text)
        # Unlike RESIZE/COMPRESS below (which have a sensible default when
        # no number is given), there's no sensible default TARGET FORMAT --
        # returning None here (not an empty dict) routes through the normal
        # MISSING_SLOT_QUESTIONS ask-instead-of-guess path, same as
        # MAKE_FOLDER's missing name.
        return {"target_format": fmt} if fmt else None

    if intent == "RESIZE_SELECTED_FILE":
        return _extract_resize_params(text)

    if intent == "COMPRESS_SELECTED_FILE":
        return {"quality": _extract_compress_quality(text)}

    if intent == "EXTRACT_SELECTED_FILE":
        return {}

    if intent == "DOWNLOAD_VIDEO_URL":
        url = _extract_url(text)
        # No sensible default here (unlike RESIZE's shrink-by-50%) -- a
        # download with no link at all has nothing to act on, so this
        # routes through MISSING_SLOT_QUESTIONS same as CONVERT_SELECTED_FILE's
        # missing target_format does.
        if not url:
            return None
        return {"url": url, "audio_only": _extract_audio_only(text)}

    if intent == "DOWNLOAD_PLAYING_VIDEO":
        # No required slot -- "download this" / "grab what I'm watching"
        # is a complete instruction on its own; VideoDownloadAPI resolves
        # the actual URL itself via now_playing.get_now_playing_url() at
        # dispatch time, same "resolve against current state, not a stale
        # slot" pattern FileConvertAPI already uses for selection_context.
        return {"audio_only": _extract_audio_only(text)}

    if intent in ("MAKE_FOLDER", "MAKE_FILE"):
        name = _extract_name(text)
        if not name:
            return None
        if intent == "MAKE_FILE" and "." not in ntpath.basename(name):
            name += ".txt"  # sensible default extension for a bare file name
        path = resolve_path(name, _default_root_for(text))
        return {"path": path} if path else None

    if intent in ("DELETE_ITEM", "READ_FILE", "OPEN_ITEM", "LIST_FILES", "SORT_FOLDER_BY_TYPE"):
        # BETA 0.3.31 fix (closes the long-standing known-open issue --
        # see tests/test_extractor.py::TestKnownOpenIssues, previously
        # xfail): _extract_name() covers quoted/called/named-triggered
        # names and is trusted as-is (a quote is an explicit, deliberate
        # override of any heuristic -- see _looks_like_real_name's own
        # docstring). _extract_bare_path() is a last-resort GUESS with no
        # anchor, though, and its relative-filename branch's regex has
        # nothing to stop it from swallowing a leading verb phrase too
        # (confirmed repro: "delete the file version 2.0 from my desktop"
        # -> "delete the file version 2.0", not just "version 2.0"). The
        # _looks_like_real_name() plausibility guard already exists in
        # this file for exactly this shape of problem (built for
        # resolve_missing_slot()'s answer-validation and
        # extract_open_target_name()) but was never wired in here --
        # so a bare-path guess this bad still got silently dispatched as
        # a real path (and Get-Item/Remove-Item would fail confusingly,
        # or worse, silently no-op, on the wrong target) instead of
        # falling through to ask the user, same as every other guess-vs-
        # ask decision elsewhere in this codebase. Deliberately does NOT
        # try to correctly parse "version 2.0" out of the sentence --
        # that needs real language understanding this file doesn't have;
        # asking instead of guessing wrong is the same "ties go to
        # rejecting" posture as the plausibility guard's own docstring.
        name = _extract_name(text)
        if not name:
            bare = _extract_bare_path(text)
            name = bare if bare and _looks_like_real_name(bare) else None
        if not name:
            # LIST_FILES/SORT_FOLDER_BY_TYPE with no name given → operate on
            # the default root itself (e.g. "sort my desktop by type" names
            # no explicit folder, but _default_root_for already resolves
            # "desktop" mentions -- same bare-target fallback LIST_FILES uses).
            if intent in ("LIST_FILES", "SORT_FOLDER_BY_TYPE"):
                return {"path": _default_root_for(text)}
            return None
        path = resolve_path(name, _default_root_for(text))
        return {"path": path} if path else None

    if intent == "ORGANIZE_FILES_BY_TOPIC":
        # Same "no explicit name -> operate on the default root" fallback
        # LIST_FILES/SORT_FOLDER_BY_TYPE use just above -- "organize my
        # desktop"/"organize this" both need to resolve to a real path
        # without the user having to spell one out. Unlike those two
        # intents, though, this ALWAYS returns a usable dict (never None) --
        # there's no required slot here that could plausibly be missing,
        # `path` always has a default and `include_suggestions` is a pure
        # opt-in flag, not something worth pausing the turn to ask about.
        name = _extract_name(text)
        if not name:
            bare = _extract_bare_path(text)
            name = bare if bare and _looks_like_real_name(bare) else None
        path = resolve_path(name, _default_root_for(text)) if name else _default_root_for(text)
        include_suggestions = "true" if _ORGANIZE_INCLUDE_SUGGESTIONS_RE.search(text) else ""
        return {"path": path or _default_root_for(text), "include_suggestions": include_suggestions}

    if intent == "GROUP_FILES_BY_EXTENSION":
        # Deliberately the OPPOSITE extraction posture from
        # ORGANIZE_FILES_BY_TOPIC just above: this intent is a fully
        # explicit instruction ("put the pdfs and json files in a folder
        # named rezero") with no evidence-based inference at all, so BOTH
        # slots here are required and a miss on either returns None (ask
        # the user) rather than guessing a folder name or a file type from
        # thin air -- same never-guess posture as the rest of this file.
        dest_m = _GROUP_DEST_FOLDER_RE.search(text)
        dest_name = dest_m.group(1).strip() if dest_m else None
        if not dest_name or not _looks_like_real_name(dest_name):
            return None
        extensions = _extract_extensions(text)
        if not extensions:
            return None
        return {
            "path": _default_root_for(text),
            "extensions": ",".join(extensions),
            "dest_name": dest_name,
        }

    if intent == "RENAME_ITEM":
        m = re.search(r"rename\s+(.+?)\s+to\s+(.+)$", text, re.IGNORECASE)
        if not m:
            return None
        path = resolve_path(m.group(1).strip().strip("'\""), _default_root_for(text))
        new_name = m.group(2).strip().strip("'\"")
        return {"path": path, "new_name": new_name} if path and new_name else None

    if intent in ("MOVE_ITEM", "COPY_ITEM"):
        verb = "move" if intent == "MOVE_ITEM" else "copy"
        m = re.search(rf"{verb}\s+(.+?)\s+to\s+(.+)$", text, re.IGNORECASE)
        if not m:
            return None
        src  = resolve_path(m.group(1).strip().strip("'\""), _default_root_for(text))
        dest = resolve_path(m.group(2).strip().strip("'\""), _default_root_for(text))
        return {"path": src, "dest": dest} if src and dest else None

    if intent == "FIND_FILES":
        name = _extract_name(text) or _extract_bare_path(text)
        if not name:
            return None
        return {"root": _default_root_for(text), "query": name}

    if intent == "KILL_PROCESS":
        # (?:process\s+)? added after the trigger -- without it, "stop
        # process notepad.exe" left the literal word "process" glued onto
        # the captured name ("process notepad.exe"), since "process" isn't
        # one of this intent's OWN trigger words (kill/stop/close/end) so
        # nothing was stripping it. Confirmed directly, same class of bug
        # fixed below for FIND_PROCESS/WAIT_FOR_PROCESS/FIND_SERVICE.
        m = re.search(r"(?:kill|stop|close|end)\s+(?:the\s+)?(?:process\s+)?(.+?)(?:\s+process)?$", text, re.IGNORECASE)
        if not m:
            return None
        proc = m.group(1).strip().strip("'\"")
        return {"process": _strip_exe_extension(proc)} if proc else None

    if intent == "GET_WEATHER":
        city = _extract_city(text)
        return {"city": city} if city else {}   # empty city → caller uses cached location

    if intent == "GET_FORECAST":
        city = _extract_city(text)
        days_m = re.search(r"(\d+)\s*day", text, re.IGNORECASE)
        days = days_m.group(1) if days_m else "3"
        return {"city": city or "", "days": days}

    if intent == "SEARCH_WEB":
        # Same filler-stripping approach as LAUNCH_APP below -- the old
        # version only stripped the trigger word if it was the literal
        # first token in the message, so anything like "can you search for
        # X" or "search the web for X" left the filler/trigger words IN the
        # query, and DuckDuckGo (or any search backend) got a garbage query
        # instead of just "X". Strip leading conversational filler first,
        # then the trigger verb and any "the web"/"online"/"for" that
        # follows it, in either order and however many times they stack.
        cleaned = text
        _SEARCH_FILLER_LEAD = re.compile(
            r"^(okay,?\s*|ok,?\s*|sure,?\s*|alright,?\s*)?"
            r"(can|could|would|will)?\s*(you\s+)?"
            r"(please\s+)?"
            r"(i'?ll\s+|i will\s+|i'?d like to\s+|i want you to\s+)?",
            re.IGNORECASE,
        )
        for _ in range(2):
            cleaned = _SEARCH_FILLER_LEAD.sub("", cleaned).strip()
        cleaned = re.sub(
            r"^(search|look up|google|find)\s+(the\s+)?(web|internet|online)?\s*(for me\s+)?(for\s+)?",
            "", cleaned, flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(r"\s+(for me|please|now)\.?$", "", cleaned, flags=re.IGNORECASE).strip()
        query = cleaned.strip("'\" ") or None
        # A bare leftover trigger word/stub ("search", "for", "google") isn't
        # a usable query -- treat it the same as "nothing extracted" so the
        # caller asks the fixed follow-up question instead of searching for
        # a single meaningless word.
        if query and query.lower() in {"search", "for", "google", "look up", "find", "web", "the web", "online"}:
            query = None
        return {"query": query} if query else None

    # ── Batch-1 extended intents (intents_extended.py) ────────────────────

    if intent in ("PATH_EXISTS", "ITEM_PROPERTIES", "RESOLVE_PATH", "SPLIT_PATH"):
        # Strip trailing "exist(s)?" first so it never gets swallowed into a
        # "called X" match, then try the shared name-trigger extractor, then
        # fall back to stripping known leading verb phrases for bare paths.
        cleaned = re.sub(r"\s+exist(s)?\??$", "", text, flags=re.IGNORECASE).strip()
        name = _extract_name(cleaned)
        if not name:
            stripped = re.sub(
                r"^(does|is there|check if)?\s*(the\s+)?(file|folder|path)?\s*|"
                r"^(show|get)?\s*(properties of|the properties of)\s*|"
                r"^(resolve|split)\s*(the\s+path\s+(for|to)?)?\s*",
                "", cleaned, flags=re.IGNORECASE,
            ).strip()
            name = _extract_bare_path(stripped) or (stripped or None)
        if not name:
            return {"path": _default_root_for(text)}
        path = resolve_path(name, _default_root_for(text))
        return {"path": path} if path else None

    if intent in ("COUNT_FILES", "COUNT_FOLDERS", "FILE_TYPE_BREAKDOWN",
                  "FIND_DUPLICATE_FILES", "EXPORT_FOLDER_LISTING_CSV"):
        name = _extract_name(text)
        if not name:
            # These all make sense against the default root with no name given.
            return {"path": _default_root_for(text)}
        path = resolve_path(name, _default_root_for(text))
        return {"path": path} if path else None

    if intent == "FIND_FILES_BY_CONTENT":
        # BUG FOUND AND FIXED: the original pattern only matched 3 exact
        # trigger phrases ("containing X" / "with the text X" / "for the
        # text X"). Tested directly against very ordinary phrasings --
        # "search inside files in downloads for TODO", "search for TODO
        # in my files", "find files that contain TODO", "search files for
        # the word urgent" -- and 4 of these 6 realistic cases returned
        # None, falling through to a generic "I didn't catch that"
        # instead of running the search. Fixed with a broader trigger
        # (search/find/look ... for/containing/contains/with) plus two
        # post-processing cleanups on whatever gets captured:
        #   1. Strip a leading "the word"/"the text"/"the phrase" filler
        #      ("for the word urgent" -> "urgent") -- needed because
        #      trying to bake "for the word" into the TRIGGER regex
        #      itself failed: re.search() always prefers the EARLIEST
        #      starting position in the string, so "search ... for" (which
        #      starts earlier) wins over a more specific "for the word"
        #      alternative starting later, regardless of alternation
        #      order. Confirmed this exact failure mode directly against
        #      "search for the word urgent in documents" before switching
        #      to the post-processing approach.
        #   2. Strip a trailing "in my files"/"in downloads"/"in this
        #      folder" location phrase that would otherwise get captured
        #      as part of the pattern ("TODO in my files" -> "TODO").
        m = re.search(
            r"(?:search(?:ing)?|find|look)\w*\s+.*?\b(?:for|containing|contains?|with)\b\s+(.+?)$",
            text, re.IGNORECASE,
        )
        pattern = m.group(1).strip().strip("'\"") if m else None
        if pattern:
            pattern = re.sub(r"^(the\s+)?(word|text|phrase)\s+", "", pattern, flags=re.IGNORECASE).strip()
            for _ in range(2):
                stripped = re.sub(
                    r"\s+in\s+(my\s+)?(files|file|the\s+files|downloads|documents|this\s+folder|that\s+folder)\.?$",
                    "", pattern, flags=re.IGNORECASE,
                ).strip()
                if stripped == pattern:
                    break
                pattern = stripped
        if not pattern:
            return None
        return {"path": _default_root_for(text), "pattern": pattern}

    if intent == "SET_CLIPBOARD":
        m = _QUOTED_RE.search(text)
        if m:
            return {"value": m.group(1).strip()}
        m = re.search(r"(?:copy|set clipboard to)\s+(.+)$", text, re.IGNORECASE)
        value = m.group(1).strip() if m else None
        return {"value": value} if value else None

    if intent in ("WAIT_FOR_PROCESS", "FIND_PROCESS"):
        # Confirmed live: "find process explorer.exe" was captured as
        # "process explorer.exe" -- the trigger alternation only ever
        # consumes ONE of (wait for/process/find/check), so when the
        # message uses two of them back to back ("find" + the leftover
        # noun "process"), the second one was never stripped and ended up
        # glued onto the actual process name. The added (?:process\s+)?
        # absorbs that second occurrence when present; harmless no-op when
        # it isn't.
        m = re.search(r"(?:wait for|process|find|check)\s+(?:the\s+)?(?:process\s+)?(.+?)(?:\s+process)?$", text, re.IGNORECASE)
        proc = m.group(1).strip().strip("'\"") if m else None
        return {"process_name": _strip_exe_extension(proc)} if proc else None

    if intent == "TOP_PROCESSES_BY_CPU":
        m = re.search(r"\b(\d+)\b", text)
        count = m.group(1) if m else "10"
        return {"count": count}

    if intent == "FIND_SERVICE":
        # Same fix as WAIT_FOR_PROCESS/FIND_PROCESS above: "check service
        # printer" was captured as "service printer" because the trigger
        # alternation only consumes one of (service/check), leaving a
        # second occurrence of "service" glued onto the name.
        m = re.search(r"(?:service|check)\s+(?:the\s+)?(?:service\s+)?(.+?)(?:\s+service)?$", text, re.IGNORECASE)
        svc = m.group(1).strip().strip("'\"") if m else None
        return {"service_name": svc} if svc else None

    # ── APP_CONTROL intents (intents_app_control.py) ───────────────────────
    # Same never-guess rule as everywhere else: these only ever hand back a
    # DESCRIPTION string pulled from the user's own words. Deciding which
    # real on-screen element that description matches, and its coordinates,
    # is app_control.py's job at execution time -- never this function's,
    # and never the model's.

    if intent == "LAUNCH_APP":
        # NEW: 'AppName' in single quotes is an explicit, unambiguous app
        # name -- use it verbatim, skip the heuristic below entirely. This
        # is the app-name half of the "" vs '' convention (see
        # _DOUBLE_QUOTED_RE / _SINGLE_QUOTED_RE above).
        m = _SINGLE_QUOTED_RE.search(text)
        if m:
            name = m.group(1).strip()
            return {"app_name": name} if name else None

        name = extract_open_target_name(text)
        return {"app_name": name} if name else None

    if intent in ("CLICK_ELEMENT", "DOUBLE_CLICK_ELEMENT", "RIGHT_CLICK_ELEMENT"):
        m = re.search(
            r"(?:double[\s-]?click|right[\s-]?click|click)\s+(?:on\s+)?(?:the\s+)?(.+?)$",
            text, re.IGNORECASE,
        )
        target = m.group(1).strip().strip("'\"") if m else None
        return {"target_description": target} if target else None

    if intent == "TYPE_INTO_ELEMENT":
        # e.g. "type 'hello world' into the search box"
        m = re.search(
            r"type\s+(.+?)\s+(?:in(?:to)?)\s+(?:the\s+)?(.+?)$",
            text, re.IGNORECASE,
        )
        if not m:
            return None
        typed_text = m.group(1).strip().strip("'\"")
        target = m.group(2).strip().strip("'\"")
        if not typed_text or not target:
            return None
        return {"text": typed_text, "target_description": target}

    if intent == "START_LISTENING":
        # target_description is OPTIONAL (see intents_app_control.py's
        # entry) -- always returns a dict, never None, so this can never
        # trigger MISSING_SLOT_QUESTIONS's forced follow-up. An empty
        # target just means start_dictation() falls back to its own
        # focused-element/click-to-resolve logic instead of a named one.
        m = re.search(
            r"\b(?:start|begin)\s+(?:listening|dictating|dictation)\b\s*"
            r"(?:in(?:to)?|on)\s+(?:the\s+)?(.+?)$",
            text, re.IGNORECASE,
        )
        target = m.group(1).strip().strip("'\"") if m else ""
        return {"target_description": target}

    # No slots needed for this intent.
    return {}


# ─── Missing-slot follow-up questions ──────────────────────────────────────
#
# When extract_slots() returns None above, the caller (orchestrator.py) does
# NOT let the model guess or invent a value -- ever, for any intent. It asks
# a fixed, Python-authored question instead and waits for the user's next
# message. This is the one rule that survived every alternative design
# discussed for this app: the model can pick which action to take (that's
# schema-constrained and safe), but it never gets to supply an open-ended
# string value that Python then has to trust. A human typing "Homework" in
# direct response to "what should I name the folder?" is a completely
# different, far more reliable signal than a model regenerating that name
# from a paraphrase.
MISSING_SLOT_QUESTIONS: Dict[str, str] = {
    "SCHEDULE_COMMAND": "What should I do, and when? (e.g. 'shut down in 10 minutes')",
    "SET_TIMER": "How long should the timer be? (e.g. 'set a timer for 10 minutes')",
    "CANCEL_SCHEDULED": "Which scheduled command should I cancel? (give me its ID, like S1, or describe it)",
    "CONDITIONAL_COMMAND": "What exact condition should I check, and what should I do if it's true? (e.g. 'if wifi is off, turn it on')",
    "MAKE_FOLDER": "What should I name the folder?",
    "MAKE_FILE": "What should I name the file?",
    "GROUP_FILES_BY_EXTENSION": "Which file types, and what should I name the new folder? (e.g. 'put the pdfs and json files in a folder named rezero')",
    "DELETE_ITEM": "Which file or folder should I delete? (just give me the name)",
    "READ_FILE": "Which file should I read?",
    "OPEN_ITEM": "Which file or folder should I open?",
    "FIND_FILES": "What should I search for?",
    "KILL_PROCESS": "Which process should I stop? (give me its name)",
    "SEARCH_WEB": "What should I search the web for?",
    "SET_CLIPBOARD": "What should I copy to the clipboard?",
    "RENAME_ITEM": "Which item, and what should the new name be? (e.g. 'notes.txt to notes-old.txt')",
    "MOVE_ITEM": "Which item, and where should it go? (e.g. 'notes.txt to D:\\')",
    "COPY_ITEM": "Which item, and where should the copy go?",
    "FIND_FILES_BY_CONTENT": "What text should I search for inside files?",
    "WAIT_FOR_PROCESS": "Which process should I wait for?",
    "FIND_PROCESS": "Which process should I look for?",
    "FIND_SERVICE": "Which service should I check?",
    "LAUNCH_APP": "Which app should I open?",
    "CLICK_ELEMENT": "What should I click?",
    "DOUBLE_CLICK_ELEMENT": "What should I double-click?",
    "RIGHT_CLICK_ELEMENT": "What should I right-click?",
    "TYPE_INTO_ELEMENT": "What should I type, and into which field? (e.g. 'hello into the search box')",
    "STOP_SEEING": "What should I call this macro? (one word is easiest to trigger later)",
    "CONVERT_SELECTED_FILE": "What format should I convert it to? (e.g. text, pdf, json, jpg)",
    "DOWNLOAD_VIDEO_URL": "What's the link to the video you want downloaded?",
    "GENERATE_FILE": "What should I name it? (or say 'skip' and I'll pick a name)",
}


# Answers to GENERATE_FILE's "what should I name it?" question that mean
# "I don't want to give one, just pick something" -- checked in
# orchestrator.py's _resume_pending() BEFORE _strip_answer_filler/
# generator.extract_explicit_name() get anywhere near the raw answer, so
# "skip" itself never gets mistaken for a literal requested filename.
GENERATE_FILE_SKIP_NAME_ANSWERS = {
    "skip", "no", "nope", "nah", "whatever", "any", "any name",
    "you pick", "you choose", "doesn't matter", "does not matter",
    "i don't care", "i dont care", "no name", "none", "nothing",
}


_ANSWER_FILLER_RE = re.compile(
    r"^(call it|name it|it's|its|the name is|make it|use)\s+|"
    r"\s+(please|thanks|thank you)\.?$",
    re.IGNORECASE,
)


def _strip_answer_filler(answer: str) -> str:
    """
    Narrow, fixed stripping for direct follow-up answers -- e.g. "call it
    Homework" -> "Homework", "Homework please" -> "Homework". Deliberately
    NOT fuzzy/inferential: this only removes a small fixed set of extremely
    common lead-in/trail-off phrases people naturally use when answering a
    direct question like "What should I name the folder?". It does not
    attempt to parse arbitrary phrasing, so it can't reintroduce the kind of
    ambiguity extract_slots() already failed to resolve on the original
    message -- see resolve_missing_slot()'s docstring for why re-running
    fuzzy regex here would be the wrong fix.

    Bug this fixes: without this, an answer of "call it Homework" was being
    used AS the folder name verbatim (producing a folder literally named
    "call it Homework"), because the previous version took the raw answer
    completely at face value with zero stripping, not just non-fuzzy
    stripping.
    """
    stripped = _ANSWER_FILLER_RE.sub("", answer).strip()
    return stripped or answer  # never return empty -- fall back to the original


def resolve_missing_slot(intent: str, original_text: str, answer: str, wcl_variables: Optional[List[str]] = None) -> Optional[Dict[str, str]]:
    """
    Fills the slot(s) extract_slots() couldn't get from the ORIGINAL message,
    using the user's direct answer to the fixed follow-up question above.

    Applies _strip_answer_filler() once (narrow, fixed lead-in/trail-off
    phrases only), then otherwise does NOT re-run fuzzy regex/inference on
    the answer -- the user just stated the value plainly in response to an
    explicit question, so beyond that light stripping it's taken as the
    value directly (still passed through the same sandbox path validation as
    every other path in this app). This is simpler and more reliable than
    trying to stitch the answer back into the original sentence and
    re-parsing the combination, which just reintroduces the same ambiguity
    extract_slots() already failed to resolve once.

    wcl_variables: same meaning as extract_slots()'s parameter -- only
    passed for a "WCL_..." intent whose original turn produced a missing-
    slot question (single-variable, danger_level=="safe" commands only,
    see _extract_wcl_slots()). Without this branch, a WCL follow-up answer
    would fall through to this function's final `return None` forever and
    the pending question could never actually resolve.

    Returns None if the answer still doesn't give us what we need (e.g. an
    empty reply, or a two-part answer like "X to Y" missing the "to") -- the
    caller re-asks rather than guessing.
    """
    answer = answer.strip().strip("'\"")
    if not answer:
        return None
    answer = _strip_answer_filler(answer)

    if intent == "CONVERT_SELECTED_FILE":
        # Try the same closed-vocabulary format matcher against the bare
        # answer (e.g. user just replies "text" or "pdf" to "what format?").
        fmt = _extract_target_format(f"to {answer}") or _KNOWN_FORMAT_WORDS.get(answer.lower())
        return {"target_format": fmt} if fmt else None

    if intent == "DOWNLOAD_VIDEO_URL":
        # The answer to "what's the link?" is usually the bare URL itself,
        # with no surrounding sentence for _extract_url's regex to anchor
        # on quite as reliably -- so also accept a bare domain/path with no
        # scheme (e.g. "youtube.com/watch?v=...") and add one back, same
        # forgiving-but-explicit posture now_playing.py's address-bar reader
        # already applies to a scheme-less browser value.
        url = _extract_url(answer)
        if url:
            return {"url": url}
        if re.match(r"^[\w.-]+\.[a-z]{2,}(?:/\S*)?$", answer, re.IGNORECASE):
            return {"url": "https://" + answer}
        return None

    if intent == "SCHEDULE_COMMAND":
        # Original message had a time expression but nothing to schedule
        # (e.g. bare "in 10 minutes") -- the answer IS the command text,
        # re-run find_time_expression on it too in case the user restates
        # the time here instead ("shut down in 10 minutes" as the reply).
        found = find_time_expression(answer)
        if found:
            delay_seconds, _span, remainder = found
            if remainder:
                return {"command_text": remainder, "delay_seconds": str(delay_seconds)}
            return None
        # No time in the answer either -- caller already has the original
        # delay from the first turn's extraction; this path only reaches
        # here if that ALSO failed, which _process_single_request's
        # pre-check guarantees didn't happen (see orchestrator.py). Kept
        # as a safe None rather than assuming that invariant forever.
        return None

    if intent == "CANCEL_SCHEDULED":
        return {"ref": answer} if answer else None

    if intent == "CONDITIONAL_COMMAND":
        # This IS the resolution point for conditionals -- per product
        # decision, TOKI never guesses the condition/action split itself.
        # The user's plain-language answer to "what exact condition... and
        # what should I do" is taken as the full description of both parts
        # verbatim (condition_and_action), same "trust the direct answer"
        # principle as every other resolve_missing_slot branch. This is
        # NOT evaluated as a live condition anywhere yet -- see
        # intents.py's CONDITIONAL_COMMAND entry and STATUS.md for why
        # that's a stated, explicit scope boundary (no general condition
        # evaluator exists in this codebase) rather than a bug: TOKI
        # acknowledges the request and states plainly it can't yet
        # monitor a condition in the background, instead of silently
        # pretending to.
        return {"condition_and_action": answer} if answer else None

    if intent == "STOP_SEEING":
        # The answer IS the macro name, taken verbatim (after the same
        # filler-stripping every other free-text slot already gets above)
        # -- macro_recorder.py's _safe_macro_filename() does its own
        # sanitizing for the actual filename, so this doesn't need to
        # validate shape here, just confirm there's something to name.
        return {"macro_name": answer} if answer else None

    if intent.startswith("WCL_") and wcl_variables and len(wcl_variables) == 1:
        var_name = wcl_variables[0]
        # BETA 0.3.37: same hard block as _extract_wcl_slots() -- without
        # this, a code-like variable that extract_slots() correctly
        # refused to fill on the FIRST message would still get filled
        # here from the user's ANSWER to the resulting missing-slot
        # question, completely bypassing the block via a different entry
        # point into the exact same underlying risk.
        if _is_wcl_code_like_var(var_name):
            return None
        if var_name in _WCL_PATH_VAR_NAMES:
            path = resolve_path(answer, _default_root_for(original_text))
            return {var_name: path} if path else None
        if not _looks_like_real_name(answer):
            return None
        return {var_name: answer}

    # Guard against a reply that's actually a whole new sentence/instruction
    # ("now delete the folder you just made") rather than a bare name --
    # see _looks_like_real_name()'s docstring. Explicit "" quoting still
    # bypasses this entirely (checked first, above _strip_answer_filler in
    # spirit -- an explicitly quoted answer is trusted verbatim either way
    # since strip("'\"") already unwrapped it before we get here... but a
    # quoted answer that's ALSO implausible is still worth rejecting, so
    # the check runs unconditionally on every path/name slot below, not
    # just the un-quoted case).
    if intent in ("MAKE_FOLDER", "MAKE_FILE", "DELETE_ITEM", "READ_FILE", "OPEN_ITEM") \
            and not _looks_like_real_name(answer):
        return None

    if intent in ("MAKE_FOLDER", "MAKE_FILE"):
        name = answer
        if intent == "MAKE_FILE" and "." not in ntpath.basename(name):
            name += ".txt"
        path = resolve_path(name, _default_root_for(original_text))
        return {"path": path} if path else None

    if intent in ("DELETE_ITEM", "READ_FILE", "OPEN_ITEM"):
        path = resolve_path(answer, _default_root_for(original_text))
        return {"path": path} if path else None

    if intent == "FIND_FILES":
        return {"root": _default_root_for(original_text), "query": answer}

    if intent == "KILL_PROCESS":
        return {"process": _strip_exe_extension(answer)}

    if intent == "SEARCH_WEB":
        return {"query": answer}

    if intent == "SET_CLIPBOARD":
        return {"value": answer}

    if intent in ("WAIT_FOR_PROCESS", "FIND_PROCESS"):
        return {"process_name": _strip_exe_extension(answer)}

    if intent == "FIND_SERVICE":
        return {"service_name": answer}

    if intent == "FIND_FILES_BY_CONTENT":
        return {"path": _default_root_for(original_text), "pattern": answer}

    if intent in ("RENAME_ITEM", "MOVE_ITEM", "COPY_ITEM"):
        m = re.search(r"(.+?)\s+to\s+(.+)$", answer, re.IGNORECASE)
        if not m:
            return None
        first = m.group(1).strip().strip("'\"")
        second = m.group(2).strip().strip("'\"")
        if intent == "RENAME_ITEM":
            path = resolve_path(first, _default_root_for(original_text))
            return {"path": path, "new_name": second} if path and second else None
        src = resolve_path(first, _default_root_for(original_text))
        dest = resolve_path(second, _default_root_for(original_text))
        return {"path": src, "dest": dest} if src and dest else None

    if intent == "LAUNCH_APP":
        return {"app_name": answer}

    if intent in ("CLICK_ELEMENT", "DOUBLE_CLICK_ELEMENT", "RIGHT_CLICK_ELEMENT"):
        return {"target_description": answer}

    if intent == "TYPE_INTO_ELEMENT":
        m = re.search(r"(.+?)\s+(?:in(?:to)?)\s+(?:the\s+)?(.+?)$", answer, re.IGNORECASE)
        if not m:
            return None
        typed_text = m.group(1).strip().strip("'\"")
        target = m.group(2).strip().strip("'\"")
        return {"text": typed_text, "target_description": target} if typed_text and target else None

    return None


# ─── Canned greeting/closing replies (latency fix, not a new feature) ───────
#
# Real, measured problem: on a CPU-only Ollama box, EVERY CHAT/ASK_CONTEXT
# turn currently pays a full second LLM call (_run_thinking) just to
# reword a fixed instruction into one sentence -- even for "hey", "hi",
# "thanks" -- confirmed directly against a live run: these each cost
# 20-30+ seconds of prompt_eval, on top of the classify() call that
# already ran to route them to CHAT in the first place. Measured the
# system prompt itself (_build_thinking_system_prompt): ~500-700 tokens,
# fully re-evaluated from scratch on every single call since Ollama's
# /api/chat has no cross-call KV-cache reuse via this integration --
# that prompt size, not a cold model reload (keep_alive=30m already
# covers that, confirmed separately), is what CPU-only prompt_eval time
# actually measures here.
#
# Fix, scoped exactly to what's actually deterministic: a small, explicit
# set of PURE greetings and closings gets a hand-written canned reply,
# skipping BOTH the classify() call AND the thinking call entirely for
# just these exact phrasings. This is deliberately NOT a general CHAT
# shortcut -- test_graph_router.py's own TestChatNeverGraphHits documents
# why a broad "CHAT hit skips the LLM" behavior is unsafe (CHAT needs to
# stay genuinly open-ended, see that test's comment). The set here is
# narrow enough that every match is unambiguous: no content beyond a
# greeting/closing word ever matches (see negative-case tests), so
# nothing that could plausibly need real generation is ever intercepted.
#
# Placement: checked BEFORE the scheduling/conditional pre-checks in
# orchestrator.py's _process_single_request, same reasoning as those --
# runs first because there's nothing upstream that could need to see
# this text before a canned reply is the right, fully deterministic
# answer.

_GREETING_RE = re.compile(
    r"^\s*(hey|hi|hello|yo|sup|howdy)\s*[!.]?\s*"
    r"(how'?s?\s+it\s+going|how\s+are\s+you|what'?s\s+up)?\s*[!.?]?\s*$",
    re.IGNORECASE,
)
_CLOSING_RE = re.compile(
    r"^\s*(thanks?|thank\s+you|thx|ok(ay)?|cool|got\s+it|sounds?\s+good|"
    r"that'?s\s+all|bye|goodbye|see\s+ya)"
    r"[,.]?\s*(that'?s\s+all(\s+for\s+now)?|for\s+now|for\s+the\s+help)?\s*[!.]?\s*$",
    re.IGNORECASE,
)

_GREETING_REPLIES = [
    "Hey! What can I help you with?",
    "Hi there — what do you need?",
    "Hey, doing well — what can I do for you?",
]
_CLOSING_REPLIES = [
    "You got it — let me know if you need anything else.",
    "Anytime! I'm here if you need me.",
    "Sounds good — I'll be here.",
]


def canned_reply(text: str) -> Optional[str]:
    """Returns a hand-written reply for a PURE greeting/closing message, or
    None if text isn't one -- callers must treat None as 'run the real
    pipeline', never as a failure. Deliberately deterministic (a stable
    pick per exact input, not random) so repeated identical testing/CI
    runs get identical output -- picks by a simple hash of the text
    rather than random.choice, which would make this non-reproducible in
    tests for no real benefit (the person typing "hey" twice in a row
    isn't harmed by seeing the same reply twice)."""
    stripped = text.strip()
    if not stripped:
        return None
    idx = sum(ord(c) for c in stripped.lower()) 
    if _GREETING_RE.match(stripped):
        return _GREETING_REPLIES[idx % len(_GREETING_REPLIES)]
    if _CLOSING_RE.match(stripped):
        return _CLOSING_REPLIES[idx % len(_CLOSING_REPLIES)]
    return None
