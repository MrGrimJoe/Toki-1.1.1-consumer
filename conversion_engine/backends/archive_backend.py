"""
archive_backend.py — zipfile + tarfile-only compress/extract.

Deliberately zip/tar only, not 7z/rar. Both zipfile AND tarfile are
stdlib (zero new dependency, matches TOKI's "no hard dependency for a
core path" posture), and together they're the format the vast majority
of "can you zip this up" / "unzip this" requests actually mean --
zip has first-class Explorer support on Windows, tar/tgz/tar.bz2 cover
almost everything downloaded from a dev-facing source (GitHub release
tarballs, Linux-originated archives someone's sharing). rar/7z would
need bundling 7-Zip or py7zr as a new dependency -- a deliberate
follow-up, not folded in here.

BETA 0.3.43: widened from zip-only to also read/write tar, tar.gz/tgz,
and tar.bz2/tbz2 -- same zip-slip protection now applies to BOTH archive
types, since tar has an identical "../"-style member-path attack shape.
"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path
from typing import Optional

# ── extension helpers ───────────────────────────────────────────────────────
#
# Path.suffix only ever returns the LAST dot-segment ("notes.tar.gz" ->
# ".gz"), which can't distinguish a real gzip file from a tarball -- so
# every function here inspects the full lowercased filename, not just
# .suffix, to decide which of the four tar variants (or plain zip) it's
# actually looking at.

_TAR_GZ_SUFFIXES = (".tar.gz", ".tgz")
_TAR_BZ2_SUFFIXES = (".tar.bz2", ".tbz2")
_PLAIN_TAR_SUFFIX = ".tar"


def _tar_mode_for_read(name: str) -> Optional[str]:
    lower = name.lower()
    if lower.endswith(_TAR_GZ_SUFFIXES):
        return "r:gz"
    if lower.endswith(_TAR_BZ2_SUFFIXES):
        return "r:bz2"
    if lower.endswith(_PLAIN_TAR_SUFFIX):
        return "r:"
    return None


def _tar_mode_and_ext_for_write(target_ext: str):
    """Returns (tarfile write-mode, output-suffix-including-leading-dot)
    for a requested tar-family target extension, or (None, None) if
    target_ext isn't a tar variant at all."""
    ext = target_ext.lower().lstrip(".")
    if ext in ("tar.gz", "tgz"):
        return "w:gz", ".tar.gz"
    if ext in ("tar.bz2", "tbz2"):
        return "w:bz2", ".tar.bz2"
    if ext == "tar":
        return "w:", ".tar"
    return None, None


def compress(source_path: str, overwrite: bool = False, target_ext: str = "zip") -> str:
    """Zips (or tars) a single file or an entire folder (recursively).
    target_ext defaults to "zip" -- unchanged behavior for every existing
    caller (apis.py's compress_selected, conversion_engine.compress_file)
    that never passes it explicitly."""
    source = Path(source_path)

    tar_mode, tar_suffix = _tar_mode_and_ext_for_write(target_ext)
    if tar_mode:
        out_path = source.with_name(f"{source.stem}{tar_suffix}")
        with tarfile.open(out_path, tar_mode) as tf:
            tf.add(source, arcname=source.name)
        return str(out_path)

    # Default / anything else requested: zip, same as before this session.
    out_path = source.with_suffix(".zip") if overwrite else source.with_name(f"{source.stem}.zip")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if source.is_dir():
            for item in source.rglob("*"):
                if item.is_file():
                    zf.write(item, item.relative_to(source.parent))
        else:
            zf.write(source, source.name)

    return str(out_path)


def _dest_for(source: Path, destination: Optional[str]) -> Path:
    if destination:
        return Path(destination)
    # Strip every known compound suffix, not just the last one, so
    # "notes.tar.gz" -> "notes/", not "notes.tar/".
    name = source.name
    lower = name.lower()
    for suf in (*_TAR_GZ_SUFFIXES, *_TAR_BZ2_SUFFIXES, _PLAIN_TAR_SUFFIX, ".zip"):
        if lower.endswith(suf):
            return source.with_name(name[: -len(suf)])
    return source.with_suffix("")


def extract(source_path: str, destination: str = None) -> str:
    """Extracts a .zip/.tar/.tgz/.tar.gz/.tar.bz2 to a sibling folder named
    after the archive, or to an explicit destination if given. Rejects any
    archive member whose path would land outside `dest` -- a crafted
    archive with "../"-style entries (zip-slip / the identical tar-slip
    shape) could otherwise write files anywhere the process has permission
    for, e.g. outside TOKI's own sandbox. resize_file()/convert()/
    compress() all operate on a path the user already picked and trust
    its contents; extract() is the one operation here that reads paths
    supplied BY the archive itself, so it's the one that needs this
    check -- for BOTH archive types, identically."""
    source = Path(source_path)
    dest = _dest_for(source, destination)
    dest_resolved = dest.resolve()

    def _reject_if_outside(member_name: str) -> None:
        member_path = (dest_resolved / member_name).resolve()
        if dest_resolved not in member_path.parents and member_path != dest_resolved:
            raise ValueError(
                f"Refusing to extract -- archive member {member_name!r} would "
                f"land outside the destination folder."
            )

    tar_mode = _tar_mode_for_read(source.name)
    if tar_mode:
        with tarfile.open(source, tar_mode) as tf:
            for member in tf.getmembers():
                _reject_if_outside(member.name)
            # Explicit filter (Python 3.12+): the member-path check above
            # already rejects anything that would escape `dest`, but
            # passing "data" here too silences the 3.14-forward-compat
            # DeprecationWarning and additionally strips setuid/device
            # files etc. -- defense in depth, not a replacement for
            # _reject_if_outside.
            try:
                tf.extractall(dest, filter="data")
            except TypeError:
                # Python < 3.12 tarfile has no `filter` kwarg at all.
                tf.extractall(dest)
        return str(dest)

    with zipfile.ZipFile(source, "r") as zf:
        for member in zf.namelist():
            _reject_if_outside(member)
        zf.extractall(dest)

    return str(dest)


def convert(source_path: str, target_ext: str, overwrite: bool = False) -> str:
    """Only meaningful direction right now is "zip/tar this up" -- routed
    here from registry.py when the requested operation is really a
    compress/extract in disguise. target_ext lets this pick zip vs a tar
    variant (see compress() above); previously hardcoded to zip only."""
    ext = target_ext.lower().lstrip(".")
    if ext in ("zip", "tar", "tar.gz", "tgz", "tar.bz2", "tbz2"):
        return compress(source_path, overwrite=overwrite, target_ext=ext)
    raise NotImplementedError(
        f"Archive conversion to '.{target_ext}' isn't supported yet -- "
        f"only zip/tar/tgz/tar.gz/tar.bz2 are."
    )
