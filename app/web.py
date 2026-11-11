"""Shared helpers for the web layer: templates, flash messages, redirects, year selection."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, storage
from .models import UNSORTED, Document, Person
from .money import fmt_eur
from .people import ensure_people
from .settings import get_settings

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["eur"] = fmt_eur
templates.env.filters["dmy"] = lambda d: d.strftime("%d.%m.%Y") if d else "–"

STATUS_LABEL = {
    "pending": "Queued", "processing": "Reading…", "processed": "Auto-read",
    "needs_review": "Needs review", "approved": "Approved", "error": "Error",
}
templates.env.globals["STATUS_LABEL"] = STATUS_LABEL
templates.env.globals["UNSORTED"] = UNSORTED


def get_year(request: Request) -> int:
    try:
        y = int(request.query_params.get("year", ""))
    except ValueError:
        return get_settings().default_year
    return y if 1990 <= y <= 2100 else get_settings().default_year


def safe_next(url: str | None, default: str) -> str:
    """Only allow local paths (no open redirects)."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return default


def go(url: str, msg: str | None = None, kind: str = "ok") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if msg:
        resp.set_cookie("flash", quote(f"{kind}|{msg}"), max_age=60, httponly=True, samesite="lax")
    return resp


def _read_flash(request: Request) -> dict | None:
    raw = request.cookies.get("flash")
    if not raw:
        return None
    kind, _, msg = unquote(raw).partition("|")
    return {"kind": kind if kind in ("ok", "err", "warn") else "ok", "msg": msg}


def worker_state() -> str:
    try:
        ts = datetime.fromisoformat(get_settings().heartbeat_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return "never"
    return "ok" if (datetime.now() - ts).total_seconds() < 300 else "stale"


def years_available(s: Session, current: int) -> list[int]:
    ys = set(s.scalars(select(Document.tax_year).distinct()))
    ys |= set(s.scalars(select(Person.tax_year).distinct()))
    ys |= {get_settings().default_year, get_settings().default_year - 1, current}
    return sorted(ys, reverse=True)


def render(request: Request, name: str, s: Session, year: int, **ctx):
    ok, msg = storage.nas_status()
    flash = _read_flash(request)
    people = ensure_people(s, year)
    data = dict(
        year=year, years=years_available(s, year), nas_ok=ok, nas_msg=msg, worker=worker_state(),
        flash=flash, people=people, pnames={p.id: p.name for p in people},
        catlabels={**{k: c["label"] for k, c in config.categories().items()}, UNSORTED: "Unsorted"},
    )
    data.update(ctx)
    resp = templates.TemplateResponse(request, name, data)
    if flash:
        resp.delete_cookie("flash")
    return resp
