from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from worker.calculate import compute_year

from .. import config
from ..auth import require_auth
from ..db import get_db
from ..models import Document
from ..money import fmt_plain
from ..web import get_year, render

router = APIRouter(dependencies=[Depends(require_auth)])


def csv_safe(value) -> str:
    """Stop spreadsheet formula injection: OCR'd vendor names come from untrusted documents."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _response(rows: list[list], name: str) -> Response:
    buf = io.StringIO()
    csv.writer(buf, delimiter=";", lineterminator="\r\n").writerows(rows)
    return Response(
        content="﻿" + buf.getvalue(),  # BOM so German Excel reads UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _dec(request: Request) -> str:
    return "." if request.query_params.get("decimal") == "dot" else ","


@router.get("/export")
def page(request: Request, s: Session = Depends(get_db)):
    year = get_year(request)
    return render(request, "export.html", s, year, sm=compute_year(s, year))


@router.get("/export/summary.csv")
def summary_csv(request: Request, s: Session = Depends(get_db)):
    year, dec = get_year(request), _dec(request)
    sm = compute_year(s, year)
    state = "SIGNED-OFF" if sm.signed_off_at and not sm.changed_since_signoff else "DRAFT"
    f = lambda c: fmt_plain(c, dec)  # noqa: E731
    rows: list[list] = [["Year", "Status", "Section", "Line", "Documents/days", "Gross", "Counted", "Reviewed", "Open"]]
    for sec in sm.sections:
        if sec.documents_only:  # documents are listed, but no amount counts towards anything
            for ln in sec.lines:
                rows.append([year, state, csv_safe(sec.label), csv_safe(ln.label), ln.count, "", "", "", ln.open_count])
            continue
        for ln in sec.lines:
            rows.append([year, state, csv_safe(sec.label), csv_safe(ln.label), ln.count, f(ln.gross_cents),
                         f(ln.counted_cents), f(ln.approved_cents), ln.open_count])
        rows.append([year, state, csv_safe(sec.label), "TOTAL", "", "", f(sec.total_cents), f(sec.approved_cents),
                     sec.open_count])
    rows += [[], ["Person", "Steuerklasse", "Faktor", "Office days", "Home office days", "Home days counted",
                  "Commute km (one way)", "Home office allowance", "Commuting allowance", "Documents (Werbungskosten)",
                  "Werbungskosten total", "Pauschbetrag", "Above Pauschbetrag"]]
    for ps in sm.people:
        p = ps.person
        rows.append([csv_safe(p.name), p.steuerklasse, "" if p.faktor is None else str(p.faktor).replace(".", dec),
                     ps.office_days, ps.home_days, ps.home_counted_days, p.commute_km, f(ps.home_cents),
                     f(ps.commute_cents), f(ps.doc_cents), f(ps.werbungskosten_cents), f(ps.pauschbetrag_cents),
                     "yes" if ps.above_pauschbetrag else "no"])
    return _response(rows, f"steuer-{year}-summary-{state}.csv")


@router.get("/export/documents.csv")
def documents_csv(request: Request, s: Session = Depends(get_db)):
    year, dec = get_year(request), _dec(request)
    sm = compute_year(s, year)
    state = "SIGNED-OFF" if sm.signed_off_at and not sm.changed_since_signoff else "DRAFT"
    cats = config.categories()
    names = {ps.person.id: ps.person.name for ps in sm.people}
    rows: list[list] = [["ID", "Year", "Person", "Category", "Section", "Date", "Vendor", "Amount", "Status",
                         "Excluded", "Flags", "Note", "Stored path", "SHA-256"]]
    for d in s.scalars(select(Document).where(Document.tax_year == year).order_by(Document.doc_date, Document.id)):
        cat = cats.get(d.category, {})
        rows.append([d.id, d.tax_year, csv_safe(names.get(d.person_id, "")), csv_safe(cat.get("label", d.category)),
                     csv_safe(config.sections().get(cat.get("section", ""), {}).get("label", "")),
                     d.doc_date.isoformat() if d.doc_date else "", csv_safe(d.vendor), fmt_plain(d.amount_cents, dec),
                     d.status, "yes" if d.excluded else "", csv_safe(" | ".join(d.flag_list)), csv_safe(d.note),
                     csv_safe(d.stored_path), d.sha256])
    return _response(rows, f"steuer-{year}-documents-{state}.csv")
