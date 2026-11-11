from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import require_auth
from ..db import get_db
from ..models import Document, Person, WorkDay
from ..people import KLASSEN, ensure_people, joint_assessment_hint, steuerklasse_warnings
from ..web import get_year, go, render

router = APIRouter(dependencies=[Depends(require_auth)])


def _parse(name: str, steuerklasse: int, faktor: str, commute_km: str) -> tuple[str, int, float | None, int]:
    name = name.strip()[:80]
    if not name:
        raise ValueError("Name is required.")
    if steuerklasse not in KLASSEN:
        raise ValueError("Steuerklasse must be 1-6.")
    fk = None
    if faktor.strip():
        try:
            fk = float(faktor.replace(",", "."))
        except ValueError:
            raise ValueError("Faktor must be a number like 0,85.") from None
        if not 0 < fk < 1:
            raise ValueError("Faktor must be between 0 and 1.")
    try:
        km = int(commute_km or 0)
    except ValueError:
        raise ValueError("Commute km must be a whole number.") from None
    if not 0 <= km <= 500:
        raise ValueError("Commute km must be between 0 and 500.")
    return name, steuerklasse, fk, km


@router.get("/people")
def page(request: Request, s: Session = Depends(get_db)):
    year = get_year(request)
    people = ensure_people(s, year)
    return render(request, "people.html", s, year, klassen=KLASSEN,
                  warnings=steuerklasse_warnings(people), hint=joint_assessment_hint(people))


@router.post("/people/add")
def add(year: int = Form(...), name: str = Form(...), steuerklasse: int = Form(1), faktor: str = Form(""),
        commute_km: str = Form("0"), s: Session = Depends(get_db)):
    back = f"/people?year={year}"
    try:
        n, k, f, km = _parse(name, steuerklasse, faktor, commute_km)
    except ValueError as exc:
        return go(back, str(exc), "err")
    order = (s.scalar(select(func.max(Person.sort)).where(Person.tax_year == year)) or 0) + 1
    ensure_people(s, year)
    s.add(Person(tax_year=year, name=n, steuerklasse=k, faktor=f, commute_km=km, sort=order))
    s.commit()
    return go(back, "Person added.")


@router.post("/people/{pid}")
def update(pid: int, name: str = Form(...), steuerklasse: int = Form(1), faktor: str = Form(""),
           commute_km: str = Form("0"), s: Session = Depends(get_db)):
    p = s.get(Person, pid)
    if p is None:
        raise HTTPException(404, "Person not found")
    back = f"/people?year={p.tax_year}"
    try:
        p.name, p.steuerklasse, p.faktor, p.commute_km = _parse(name, steuerklasse, faktor, commute_km)
    except ValueError as exc:
        s.rollback()
        return go(back, str(exc), "err")
    s.commit()
    return go(back, "Saved.")


@router.post("/people/{pid}/delete")
def delete(pid: int, s: Session = Depends(get_db)):
    p = s.get(Person, pid)
    if p is None:
        raise HTTPException(404, "Person not found")
    back = f"/people?year={p.tax_year}"
    used = (s.scalar(select(func.count(Document.id)).where(Document.person_id == pid)) or 0) + (
        s.scalar(select(func.count()).select_from(WorkDay).where(WorkDay.person_id == pid)) or 0)
    if used:
        return go(back, f"{p.name} still has documents or work days - reassign or remove them first.", "err")
    if len(ensure_people(s, p.tax_year)) <= 1:
        return go(back, "At least one person is needed.", "err")
    s.delete(p)
    s.commit()
    return go(back, "Person removed.")
