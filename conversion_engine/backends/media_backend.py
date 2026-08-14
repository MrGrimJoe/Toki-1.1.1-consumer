"""
media_backend.py — audio/video convert + compress via ffmpeg.

WHY FFMPEG, AND WHY NOT SILENTLY WORK AROUND ITS ABSENCE
----------------------------------------------------------------------------
Same reasoning as document_backend.py's pandoc dependency: there is no
small stdlib-only way to decode/re-encode audio or video correctly. A
hand-rolled container remux might work for a few lucky codec pairs and
silently produce a corrupt or audio-less file for everything else --
exactly the kind of quiet-wrong-output this project's whole design
philosophy ("a miss is a clear no, never a guess") exists to rule out.

So: this is a thin wrapper around the `ffmpeg` binary, checked explicitly
before use. If it can't be found (bundled or on PATH), this raises a
specific, actionable error -- "ffmpeg isn't installed" -- rather than
attempting a fake conversion. Mirrors document_backend.py's
BUNDLED_DIR-then-PATH lookup order and executor.py's existing precedent
of shelling out to a real external tool rather than reimplementing
codec/container behavior in Python.

INSTALLER NOTE: same story as pandoc -- ffmpeg ships as a single
self-contained executable with no installer footprint, so it can be
dropped into this app's own tree (bin/ffmpeg/) exactly like pandoc is,
whenever that installer work happens. Until then this falls back to
PATH, which is what a dev machine with ffmpeg already installed uses.

SCOPE
----------------------------------------------------------------------------
- convert(): container/codec change (mp4 -> mp3, mov -> mp4, wav -> flac,
  etc). Audio-only source -> video target is rejected with a clear
  message rather than producing a video with a black/blank frame the
  user never asked for.
- compress(): re-encodes at a lower bitrate/CRF to shrink the file --
  video uses libx264 CRF (higher CRF = smaller/lower quality, same
  "quality knob" shape image_backend.compress() already uses for JPEG),
  audio uses a lower target bitrate.
- No resize()/extract() here -- video "resize" (resolution change) and
  archive-style extraction aren't part of this pass; registry.py never
  routes either operation to this module.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from ..registry import AUDIO_EXTS, VIDEO_EXTS, UnsupportedFormatError

BUNDLED_DIR = Path(__file__).resolve().parents[2] / "bin" / "ffmpeg"

# CRF: 0 = lossless, 51 = worst. 23 is libx264's own default ("visually
# near-lossless, no meaningful size win over the source"); higher values
# below are deliberately larger jumps so "compress this a lot" actually
# feels like a lot, mirroring _extract_compress_quality()'s three-tier
# shape in extractor.py.
_DEFAULT_VIDEO_CRF = 28
_DEFAULT_AUDIO_BITRATE = "128k"


class FfmpegNotFoundError(RuntimeError):
    pass


def _bundled_ffmpeg_path() -> Path:
    name = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"
    return BUNDLED_DIR / name


def _require_ffmpeg() -> str:
    bundled = _bundled_ffmpeg_path()
    if bundled.is_file():
        return str(bundled)

    exe = shutil.which("ffmpeg")
    if not exe:
        raise FfmpegNotFoundError(
            "Converting this file needs ffmpeg, which isn't installed on "
            "this machine (or isn't on PATH). Install it from ffmpeg.org, "
            "then try again."
        )
    return exe


def _output_path(source: Path, target_ext: str, suffix: str, overwrite: bool) -> Path:
    if overwrite:
        return source.with_suffix(f".{target_ext}")
    return source.with_name(f"{source.stem}{suffix}.{target_ext}")


def _run_ffmpeg(args: list) -> None:
    result = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        # ffmpeg's stderr is verbose by design (codec probing, banner,
        # progress) -- take only the last real line, which is almost
        # always the actual error, rather than dumping the whole thing.
        lines = [ln for ln in result.stderr.strip().splitlines() if ln.strip()]
        reason = lines[-1] if lines else "unknown ffmpeg error"
        raise RuntimeError(f"ffmpeg conversion failed: {reason}")


def convert(source_path: str, target_ext: str, overwrite: bool = False) -> str:
    source = Path(source_path)
    source_ext = source.suffix.lower().lstrip(".")
    target_ext = target_ext.lower().lstrip(".")

    if source_ext in AUDIO_EXTS and target_ext in VIDEO_EXTS:
        raise UnsupportedFormatError(
            f"Can't convert an audio file ('.{source_ext}') to a video "
            f"format ('.{target_ext}') -- there's no picture to put in it."
        )

    ffmpeg = _require_ffmpeg()
    out_path = _output_path(source, target_ext, "_converted", overwrite)

    args = [ffmpeg, "-y", "-i", str(source)]
    if target_ext in AUDIO_EXTS and source_ext in VIDEO_EXTS:
        # Dropping the video stream entirely when going video -> audio,
        # rather than letting ffmpeg guess/attach a blank picture track.
        args += ["-vn"]
    args += [str(out_path)]

    _run_ffmpeg(args)
    return str(out_path)


def compress(source_path: str, quality: Optional[int] = None, overwrite: bool = False) -> str:
    """quality mirrors image_backend.compress()'s convention: a 0-100
    "how much to shrink" hint from extractor.py's _extract_compress_quality
    (30 = a lot smaller, 60 = default/unspecified, 80 = slightly). Mapped
    onto ffmpeg's own CRF (video) / bitrate (audio) scales below rather
    than passed straight through, since those are inverted and
    differently-scaled from a 0-100 "quality" percentage."""
    source = Path(source_path)
    source_ext = source.suffix.lower().lstrip(".")
    ffmpeg = _require_ffmpeg()
    out_path = _output_path(source, source_ext, "_compressed", overwrite)

    q = quality if quality is not None else 60
    if source_ext in VIDEO_EXTS:
        # Lower "quality" hint (a-lot-smaller) -> HIGHER crf (worse/smaller).
        crf = max(18, min(40, _DEFAULT_VIDEO_CRF + round((60 - q) / 4)))
        args = [
            ffmpeg, "-y", "-i", str(source),
            "-vcodec", "libx264", "-crf", str(crf),
            "-preset", "medium", "-acodec", "aac",
            str(out_path),
        ]
    elif source_ext in AUDIO_EXTS:
        bitrate_kbps = max(64, min(192, round(64 + (q / 100) * 128)))
        args = [
            ffmpeg, "-y", "-i", str(source),
            "-b:a", f"{bitrate_kbps}k",
            str(out_path),
        ]
    else:
        raise UnsupportedFormatError(
            f"'.{source_ext}' isn't an audio/video format this backend compresses."
        )

    _run_ffmpeg(args)
    return str(out_path)
