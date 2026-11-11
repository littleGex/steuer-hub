from __future__ import annotations

from calendar import monthrange
from collections import Counter
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import require_auth
from ..db import get_db
from ..models import WORK_KINDS, Person, WorkDay
from ..people import ensure_people
from ..web import get_year, go, render, safe_next

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/workdays")
def page(request: Request, s: Session = Depends(get_db)):
    year = get_year(request)
    today = date.today()
    people = ensure_people(s, year)
    q = request.query_params
    pid = int(q["person"]) if q.get("person", "").isdigit() else people[0].id
    person = next((p for p in people if p.id == pid), people[0])
    month = int(q["m"]) if q.get("m", "").isdigit() else (today.month if year == today.year else 1)
    month = min(max(month, 1), 12)
    first, last = date(year, month, 1), date(year, month, monthrange(year, month)[1])
    kinds = {w.day: w.kind for w in s.scalars(
        select(WorkDay).where(WorkDay.person_id == person.id, WorkDay.day >= first, WorkDay.day <= last))}
    days = [{"day": first + timedelta(days=i), "kind": kinds.get(first + timedelta(days=i)),
             "weekend": (first + timedelta(days=i)).weekday() >= 5} for i in range((last - first).days + 1)]
    year_counts = Counter(w.kind for w in s.scalars(
        select(WorkDay).where(WorkDay.person_id == person.id, WorkDay.day >= date(year, 1, 1),
                              WorkDay.day <= date(year, 12, 31))))
    return render(request, "workdays.html", s, year, person=person, month=month, days=days,
                  month_name=first.strftime("%B"), year_counts=year_counts, today=today,
                  month_counts=Counter(kinds.values()))


def _check(s: Session, person_id: int, day: date) -> str | None:
    p = s.get(Person, person_id)
    if p is None or p.tax_year != day.year:
        return "That person is not set up for this tax year."
    return None


@router.post("/workdays/set")
def set_day(person_id: int = Form(...), day: str = Form(""), kind: str = Form(...),
            next_url: str = Form("/", alias="next"), s: Session = Depends(get_db)):
    dest = safe_next(next_url, "/")
    try:
        d = date.fromisoformat(day) if day else date.today()
    except ValueError:
        return go(dest, "Invalid date.", "err")
    if kind not in WORK_KINDS and kind != "clear":
        return go(dest, "Invalid kind.", "err")
    if (err := _check(s, person_id, d)):
        return go(dest, err, "err")
    if kind == "clear":
        existing = s.get(WorkDay, (person_id, d))
        if existing:
            s.delete(existing)
    else:
        s.merge(WorkDay(person_id=person_id, day=d, kind=kind))
    s.commit()
    return go(dest)


@router.post("/workdays/range")
def set_range(person_id: int = Form(...), start: str = Form(...), end: str = Form(...), kind: str = Form(...),
              weekdays_only: str = Form(""), next_url: str = Form("/", alias="next"), s: Session = Depends(get_db)):
    dest = safe_next(next_url, "/")
    try:
        a, b = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return go(dest, "Invalid date.", "err")
    if kind not in WORK_KINDS and kind != "clear":
        return go(dest, "Invalid kind.", "err")
    if b < a or (b - a).days > 366 or a.year != b.year:
        return go(dest, "Range must be within one calendar year and end after it starts.", "err")
    if (err := _check(s, person_id, a)):
        return go(dest, err, "err")
    n = 0
    for i in range((b - a).days + 1):
        d = a + timedelta(days=i)
        if weekdays_only == "1" and d.weekday() >= 5:
            continue
        existing = s.get(WorkDay, (person_id, d))
        if kind == "clear":
            if existing:
                s.delete(existing)
                n += 1
        else:
            s.merge(WorkDay(person_id=person_id, day=d, kind=kind))
            n += 1
    s.commit()
    return go(dest, f"{n} day(s) updated.")
