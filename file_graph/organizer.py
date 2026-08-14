"""
file_graph/organizer.py -- ties metadata.py + scoring.py + store.py
together into the actual ORGANIZE_FILES_BY_TOPIC action, and is the only
thing in this package that touches the real filesystem for a WRITE
(moving a file).

NEVER INVENTS A FOLDER, EVER
----------------------------------------------------------------------------
This is the single most important safety property of this feature, and
it's enforced structurally, not by a policy comment: candidate
destination folders (_find_candidate_folders() below) are ONLY folders
that ALREADY exist and ALREADY contain at least one file. There is no
code path anywhere in this package that creates a new folder or guesses
a topic name from thin air ("this looks like it's about physics, let's
make a Physics folder") -- that would be exactly the kind of invented
structure extractor.py's regex-only slot extraction already avoids
everywhere else in this app, just relocated into a new subsystem. If a
file's topic genuinely has no existing home, this organizer correctly
finds nothing and leaves it alone, in the <60% "don't touch it" band.

RESCAN, DON'T INCREMENTALLY MAINTAIN
----------------------------------------------------------------------------
Every organize() call does a fresh, bounded walk of the target folder
(see _MAX_WALK_DEPTH below) -- no persisted file/folder graph carried
across calls (see store.py's own docstring for why). This keeps
correctness simple (nothing can go stale) at the cost of doing real,
if cheap, disk I/O on every call -- an acceptable trade for a feature
invoked by an explicit user request, not something running continuously
in the background.

SANDBOX ENFORCEMENT
----------------------------------------------------------------------------
Every path this touches -- both the scan root and, separately, every
individual move destination right before it happens -- is re-checked
against extractor.is_within_sandbox(). Belt-and-suspenders on purpose:
the scan root already comes from extractor.py's own path resolution
(which itself only ever resolves within the sandbox), but a destination
folder is discovered by walking the filesystem, not by
resolve_path()-style user-text parsing, so it gets its own independent
check rather than trusting the root check to cover it transitively.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Dict, List, Optional

from file_graph.metadata import extract_metadata, FileMetadata
from file_graph.scoring import (
    Candidate, FolderProfile, build_folder_profile, score_candidate,
)
from file_graph.store import FileGraphStore

# How many directory levels below the scan root to walk looking for
# candidate folders -- e.g. depth 3 lets "organize Desktop" discover
# Desktop/School/Physics (2 levels down) as a candidate, matching the
# design doc's own "School/Physics/" example, without walking an
# unbounded tree if the sandbox root happens to be deep (D:\ especially).
_MAX_WALK_DEPTH = 3

# Directories never treated as organize candidates OR scanned into --
# nothing in this app creates these, but a user's own filesystem might
# have them, and neither is ever a meaningful "topic" folder.
_SKIP_DIR_NAMES = frozenset({
    "$recycle.bin", "system volume information", ".git", "__pycache__",
    "node_modules", ".venv", "venv",
})


@dataclass
class OrganizeFileResult:
    file: FileMetadata
    candidate: Optional[Candidate]
    moved: bool
    error: Optional[str] = None


def _walk_scope(root: str):
    """Yields (dir_path, direct_file_names, depth) for root and every
    subdirectory up to _MAX_WALK_DEPTH, skipping _SKIP_DIR_NAMES.
    Depth-bounded, hidden-dir-skipping variant of os.walk -- deliberately
    NOT using os.walk directly since that has no clean depth cutoff."""
    root = os.path.normpath(root)
    root_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath.rstrip(os.sep).count(os.sep) - root_depth
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d.lower() not in _SKIP_DIR_NAMES
        ]
        if depth > _MAX_WALK_DEPTH:
            dirnames[:] = []
            continue
        yield dirpath, filenames, depth


def _unique_destination(folder_path: str, name: str) -> str:
    """Windows-Explorer-style de-duplication: 'x.pdf' -> 'x (1).pdf' ->
    'x (2).pdf' if the name is already taken in the destination, so an
    organize run never silently overwrites an existing file."""
    candidate = os.path.join(folder_path, name)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(name)
    n = 1
    while True:
        candidate = os.path.join(folder_path, f"{stem} ({n}){ext}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


class FileOrganizer:
    """Stateless across calls except for the FileGraphStore it owns
    (weights persist across calls via Kùzu -- see store.py). One
    instance lives on ToolDispatcher, same lifetime as every other API
    class in apis.py."""

    def __init__(self, store: Optional[FileGraphStore] = None):
        self._store = store or FileGraphStore()

    def organize(self, path: str, include_suggestions: bool = False) -> str:
        from extractor import is_within_sandbox

        path = os.path.normpath(path)
        if not is_within_sandbox(path):
            return f"Can't organize '{path}' -- that's outside the sandbox (D:\\ and Desktop only)."
        if not os.path.isdir(path):
            return f"Couldn't find the folder '{path}' to organize."

        loose_files, folder_profiles = self._scan(path)
        if not loose_files:
            return f"Nothing loose to organize in {path} -- everything's already in a folder."
        if not folder_profiles:
            return (
                f"Didn't find any existing topic folders under {path} to match files "
                f"against -- I only move files into folders that already show a clear "
                f"pattern, never invent new ones."
            )

        weights = self._store.load_weights()
        results = self._score_all(loose_files, folder_profiles, weights)
        return self._execute_and_report(path, results, include_suggestions)

    # -- Scanning ---------------------------------------------------------

    def _scan(self, root: str):
        loose_files: List[FileMetadata] = []
        folder_profiles: Dict[str, FolderProfile] = {}

        for dirpath, filenames, depth in _walk_scope(root):
            metas = []
            for fname in filenames:
                fm = extract_metadata(os.path.join(dirpath, fname))
                if fm:
                    metas.append(fm)
            if depth == 0:
                loose_files = metas
            elif metas:
                # Only folders that already have real content become
                # candidates -- see this module's own docstring on never
                # inventing structure.
                folder_profiles[dirpath] = build_folder_profile(dirpath, metas)

        return loose_files, folder_profiles

    # -- Scoring ------------------------------------------------------------

    def _score_all(self, loose_files: List[FileMetadata],
                    folder_profiles: Dict[str, FolderProfile],
                    weights: Dict[str, float]) -> List[OrganizeFileResult]:
        results = []
        for fm in loose_files:
            best: Optional[Candidate] = None
            for profile in folder_profiles.values():
                c = score_candidate(fm, profile, weights)
                if best is None or c.confidence > best.confidence:
                    best = c
            if best and best.confidence > 0:
                results.append(OrganizeFileResult(file=fm, candidate=best, moved=False))
            else:
                results.append(OrganizeFileResult(file=fm, candidate=None, moved=False))
        return results

    # -- Execution + report -------------------------------------------------

    def _execute_and_report(self, root: str, results: List[OrganizeFileResult],
                             include_suggestions: bool) -> str:
        from extractor import is_within_sandbox

        moved_lines: List[str] = []
        suggested_lines: List[str] = []
        left_alone: List[str] = []

        for r in results:
            c = r.candidate
            if c is None or c.band == "skip":
                left_alone.append(r.file.name)
                continue

            should_execute = c.band == "auto" or (c.band == "suggest" and include_suggestions)
            if should_execute:
                dest_dir = c.folder_path
                if not is_within_sandbox(dest_dir):
                    left_alone.append(r.file.name)
                    continue
                dest_path = _unique_destination(dest_dir, r.file.name)
                try:
                    shutil.move(r.file.path, dest_path)
                    r.moved = True
                except Exception as e:
                    r.error = str(e)
                    left_alone.append(f"{r.file.name} (move failed: {e})")
                    continue
                self._store.log_decision(
                    r.file.path, c.folder_path, c.confidence, c.band, accepted=True,
                )
                self._store.record_feedback(list(c.evidence.keys()), accepted=True)
                rel = os.path.relpath(c.folder_path, root)
                explanation = ", ".join(c.explanation) if c.explanation else "matching evidence"
                moved_lines.append(f"{r.file.name} -> {rel} ({c.confidence:.0f}% confidence -- {explanation})")
            elif c.band == "suggest":
                rel = os.path.relpath(c.folder_path, root)
                explanation = ", ".join(c.explanation) if c.explanation else "matching evidence"
                suggested_lines.append(f"{r.file.name} -> {rel} ({c.confidence:.0f}% confidence -- {explanation})")

        parts: List[str] = []
        if moved_lines:
            parts.append("Organized automatically:\n  " + "\n  ".join(moved_lines))
        if suggested_lines:
            header = "Also worth a look, but not confident enough to move without asking:"
            parts.append(header + "\n  " + "\n  ".join(suggested_lines) +
                          '\n  (say "organize including suggestions" to apply these too)')
        if left_alone:
            parts.append("Left alone (no confident match): " + ", ".join(left_alone))

        if not parts:
            return f"Nothing to organize in {root} right now."
        return "\n\n".join(parts)


__all__ = ["FileOrganizer", "OrganizeFileResult"]
