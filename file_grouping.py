"""
file_grouping.py -- GROUP_FILES_BY_EXTENSION: "put all the pdfs and json
files in a new folder named rezero".

DELIBERATELY NOT PART OF file_graph/
----------------------------------------------------------------------------
This looks like a cousin of file_graph/'s organizer at first glance (both
move loose files into a folder), but it's actually the opposite kind of
operation, which is why it's a separate top-level module rather than
folded into that package:

  - file_graph.organizer.FileOrganizer INFERS a destination from evidence
    (filename/content similarity to what's already in existing folders)
    when the user doesn't say where things should go, so it needs
    confidence bands, an explanation, and a hard rule against ever
    inventing a new folder.
  - This module does none of that. The user has ALREADY specified both
    the filter (which file types) and the destination (a folder name,
    possibly new) explicitly -- there's nothing to infer and no
    confidence involved. It always executes what was asked, creates the
    destination folder if it doesn't exist yet, and reports exactly what
    moved.

Kept intentionally tiny and dependency-free from file_graph/ so the two
features can be reasoned about, tested, and changed independently.
"""

from __future__ import annotations

import os
import shutil
from typing import List


def _unique_destination(folder_path: str, name: str) -> str:
    """Same de-duplication convention as file_graph.organizer's own
    helper (Windows-Explorer-style 'x.pdf' -> 'x (1).pdf') -- duplicated
    here rather than imported so this module has zero dependency on
    file_graph/, per this file's own docstring."""
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


def group_files_by_extension(path: str, extensions: List[str], dest_name: str) -> str:
    """Moves every file directly inside `path` (non-recursive -- same
    "loose files only" scope as SORT_FOLDER_BY_TYPE/file_graph's
    organizer) whose extension is in `extensions` into a
    `path/dest_name` subfolder, creating it if needed. Never touches
    files already inside a subfolder. Returns a plain human-readable
    summary; never raises."""
    from extractor import is_within_sandbox

    path = os.path.normpath(path)
    if not is_within_sandbox(path):
        return f"Can't do that in '{path}' -- outside the sandbox (D:\\ and Desktop only)."
    if not os.path.isdir(path):
        return f"Couldn't find the folder '{path}'."

    dest = os.path.normpath(os.path.join(path, dest_name))
    if not is_within_sandbox(dest):
        return f"Can't create '{dest_name}' there -- outside the sandbox."

    ext_set = {e.lower() for e in extensions}
    try:
        entries = os.listdir(path)
    except Exception as e:
        return f"Couldn't list {path}: {e}"

    matches = [
        f for f in entries
        if os.path.isfile(os.path.join(path, f)) and os.path.splitext(f)[1].lower() in ext_set
    ]
    if not matches:
        return f"No {'/'.join(sorted(ext_set))} files found directly in {path}."

    try:
        os.makedirs(dest, exist_ok=True)
    except Exception as e:
        return f"Couldn't create the folder '{dest_name}': {e}"

    moved: List[str] = []
    failed: List[str] = []
    for fname in matches:
        src = os.path.join(path, fname)
        target = _unique_destination(dest, fname)
        try:
            shutil.move(src, target)
            moved.append(fname)
        except Exception:
            failed.append(fname)

    summary = f"Moved {len(moved)} file(s) into '{dest_name}': " + ", ".join(moved)
    if failed:
        summary += f"\nCouldn't move: {', '.join(failed)}"
    return summary


__all__ = ["group_files_by_extension"]
