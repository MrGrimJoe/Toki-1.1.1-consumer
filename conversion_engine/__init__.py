"""
conversion_engine — the format-conversion engine, entry point for
apis.py's FileConvertAPI.

USAGE
----------------------------------------------------------------------------
    from conversion_engine import convert_file, resize_file, compress_file

    convert_file("C:/Users/me/data.json", target_ext="txt")
    resize_file("C:/Users/me/photo.jpg", scale=0.5)
    compress_file("C:/Users/me/photo.jpg")
    compress_file("C:/Users/me/notes/")          # -> notes.zip
    extract_file("C:/Users/me/notes.zip")

Every function returns the output path as a string on success, or raises
one of:
    registry.UnsupportedFormatError   -- no backend for this combination
    FileNotFoundError                 -- source_path doesn't exist
    (a backend-specific error, e.g. document_backend.PandocNotFoundError)

apis.py is responsible for catching these and turning them into the
friendly one-line sentences the rest of TOKI's API layer already returns
(see apis.py's _friendly_request_error / is_api_failure convention) --
this package itself never prints or narrates, it just does the work or
raises a clear, specific reason it couldn't.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .registry import backend_for, family_for_extension, UnsupportedFormatError

__all__ = [
    "convert_file",
    "resize_file",
    "compress_file",
    "extract_file",
    "supported_formats",
    "UnsupportedFormatError",
]


def _check_exists(source_path: str) -> Path:
    p = Path(source_path)
    if not p.exists():
        raise FileNotFoundError(f"'{source_path}' doesn't exist.")
    return p


def convert_file(source_path: str, target_ext: str, overwrite: bool = False) -> str:
    source = _check_exists(source_path)
    source_ext = source.suffix.lstrip(".")
    backend = backend_for(source_ext, "convert", target_ext)
    return backend.convert(str(source), target_ext, overwrite=overwrite)


def resize_file(
    source_path: str,
    width: Optional[int] = None,
    height: Optional[int] = None,
    scale: Optional[float] = None,
    overwrite: bool = False,
) -> str:
    source = _check_exists(source_path)
    source_ext = source.suffix.lstrip(".")
    backend = backend_for(source_ext, "resize")
    return backend.resize(str(source), width=width, height=height, scale=scale, overwrite=overwrite)


def compress_file(source_path: str, quality: int = 60, overwrite: bool = False) -> str:
    source = _check_exists(source_path)
    source_ext = source.suffix.lstrip(".")
    backend = backend_for(source_ext, "compress")
    from .registry import IMAGE_EXTS, MEDIA_EXTS

    if source.is_dir() or (source_ext not in IMAGE_EXTS and source_ext not in MEDIA_EXTS):
        # Non-image, non-media sources (or whole folders) get zipped, not
        # re-encoded -- there's no meaningful "quality" knob for e.g. a
        # .txt file, so archive_backend's compress() (bundled below) is
        # the only sane fallback.
        return backend.compress(str(source), overwrite=overwrite)
    return backend.compress(str(source), quality=quality, overwrite=overwrite)


def extract_file(source_path: str, destination: Optional[str] = None) -> str:
    source = _check_exists(source_path)
    source_ext = source.suffix.lstrip(".")
    backend = backend_for(source_ext, "extract")
    return backend.extract(str(source), destination=destination)


def supported_formats() -> dict:
    """Small introspection helper -- lets the UI or a help command answer
    "what can you convert" without duplicating the registry's format
    lists by hand."""
    from . import registry
    return {
        "image": sorted(registry.IMAGE_EXTS),
        "text": sorted(registry.TEXT_EXTS),
        "document": sorted(registry.DOCUMENT_EXTS),
        "archive": sorted(registry.ARCHIVE_EXTS),
        "audio": sorted(registry.AUDIO_EXTS),
        "video": sorted(registry.VIDEO_EXTS),
    }
