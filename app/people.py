"""People on the tax return (per tax year) and Steuerklasse sanity checks."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Person

KLASSEN = (1, 2, 3, 4, 5, 6)


def people_for(s: Session, year: int) -> list[Person]:
    return list(s.scalars(select(Person).where(Person.tax_year == year).order_by(Person.sort, Person.id)))


def ensure_people(s: Session, year: int) -> list[Person]:
    """Return the people for a year. First use: copy from the latest earlier year, else 'Person 1'."""
    people = people_for(s, year)
    if people:
        return people
    prev_year = s.scalar(select(Person.tax_year).where(Person.tax_year < year).order_by(Person.tax_year.desc()).limit(1))
    if prev_year is not None:
        for p in people_for(s, prev_year):
            s.add(Person(tax_year=year, name=p.name, steuerklasse=p.steuerklasse,
                         faktor=p.faktor, commute_km=p.commute_km, sort=p.sort))
    else:
        s.add(Person(tax_year=year, name="Person 1", steuerklasse=1, sort=0))
    s.commit()
    return people_for(s, year)


def steuerklasse_warnings(people: list[Person]) -> list[str]:
    out: list[str] = []
    for p in people:
        if p.steuerklasse not in KLASSEN:
            out.append(f"{p.name}: Steuerklasse {p.steuerklasse} is not valid (1-6).")
        if p.faktor is not None and p.steuerklasse != 4:
            out.append(f"{p.name}: a Faktor only applies to Steuerklasse IV.")
    if len(people) == 2:
        a, b = sorted(p.steuerklasse for p in people)
        married_like = any(k in (3, 4, 5) for k in (a, b))
        if married_like and (a, b) not in ((3, 5), (4, 4)):
            out.append(
                f"Steuerklassen {a}/{b} do not form a usual married-couple combination "
                "(III + V or IV + IV). Check them."
            )
    return out


def joint_assessment_hint(people: list[Person]) -> str | None:
    """Steuerklasse III/IV/V only exist for married couples / registered partners."""
    if any(p.steuerklasse in (3, 4, 5) for p in people):
        return ("Steuerklasse III/IV/V implies Zusammenveranlagung (joint assessment): "
                "one return, one Anlage N per person.")
    return None
