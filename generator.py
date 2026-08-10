"""
generator.py — file CONTENT generation, deliberately isolated from the rest
of TOKI's command pipeline.

This is NOT part of the classify -> extract_slots -> template.format() ->
execute path that every other intent goes through. It's a different kind of
capability and is kept structurally separate on purpose:

  - Every other intent: model picks a word from a closed list -> Python
    regex pulls values straight from the user's own text -> Python fills a
    PowerShell template -> Python executes it. The model NEVER produces a
    value that ends up inside a command string.

  - GENERATE_FILE: the model writes free-text content (e.g. actual Python
    code for a calculator). That content is real generation — there's no
    closed vocabulary "a working calculator program" could be picked from,
    so pretending this fits the classify-only model would be dishonest.

The one rule that keeps this safe despite being real generation: the
generated text is written to disk as PLAIN DATA via Python's own file I/O
(open().write()), and is NEVER concatenated into a PowerShell command
string. It doesn't matter what the model outputs -- quotes, `$variables`,
backticks, pipes, anything -- none of it is ever parsed as command syntax,
because it never passes through a shell. Writing the file and running the
file are two separate actions; running it later goes through the normal
OPEN_ITEM-style sandboxed path, same as any other file.

Streaming: unlike the classify calls (which use "format": <schema> and are
NOT streamed, since a schema-constrained call needs to validate the whole
JSON object and doesn't give you clean incremental output), this call has
no schema at all -- it's asking for plain text -- so real token-by-token
streaming works cleanly here. This is the one place in the app where
streaming both makes sense and is worth the UI wiring.
"""

import json
import ntpath
import re
import requests
from typing import Callable, Optional

from extractor import is_within_sandbox, resolve_path


_SYSTEM_PROMPT = (
    "You are TOKI's file-writing mode. The user wants a NEW file created "
    "with specific content (e.g. a script, a small program, a document).\n\n"
    "Output ONLY the raw file content -- no markdown fences, no explanation, "
    "no commentary before or after. Just the file's contents, exactly as they "
    "should be written to disk."
)

# Reasonable default extensions for common asks, so "make me a python
# calculator" doesn't need the user to also specify a filename/extension.
_LANGUAGE_EXTENSION_HINTS = [
    (r"\bpython\b", ".py"),
    (r"\bjavascript\b|\bjs\b", ".js"),
    (r"\bhtml\b", ".html"),
    (r"\bbatch\b|\.bat\b", ".bat"),
    (r"\bpowershell\b", ".ps1"),
    (r"\bjson\b", ".json"),
    (r"\bmarkdown\b", ".md"),
]

_NAME_RE = re.compile(
    r"\bcalled\s+([\w .\-]+?)(?:\s+that|\s+which|\s+with|\s+in|\s+about|\s+for|$)"
    r"|\bnamed\s+([\w .\-]+?)(?:\s+that|\s+which|\s+with|\s+in|\s+about|\s+for|$)",
    re.IGNORECASE,
)


def infer_filename(user_text: str) -> str:
    """
    Pick a sandboxed filename for the generated content. Falls back to a
    generic name + inferred extension if the user didn't give one -- this
    function only ever produces a *name*, never file content, so there's
    nothing here that needs model involvement.
    """
    m = _NAME_RE.search(user_text)
    base = None
    if m:
        base = (m.group(1) or m.group(2)).strip().rstrip(".,!")

    ext = ".txt"
    for pattern, candidate_ext in _LANGUAGE_EXTENSION_HINTS:
        if re.search(pattern, user_text, re.IGNORECASE):
            ext = candidate_ext
            break

    if not base:
        base = "generated_file"

    if "." not in ntpath.basename(base):
        base += ext

    return base


class FileGenerator:
    """
    Handles the GENERATE_FILE intent end-to-end: streams generated content
    from Ollama (plain text, no schema) and writes it to a sandboxed path
    via plain Python file I/O -- never through a PowerShell command string.
    """

    def __init__(self, model_name: str = "phi4-mini", base_url: str = "http://localhost:11434"):
        self.model_name = model_name
        self.base_url = base_url
        self._session: Optional[requests.Session] = None
        self._cancelled = False

    def cancel(self):
        """Same Stop-button contract as OllamaRouter.cancel()."""
        self._cancelled = True
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass

    def generate_and_save(
        self,
        user_prompt: str,
        on_token: Callable[[str], None],
        on_done: Callable[[Optional[str], Optional[str]], None],
    ) -> None:
        """
        on_token(text_chunk) is called for every streamed piece of content,
        so the UI can show it appearing live -- same idea as executor.py's
        on_output callback for PowerShell lines, just token-by-token instead
        of line-by-line.

        on_done(saved_path, error) is called exactly once at the end:
          - (path, None)  on success
          - (None, error) if generation failed or the resolved path fell
            outside the sandbox (D:\\ / Desktop) -- in which case NOTHING
            is written to disk.
        """
        self._cancelled = False
        filename = infer_filename(user_prompt)
        path = resolve_path(filename)
        if path is None:
            on_done(None, "That filename would fall outside the sandbox (D:\\ or Desktop only) -- not writing anything.")
            return

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        self._session = requests.Session()
        full_text = ""
        try:
            resp = self._session.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model_name,
                    "messages": messages,
                    "stream": True,   # plain text, no schema -- safe to stream
                    "options": {"temperature": 0.2},
                    "keep_alive": "30m",  # see orchestrator.py's OllamaRouter._call() for why
                },
                timeout=120,
                stream=True,
            )
            resp.raise_for_status()

            for raw_line in resp.iter_lines():
                if self._cancelled:
                    on_done(None, "Stopped.")
                    return
                if not raw_line:
                    continue
                try:
                    chunk = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                token = chunk.get("message", {}).get("content", "")
                if token:
                    full_text += token
                    on_token(token)
                if chunk.get("done"):
                    break

        except requests.exceptions.ConnectionError:
            on_done(None, "Can't reach Ollama -- is it running on localhost:11434?")
            return
        except requests.exceptions.Timeout:
            on_done(None, "Ollama timed out while generating.")
            return
        except Exception as e:
            if self._cancelled:
                on_done(None, "Stopped.")
            else:
                on_done(None, f"Unexpected error: {e}")
            return
        finally:
            self._session = None

        if self._cancelled:
            on_done(None, "Stopped.")
            return

        if not full_text.strip():
            on_done(None, "Model produced no content -- nothing was written.")
            return

        # The one rule that matters: full_text is written as plain data via
        # Python's own file write. It is NEVER interpolated into a
        # PowerShell command string, so nothing in the generated content --
        # quotes, $variables, backticks, pipes -- can be parsed as command
        # syntax. Re-check the sandbox right before writing too, in case
        # get_sandbox_roots()'s cached Desktop path changed mid-session.
        if not is_within_sandbox(path):
            on_done(None, "Resolved path fell outside the sandbox at write time -- not writing anything.")
            return

        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(full_text)
        except OSError as e:
            on_done(None, f"Couldn't write the file: {e}")
            return

        on_done(path, None)
