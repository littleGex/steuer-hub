"""NAS file handling. Originals are never modified; DB rows point at them by relative path."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from datetime import date
from pathlib import Path
from typing import BinaryIO

from . import config
from .models import UNSORTED, Document
from .settings import get_settings

MARKER = ".steuer-hub-marker"
ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
_SAFE = re.compile(r"[^A-Za-z0-9ÄÖÜäöüß._-]+")


class StorageError(Exception):
    pass


def root() -> Path:
    return get_settings().nas_root


def nas_status() -> tuple[bool, str]:
    """An unmounted NAS often shows up as an empty local folder. The marker file catches that."""
    r = root()
    if not r.is_dir():
        return False, f"NAS folder {r} does not exist."
    if not (r / MARKER).exists():
        return False, ("NAS marker file missing - is the share mounted? "
                       f"(If this is a fresh install, create an empty file named {MARKER} in the NAS root.)")
    return True, "ok"


def init_marker(has_docs: bool) -> None:
    """Create the marker automatically on a fresh install only (no documents known yet)."""
    r = root()
    r.mkdir(parents=True, exist_ok=True)
    marker = r / MARKER
    if not marker.exists() and not has_docs:
        marker.write_text("steuer-hub nas marker\n", encoding="utf-8")


def safe_name(name: str) -> str:
    base = Path(name.replace("\\", "/")).name
    stem, ext = os.path.splitext(base)
    stem = _SAFE.sub("_", stem).strip("._") or "file"
    ext = _SAFE.sub("", ext).lower()
    return f"{stem[:80]}{ext}"


def ext_ok(name: str) -> bool:
    return Path(name).suffix.lower() in ALLOWED_EXT


def category_dir(year: int, category: str) -> Path:
    if not 1990 <= year <= 2100:
        raise StorageError("Invalid tax year.")
    if category == UNSORTED:
        sub = "_inbox"
    elif category in config.categories():
        sub = category
    else:
        raise StorageError(f"Unknown category '{category}'.")
    return root() / str(year) / sub


def stream_to_temp(src: BinaryIO, dest_dir: Path, max_bytes: int) -> tuple[Path, str, int]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / f".tmp-{uuid.uuid4().hex}"
    h = hashlib.sha256()
    size = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := src.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise StorageError(f"File larger than {max_bytes // (1024 * 1024)} MB.")
                h.update(chunk)
                out.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, h.hexdigest(), size


def finalize(tmp: Path, dest_dir: Path, safe: str, sha: str) -> Path:
    final = dest_dir / f"{date.today():%Y%m%d}-{sha[:8]}-{safe}"
    if final.exists():
        tmp.unlink(missing_ok=True)
        return final
    os.replace(tmp, final)
    return final


def discard(tmp: Path) -> None:
    tmp.unlink(missing_ok=True)


def rel(path: Path) -> str:
    return str(path.resolve().relative_to(root().resolve()))


def abs_path(doc: Document) -> Path:
    r = root().resolve()
    p = (r / doc.stored_path).resolve()
    if not p.is_relative_to(r):
        raise StorageError("Stored path escapes the NAS root.")
    return p


def sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for i in range(1, 1000):
        cand = dest.with_name(f"{dest.stem}-{i}{dest.suffix}")
        if not cand.exists():
            return cand
    raise StorageError("Could not find a free file name.")


def move_to_category(doc: Document, new_category: str) -> str:
    """User-initiated move (re-categorise / sort out of _inbox). Returns the new relative path."""
    dest_dir = category_dir(doc.tax_year, new_category)
    src = abs_path(doc)
    if src.parent == dest_dir.resolve():
        return doc.stored_path
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique(dest_dir / src.name)
    shutil.move(str(src), str(dest))
    return rel(dest)


def move_to_trash(doc: Document) -> str:
    """'Delete' never removes the original: it goes to <year>/_trash/."""
    trash = root() / str(doc.tax_year) / "_trash"
    trash.mkdir(parents=True, exist_ok=True)
    src = abs_path(doc)
    if not src.exists():
        return doc.stored_path
    dest = _unique(trash / src.name)
    shutil.move(str(src), str(dest))
    return rel(dest)
