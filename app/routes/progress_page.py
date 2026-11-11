from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from worker.calculate import compute_year

from ..auth import require_auth
from ..db import get_db
from ..progress import compute_progress, earned_payload
from ..web import get_year, render

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/progress")
def page(request: Request, s: Session = Depends(get_db)):
    year = get_year(request)
    sm = compute_year(s, year)
    prog = compute_progress(s, year, sm)
    return render(request, "progress.html", s, year, sm=sm, prog=prog, earned=earned_payload(prog, year),
                  today=date.today())
