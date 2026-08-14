"""
file_graph/scoring.py -- turns raw file/folder metadata into an
explainable confidence score, per the original design doc's exact bands:

    >90%    -> organize automatically
    60-90%  -> suggest / ask
    <60%    -> don't touch it

No LLM anywhere in this file -- every signal below is a plain arithmetic
comparison over FileMetadata (metadata.py) and FolderProfile (this file).
"CONFIDENCE" here means "how much of the AVAILABLE evidence agrees",
not an absolute probability -- see score_candidate()'s docstring for
exactly how that's computed and why folders that simply don't have a
particular kind of evidence (e.g. an Images folder has no meaningful
extracted-text overlap to offer) aren't penalized for its absence.

EVIDENCE TYPES (each is a 0..1 raw signal for one (file, folder) pair):
  filename_similarity      -- fraction of the file's own name-tokens that
                               already appear among the folder's existing
                               file names.
  extension_match          -- fraction of the folder's existing files
                               that share this file's extension.
  shared_topic_group       -- how many individual files in the folder
                               have their OWN meaningful name-token
                               overlap with this file (the "8 related
                               Physics documents" evidence from the
                               design doc's own example).
  recent_activity          -- how recently the folder was last added to,
                               relative to this file's own mtime (a
                               folder that's been getting new, similar
                               files recently is more likely the right
                               home for one more).
  content_hash_duplicate   -- this file's content is (near-)identical to
                               a file already in the folder -- far and
                               away the strongest single signal when it
                               fires.
  extracted_text_overlap   -- shared vocabulary between this file's
                               extracted text and the folder's aggregate
                               text profile (only for text-readable
                               formats -- see metadata.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional

from file_graph.metadata import FileMetadata

# Default per-evidence-type weights, used the first time the organizer
# ever runs (before any learning has happened) and as the floor/ceiling
# learning is clamped to -- see store.py's clamp bounds. Tuned by hand
# against the synthetic fixtures in tests/test_file_graph.py, not
# against real user data (none available from this sandbox) -- these are
# a reasonable starting point, not a claim of being optimally tuned; the
# whole point of persisting and adjusting them (store.py) is that they
# don't have to be perfect on day one.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "filename_similarity": 1.0,
    "extension_match": 0.6,
    "shared_topic_group": 1.0,
    "recent_activity": 0.4,
    "content_hash_duplicate": 2.0,
    "extracted_text_overlap": 0.8,
}

# An evidence signal below this is treated as "not really present" for
# both scoring (excluded from the weighted average -- see
# score_candidate()) and explanation purposes (no bullet generated for
# it) -- avoids diluting confidence with noise-level near-zero matches,
# and avoids explanation bullets like "matching project/topic terms"
# when the actual overlap was one incidental short word.
_SIGNAL_FLOOR = 0.15

# A file whose OWN name-token overlap with the candidate file is at or
# above this fraction counts as "related" for shared_topic_group's
# evidence count and explanation bullet.
_RELATED_FILE_TOKEN_OVERLAP = 0.34

# recent_activity's decay window -- a folder whose most recent file is
# within this many days of the candidate file's own mtime gets full or
# partial credit; beyond it, none. 90 days is a deliberately generous,
# round number for "recent", not tuned against real usage data.
_RECENCY_WINDOW_DAYS = 90.0
_SECONDS_PER_DAY = 86400.0


@dataclass
class FolderProfile:
    """Aggregated evidence profile for one candidate destination folder,
    built once from its EXISTING contents (see build_folder_profile())
    and then compared against every loose file being considered for it.
    Deliberately excludes anything about the loose files themselves --
    this is purely "what does this folder already look like"."""
    path: str
    files: List[FileMetadata] = field(default_factory=list)
    name_token_counts: Dict[str, int] = field(default_factory=dict)
    ext_counts: Dict[str, int] = field(default_factory=dict)
    text_token_counts: Dict[str, int] = field(default_factory=dict)
    content_hashes: FrozenSet[str] = field(default_factory=frozenset)
    most_recent_mtime: Optional[float] = None

    @property
    def file_count(self) -> int:
        return len(self.files)


def build_folder_profile(path: str, files: List[FileMetadata]) -> FolderProfile:
    """files: the FileMetadata for every file ALREADY inside this
    folder (not the loose files being organized). An empty `files` list
    produces a valid, empty profile -- callers filter those out before
    treating a folder as a candidate (see organizer.py: only folders
    that already contain at least one file are ever suggested as a
    destination, so this app never invents/guesses a brand-new topic
    folder name out of thin air)."""
    name_counts: Dict[str, int] = {}
    ext_counts: Dict[str, int] = {}
    text_counts: Dict[str, int] = {}
    hashes = set()
    most_recent: Optional[float] = None

    for fm in files:
        for tok in fm.name_tokens:
            name_counts[tok] = name_counts.get(tok, 0) + 1
        ext_counts[fm.ext] = ext_counts.get(fm.ext, 0) + 1
        for tok in fm.text_tokens:
            text_counts[tok] = text_counts.get(tok, 0) + 1
        if fm.content_hash:
            hashes.add(fm.content_hash)
        if most_recent is None or fm.mtime > most_recent:
            most_recent = fm.mtime

    return FolderProfile(
        path=path, files=list(files), name_token_counts=name_counts,
        ext_counts=ext_counts, text_token_counts=text_counts,
        content_hashes=frozenset(hashes), most_recent_mtime=most_recent,
    )


@dataclass
class Candidate:
    """One scored (file, folder) suggestion. `evidence` maps each
    evidence type that actually fired (>= _SIGNAL_FLOOR) to its raw 0..1
    signal -- kept separately from `explanation` (the human-readable
    bullets) so callers/tests can assert on the underlying numbers, not
    just the rendered English."""
    file: FileMetadata
    folder_path: str
    confidence: float               # 0..100
    evidence: Dict[str, float] = field(default_factory=dict)
    explanation: List[str] = field(default_factory=list)

    @property
    def band(self) -> str:
        if self.confidence >= 90.0:
            return "auto"
        if self.confidence >= 60.0:
            return "suggest"
        return "skip"


def _related_file_count(file: FileMetadata, folder: FolderProfile) -> int:
    if not file.name_tokens:
        return 0
    count = 0
    for other in folder.files:
        if not other.name_tokens:
            continue
        overlap = file.name_tokens & other.name_tokens
        smaller = min(len(file.name_tokens), len(other.name_tokens))
        if smaller and len(overlap) / smaller >= _RELATED_FILE_TOKEN_OVERLAP:
            count += 1
    return count


def _dominant_topic_word(file: FileMetadata, folder: FolderProfile) -> Optional[str]:
    """Picks the single name-token this file shares with the folder that
    appears MOST OFTEN among the folder's existing files -- used purely
    to make explanation bullets concrete ("8 related Physics documents"
    instead of "8 related documents"). Ties broken by token length
    (longer, more specific words make a more informative label) then
    alphabetically, for deterministic output (tests rely on this)."""
    shared = file.name_tokens & set(folder.name_token_counts)
    if not shared:
        return None
    return max(shared, key=lambda t: (folder.name_token_counts[t], len(t), t))


# When only ONE evidence type is present, the plain weighted-average
# formula above degenerates to "that evidence's own raw signal" --
# undoing the whole point of DEFAULT_WEIGHTS (a weight only actually
# discounts anything when it's compared against OTHER weights). These
# caps restore that intent for the solo-evidence case: how much a single
# signal is allowed to mean entirely on its own, roughly proportional to
# how much real information that evidence type carries alone. Two
# completely unrelated files that just happen to have been touched
# minutes apart (recent_activity, weakest -- pure coincidence is common)
# should land in the "leave it alone" band; an exact-content duplicate
# (content_hash_duplicate, strongest) is real signal even by itself and
# can still surface as a suggestion. None of these solo caps reach the
# 90% auto band -- corroboration from a second, independent evidence
# type is always required before this ever moves a file without asking.
_SOLO_EVIDENCE_CAP: Dict[str, float] = {
    "content_hash_duplicate": 85.0,
    "filename_similarity": 65.0,
    "shared_topic_group": 65.0,
    "extracted_text_overlap": 60.0,
    "extension_match": 40.0,
    "recent_activity": 35.0,
}


def score_candidate(file: FileMetadata, folder: FolderProfile,
                     weights: Optional[Dict[str, float]] = None) -> Candidate:
    """Scores ONE (file, folder) pair. Confidence is a weighted average
    over only the evidence types that actually produced a signal >=
    _SIGNAL_FLOOR -- e.g. an Images folder with no text-readable files
    simply never contributes an extracted_text_overlap term, rather than
    being scored as if that evidence were present and equal to zero
    (which would unfairly drag down every image file's confidence just
    because images don't have extractable text). If NO evidence type
    fires at all, confidence is 0 and band is "skip"."""
    w = weights or DEFAULT_WEIGHTS
    raw: Dict[str, float] = {}

    # -- filename_similarity: fraction of the file's OWN tokens already
    # seen somewhere in the folder's existing filenames.
    if file.name_tokens:
        hits = sum(1 for t in file.name_tokens if t in folder.name_token_counts)
        raw["filename_similarity"] = hits / len(file.name_tokens)

    # -- extension_match: how dominant this extension already is in the
    # folder.
    if folder.file_count:
        raw["extension_match"] = folder.ext_counts.get(file.ext, 0) / folder.file_count

    # -- shared_topic_group: related-file count, normalized (capped at 5
    # related files for full credit -- the count itself, uncapped, still
    # goes into the explanation bullet).
    related = _related_file_count(file, folder)
    if related:
        raw["shared_topic_group"] = min(related / 5.0, 1.0)

    # -- recent_activity: decay from the folder's most recently touched
    # file, relative to THIS file's own mtime.
    if folder.most_recent_mtime is not None:
        days_apart = abs(file.mtime - folder.most_recent_mtime) / _SECONDS_PER_DAY
        if days_apart <= _RECENCY_WINDOW_DAYS:
            raw["recent_activity"] = 1.0 - (days_apart / _RECENCY_WINDOW_DAYS)

    # -- content_hash_duplicate: near-identical to something already
    # there.
    if file.content_hash and file.content_hash in folder.content_hashes:
        raw["content_hash_duplicate"] = 1.0

    # -- extracted_text_overlap: shared vocabulary, only when both sides
    # actually have extracted text to compare.
    if file.text_tokens and folder.text_token_counts:
        folder_text_vocab = set(folder.text_token_counts)
        overlap = file.text_tokens & folder_text_vocab
        raw["extracted_text_overlap"] = len(overlap) / len(file.text_tokens)

    present = {k: v for k, v in raw.items() if v >= _SIGNAL_FLOOR}
    if not present:
        return Candidate(file=file, folder_path=folder.path, confidence=0.0)

    weighted_sum = sum(present[k] * w.get(k, 0.0) for k in present)
    weight_total = sum(w.get(k, 0.0) for k in present)
    confidence = 0.0 if weight_total <= 0 else max(0.0, min(1.0, weighted_sum / weight_total)) * 100.0

    # A SINGLE evidence type, however strong its own raw signal, should
    # never alone justify the auto-organize band -- see _SOLO_EVIDENCE_CAP's
    # own docstring just above for why the cap is per-evidence-type, not
    # a single flat number: some solo evidence is real signal (an exact
    # content duplicate), some is closer to coincidence (two files just
    # touched around the same time).
    if len(present) == 1:
        sole = next(iter(present))
        confidence = min(confidence, _SOLO_EVIDENCE_CAP.get(sole, 70.0))

    explanation = _build_explanation(file, folder, present, related)
    return Candidate(
        file=file, folder_path=folder.path, confidence=round(confidence, 1),
        evidence=present, explanation=explanation,
    )


def _build_explanation(file: FileMetadata, folder: FolderProfile,
                        present: Dict[str, float], related_count: int) -> List[str]:
    """Human-readable bullets, same shape as the design doc's own
    example ("8 related Physics documents", "matching project/topic
    terms", "same document group", "recent activity around Physics
    files"). Order matches roughly how a person would explain the
    decision: the most concrete/specific evidence first, general
    corroborating evidence after."""
    bullets: List[str] = []
    topic = _dominant_topic_word(file, folder)
    topic_label = topic.capitalize() if topic else None

    if "content_hash_duplicate" in present:
        bullets.append("identical to a file already in this folder")
    if "shared_topic_group" in present and related_count:
        noun = f"{topic_label} documents" if topic_label else "related documents"
        bullets.append(f"{related_count} related {noun}")
    if "filename_similarity" in present:
        bullets.append("matching project/topic terms")
    if "extension_match" in present and present["extension_match"] >= 0.7:
        bullets.append("same document group")
    if "extracted_text_overlap" in present:
        bullets.append("shared wording with files already here")
    if "recent_activity" in present:
        label = f"{topic_label} files" if topic_label else "similar files"
        bullets.append(f"recent activity around {label}")
    return bullets


__all__ = [
    "DEFAULT_WEIGHTS", "FolderProfile", "Candidate",
    "build_folder_profile", "score_candidate",
]
