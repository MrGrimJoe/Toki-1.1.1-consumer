"""
file_graph/metadata.py -- per-file evidence extraction for the graph-based
file organizer (BETA 0.3.44, checkpoint 4 -- see STATUS.md for the full
design writeup and the honest scope limits).

Everything here is CHEAP, on purpose: this runs over every loose file in
a folder on every organize() call (no incremental/background indexing in
this checkpoint -- see organizer.py's own docstring), so a single file's
metadata extraction needs to stay well under a millisecond for anything
that isn't doing real disk I/O beyond one os.stat() and, for a small set
of plain-text formats, one bounded file read.

WHAT COUNTS AS "EVIDENCE" HERE, AND WHY EXTRACTED TEXT IS DELIBERATELY
LIMITED TO PLAIN-TEXT FORMATS
----------------------------------------------------------------------------
Filename, extension, size, and mtime are free (already in a single
os.stat() call). content_hash is one bounded read of the file's own
bytes (see _content_hash()'s docstring for the exact cap). extracted
TEXT, though, is deliberately only attempted for formats that are
ALREADY plain text on disk (.txt/.md/.csv/.log/.json/.py and a handful
of other source-code extensions) -- reading a .docx or .pdf's actual
text needs pandoc/pdf-parsing (document_backend.py's own pandoc
subprocess, or a PDF library neither this file nor conversion_engine
currently carries for TEXT EXTRACTION specifically, only for format
CONVERSION), and shelling out to pandoc per loose file in a folder would
be slow (a real subprocess per file) for something that's supposed to
run inline on every "organize this" request. Content-based matching for
binary document formats is a clearly-flagged fast-follow (see
STATUS.md), not attempted here -- filename/extension/timestamp/hash
evidence still work perfectly well for those files, they just don't
additionally benefit from extracted-text overlap scoring.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import FrozenSet, Optional

# Extensions cheap enough to read directly as text -- no external tool,
# no binary parsing, just open() and decode. Deliberately narrow: this is
# an evidence SOURCE, not a general-purpose text extractor, so it's fine
# (better, even) to skip a format entirely rather than guess at decoding
# something that isn't really plain text.
_TEXT_READABLE_EXTS = frozenset({
    ".txt", ".md", ".markdown", ".csv", ".log", ".json", ".py", ".js",
    ".ts", ".java", ".c", ".cpp", ".h", ".cs", ".yaml", ".yml", ".ini",
    ".cfg", ".rst",
})

# Cap on how much of a text file is actually read for token extraction --
# enough to catch a document's real topic vocabulary (titles, headers,
# first few paragraphs) without reading, say, a multi-MB log file in
# full on every organize() call.
_TEXT_READ_CAP_BYTES = 8_192

# Files larger than this are skipped for BOTH text reading and hashing --
# organize() runs inline on a user request, so a stray multi-GB file
# sitting loose on the Desktop shouldn't make a single "organize this"
# turn stall on hashing it. Filename/extension/timestamp evidence still
# apply regardless of size; only the two byte-reading evidence sources
# are size-gated.
_MAX_HASHABLE_OR_READABLE_BYTES = 100 * 1024 * 1024  # 100 MB

# Generic filler words that carry no real topic signal in a filename --
# distinct from graph_router.py's STOPWORDS (that set is tuned for
# natural-language command phrasings; this one is tuned for the very
# different vocabulary of file names: version/status markers, not
# grammar words).
_GENERIC_FILENAME_WORDS = frozenset({
    "copy", "copy1", "copy2", "new", "final", "final2", "untitled",
    "document", "file", "draft", "v1", "v2", "v3", "old", "backup",
    "the", "a", "an", "of", "and", "for", "to",
})

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")
# CamelCase boundary: lowercase/digit followed by an uppercase letter --
# splits "PhysicsChapter" into "Physics"+"Chapter" before lowercasing, the
# same way "Physics_Chapter_4" already splits on its underscore.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokenize_name(stem: str) -> FrozenSet[str]:
    """Turns a filename's stem (no extension) into a set of lowercase
    topic tokens: splits on camelCase boundaries AND any non-alphanumeric
    separator (underscore, hyphen, space, dot), drops single-character
    tokens (too weak a signal on their own) and generic filler words.
    Numbers are KEPT (e.g. "4" in "Physics_Chapter_4") -- a shared
    chapter/part/version number between a file and a folder's other
    contents is real, if weak, evidence, not noise."""
    expanded = _CAMEL_BOUNDARY_RE.sub(" ", stem)
    raw_tokens = _TOKEN_SPLIT_RE.split(expanded.lower())
    return frozenset(
        t for t in raw_tokens
        if t and (len(t) > 1 or t.isdigit()) and t not in _GENERIC_FILENAME_WORDS
    )


def tokenize_text(text: str, limit: int = 60) -> FrozenSet[str]:
    """Same shape as tokenize_name() but for extracted file CONTENT
    rather than a filename -- no camelCase splitting (prose doesn't use
    it), same generic-word/length filtering, capped to the `limit` most
    frequent qualifying tokens so one huge, repetitive file can't
    dominate every folder-profile comparison it's compared against."""
    from collections import Counter
    raw_tokens = _TOKEN_SPLIT_RE.split(text.lower())
    counts = Counter(
        t for t in raw_tokens
        if t and len(t) > 2 and t not in _GENERIC_FILENAME_WORDS
    )
    return frozenset(t for t, _ in counts.most_common(limit))


