from __future__ import annotations

from calendar import monthrange
from collections import Counter
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..auth import require_auth
from ..db import get_db
from ..models import WORK_KINDS, Person, WorkDay
from ..people import ensure_people
from ..web import get_year, go, render, safe_next

router = APIRouter(dependencies=[Depends(require_auth)])


EDITABLE_KINDS = ("office", "home")  # kinds the month cards adjust; "away" is set per day


def _month_days(year: int, month: int) -> list[date]:
    return [date(year, month, d) for d in range(1, monthrange(year, month)[1] + 1)]


def _weekdays(year: int, month: int) -> list[date]:
    return [d for d in _month_days(year, month) if d.weekday() < 5]


def _month_rows(s: Session, person_id: int, year: int, month: int) -> dict[date, str]:
    days = _month_days(year, month)
    return {w.day: w.kind for w in s.scalars(
        select(WorkDay).where(WorkDay.person_id == person_id, WorkDay.day >= days[0], WorkDay.day <= days[-1]))}


def month_summary(s: Session, person_id: int, year: int, month: int) -> dict:
    rows = _month_rows(s, person_id, year, month)
    counts = Counter(rows.values())
    weekdays = _weekdays(year, month)
    return {
        "month": month, "name": date(year, month, 1).strftime("%B"), "short": date(year, month, 1).strftime("%b"),
        "office": counts.get("office", 0), "home": counts.get("home", 0), "away": counts.get("away", 0),
        "weekdays": len(weekdays), "free": sum(1 for d in weekdays if d not in rows),
    }


def year_counts(s: Session, person_id: int, year: int) -> dict[str, int]:
    c = Counter(w.kind for w in s.scalars(
        select(WorkDay).where(WorkDay.person_id == person_id, WorkDay.day >= date(year, 1, 1),
                              WorkDay.day <= date(year, 12, 31))))
    return {k: c.get(k, 0) for k in WORK_KINDS}


def set_month_count(s: Session, person_id: int, year: int, month: int, kind: str, target: int) -> str | None:
    """Bring the number of `kind` days in a month to `target`.

    Lowering clears the latest days of that kind; raising marks the earliest free weekdays. Days already set to
    another kind are never overwritten, and weekends are only filled from the day calendar.
    Returns an error message, or None on success.
    """
    rows = _month_rows(s, person_id, year, month)
    current = sorted(d for d, k in rows.items() if k == kind)
    if target < len(current):
        for d in current[target:]:
            s.delete(s.get(WorkDay, (person_id, d)))
    elif target > len(current):
        free = [d for d in _weekdays(year, month) if d not in rows]
        need = target - len(current)
        if need > len(free):
            name = date(year, month, 1).strftime("%B")
            return (f"{name} has only {len(free)} free weekday(s) left. "
                    "Lower another count or set weekend days in the day calendar.")
        for d in free[:need]:
            s.add(WorkDay(person_id=person_id, day=d, kind=kind))
    s.commit()
    return None


@router.get("/workdays")
def page(request: Request, s: Session = Depends(get_db)):
    year = get_year(request)
    today = date.today()
    people = ensure_people(s, year)
    q = request.query_params
    pid = int(q["person"]) if q.get("person", "").isdigit() else people[0].id
    person = next((p for p in people if p.id == pid), people[0])
    max_home = int(config.rules(year)["homeoffice"]["max_days"])
    if not q.get("m", "").isdigit():
        months = [month_summary(s, person.id, year, m) for m in range(1, 13)]
        return render(request, "workdays_year.html", s, year, person=person, months=months,
                      totals=year_counts(s, person.id, year), max_home=max_home, today=today)
    month = min(max(int(q["m"]), 1), 12)
    first, last = date(year, month, 1), date(year, month, monthrange(year, month)[1])
    kinds = {w.day: w.kind for w in s.scalars(
        select(WorkDay).where(WorkDay.person_id == person.id, WorkDay.day >= first, WorkDay.day <= last))}
    days = [{"day": first + timedelta(days=i), "kind": kinds.get(first + timedelta(days=i)),
             "weekend": (first + timedelta(days=i)).weekday() >= 5} for i in range((last - first).days + 1)]
    return render(request, "workdays.html", s, year, person=person, month=month, days=days,
                  month_name=first.strftime("%B"), year_counts=year_counts(s, person.id, year), today=today,
                  month_counts=Counter(kinds.values()), max_home=max_home)


@router.post("/workdays/month")
def set_month(request: Request, person_id: int = Form(...), year: int = Form(...), month: int = Form(...),
              office: str = Form(""), home: str = Form(""), next_url: str = Form("/", alias="next"),
              s: Session = Depends(get_db)):
    """Set office/home totals for one month. Answers JSON for the month cards' script, else redirects."""
    wants_json = "application/json" in request.headers.get("accept", "")
    dest = safe_next(next_url, f"/workdays?year={year}&person={person_id}")

    def reply(err: str | None):
        if wants_json:
            body = {"ok": err is None, "error": err}
            if 1 <= month <= 12 and (p := s.get(Person, person_id)) is not None and p.tax_year == year:
                body["month"] = month_summary(s, person_id, year, month)
                body["totals"] = year_counts(s, person_id, year)
            return JSONResponse(body, status_code=200 if err is None else 400)
        return go(dest, err, "err") if err else go(dest)

    if not 1 <= month <= 12 or not 1990 <= year <= 2100:
        return reply("Invalid month.")
    if (err := _check(s, person_id, date(year, month, 1))):
        return reply(err)
    targets = {}
    for kind, raw in (("office", office), ("home", home)):
        if raw.strip() == "":
            continue
        if not raw.strip().isdigit() or int(raw) > 31:
            return reply("Enter a whole number of days between 0 and 31.")
        targets[kind] = int(raw)
    # lower counts first, so the days they free up can be used by the other kind; then office before home
    current = month_summary(s, person_id, year, month)
    for kind in sorted(targets, key=lambda k: (targets[k] > current[k], EDITABLE_KINDS.index(k))):
        if (err := set_month_count(s, person_id, year, month, kind, targets[kind])):
            s.rollback()
            return reply(err)
    return reply(None)


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
