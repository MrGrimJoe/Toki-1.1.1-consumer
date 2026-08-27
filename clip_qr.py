"""
clip_qr.py — SAVE_CLIPBOARD_TO_FILE / GENERATE_QR_CODE / SCAN_QR_CODE.

Three small, independent api-kind capabilities kept in one file because
each is a handful of lines and none deserves its own package the way
conversion_engine/ or video_downloader/ do. Same conventions as apis.py's
other classes: every public method takes plain string slots and returns a
short human-readable sentence -- never a raw path, never a traceback.

SAVE_CLIPBOARD_TO_FILE — reads the current clipboard text via the same
`Get-Clipboard` PowerShell call GET_CLIPBOARD already uses (no new
dependency), and writes it to a real file via plain Python file I/O — same
"never through a shell" posture as generator.py: whatever's on the
clipboard (quotes, $variables, backticks) is never parsed as command
syntax, only ever written as plain text.

GENERATE_QR_CODE / SCAN_QR_CODE — wrap the `qrcode` (generation, pure
Python + Pillow, already a dependency) and `pyzbar` (decoding; its Windows
wheel bundles the zbar DLLs, so no separate system install) packages.
Both are optional, same "name the exact missing tool" posture as
document_backend.py's pandoc / media_backend.py's ffmpeg: a missing
package returns a specific pip-install message, not a traceback.
GENERATE_QR_CODE falls back to the current clipboard contents when no
explicit content is given in the request — "turn this into a QR code"
almost always means whatever was just copied. SCAN_QR_CODE reads the
currently selected file the exact same way FileConvertAPI does
(selection_context.get_selection_context()) — a QR code to scan is always
a file the user is pointing at, never a path guessed from their text.
"""

import os
import subprocess
import time
from typing import Optional


def _default_output_dir() -> str:
    # Same convention as TAKE_SCREENSHOT / video_downloader's default
    # destination -- the real Desktop, resolved via get_sandbox_roots()
    # (Desktop is always the last entry -- see extractor.get_sandbox_roots()).
    # Imported locally (not at module top), same convention file_grouping.py/
    # video_downloader/__init__.py already use, so tests can monkeypatch
    # extractor.get_sandbox_roots and have it actually take effect here --
    # a top-level `from extractor import get_sandbox_roots` would bind a
    # stale reference that monkeypatching extractor's own attribute never
    # reaches.
    from extractor import get_sandbox_roots
    return get_sandbox_roots()[-1]


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _read_clipboard_text() -> Optional[str]:
    """Shared by ClipboardFileAPI and QrCodeAPI's fallback -- returns None
    on any read failure (missing PowerShell, timeout), '' for a genuinely
    empty clipboard, or the text otherwise. Callers distinguish those
    cases themselves rather than this function picking one error message
    for both."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    text = result.stdout
    return text.rstrip("\r\n") if text is not None else ""


class ClipboardFileAPI:
    def save_clipboard_to_file(self, filename: str = "", extension: str = "") -> str:
        text = _read_clipboard_text()
        if text is None:
            return "Couldn't read the clipboard."
        if not text:
            return "The clipboard is empty -- nothing to save."

        ext = (extension or "md").lstrip(".").strip() or "md"
        name = (filename or f"clipboard_{_timestamp()}").strip()
        if not name.lower().endswith(f".{ext.lower()}"):
            name = f"{name}.{ext}"

        path = os.path.join(_default_output_dir(), name)
        from extractor import is_within_sandbox
        if not is_within_sandbox(path):
            return "That would write outside the sandbox -- not saving."
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            return f"Couldn't write the file: {e}"
        return f"Saved the clipboard to {path}"


class QrCodeAPI:
    def generate_qr_code(self, content: str = "", filename: str = "") -> str:
        if not content:
            clip = _read_clipboard_text()
            content = clip or ""
        if not content:
            return "I need something to encode -- what should the QR code say/link to?"

        try:
            import qrcode
        except ImportError:
            return "QR code generation isn't available -- run: pip install qrcode[pil]"

        name = (filename or f"qrcode_{_timestamp()}").strip()
        if not name.lower().endswith(".png"):
            name = f"{name}.png"
        path = os.path.join(_default_output_dir(), name)
        from extractor import is_within_sandbox
        if not is_within_sandbox(path):
            return "That would write outside the sandbox -- not saving."

        try:
            img = qrcode.make(content)
            img.save(path)
        except Exception as e:
            return f"Couldn't generate the QR code: {e}"
        return f"Saved a QR code for that to {path}"

    def _current_selection_path(self) -> Optional[str]:
        from selection_context import get_selection_context
        sel = get_selection_context().get_selected()
        return sel["path"] if sel else None

    def scan_qr_code(self) -> str:
        path = self._current_selection_path()
        if not path:
            return "Nothing is selected right now -- drag the image with the QR code onto TOKI first."

        try:
            from pyzbar.pyzbar import decode
        except ImportError:
            return "QR code scanning isn't available -- run: pip install pyzbar"

        try:
            from PIL import Image
            img = Image.open(path)
        except Exception as e:
            return f"Couldn't open that image: {e}"

        try:
            results = decode(img)
        except Exception as e:
            return f"Couldn't scan that image: {e}"

        if not results:
            return "Didn't find a QR code in that image."
        values = [r.data.decode("utf-8", errors="replace") for r in results]
        if len(values) == 1:
            return f"QR code says: {values[0]}"
        joined = "; ".join(f"{i + 1}) {v}" for i, v in enumerate(values))
        return f"Found {len(values)} QR codes: {joined}"
