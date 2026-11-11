from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from worker.calculate import compute_year

from .. import config
from ..auth import require_auth
from ..db import get_db
from ..models import UNSORTED, Document, YearReview
from ..web import get_year, go, render

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/review")
def page(request: Request, s: Session = Depends(get_db)):
    year = get_year(request)
    sm = compute_year(s, year)
    docs = list(s.scalars(select(Document).where(Document.tax_year == year, Document.excluded.is_(False))
                          .order_by(Document.id)))
    per_person_cats = {k for k, c in config.categories().items()
                       if config.sections().get(c["section"], {}).get("per_person")}
    multi = len(sm.people) > 1
    open_docs = [d for d in docs if d.is_open or (multi and d.person_id is None and d.category in per_person_cats)]
    auto_docs = [d for d in docs if d.status == "processed" and d.category != UNSORTED]
    return render(request, "review.html", s, year, sm=sm, open_docs=open_docs, auto_docs=auto_docs)


@router.post("/review/approve-all")
def approve_all(year: int = Form(...), s: Session = Depends(get_db)):
    res = s.execute(update(Document).where(
        Document.tax_year == year, Document.status == "processed", Document.excluded.is_(False),
        Document.amount_cents.is_not(None), Document.category != UNSORTED).values(status="approved"))
    s.commit()
    return go(f"/review?year={year}", f"{res.rowcount} auto-read document(s) approved.")


@router.post("/review/signoff")
def signoff(year: int = Form(...), confirm: str = Form(""), s: Session = Depends(get_db)):
    back = f"/review?year={year}"
    sm = compute_year(s, year)
    if sm.open_items:
        return go(back, f"{sm.open_items} item(s) are still open.", "err")
    if confirm != "1":
        return go(back, "Please tick the confirmation box.", "warn")
    existing = s.get(YearReview, year)
    if existing:
        existing.signed_off_at, existing.snapshot = datetime.now(), json.dumps(sm.snapshot)
    else:
        s.add(YearReview(tax_year=year, snapshot=json.dumps(sm.snapshot)))
    s.commit()
    return go(back, "Signed off. Enter the figures in Mein ELSTER yourself - nothing is sent automatically.")


@router.post("/review/reopen")
def reopen(year: int = Form(...), s: Session = Depends(get_db)):
    existing = s.get(YearReview, year)
    if existing:
        s.delete(existing)
        s.commit()
    return go(f"/review?year={year}", "Sign-off removed.")
