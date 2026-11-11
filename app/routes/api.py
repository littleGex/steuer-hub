from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from worker.calculate import compute_year

from .. import storage
from ..auth import require_auth
from ..db import get_db
from ..progress import compute_progress, status_payload
from ..web import get_year, worker_state

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/api/status")
def status(request: Request, s: Session = Depends(get_db)):
    """Polled by the dashboard animation. Small JSON, never cached."""
    year = get_year(request)
    sm = compute_year(s, year)
    prog = compute_progress(s, year, sm)
    payload = status_payload(s, year, sm, prog, worker_state(), storage.nas_status()[0])
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})
