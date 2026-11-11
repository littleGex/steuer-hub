from __future__ import annotations

from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from worker.calculate import compute_year

from .. import config
from ..auth import require_auth
from ..db import get_db
from ..models import Document, WorkDay
from ..web import get_year, render

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/")
def dashboard(request: Request, s: Session = Depends(get_db)):
    year = get_year(request)
    sm = compute_year(s, year)
    recent = list(s.scalars(select(Document).where(Document.tax_year == year).order_by(Document.id.desc()).limit(8)))
    today = date.today()
    today_kinds = {wd.person_id: wd.kind for wd in s.scalars(select(WorkDay).where(WorkDay.day == today))}
    pending = sm.counts.get("pending", 0) + sm.counts.get("processing", 0)
    return render(request, "dashboard.html", s, year, sm=sm, recent=recent, categories=config.categories(),
                  today=today, today_kinds=today_kinds, pending=pending,
                  needs_review=sm.counts.get("needs_review", 0) + sm.counts.get("error", 0))


@router.get("/sections")
def sections(request: Request, s: Session = Depends(get_db)):
    year = get_year(request)
    sm = compute_year(s, year)
    by_cat: dict[str, list[Document]] = defaultdict(list)
    for d in s.scalars(select(Document).where(Document.tax_year == year, Document.excluded.is_(False))
                       .order_by(Document.doc_date, Document.id)):
        by_cat[d.category].append(d)
    return render(request, "sections.html", s, year, sm=sm, by_cat=by_cat)
