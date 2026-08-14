"""
registry.py — deterministic (extension, operation) -> backend routing.

Same philosophy as tier_a_wcl_map.py / graph_router.py: no LLM guesses which
library handles a ".heic" file. This is a lookup table. A miss returns a
clear "not supported yet" instead of a wrong or silent conversion.

OPERATIONS
----------------------------------------------------------------------------
  "convert"   change container/format, same content   (a.json -> a.txt)
  "resize"    images only: change pixel dimensions
  "compress"  images: re-encode smaller; audio/video: re-encode smaller;
              anything else / a folder: zip up
  "extract"   archives: unzip / untar

FORMAT FAMILIES  (BETA 0.3.43: added media, widened archive/document/text)
----------------------------------------------------------------------------
  image     png jpg jpeg webp bmp gif tiff ico ppm pcx tga
  text      txt md csv tsv json xml yaml yml log ini toml conf
  document  docx pdf html htm rtf odt pptx epub   (requires pandoc on PATH;
            see backends/document_backend.py for the exact failure message
            when it's missing -- deliberately not silently degraded)
  archive   zip tar tgz tar.gz tar.bz2 tbz2
  media     audio: mp3 wav flac aac ogg m4a wma
            video: mp4 mkv webm mov avi flv
            (requires ffmpeg on PATH; see backends/media_backend.py for the
            exact failure message when it's missing)

Each family maps to exactly one backend module, so adding a new family is
"write one backend + add its extensions here" -- never touches the other
backends.
"""

from __future__ import annotations

from typing import Optional

IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "bmp", "gif", "tiff", "tif", "ico", "ppm", "pcx", "tga"}
TEXT_EXTS = {"txt", "md", "csv", "tsv", "json", "xml", "yaml", "yml", "log", "ini", "toml", "conf"}
DOCUMENT_EXTS = {"docx", "pdf", "html", "htm", "rtf", "odt", "md", "pptx", "epub"}
ARCHIVE_EXTS = {"zip", "tar", "tgz", "tar.gz", "tar.bz2", "tbz2"}
AUDIO_EXTS = {"mp3", "wav", "flac", "aac", "ogg", "m4a", "wma"}
VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "avi", "flv"}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS


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
    if ext in MEDIA_EXTS:
        return "media"
    if ext in ARCHIVE_EXTS:
        return "archive"
    if ext in DOCUMENT_EXTS and ext not in TEXT_EXTS:
        return "document"
    if ext in TEXT_EXTS:
        return "text"
    return None


def _archive_ext(source_ext: str) -> str:
    """Normalizes the two-part tar variants (tar.gz/tar.bz2) that a plain
    Path.suffix lookup can't see on its own -- callers here always pass
    the last suffix only (e.g. "gz" from "notes.tar.gz"), so this maps
    that back onto the compound key the ARCHIVE_EXTS set actually uses."""
    ext = source_ext.lower().lstrip(".")
    if ext in ("gz",):
        return "tar.gz"
    if ext in ("bz2",):
        return "tar.bz2"
    return ext


def backend_for(source_ext: str, operation: str, target_ext: Optional[str] = None):
    """Returns the backend module to handle this request, or raises
    UnsupportedFormatError with a specific, actionable message."""
    from .backends import image_backend, text_backend, document_backend, archive_backend, media_backend

    source_ext = source_ext.lower().lstrip(".")
    target_ext = target_ext.lower().lstrip(".") if target_ext else None

    if operation in ("resize",) and source_ext in IMAGE_EXTS:
        return image_backend

    if operation == "compress":
        if source_ext in IMAGE_EXTS:
            return image_backend
        if source_ext in MEDIA_EXTS:
            return media_backend
        return archive_backend

    if operation == "extract":
        norm = _archive_ext(source_ext)
        if norm in ARCHIVE_EXTS or source_ext in ARCHIVE_EXTS:
            return archive_backend
        raise UnsupportedFormatError(
            f"'.{source_ext}' isn't an archive format I can extract."
        )

    if operation == "convert":
        # Image -> image is unambiguous and the common case ("shrink this
        # image" is really resize; "turn this png into a jpg" is convert).
        if source_ext in IMAGE_EXTS and (target_ext is None or target_ext in IMAGE_EXTS):
            return image_backend

        # Audio/video <-> audio/video (mp4 -> mp3, mov -> mp4, etc.) --
        # checked before the text/document fallbacks below since a media
        # extension never overlaps either of those sets.
        if source_ext in MEDIA_EXTS and (target_ext is None or target_ext in MEDIA_EXTS):
            return media_backend

        # Prefer the plain text backend for anything it can round-trip
        # (json/csv/txt/md/ini/etc <-> each other) before falling back to
        # pandoc for genuinely rich documents (docx/pdf/odt/rtf/pptx/epub).
        if source_ext in TEXT_EXTS and (target_ext is None or target_ext in TEXT_EXTS):
            return text_backend

        if source_ext in DOCUMENT_EXTS or (target_ext and target_ext in DOCUMENT_EXTS):
            return document_backend

        norm_source = _archive_ext(source_ext)
        norm_target = _archive_ext(target_ext) if target_ext else None
        if norm_source in ARCHIVE_EXTS or (norm_target and norm_target in ARCHIVE_EXTS):
            return archive_backend

    raise UnsupportedFormatError(
        f"I don't have a converter for '.{source_ext}'"
        + (f" to '.{target_ext}'" if target_ext else "")
        + " yet."
    )
