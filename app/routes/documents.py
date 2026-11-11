from __future__ import annotations

import mimetypes
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config, storage
from ..auth import require_auth
from ..db import get_db
from ..models import UNSORTED, Document, Person
from ..money import to_cents
from ..settings import get_settings
from ..web import get_year, go, render, safe_next

router = APIRouter(dependencies=[Depends(require_auth)])


def _person_or_none(s: Session, raw: str, year: int) -> int | None:
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        raise ValueError("Invalid person.") from None
    p = s.get(Person, pid)
    if p is None or p.tax_year != year:
        raise ValueError("Unknown person for this tax year.")
    return pid


@router.post("/upload")
def upload(
    year: int = Form(...), category: str = Form(...), person_id: str = Form(""),
    files: list[UploadFile] = File(...), s: Session = Depends(get_db),
):
    back = f"/?year={year}"
    ok, msg = storage.nas_status()
    if not ok:
        return go(back, f"Upload refused: {msg}", "err")
    if category != UNSORTED and category not in config.categories():
        return go(back, "Unknown category.", "err")
    try:
        pid = _person_or_none(s, person_id, year)
    except ValueError as exc:
        return go(back, str(exc), "err")
    max_bytes = get_settings().max_upload_mb * 1024 * 1024
    saved = dups = rejected = 0
    last_error = ""
    for f in files:
        if not f.filename:
            continue
        if not storage.ext_ok(f.filename):
            rejected += 1
            last_error = "Only PDF, JPG, PNG and TIFF files are accepted."
            continue
        try:
            dest_dir = storage.category_dir(year, category)
            tmp, sha, size = storage.stream_to_temp(f.file, dest_dir, max_bytes)
        except (storage.StorageError, OSError) as exc:
            rejected += 1
            last_error = str(exc)
            continue
        if s.scalar(select(Document.id).where(Document.tax_year == year, Document.sha256 == sha)):
            storage.discard(tmp)
            dups += 1
            continue
        safe = storage.safe_name(f.filename)
        final = storage.finalize(tmp, dest_dir, safe, sha)
        s.add(Document(tax_year=year, person_id=pid, category=category, original_name=safe,
                       stored_path=storage.rel(final), sha256=sha, size_bytes=size))
        s.commit()
        saved += 1
    parts = []
    if saved:
        parts.append(f"{saved} file(s) saved - reading them now")
    if dups:
        parts.append(f"{dups} duplicate(s) skipped")
    if rejected:
        parts.append(f"{rejected} rejected ({last_error})")
    if not parts:
        return go(back, "No file selected.", "warn")
    return go(back, "; ".join(parts), "ok" if saved and not rejected else "warn" if saved or dups else "err")


@router.get("/documents")
def list_documents(request: Request, s: Session = Depends(get_db)):
    year = get_year(request)
    q = select(Document).where(Document.tax_year == year)
    category = request.query_params.get("category", "")
    status = request.query_params.get("status", "")
    person = request.query_params.get("person", "")
    if category:
        q = q.where(Document.category == category)
    if status:
        q = q.where(Document.status == status)
    if person.isdigit():
        q = q.where(Document.person_id == int(person))
    docs = list(s.scalars(q.order_by(Document.id.desc()).limit(500)))
    return render(request, "documents.html", s, year, docs=docs, f_category=category, f_status=status,
                  f_person=person, categories=config.categories())


def _get_doc(s: Session, doc_id: int) -> Document:
    doc = s.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "Document not found")
    return doc


@router.get("/documents/{doc_id}")
def detail(doc_id: int, request: Request, s: Session = Depends(get_db)):
    doc = _get_doc(s, doc_id)
    return render(request, "document.html", s, doc.tax_year, doc=doc, categories=config.categories(),
                  next_url=safe_next(request.query_params.get("next"), f"/documents?year={doc.tax_year}"),
                  cents_text=("" if doc.amount_cents is None else f"{doc.amount_cents / 100:.2f}".replace(".", ",")))


