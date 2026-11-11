"""Background worker: registers files dropped in <year>/_inbox, OCRs pending documents, backs up the DB.

Run with:  python -m worker.run
Polling (not inotify) is deliberate: file-change events are unreliable on NFS/SMB mounts.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, update

from app import storage
from app.db import configure, session_scope
from app.models import UNSORTED, Document
from app.settings import get_settings

from .extract import extract_document

log = logging.getLogger("steuer.worker")
_seen: dict[str, tuple[int, float]] = {}


def recover_stale() -> None:
    """After a crash, documents stuck in 'processing' go back to the queue."""
    with session_scope() as s:
        s.execute(update(Document).where(Document.status == "processing").values(status="pending"))


def scan_inbox() -> int:
    st = get_settings()
    added = 0
    for ydir in sorted(st.nas_root.glob("[12][0-9][0-9][0-9]")):
        inbox = ydir / "_inbox"
        if not inbox.is_dir():
            continue
        year = int(ydir.name)
        for f in sorted(inbox.iterdir()):
            if not f.is_file() or f.name.startswith(".") or not storage.ext_ok(f.name):
                continue
            info = f.stat()
            if time.time() - info.st_mtime < st.inbox_settle_seconds:
                continue  # still being copied
            sig = (info.st_size, info.st_mtime)
            if _seen.get(str(f)) == sig:
                continue
            _seen[str(f)] = sig
            sha = storage.sha_file(f)
            with session_scope() as s:
                if s.scalar(select(Document.id).where(Document.tax_year == year, Document.sha256 == sha)):
                    continue
                s.add(Document(tax_year=year, category=UNSORTED, original_name=f.name,
                               stored_path=storage.rel(f), sha256=sha, size_bytes=info.st_size))
                added += 1
                log.info("registered inbox file %s", f.name)
    return added


def process_pending(limit: int = 25) -> int:
    st = get_settings()
    done = 0
    for _ in range(limit):
        with session_scope() as s:
            doc = s.scalars(select(Document).where(Document.status == "pending").order_by(Document.id).limit(1)).first()
            if doc is None:
                break
            doc.status = "processing"
            doc_id, year = doc.id, doc.tax_year
            try:
                path: Path | None = storage.abs_path(doc)
            except storage.StorageError:
                path = None
        # OCR runs with no DB transaction open, so the web app is never blocked
        ex, err = None, None
        try:
            if path is None or not path.exists():
                err = "File not found on NAS"
            else:
                ex = extract_document(path, st.ocr_lang, year)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the queue
            err = f"Extraction failed: {type(exc).__name__}: {exc}"
        with session_scope() as s:
            doc = s.get(Document, doc_id)
            if doc is None or doc.status != "processing":
                continue  # deleted or changed by the user meanwhile
            if err or ex is None:
                doc.status, doc.flags = "error", err or "Unknown error"
            else:
                doc.doc_date, doc.amount_cents, doc.vendor = ex.doc_date, ex.amount_cents, ex.vendor
                doc.confidence = ex.confidence
                doc.flags = "\n".join(ex.flags)
                doc.text_excerpt = ex.text[:2000]
                ok = ex.confidence >= st.confidence_threshold and not ex.flags
                doc.status = "processed" if ok else "needs_review"
        done += 1
    return done


def backup_db(now: datetime | None = None) -> Path | None:
    """Daily copy of the SQLite DB to <NAS>/_backups using SQLite's online backup API."""
    st = get_settings()
    ok, _ = storage.nas_status()
    if not ok or not st.db_path.exists():
        return None
    now = now or datetime.now()
    st.backup_dir.mkdir(parents=True, exist_ok=True)
    dest = st.backup_dir / f"steuer-{now:%Y%m%d}.db"
    if dest.exists():
        return None
    tmp = dest.with_suffix(".tmp")
    src, dst = sqlite3.connect(st.db_path), sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    tmp.replace(dest)
    for old in sorted(st.backup_dir.glob("steuer-*.db"))[: -st.backup_keep]:
        old.unlink(missing_ok=True)
    log.info("backup written: %s", dest.name)
    return dest


def run_once() -> tuple[int, int]:
    ok, msg = storage.nas_status()
    if not ok:
        log.warning("NAS not ready: %s", msg)
        return 0, 0
    return scan_inbox(), process_pending()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    st = get_settings()
    configure()
    recover_stale()
    log.info("worker started, polling every %ss", st.poll_seconds)
    while True:
        try:
            st.heartbeat_path.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
            run_once()
            if datetime.now().hour >= st.backup_hour:
                backup_db()
        except Exception:  # noqa: BLE001
            log.exception("worker cycle failed")
        time.sleep(st.poll_seconds)


if __name__ == "__main__":
    main()
