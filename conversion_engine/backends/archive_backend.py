"""
archive_backend.py — stdlib zipfile-only compress/extract.

Deliberately zip-only for this pass, not 7z/rar/tar.gz. zipfile is stdlib
(zero new dependency, matches TOKI's "no hard dependency for a core path"
posture), and it's the format the vast majority of "can you zip this up"
/ "unzip this" requests actually mean on Windows, where zip has first-
class Explorer support. rar/7z would need bundling 7-Zip or py7zr as a
new dependency -- a deliberate follow-up, not folded in here.
"""

from __future__ import annotations

import zipfile
from pathlib import Path


def compress(source_path: str, overwrite: bool = False) -> str:
    """Zips a single file or an entire folder (recursively)."""
    source = Path(source_path)
    out_path = source.with_suffix(".zip") if overwrite else source.with_name(f"{source.stem}.zip")

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if source.is_dir():
            for item in source.rglob("*"):
                if item.is_file():
                    zf.write(item, item.relative_to(source.parent))
        else:
            zf.write(source, source.name)

    return str(out_path)


def extract(source_path: str, destination: str = None) -> str:
    """Extracts a .zip to a sibling folder named after the archive, or to
    an explicit destination if given. Rejects any archive member whose
    path would land outside `dest` -- a zip with "../"-style entries
    (zip-slip) could otherwise write files anywhere the process has
    permission for, e.g. outside TOKI's own sandbox. resize_file()/
    convert()/compress() all operate on a path the user already picked
    and trust its contents; extract() is the one operation here that
    reads paths supplied BY the archive itself, so it's the one that
    needs this check."""
    source = Path(source_path)
    dest = Path(destination) if destination else source.with_suffix("")
    dest_resolved = dest.resolve()

    with zipfile.ZipFile(source, "r") as zf:
        for member in zf.namelist():
            member_path = (dest_resolved / member).resolve()
            if dest_resolved not in member_path.parents and member_path != dest_resolved:
                raise ValueError(
                    f"Refusing to extract -- archive member {member!r} would "
                    f"land outside the destination folder."
                )
        zf.extractall(dest)

    return str(dest)


def convert(source_path: str, target_ext: str, overwrite: bool = False) -> str:
    """Only meaningful direction right now is "zip this up" -- routed here
    from registry.py when the requested operation is really a compress/
    extract in disguise. Kept for interface symmetry with the other
    backends' convert()."""
    if target_ext.lower().lstrip(".") == "zip":
        return compress(source_path, overwrite=overwrite)
    raise NotImplementedError(
        f"Archive conversion to '.{target_ext}' isn't supported yet -- only zip is."
    )