@router.post("/documents/{doc_id}")
def update(
    doc_id: int, category: str = Form(...), person_id: str = Form(""), doc_date: str = Form(""),
    amount: str = Form(""), vendor: str = Form(""), note: str = Form(""), excluded: str = Form(""),
    action: str = Form("save"), next_url: str = Form("", alias="next"), s: Session = Depends(get_db),
):
    doc = _get_doc(s, doc_id)
    back = f"/documents/{doc.id}?year={doc.tax_year}"
    dest = safe_next(next_url, f"/documents?year={doc.tax_year}")
    if doc.status in ("pending", "processing"):
        return go(back, "Still being read - try again in a moment.", "warn")
    try:
        pid = _person_or_none(s, person_id, doc.tax_year)
        parsed_date = date.fromisoformat(doc_date) if doc_date.strip() else None
    except ValueError as exc:
        return go(back, f"Invalid input: {exc}", "err")
    cents = to_cents(amount)
    if amount.strip() and cents is None:
        return go(back, "Amount not understood - use e.g. 119,00", "err")
    if category != doc.category:
        if category != UNSORTED and category not in config.categories():
            return go(back, "Unknown category.", "err")
        try:
            doc.stored_path = storage.move_to_category(doc, category)
        except (storage.StorageError, OSError) as exc:
            return go(back, f"Could not move the file: {exc}", "err")
        doc.category = category
    doc.person_id, doc.doc_date, doc.amount_cents = pid, parsed_date, cents
    doc.vendor = vendor.strip()[:120] or None
    doc.note = note.strip()[:2000]
    doc.excluded = excluded == "1"
    doc.manually_edited = True
    if action == "approve":
        if doc.amount_cents is None and not doc.excluded:
            s.commit()
            return go(back, "Saved, but an amount is needed before approving.", "warn")
        if doc.category == UNSORTED:
            s.commit()
            return go(back, "Saved, but choose a category before approving.", "warn")
        doc.status = "approved"
    elif doc.status == "error":
        doc.status = "needs_review"
    s.commit()
    return go(dest, "Approved." if action == "approve" else "Saved.")


@router.post("/documents/{doc_id}/approve")
def approve(doc_id: int, next_url: str = Form("", alias="next"), s: Session = Depends(get_db)):
    doc = _get_doc(s, doc_id)
    dest = safe_next(next_url, f"/review?year={doc.tax_year}")
    if doc.amount_cents is None or doc.category == UNSORTED:
        return go(f"/documents/{doc.id}?year={doc.tax_year}", "Add an amount and a category first.", "warn")
    doc.status = "approved"
    s.commit()
    return go(dest)


@router.post("/documents/{doc_id}/reprocess")
def reprocess(doc_id: int, s: Session = Depends(get_db)):
    doc = _get_doc(s, doc_id)
    doc.status = "pending"
    s.commit()
    return go(f"/documents/{doc.id}?year={doc.tax_year}", "Queued for re-reading (this overwrites the extracted values).")


@router.post("/documents/{doc_id}/delete")
def delete(doc_id: int, s: Session = Depends(get_db)):
    doc = _get_doc(s, doc_id)
    year = doc.tax_year
    try:
        storage.move_to_trash(doc)
    except (storage.StorageError, OSError) as exc:
        return go(f"/documents/{doc.id}?year={year}", f"Could not move the file to _trash: {exc}", "err")
    s.delete(doc)
    s.commit()
    return go(f"/documents?year={year}", "Removed. The original file was moved to the _trash folder on the NAS.")


@router.get("/documents/{doc_id}/file")
def original(doc_id: int, s: Session = Depends(get_db)):
    doc = _get_doc(s, doc_id)
    try:
        path = storage.abs_path(doc)
    except storage.StorageError:
        raise HTTPException(404, "File not available") from None
    if not path.is_file():
        raise HTTPException(404, "File missing on the NAS")
    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media, filename=doc.original_name, content_disposition_type="inline",
                        headers={"X-Content-Type-Options": "nosniff"})
