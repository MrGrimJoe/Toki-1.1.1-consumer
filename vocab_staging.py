"""
vocab_staging.py -- the staging database for the 👍/👎 vocab-learning loop.

── What this is for ────────────────────────────────────────────────────────
When GraphRouter.classify_or_ask() isn't confident enough to auto-dispatch,
it asks a clarifying question naming the SPECIFIC unknown word(s) it
couldn't place (e.g. `does "frobnicate" mean you want to make folder?`).
That question, plus whatever happens next, is a labeled training example --
but it should only ever get promoted into the real graph (toki_graph_db)
after a human confirms it. This file is where the CANDIDATE examples live
in between "the graph asked" and "someone decided whether it was right,"
per the explicit design: like/dislike -> staging DB -> manual promotion
into the main graph. Nothing in this file ever writes to toki_graph_db
directly -- promotion is a separate, manual step (see STATUS.md/README's
existing "review step before auto-learned edges get promoted" note).

── Format ───────────────────────────────────────────────────────────────────
Plain JSONL, one record per line, append-only. Chosen over sqlite because
the rest of this project's source data (graph_source_data/*.json,
windows_command_library*.json) is already flat JSON/JSON-ish files meant to
be hand-edited or reviewed directly -- this matches that pattern instead of
introducing a new storage mechanism, and it's trivial to `cat`, grep, or
diff by hand before a manual promotion pass.

Record shape:
    {
        "id": "<uuid4>",
        "timestamp": "<ISO8601>",
        "user_prompt": "the original message",
        "unknown_word": "frobnicate",         # ONE word per record --
                                                 see log_graph_ask()
        "candidate_intent": "MAKE_FOLDER" | null,
        "status": "pending" | "confirmed" | "rejected",
        "resolved_at": "<ISO8601>" | null
    }

One record per unknown word, not one record per question, even though a
single clarifying question can name several words at once -- a user's
single 👍/👎 on the question confirms/rejects ALL of that question's words
together (see confirm_graph_ask/reject_graph_ask), but keeping them as
separate rows means a later manual review pass can promote word-by-word if
it turns out only some of them were actually right.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

STAGING_PATH = Path(__file__).parent / "vocab_staging.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_graph_ask(
    user_prompt: str,
    unknown_words: List[str],
    candidate_intent: Optional[str],
    path: Path = STAGING_PATH,
) -> List[str]:
    """Called right after classify_or_ask() returns an {"ask": ...} result.
    Writes one pending record per unknown word and returns their ids, so
    the caller (orchestrator.py) can hold onto them in _pending_graph_ask
    and resolve them together once the user answers.

    candidate_intent may be None (total miss, no leading guess at all --
    see classify_or_ask's docstring) -- staged with a null intent so a
    human reviewer can fill one in by hand later; never auto-resolved.
    """
    ids = []
    with open(path, "a", encoding="utf-8") as f:
        for word in unknown_words:
            record = {
                "id": str(uuid.uuid4()),
                "timestamp": _now(),
                "user_prompt": user_prompt,
                "unknown_word": word,
                "candidate_intent": candidate_intent,
                "status": "pending",
                "resolved_at": None,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            ids.append(record["id"])
    return ids


def _rewrite_status(ids: List[str], new_status: str, path: Path = STAGING_PATH) -> int:
    """Shared by confirm/reject -- JSONL has no in-place update, so this
    reads every record, flips status on matching ids, writes the whole
    file back. Fine at this file's expected scale (manual review, not a
    high-volume production log); revisit if this ever needs to handle
    thousands of pending records per session."""
    if not path.exists():
        return 0

    ids_set = set(ids)
    updated = 0
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record["id"] in ids_set and record["status"] == "pending":
                record["status"] = new_status
                record["resolved_at"] = _now()
                updated += 1
            records.append(record)

    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return updated


def confirm_graph_ask(ids: List[str], path: Path = STAGING_PATH) -> int:
    """👍 on a graph clarifying question -- marks all of that question's
    staged words 'confirmed'. Does NOT touch toki_graph_db; confirmed
    records still need a manual promotion pass (by design)."""
    return _rewrite_status(ids, "confirmed", path)


def reject_graph_ask(ids: List[str], path: Path = STAGING_PATH) -> int:
    """👎 on a graph clarifying question -- marks all of that question's
    staged words 'rejected', so a review pass can skip them."""
    return _rewrite_status(ids, "rejected", path)


def pending_review(path: Path = STAGING_PATH) -> List[Dict[str, Any]]:
    """All 'confirmed' records not yet promoted into the graph -- the
    actual review queue for the manual promotion step."""
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record["status"] == "confirmed":
                out.append(record)
    return out
