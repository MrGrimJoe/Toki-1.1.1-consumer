"""
image_backend.py — Pillow-based convert / resize / compress for images.

Covers the two example prompts from the feature request directly:
  "turn the file I'm selecting into a text file"  -> NOT this backend
      (that's a text/document conversion; see text_backend.py)
  "this image is too big, can you shrink it"       -> resize(), see below

DESIGN NOTES
----------------------------------------------------------------------------
- Output is written NEXT TO the original with a suffix by default
  (photo.jpg -> photo_resized.jpg), never overwriting in place. This is
  the "confirmation before overwrite" behavior flagged in the original
  design discussion -- a silent overwrite of someone's only copy of a
  photo is the fastest way to lose trust in a "just trust me" assistant.
  Callers (apis.py) can pass overwrite=True if the user explicitly says
  "replace it" / "overwrite it".
- RGBA -> JPEG is handled explicitly (JPEG has no alpha channel) by
  flattening onto white, rather than letting Pillow raise or silently
  produce a corrupt file.
- "shrink it" with no explicit target size defaults to a 50% scale --
  a reasonable, stated default rather than silently picking something
  the user can't predict.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image

DEFAULT_SHRINK_SCALE = 0.5
DEFAULT_JPEG_QUALITY = 85


def _output_path(source: Path, new_ext: Optional[str], suffix: str, overwrite: bool) -> Path:
    if overwrite:
        if new_ext:
            return source.with_suffix(f".{new_ext.lower()}")
        return source
    ext = f".{new_ext.lower()}" if new_ext else source.suffix
    return source.with_name(f"{source.stem}{suffix}{ext}")


def _flatten_for_jpeg(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return img.convert("RGB")


def convert(source_path: str, target_ext: str, overwrite: bool = False) -> str:
    """Change container format only (e.g. png -> webp). Returns the new
    file's path."""
    source = Path(source_path)
    target_ext = target_ext.lower().lstrip(".")
    out_path = _output_path(source, target_ext, "", overwrite)

    with Image.open(source) as img:
        img.load()
        if target_ext in ("jpg", "jpeg"):
            img = _flatten_for_jpeg(img)
            img.save(out_path, format="JPEG", quality=DEFAULT_JPEG_QUALITY)
        else:
            img.save(out_path)

    return str(out_path)


def resize(
    source_path: str,
    width: Optional[int] = None,
    height: Optional[int] = None,
    scale: Optional[float] = None,
    overwrite: bool = False,
) -> str:
    """Resize an image. Priority: explicit width/height > scale factor >
    DEFAULT_SHRINK_SCALE. Aspect ratio is preserved when only one of
    width/height is given."""
    source = Path(source_path)

    with Image.open(source) as img:
        img.load()
        orig_w, orig_h = img.size

        if width and height:
            new_size = (width, height)
        elif width:
            new_size = (width, round(orig_h * (width / orig_w)))
        elif height:
            new_size = (round(orig_w * (height / orig_h)), height)
        else:
            factor = scale if scale else DEFAULT_SHRINK_SCALE
            new_size = (max(1, round(orig_w * factor)), max(1, round(orig_h * factor)))

        resized = img.resize(new_size, Image.LANCZOS)
        out_path = _output_path(source, None, "_resized", overwrite)

        if source.suffix.lower() in (".jpg", ".jpeg"):
            resized = _flatten_for_jpeg(resized)
            resized.save(out_path, format="JPEG", quality=DEFAULT_JPEG_QUALITY)
        else:
            resized.save(out_path)

    return str(out_path)


def compress(source_path: str, quality: int = 60, overwrite: bool = False) -> str:
    """Re-encode to reduce file size. For JPEG this lowers quality; for
    PNG this re-saves with max compression (PNG is lossless, so "quality"
    doesn't apply the same way -- documented rather than silently
    ignored)."""
    source = Path(source_path)
    ext = source.suffix.lower().lstrip(".")

    with Image.open(source) as img:
        img.load()
        out_path = _output_path(source, None, "_compressed", overwrite)

        if ext in ("jpg", "jpeg"):
            flattened = _flatten_for_jpeg(img)
            flattened.save(out_path, format="JPEG", quality=quality, optimize=True)
        elif ext == "png":
            img.save(out_path, format="PNG", optimize=True, compress_level=9)
        elif ext == "webp":
            img.save(out_path, format="WEBP", quality=quality)
        else:
            # No lossy compression path for this format -- re-save with
            # whatever optimization Pillow supports rather than pretending
            # this shrank the file when it may not have.
            img.save(out_path, optimize=True)

    return str(out_path)
