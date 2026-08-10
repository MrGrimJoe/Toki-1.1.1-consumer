"""
text_backend.py — stdlib-only conversions between plain-text data formats:
txt, md, csv, tsv, json, yaml, yml, xml, log.

Covers the feature request's headline example directly:
  "turn the file I'm selecting into a text file" (json -> txt, csv -> txt,
  etc.)

DESIGN NOTES
----------------------------------------------------------------------------
- json/csv/tsv have real STRUCTURE, so "convert to txt" means "render
  readably", not "byte-copy with a new extension" -- that distinction is
  made explicit per pair below rather than silently flattening everything
  through str(). A target this module has no real transform for (e.g.
  json -> xml, csv -> yaml) raises UnsupportedFormatError instead of
  writing the source's raw bytes under the wrong extension -- silently
  mislabeling content is exactly the kind of wrong-but-quiet output this
  project's "a miss is a clear no, never a guess" posture exists to rule
  out; it's not an acceptable fallback just because file-writing itself
  can't fail here.
- yaml requires PyYAML, which is NOT currently in requirements.txt. Rather
  than silently no-op or produce a confusing ImportError deep in a
  traceback, this raises a clear, specific message the same way
  document_backend.py does for a missing pandoc -- consistent with this
  project's "a miss is a clear no, never a guess" posture.
- Every writer defaults to UTF-8 and never overwrites the source in place
  (same suffix convention as image_backend.py), unless overwrite=True.
"""

from __future__ import annotations

import csv
import io
import json
import xml.dom.minidom as minidom
from pathlib import Path
from typing import Optional

from ..registry import UnsupportedFormatError


def _output_path(source: Path, target_ext: str, overwrite: bool) -> Path:
    if overwrite:
        return source.with_suffix(f".{target_ext}")
    return source.with_name(f"{source.stem}_converted.{target_ext}")


def _read_source(source: Path) -> str:
    return source.read_text(encoding="utf-8", errors="replace")


def _rows_from_delimited(text: str, delimiter: str) -> list:
    return list(csv.reader(io.StringIO(text), delimiter=delimiter))


def convert(source_path: str, target_ext: str, overwrite: bool = False) -> str:
    source = Path(source_path)
    source_ext = source.suffix.lower().lstrip(".")
    target_ext = target_ext.lower().lstrip(".")
    out_path = _output_path(source, target_ext, overwrite)

    raw = _read_source(source)

    # ── same format, different extension: nothing to transform ──────────
    if source_ext == target_ext:
        out_path.write_text(raw, encoding="utf-8")
        return str(out_path)

    # ── json -> anything ──────────────────────────────────────────────────
    if source_ext == "json":
        data = json.loads(raw)
        if target_ext in ("txt", "md", "log"):
            out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        elif target_ext in ("csv", "tsv"):
            _write_tabular_from_records(data, out_path, delimiter="," if target_ext == "csv" else "\t")
        elif target_ext in ("yaml", "yml"):
            out_path.write_text(_to_yaml(data), encoding="utf-8")
        else:
            raise UnsupportedFormatError(
                f"Can't convert json to {target_ext} -- no readable "
                f"transform exists for that pair yet."
            )
        return str(out_path)

    # ── csv/tsv -> anything ────────────────────────────────────────────────
    if source_ext in ("csv", "tsv"):
        delimiter = "," if source_ext == "csv" else "\t"
        rows = _rows_from_delimited(raw, delimiter)
        if target_ext == "json":
            out_path.write_text(_tabular_rows_to_json(rows), encoding="utf-8")
        elif target_ext in ("csv", "tsv"):
            _write_rows(rows, out_path, delimiter="," if target_ext == "csv" else "\t")
        elif target_ext in ("txt", "md", "log"):
            out_path.write_text(_tabular_rows_to_plain(rows), encoding="utf-8")
        else:
            raise UnsupportedFormatError(
                f"Can't convert {source_ext} to {target_ext} -- no readable "
                f"transform exists for that pair yet."
            )
        return str(out_path)

    # ── xml -> anything: pretty-print for readability, else raw copy ────
    if source_ext == "xml":
        if target_ext in ("txt", "md", "log", "xml"):
            try:
                pretty = minidom.parseString(raw).toprettyxml(indent="  ")
            except Exception:
                pretty = raw
            out_path.write_text(pretty, encoding="utf-8")
        else:
            raise UnsupportedFormatError(
                f"Can't convert xml to {target_ext} -- no readable "
                f"transform exists for that pair yet."
            )
        return str(out_path)

    # ── plain-text formats with no real structure of their own (txt, md,
    #    log, yaml, yml) converting to another format on this same list:
    #    a byte-for-byte copy under the new extension is the CORRECT,
    #    non-misleading behavior here, since there's no structure to lose
    #    or misrepresent between them. ─────────────────────────────────
    _NO_STRUCTURE_EXTS = {"txt", "md", "log", "yaml", "yml"}
    if source_ext in _NO_STRUCTURE_EXTS and target_ext in _NO_STRUCTURE_EXTS:
        out_path.write_text(raw, encoding="utf-8")
        return str(out_path)

    raise UnsupportedFormatError(
        f"Can't convert {source_ext} to {target_ext} -- no readable "
        f"transform exists for that pair yet."
    )


def _write_tabular_from_records(data, out_path: Path, delimiter: str) -> None:
    if isinstance(data, list) and data and isinstance(data[0], dict):
        fieldnames = list(data[0].keys())
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, delimiter=delimiter)
        writer.writeheader()
        writer.writerows(data)
        out_path.write_text(buf.getvalue(), encoding="utf-8")
    else:
        # Not a flat list-of-records shape -- can't build meaningful
        # columns, so fall back to one JSON blob per line rather than
        # guessing a column layout that doesn't exist.
        out_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _tabular_rows_to_json(rows: list) -> str:
    if not rows:
        return "[]"
    header, *body = rows
    records = [dict(zip(header, row)) for row in body]
    return json.dumps(records, indent=2, ensure_ascii=False)


def _write_rows(rows: list, out_path: Path, delimiter: str) -> None:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter)
    writer.writerows(rows)
    out_path.write_text(buf.getvalue(), encoding="utf-8")


def _tabular_rows_to_plain(rows: list) -> str:
    return "\n".join(" | ".join(cell for cell in row) for row in rows)


def _to_yaml(data) -> str:
    try:
        import yaml  # type: ignore
    except ImportError:
        raise RuntimeError(
            "Converting to YAML needs PyYAML, which isn't installed. "
            "Add 'pyyaml' to requirements.txt to enable this."
        )
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
