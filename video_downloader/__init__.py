"""
video_downloader — wraps yt-dlp (the community-maintained, actively
updated fork of youtube-dl -- supports YouTube plus roughly a thousand
other sites) to save a video to disk from either an explicit URL
(DOWNLOAD_VIDEO_URL) or a URL captured from the focused browser's address
bar (DOWNLOAD_PLAYING_VIDEO, via now_playing.get_now_playing_url()).

Same posture as this project's other external-tool wrappers:
  - document_backend.py names the exact missing tool (pandoc) rather than
    failing silently; this does the same for yt-dlp (missing package) and
    ffmpeg (missing binary, only needed for audio-only extraction or
    merging separately-served video+audio streams).
  - generator.py writes files via plain I/O, never through a shell; here,
    yt-dlp is used as a real Python library import (yt_dlp.YoutubeDL),
    never invoked as a subprocess.
  - Every path this writes to comes from extractor.py's sandbox helpers
    (get_sandbox_roots()) -- downloads land in a fixed "TOKI Downloads"
    folder on the real Desktop by default, never an arbitrary location
    implied by the user's phrasing or a website's own suggested filename.

USAGE
----------------------------------------------------------------------------
    from video_downloader import download_video

    download_video("https://www.youtube.com/watch?v=...")
    download_video("https://...", audio_only=True)   # -> saved as .mp3

Raises one of:
    InvalidUrlError      -- what was passed isn't an http(s) link
    ImportError           -- yt-dlp isn't installed
    FfmpegNotFoundError   -- ffmpeg isn't installed/on PATH
    (a yt-dlp-raised exception, e.g. "Video unavailable")

apis.py's VideoDownloadAPI is responsible for catching these and turning
them into the short, friendly sentences the rest of TOKI's API layer
already returns (see apis.py's FAILURE_PREFIXES convention) -- this
package itself never prints or narrates, it just does the work or raises
a clear, specific reason it couldn't, exactly like conversion_engine.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Optional

_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Subfolder name, not the root itself -- keeps downloaded video files
# visually separate from anything else the user has on their Desktop or
# in D:\, and gives GENERATE_FILE-style output a predictable single place
# to look, mirroring how conversion_engine's outputs sit right next to
# their source rather than scattered.
DOWNLOAD_SUBFOLDER = "TOKI Downloads"


class InvalidUrlError(ValueError):
    """Raised when the given string isn't an http(s) URL -- never guessed
    or partially trusted; the caller asks instead."""


class FfmpegNotFoundError(RuntimeError):
    """Raised when ffmpeg is required (audio-only extraction, or merging
    separately-served video+audio streams) but isn't on PATH."""


def _default_destination() -> Path:
    from extractor import get_desktop_root

    desktop = Path(get_desktop_root())
    dest = desktop / DOWNLOAD_SUBFOLDER
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise FfmpegNotFoundError(
            "Downloading video needs ffmpeg, which isn't installed on "
            "this machine (or isn't on PATH). Install it from ffmpeg.org, "
            "then try again."
        )


def download_video(
    url: str,
    destination: Optional[str] = None,
    audio_only: bool = False,
    overwrite: bool = False,
) -> str:
    """Downloads `url` via yt-dlp. Returns the saved file's path.

    destination: a folder (not a filename) to save into. Defaults to the
    sandboxed "Desktop/TOKI Downloads" folder -- see _default_destination().
    If given explicitly, it is used as-is (this project's other sandboxed
    paths are validated by resolve_path() at the extractor layer; this
    function itself, like conversion_engine's, trusts a caller-supplied
    absolute path and only ever picks its OWN default from the sandbox).

    audio_only: extracts and saves just the audio track as an mp3,
    instead of the full video.

    overwrite: if False (default), yt-dlp will not clobber a file that
    already has the exact same title+id -- same "never silently
    overwrite" posture as conversion_engine's backends.
    """
    url = (url or "").strip()
    if not _URL_RE.match(url):
        raise InvalidUrlError(f"'{url}' doesn't look like a video link.")

    try:
        import yt_dlp
    except ImportError:
        raise ImportError(
            "Downloading video needs the 'yt-dlp' package, which isn't "
            "installed. Add 'yt-dlp' to requirements.txt to enable this."
        )

    # ffmpeg is needed both for audio-only extraction AND for merging a
    # separately-served best-video + best-audio pair, which is the common
    # case for a real "best quality" download on most sites today -- so
    # it's required either way, not just for the audio_only branch.
    _require_ffmpeg()

    dest_dir = Path(destination) if destination else _default_destination()
    dest_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "outtmpl": str(dest_dir / "%(title).150B [%(id)s].%(ext)s"),
        # "download this video" means exactly the one video the user is
        # pointing at, never a whole playlist it happens to sit inside --
        # same "do exactly what was literally asked, never an inferred
        # extra step" posture command chaining's segment cap already
        # enforces elsewhere in this project.
        "noplaylist": True,
        "overwrites": overwrite,
        "quiet": True,
        "no_warnings": True,
    }

    if audio_only:
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["format"] = "bestvideo*+bestaudio/best"
        ydl_opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        out_path = Path(ydl.prepare_filename(info))
        if audio_only:
            out_path = out_path.with_suffix(".mp3")
        elif ydl_opts.get("merge_output_format"):
            out_path = out_path.with_suffix(".mp4")

    return str(out_path)


__all__ = ["download_video", "InvalidUrlError", "FfmpegNotFoundError", "DOWNLOAD_SUBFOLDER"]
