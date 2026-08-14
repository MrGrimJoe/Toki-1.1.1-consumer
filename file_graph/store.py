"""
file_graph/store.py -- the DURABLE half of the file organizer: persisted
evidence-type weights (the "learns over time" part of the design doc) and
a decision log, backed by their own Kùzu database
(`file_graph_db/`, entirely separate from `toki_graph_db` -- see this
module's own schema below, never touches graph_router.py's tables).

WHAT'S PERSISTED IN KÙZU HERE, AND WHAT ISN'T (an honest scope note)
----------------------------------------------------------------------------
Only two things live in file_graph_db: per-evidence-type WEIGHTS (Weight
node table) and a DECISION log (Decision node table, one row per file
actually moved or explicitly rejected). The live file/folder evidence
graph itself -- FileMetadata/FolderProfile from metadata.py/scoring.py --
is rebuilt fresh, in plain Python, on every organize() call, NOT
persisted node-by-node into Kùzu.

That's a deliberate choice, not a shortcut: the filesystem changes
between every call (that's the whole point of running this), so a
persisted File/Folder graph would need real invalidation logic to avoid
going stale -- the same problem FileIndex in extractor.py already
solves for a much simpler "does this file exist" index by just
rescanning on invalidate() rather than trying to diff. Re-scanning a
folder's contents on each organize() call is cheap (see metadata.py's
own cost notes) and correctness-safe; incrementally maintaining a live
Kùzu graph of the filesystem is a real, separate piece of engineering
this checkpoint doesn't attempt. What DOES need to survive across calls
-- the learned weights, and a record of what was decided -- genuinely
does live in Kùzu here.

SCHEMA
----------------------------------------------------------------------------
    Weight(evidence_type STRING PRIMARY KEY, value DOUBLE)
    Decision(id STRING PRIMARY KEY, file_path STRING, folder_path STRING,
              confidence DOUBLE, band STRING, accepted BOOL, ts DOUBLE)

No relationship tables -- both tables are standalone logs, nothing here
needs a graph traversal, a plain key lookup / insert is the whole
access pattern.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from file_graph.scoring import DEFAULT_WEIGHTS

DEFAULT_DB_PATH = Path(__file__).parent.parent / "file_graph_db"

_SCHEMA_DDL = [
    "CREATE NODE TABLE Weight(evidence_type STRING, value DOUBLE, PRIMARY KEY(evidence_type))",
    "CREATE NODE TABLE Decision(id STRING, file_path STRING, folder_path STRING, "
    "confidence DOUBLE, band STRING, accepted BOOLEAN, ts DOUBLE, PRIMARY KEY(id))",
]

# Learning-rate step applied per accept/reject, and the hard floor/ceiling
# every weight is clamped to -- keeps a long run of one-sided feedback
# (e.g. every suggestion in a session happens to be a true positive) from
# driving a weight to zero or to something absurdly large. Small step, on
# purpose: this should nudge the model over MANY decisions, not lurch
# after one.
_LEARNING_RATE = 0.05
_WEIGHT_MIN = 0.05
_WEIGHT_MAX = 3.0


class FileGraphStore:
    """Owns the Kùzu connection. Constructing this is cheap-ish (opens/
    creates the db file) but not free, so organizer.py holds one
    instance per organize() call rather than per-file."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._conn = None

    def _connection(self):
        if self._conn is not None:
            return self._conn
        import kuzu
        is_new = not self.db_path.exists()
        db = kuzu.Database(str(self.db_path))
        conn = kuzu.Connection(db)
        if is_new:
            for ddl in _SCHEMA_DDL:
                conn.execute(ddl)
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- Weights --------------------------------------------------------

    def load_weights(self) -> Dict[str, float]:
        """Returns the persisted weights, seeded with DEFAULT_WEIGHTS for
        any evidence type that's never been written yet (a brand-new
        store, or a newer version of this app that added an evidence
        type an older store never saw). Never raises: any Kùzu failure
        (missing optional `kuzu` package, corrupt db, etc.) falls back to
        the plain in-memory defaults, same clean-degrade posture as
        every other optional-dependency path in this app."""
        weights = dict(DEFAULT_WEIGHTS)
        try:
            conn = self._connection()
            result = conn.execute("MATCH (w:Weight) RETURN w.evidence_type, w.value")
            while result.has_next():
                evidence_type, value = result.get_next()
                weights[evidence_type] = value
        except Exception:
            return dict(DEFAULT_WEIGHTS)
        return weights

    def _upsert_weight(self, conn, evidence_type: str, value: float) -> None:
        existing = conn.execute(
            "MATCH (w:Weight {evidence_type: $t}) RETURN w.evidence_type", {"t": evidence_type},
        )
        if existing.has_next():
            conn.execute(
                "MATCH (w:Weight {evidence_type: $t}) SET w.value = $v",
                {"t": evidence_type, "v": value},
            )
        else:
            conn.execute(
                "CREATE (w:Weight {evidence_type: $t, value: $v})",
                {"t": evidence_type, "v": value},
            )

    def record_feedback(self, evidence_types: List[str], accepted: bool) -> None:
        """Nudges every evidence type that contributed to a decision one
        _LEARNING_RATE step toward (accepted) or away from (rejected) its
        current weight, clamped to [_WEIGHT_MIN, _WEIGHT_MAX]. Silently
        does nothing on any failure -- learning is a bonus on top of a
        working scorer using DEFAULT_WEIGHTS, never a requirement for
        organize() to function (see organizer.py)."""
        if not evidence_types:
            return
        try:
            conn = self._connection()
            current = self.load_weights()
            direction = 1.0 if accepted else -1.0
            for et in evidence_types:
                base = current.get(et, DEFAULT_WEIGHTS.get(et, 1.0))
                updated = base + direction * _LEARNING_RATE * base
                updated = max(_WEIGHT_MIN, min(_WEIGHT_MAX, updated))
                self._upsert_weight(conn, et, updated)
        except Exception:
            return

    # -- Decision log -----------------------------------------------------

    def log_decision(self, file_path: str, folder_path: str, confidence: float,
                      band: str, accepted: bool) -> None:
        """Best-effort audit trail -- one row per file TOKI actually
        acted on (moved) or the user explicitly rejected. Never raises;
        a logging failure shouldn't block the actual organize
        operation."""
        try:
            conn = self._connection()
            conn.execute(
                "CREATE (d:Decision {id: $id, file_path: $fp, folder_path: $fo, "
                "confidence: $c, band: $b, accepted: $a, ts: $ts})",
                {
                    "id": uuid.uuid4().hex, "fp": file_path, "fo": folder_path,
                    "c": confidence, "b": band, "a": accepted, "ts": time.time(),
                },
            )
        except Exception:
            return


__all__ = ["FileGraphStore", "DEFAULT_DB_PATH"]