def _content_hash(path: str, size: int) -> Optional[str]:
    """SHA-256 of the file's first _TEXT_READ_CAP_BYTES bytes -- NOT the
    whole file. This is deliberately a "likely near-duplicate" signal,
    not a cryptographic exact-duplicate guarantee: two files could share
    a first-8KB hash while differing later. That's an acceptable,
    explicit trade-off here -- content_hash_duplicate is one evidence
    source among several (see scoring.py), never the sole basis for an
    auto-organize decision, and hashing only the head keeps this cheap
    enough to run on every loose file on every request. Returns None on
    any read failure (permissions, file vanished mid-scan, etc.) rather
    than raising -- same fail-soft contract as every other piece of
    evidence here."""
    if size > _MAX_HASHABLE_OR_READABLE_BYTES:
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(_TEXT_READ_CAP_BYTES)
    except Exception:
        return None
    return hashlib.sha256(head).hexdigest()


def _read_text_tokens(path: str, ext: str, size: int) -> FrozenSet[str]:
    if ext not in _TEXT_READABLE_EXTS or size > _MAX_HASHABLE_OR_READABLE_BYTES:
        return frozenset()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read(_TEXT_READ_CAP_BYTES)
    except Exception:
        return frozenset()
    return tokenize_text(text)


@dataclass(frozen=True)
class FileMetadata:
    path: str
    name: str
    stem: str
    ext: str                       # lowercase, includes the leading dot; "" if none
    dir: str                       # the containing folder's path
    size: int
    mtime: float
    name_tokens: FrozenSet[str] = field(default_factory=frozenset)
    text_tokens: FrozenSet[str] = field(default_factory=frozenset)
    content_hash: Optional[str] = None


def extract_metadata(path: str) -> Optional[FileMetadata]:
    """Returns None (never raises) if the path can't be stat'd at all --
    same fail-soft contract as FileIndex._scan() in extractor.py. Every
    other failure mode inside (hashing, text reading) already fails soft
    on its own and still returns a usable FileMetadata with that one
    field empty/None."""
    try:
        st = os.stat(path)
    except Exception:
        return None

    # os.path (not ntpath) here deliberately -- this module is exercised
    # directly against real files on whatever OS is actually running it
    # (Linux in this dev/test sandbox, Windows in production), unlike
    # extractor.py's path-STRING manipulation which always targets
    # Windows-shaped strings regardless of dev platform. See
    # organizer.py's own docstring for how the two meet at the boundary.
    name = os.path.basename(path)
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    directory = os.path.dirname(path)
    size = st.st_size

    return FileMetadata(
        path=path,
        name=name,
        stem=stem,
        ext=ext,
        dir=directory,
        size=size,
        mtime=st.st_mtime,
        name_tokens=tokenize_name(stem),
        text_tokens=_read_text_tokens(path, ext, size),
        content_hash=_content_hash(path, size),
    )


__all__ = ["FileMetadata", "extract_metadata", "tokenize_name", "tokenize_text"]
