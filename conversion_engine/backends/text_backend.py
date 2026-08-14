"""
text_backend.py — stdlib-only conversions between plain-text data formats:
txt, md, csv, tsv, json, yaml, yml, xml, log, ini, toml, conf.

Covers the feature request's headline example directly:
  "turn the file I'm selecting into a text file" (json -> txt, csv -> txt,
  etc.)

DESIGN NOTES
----------------------------------------------------------------------------
- json/csv/tsv/ini have real STRUCTURE, so "convert to txt" means "render
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
- ini/toml/conf (BETA 0.3.43): ini and conf are treated identically
  (Windows .conf files are near-universally INI-shaped) via stdlib
  configparser -- a real structured round-trip to/from json, same as
  csv/tsv already get, not a byte-copy. toml has no stdlib WRITER before
  Python 3.11 (only tomllib, read-only, added in 3.11) -- reading requires
  tomllib (3.11+) or the third-party `toml` package on older runtimes;
  writing always needs the third-party `toml` package. Both report a
  specific missing-dependency message rather than silently no-op'ing,
  same convention as _to_yaml()'s PyYAML check below.
- Every writer defaults to UTF-8 and never overwrites the source in place
  (same suffix convention as image_backend.py), unless overwrite=True.
"""

from __future__ import annotations

import configparser
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
        elif target_ext in ("ini", "conf"):
            out_path.write_text(_json_to_ini(data), encoding="utf-8")
        elif target_ext == "toml":
            out_path.write_text(_write_toml(data), encoding="utf-8")
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

    # ── ini/conf -> anything (BETA 0.3.43) — .conf treated as ini-shaped,
    #    the common case on Windows/cross-platform config files. ─────────
    if source_ext in ("ini", "conf"):
        parser = configparser.ConfigParser()
        parser.read_string(raw)
        data = {section: dict(parser.items(section)) for section in parser.sections()}
        if target_ext == "json":
            out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        elif target_ext in ("ini", "conf"):
            out_path.write_text(raw, encoding="utf-8")
        elif target_ext in ("txt", "md", "log"):
            out_path.write_text(_ini_data_to_plain(data), encoding="utf-8")
        else:
            raise UnsupportedFormatError(
                f"Can't convert ini to {target_ext} -- no readable "
                f"transform exists for that pair yet."
            )
        return str(out_path)

    # ── json -> ini/conf/toml handled above in the "json -> anything"
    #    branch directly (source_ext == "json" is checked first) — kept
    #    unlisted here so there's exactly one place that decides json's
    #    outgoing conversions, not two branches that could silently drift
    #    out of sync with each other.

    # ── toml -> anything (BETA 0.3.43) ───────────────────────────────────
    if source_ext == "toml":
        data = _read_toml(raw)
        if target_ext == "json":
            out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        elif target_ext == "toml":
            out_path.write_text(raw, encoding="utf-8")
        elif target_ext in ("txt", "md", "log"):
            out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            raise UnsupportedFormatError(
                f"Can't convert toml to {target_ext} -- no readable "
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


def _ini_data_to_plain(data: dict) -> str:
    lines = []
    for section, kv in data.items():
        lines.append(f"[{section}]")
        for k, v in kv.items():
            lines.append(f"  {k} = {v}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _json_to_ini(data) -> str:
    """Only a flat {section: {key: value}} shape maps cleanly onto ini's
    section/key model -- anything else (a bare list, deeply nested
    objects) has no unambiguous ini representation, so this raises rather
    than inventing a lossy flattening scheme."""
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise UnsupportedFormatError(
            "Can't convert this json to ini -- ini needs a flat "
            "{section: {key: value}} shape, and this json isn't shaped "
            "like that."
        )
    parser = configparser.ConfigParser()
    for section, kv in data.items():
        parser[section] = {k: str(v) for k, v in kv.items()}
    buf = io.StringIO()
    parser.write(buf)
    return buf.getvalue()


def _read_toml(raw: str):
    try:
        import tomllib  # Python 3.11+, stdlib, read-only
        return tomllib.loads(raw)
    except ImportError:
        pass
    try:
        import toml  # type: ignore  # third-party, works on any version
        return toml.loads(raw)
    except ImportError:
        raise RuntimeError(
            "Reading TOML on this Python version needs the 'toml' "
            "package (Python 3.11+ can read it out of the box via "
            "stdlib tomllib, but this backend can also write TOML, "
            "which tomllib can't do). Add 'toml' to requirements.txt to "
            "enable this."
        )


def _write_toml(data) -> str:
    try:
        import toml  # type: ignore  # stdlib has no TOML writer at all, even on 3.11+
        return toml.dumps(data)
    except ImportError:
        raise RuntimeError(
            "Writing TOML needs the 'toml' package, which isn't "
            "installed (there is no stdlib TOML writer, even on Python "
            "3.11+ -- tomllib is read-only). Add 'toml' to "
            "requirements.txt to enable this."
        )
