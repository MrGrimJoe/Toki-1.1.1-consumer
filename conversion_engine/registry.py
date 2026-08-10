"""
registry.py — deterministic (extension, operation) -> backend routing.

Same philosophy as tier_a_wcl_map.py / graph_router.py: no LLM guesses which
library handles a ".heic" file. This is a lookup table. A miss returns a
clear "not supported yet" instead of a wrong or silent conversion.

OPERATIONS
----------------------------------------------------------------------------
  "convert"   change container/format, same content   (a.json -> a.txt)
  "resize"    images only: change pixel dimensions
  "compress"  images: re-encode smaller; archives: zip up
  "extract"   archives: unzip

FORMAT FAMILIES
----------------------------------------------------------------------------
  image     png jpg jpeg webp bmp gif tiff ico
  text      txt md csv tsv json xml yaml yml log
  document  docx pdf html htm rtf odt   (requires pandoc on PATH; see
            backends/document_backend.py for the exact failure message when
            it's missing -- deliberately not silently degraded)
  archive   zip

Each family maps to exactly one backend module, so adding a new family is
"write one backend + add its extensions here" -- never touches the other
backends.
"""

from __future__ import annotations

from typing import Optional

IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "bmp", "gif", "tiff", "tif", "ico"}
TEXT_EXTS = {"txt", "md", "csv", "tsv", "json", "xml", "yaml", "yml", "log"}
DOCUMENT_EXTS = {"docx", "pdf", "html", "htm", "rtf", "odt", "md"}
ARCHIVE_EXTS = {"zip"}


class UnsupportedFormatError(Exception):
    """Raised when no backend covers the requested extension/operation
    combination. Callers surface this as a plain sentence -- never a
    traceback -- matching apis.py's existing "friendly error" convention."""


def family_for_extension(ext: str) -> Optional[str]:
    """Returns which format family owns this extension, or None. An
    extension can belong to more than one family in principle (md is both
    text and document) -- family_for_operation below resolves that using
    the requested OPERATION, not just the extension alone."""
    ext = ext.lower().lstrip(".")
    if ext in IMAGE_EXTS:
        return "image"
    if ext in ARCHIVE_EXTS:
        return "archive"
    if ext in DOCUMENT_EXTS and ext not in TEXT_EXTS:
        return "document"
    if ext in TEXT_EXTS:
        return "text"
    return None


def backend_for(source_ext: str, operation: str, target_ext: Optional[str] = None):
    """Returns the backend module to handle this request, or raises
    UnsupportedFormatError with a specific, actionable message."""
    from .backends import image_backend, text_backend, document_backend, archive_backend

    source_ext = source_ext.lower().lstrip(".")
    target_ext = target_ext.lower().lstrip(".") if target_ext else None

    if operation in ("resize", "compress") and source_ext in IMAGE_EXTS:
        return image_backend

    if operation == "compress" and source_ext not in IMAGE_EXTS:
        return archive_backend

    if operation == "extract":
        if source_ext in ARCHIVE_EXTS:
            return archive_backend
        raise UnsupportedFormatError(
            f"'.{source_ext}' isn't an archive format I can extract."
        )

    if operation == "convert":
        # Image -> image is unambiguous and the common case ("shrink this
        # image" is really resize; "turn this png into a jpg" is convert).
        if source_ext in IMAGE_EXTS and (target_ext is None or target_ext in IMAGE_EXTS):
            return image_backend

        # Prefer the plain text backend for anything it can round-trip
        # (json/csv/txt/md/etc <-> each other) before falling back to
        # pandoc for genuinely rich documents (docx/pdf/odt/rtf/html).
        if source_ext in TEXT_EXTS and (target_ext is None or target_ext in TEXT_EXTS):
            return text_backend

        if source_ext in DOCUMENT_EXTS or (target_ext and target_ext in DOCUMENT_EXTS):
            return document_backend

        if source_ext in ARCHIVE_EXTS or target_ext in (ARCHIVE_EXTS if target_ext else set()):
            return archive_backend

    raise UnsupportedFormatError(
        f"I don't have a converter for '.{source_ext}'"
        + (f" to '.{target_ext}'" if target_ext else "")
        + " yet."
    )
