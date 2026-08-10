"""
document_backend.py — docx / pdf / html / rtf / odt conversions via pandoc.

WHY PANDOC, AND WHY NOT SILENTLY WORK AROUND ITS ABSENCE
----------------------------------------------------------------------------
docx/pdf/rtf/odt are real binary-ish container formats with layout,
styles, and (for pdf) no editable text model at all. There is no small
stdlib-only way to do this correctly -- any hand-rolled "docx -> pdf"
using pure Python would silently drop formatting and fail on anything
non-trivial, which is exactly the kind of silent-wrong-output this
project's whole design philosophy (graph-first, deterministic, "a miss is
a clear no, never a guess") explicitly avoids elsewhere.

So: this is a thin wrapper around the `pandoc` binary. If it can't be
found (bundled or on PATH), this raises a specific, actionable error --
"pandoc isn't installed" -- rather than trying to fake a conversion.
This mirrors the existing precedent in executor.py of shelling out to a
real external tool (PowerShell) rather than reimplementing OS behavior
in Python.

INSTALLER DECISION (resolved): bundle a portable pandoc.exe rather than
require a separate system install. Pandoc ships as a single ~35-50MB
self-contained .exe with no installer/registry footprint of its own, so
it can just be dropped in the app's own tree -- no MSI, no PATH
mutation, no admin prompt. Pandoc is GPL-2.0-or-later: redistributing
the official prebuilt binary is fine (no source-availability burden
beyond what upstream already provides at github.com/jgm/pandoc), but
this is not legal advice -- worth a final glance at pandoc's license
page before shipping if that matters for your distribution.

_require_pandoc() below checks BUNDLED_DIR (a `bin/pandoc/` folder next
to this file) before falling back to PATH, in that order. This means the
still-outstanding installer work is now just "download pandoc.exe into
bin/pandoc/ as part of the build" -- no further code change needed here
once that's done. Falling back to PATH keeps this working today on a
dev machine that already has pandoc installed system-wide (as this one
does), and keeps it working for a user who installs pandoc themselves
instead of waiting on the bundled installer.

PDF AS A SOURCE FORMAT is intentionally NOT supported for extraction to
docx/text here -- reflowing PDF text reliably needs a different library
(pdfplumber/pymupdf) and is a distinct feature, not a one-line addition
to this backend. Attempting it and getting garbled output would be worse
than a clear "not supported yet".
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

# Where a bundled pandoc binary is expected, if the installer has dropped
# one in place. Checked before PATH -- see the module docstring's
# "INSTALLER DECISION" note. Layout mirrors a typical portable-tool
# convention: bin/pandoc/pandoc.exe (Windows) or bin/pandoc/pandoc
# (anything else, e.g. a dev running this on Linux/macOS).
BUNDLED_DIR = Path(__file__).resolve().parents[2] / "bin" / "pandoc"


class PandocNotFoundError(RuntimeError):
    pass


def _bundled_pandoc_path() -> Path:
    name = "pandoc.exe" if platform.system() == "Windows" else "pandoc"
    return BUNDLED_DIR / name


def _require_pandoc() -> str:
    bundled = _bundled_pandoc_path()
    if bundled.is_file():
        return str(bundled)

    exe = shutil.which("pandoc")
    if not exe:
        raise PandocNotFoundError(
            "Converting this file needs pandoc, which isn't installed on "
            "this machine. Install it from pandoc.org, then try again."
        )
    return exe


def convert(source_path: str, target_ext: str, overwrite: bool = False) -> str:
    source = Path(source_path)
    source_ext = source.suffix.lower().lstrip(".")

    if source_ext == "pdf":
        raise NotImplementedError(
            "Converting FROM pdf isn't supported yet -- reliably pulling "
            "text/layout back out of a pdf needs a different tool than "
            "the one used for the other document formats here."
        )

    pandoc = _require_pandoc()
    target_ext = target_ext.lower().lstrip(".")
    out_path = (
        source.with_suffix(f".{target_ext}")
        if overwrite
        else source.with_name(f"{source.stem}_converted.{target_ext}")
    )

    result = subprocess.run(
        [pandoc, str(source), "-o", str(out_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pandoc conversion failed: {result.stderr.strip()}")

    return str(out_path)
