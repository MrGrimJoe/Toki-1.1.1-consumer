"""
conversation_memory.py -- remembers the KEYWORDS (not full text) of the
last 10 turns, so TOKI can tell what topic a short, ambiguous later
message is probably still about, even after it's fallen out of the small
history window `orchestrator.py`'s `self.history` keeps for Ollama's
own classify()/stream_thinking() prompts.

── Why this is a SEPARATE thing from self.history and _last_touched ───────
This project already has two other "remember something across turns"
mechanisms, and this is deliberately not a replacement for either:

- `orchestrator.py`'s `self.history` (capped at 2 exchanges / 4 entries):
  full verbatim text, fed directly into Ollama's chat messages so it can
  resolve something like "stop it" referring to the IMMEDIATELY
  preceding turn. Small on purpose -- see `_commit_history`'s own
  docstring -- because it's real prompt-processing cost on every single
  tier-2 call, not just a data structure.
- `extractor.py`'s `resolve_anaphoric_target()` + `orchestrator.py`'s
  `_last_touched`: ONE slot, path-only, only for a small fixed set of
  intents (`ANAPHORA_ELIGIBLE_INTENTS`) -- "delete it" resolving to the
  file TOKI itself most recently touched.

Neither covers "I was discussing something with TOKI several turns ago,
then said something short/ambiguous that doesn't itself carry enough
information -- TOKI should still know roughly what I meant", which is a
broader, cross-turn TOPIC memory, not a single-slot path memory or a
short LLM prompt window. This module is exactly that: up to 10 turns of
extracted KEYWORDS (not full sentences -- cheap to keep around, cheap to
scan, no prompt-processing cost of its own), used ONLY to give
`OllamaRouter.classify()` a longer, still-cheap memory when Tier A's
graph completely misses (see orchestrator.py's call site for exactly
where and why) -- never used to silently change or auto-resolve a
routing decision on its own. If this module malfunctioned or produced
nothing useful, worst case is the fallback behaves exactly as it did
before this existed.

── The FIFO window ─────────────────────────────────────────────────────────
Turns 1-10 fill the window normally. When turn 11 arrives, turn 1 is
evicted from the ACTIVE window (a `collections.deque(maxlen=10)` does
this automatically) -- but every turn, including evicted ones, is ALSO
appended to a persistent JSONL log (`conversation_memory.jsonl`, same
append-only, one-record-per-line, easy-to-grep-or-diff convention as
`vocab_staging.py`'s own STAGING_PATH) so nothing is ever actually lost,
even after it drops out of the active 10-turn window -- exactly the
"but everything gets recorded to the log so i can fix anything broken"
requirement this was built from.

── Format ───────────────────────────────────────────────────────────────────
Record shape (one per line, append-only):
    {
        "turn": 47,                          # monotonic, never resets or
                                                # reuses a number even as
                                                # old turns evict from the
                                                # active window
        "timestamp": "<ISO8601>",
        "user_prompt": "the original message",
        "keywords": ["sales_report.xlsx", "delete", "folder"],
        "intent": "DELETE_ITEM" | null        # whatever orchestrator.py
                                                # resolved this turn to,
                                                # for context only
    }
"""

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from graph_router import normalize, content_words

LOG_PATH = Path(__file__).parent / "conversation_memory.jsonl"

_WINDOW_SIZE = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConversationMemory:
    """One instance per WindowsAIAssistant session (see orchestrator.py's
    __init__) -- not a module-level singleton like vocab_staging.py's
    functions, since (unlike the vocab-learning staging DB, which is
    genuinely process-wide/cross-session data meant for later manual
    review) this is explicitly per-conversation working memory: a second
    WindowsAIAssistant instance in the same process (e.g. in tests)
    should not see another instance's recent turns."""

    def __init__(self, log_path: Path = LOG_PATH):
        self._window: deque = deque(maxlen=_WINDOW_SIZE)
        self._turn_counter: int = 0
        self._log_path = log_path

    def record(self, user_prompt: str, intent: Optional[str]) -> None:
        """Called once per turn (see orchestrator.py's _commit_history,
        the one existing choke point every successful turn already runs
        through -- wiring in here means every call site that already
        calls _commit_history gets this for free, no per-call-site
        changes needed). Never raises -- a failure to extract keywords or
        write the log must never break the actual turn it's describing.
        """
        self._turn_counter += 1
        try:
            keywords = sorted(content_words(normalize(user_prompt)))
        except Exception:
            keywords = []

        record = {
            "turn": self._turn_counter,
            "timestamp": _now(),
            "user_prompt": user_prompt,
            "keywords": keywords,
            "intent": intent,
        }

        self._window.append(record)

        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            # Logging is diagnostic, never load-bearing for the turn that
            # triggered it -- same posture as OllamaRouter._log_timing().
            pass

    def get_recent_topic_context(self, max_turns: int = _WINDOW_SIZE) -> Optional[str]:
        """A short, plain-text summary of recent keywords for
        OllamaRouter.classify()'s extra_context parameter -- see that
        method's docstring for exactly how this gets used (as an
        additional system-role note, ONLY consulted on a total Tier A
        miss, never load-bearing for anything else). Returns None if
        there's nothing recorded yet (a fresh session, or every recent
        turn produced zero extractable keywords -- e.g. pure filler like
        "thanks"/"ok"), so callers can skip adding an empty/useless note.

        Deliberately most-recent-first and deduplicated (a word repeated
        across several turns is more likely to be the actual ongoing
        topic, but should still only appear once in the summary) --
        capped at a small max_turns/max_keyword count on top of the
        window's own 10-turn cap so this stays a short, cheap addition to
        the prompt rather than growing unboundedly.
        """
        if not self._window:
            return None

        seen = set()
        ordered_keywords: List[str] = []
        # most-recent-first: iterate the deque in reverse
        for entry in list(self._window)[-max_turns:][::-1]:
            for kw in entry["keywords"]:
                if kw not in seen:
                    seen.add(kw)
                    ordered_keywords.append(kw)

        if not ordered_keywords:
            return None

        # Capped independently of max_turns -- a handful of dense recent
        # turns could otherwise still produce a long list.
        ordered_keywords = ordered_keywords[:25]
        return (
            "Recent conversation topics, most recent first (for context "
            "only -- may not be relevant to the current message): "
            + ", ".join(ordered_keywords)
        )

    def get_window(self) -> List[Dict[str, Any]]:
        """The active (up to 10-turn) window, oldest first -- for
        callers/tests that need the raw records rather than the
        summarized string, and for anything that wants to inspect what's
        currently "in view" without reaching into the private deque
        directly."""
        return list(self._window)

    def find_turns_matching(self, words: set) -> List[Dict[str, Any]]:
        """Every turn currently in the active window whose keyword set
        intersects `words`, most-recent-first. Used by the low-confidence
        graph-candidate path (see orchestrator.py's classify_or_ask
        handling) to check whether an "unknown word" the graph couldn't
        place actually matches something discussed recently, purely to
        make the resulting clarifying question more specific -- never to
        skip asking or auto-resolve anything on its own.
        """
        if not words:
            return []
        matches = []
        for entry in reversed(self._window):
            if set(entry["keywords"]) & words:
                matches.append(entry)
        return matches
