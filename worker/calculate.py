"""Totals per ELSTER section and per person. Rates/caps come from config/*.yml, never from code."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.models import UNSORTED, Document, Person, WorkDay, YearReview
from app.people import ensure_people, joint_assessment_hint, steuerklasse_warnings


@dataclass
class Line:
    key: str
    label: str
    count: int = 0
    gross_cents: int = 0
    counted_cents: int = 0
    approved_cents: int = 0
    open_count: int = 0
    note: str = ""
    virtual: bool = False
    capped: bool = False


@dataclass
class Section:
    key: str
    label: str
    hint: str
    lines: list[Line]
    per_person: bool = False
    compare_pauschbetrag: bool = False
    documents_only: bool = False  # documents are collected and reviewed, but add no amount to any total

    @property
    def doc_count(self) -> int:
        return sum(l.count for l in self.lines)

    @property
    def total_cents(self) -> int:
        return sum(l.counted_cents for l in self.lines)

    @property
    def approved_cents(self) -> int:
        return sum(l.approved_cents for l in self.lines)

    @property
    def open_count(self) -> int:
        return sum(l.open_count for l in self.lines)


@dataclass
class PersonSummary:
    person: Person
    office_days: int = 0
    home_days: int = 0
    away_days: int = 0
    home_counted_days: int = 0
    home_max_days: int = 0
    home_cents: int = 0
    commute_per_day_cents: int = 0
    commute_cents: int = 0
    doc_cents: int = 0
    pauschbetrag_cents: int = 0

    @property
    def werbungskosten_cents(self) -> int:
        return self.doc_cents + self.home_cents + self.commute_cents

    @property
    def above_pauschbetrag(self) -> bool:
        return self.werbungskosten_cents > self.pauschbetrag_cents


@dataclass
class YearSummary:
    year: int
    sections: list[Section]
    people: list[PersonSummary]
    counts: dict[str, int]
    docs_total: int
    open_items: int
    auto_items: int
    needs_person: int
    empty_categories: list[str]
    rules: dict
    warnings: list[str] = field(default_factory=list)
    hint: str | None = None
    signed_off_at: datetime | None = None
    changed_since_signoff: bool = False

    @property
    def snapshot(self) -> dict:
        return {"sections": {s.key: s.doc_count if s.documents_only else s.total_cents for s in self.sections},
                "docs": self.docs_total}


def _d(x) -> Decimal:
    return Decimal(str(x))


def _round(x: Decimal) -> int:
    return int(x.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _apply(gross: int, factor: float, cap_eur: float | None) -> tuple[int, bool]:
    v = _round(_d(gross) * _d(factor))
    if cap_eur is not None:
        cap = _round(_d(cap_eur) * 100)
        if v > cap:
            return cap, True
    return v, False


def compute_year(s: Session, year: int) -> YearSummary:
    cats, secs, rules = config.categories(), config.sections(), config.rules(year)
    people = ensure_people(s, year)
    multi = len(people) > 1
    sole_id = None if multi else people[0].id

    docs = list(s.scalars(select(Document).where(Document.tax_year == year, Document.excluded.is_(False))))
    per_person_cats = {k for k, c in cats.items() if secs.get(c["section"], {}).get("per_person")}

    grouped: dict[tuple[str, int | None], list[Document]] = defaultdict(list)
    for d in docs:
        if d.category in cats:
            grouped[(d.category, d.person_id or sole_id)].append(d)

    person_doc_cents: dict[int | None, int] = defaultdict(int)
    sections: list[Section] = []
    for skey, sconf in secs.items():
        per_person = bool(sconf.get("per_person"))
        docs_only = bool(sconf.get("documents_only"))
        lines: list[Line] = []
        for ckey, cconf in cats.items():
            if cconf["section"] != skey:
                continue
            factor, cap = cconf.get("factor", 1.0), cconf.get("cap_eur")
            if per_person:
                owners = [p.id for p in people] + [None]
                groups = [(oid, grouped.get((ckey, oid), [])) for oid in owners]
            else:
                groups = [(None, [d for (c, _o), ds in grouped.items() if c == ckey for d in ds])]
            line = Line(key=ckey, label=cconf["label"], note=cconf.get("hint", ""))
            for oid, ds in groups:
                if docs_only:
                    line.count += len(ds)
                    line.open_count += sum(1 for d in ds if d.is_open)
                    continue
                gross = sum(d.amount_cents or 0 for d in ds)
                counted, capped = _apply(gross, factor, cap)
                appr, _ = _apply(sum(d.amount_cents or 0 for d in ds if d.status == "approved"), factor, cap)
                line.count += len(ds)
                line.gross_cents += gross
                line.counted_cents += counted
                line.approved_cents += appr
                line.capped = line.capped or capped
                line.open_count += sum(1 for d in ds if d.is_open)
                if per_person:
                    person_doc_cents[oid] += counted
            lines.append(line)
        sections.append(Section(skey, sconf["label"], sconf.get("hint", ""), lines, per_person,
                                bool(sconf.get("compare_pauschbetrag")), docs_only))

    # work-day based allowances
    start, end = date(year, 1, 1), date(year, 12, 31)
    wd = defaultdict(int)
    for pid, kind, n in s.execute(
        select(WorkDay.person_id, WorkDay.kind, func.count()).where(WorkDay.day >= start, WorkDay.day <= end)
        .group_by(WorkDay.person_id, WorkDay.kind)
    ):
        wd[(pid, kind)] = n

    ho, cm = rules["homeoffice"], rules["commute"]
    pausch = _round(_d(rules["werbungskosten_pauschbetrag_eur"]) * 100)
    summaries: list[PersonSummary] = []
    for p in people:
        ps = PersonSummary(person=p, office_days=wd[(p.id, "office")], home_days=wd[(p.id, "home")],
                           away_days=wd[(p.id, "away")], home_max_days=int(ho["max_days"]),
                           pauschbetrag_cents=pausch)
        ps.home_counted_days = min(ps.home_days, ps.home_max_days)
        ps.home_cents = _round(_d(ps.home_counted_days) * _d(ho["rate_eur_per_day"]) * 100)
        km = max(int(p.commute_km or 0), 0)
        per_day_eur = (_d(min(km, 20)) * _d(cm["rate_first_20km_eur"])
                       + _d(max(km - 20, 0)) * _d(cm["rate_from_21km_eur"]))
        ps.commute_per_day_cents = _round(per_day_eur * 100)
        ps.commute_cents = _round(per_day_eur * 100 * ps.office_days)
        ps.doc_cents = person_doc_cents[p.id]
        summaries.append(ps)

    for sec in sections:
        if not config.sections()[sec.key].get("work_allowances"):
            continue
        extra: list[Line] = []
        for ps in summaries:
            suffix = f" - {ps.person.name}" if multi else ""
            extra.append(Line(
                key=f"homeoffice_{ps.person.id}", label=f"Home office allowance{suffix}", virtual=True,
                count=ps.home_days, gross_cents=ps.home_cents, counted_cents=ps.home_cents,
                approved_cents=ps.home_cents, capped=ps.home_days > ps.home_max_days,
                note=f"{ps.home_counted_days} of max {ps.home_max_days} days counted"))
            note = (f"{ps.office_days} office days x {ps.person.commute_km} km one way"
                    if ps.person.commute_km else "Set the one-way commute km on the People page")
            extra.append(Line(
                key=f"commute_{ps.person.id}", label=f"Commuting (Entfernungspauschale){suffix}", virtual=True,
                count=ps.office_days, gross_cents=ps.commute_cents, counted_cents=ps.commute_cents,
                approved_cents=ps.commute_cents, note=note))
        sec.lines = extra + sec.lines

    counts: dict[str, int] = defaultdict(int)
    for d in docs:
        counts[d.status] += 1
    needs_person = sum(1 for d in docs if multi and d.person_id is None and d.category in per_person_cats)
    open_items = sum(1 for d in docs if d.is_open) + needs_person
    auto_items = sum(1 for d in docs if d.status == "processed" and d.category != UNSORTED)

    warnings = steuerklasse_warnings(people)
    if rules["_source_year"] != year:
        warnings.append(f"No tax rules for {year} in config/tax_rules.yml - using {rules['_source_year']}. Check them.")
    if needs_person:
        warnings.append(f"{needs_person} work-related document(s) are not assigned to a person.")

    used = {d.category for d in docs}
    summary = YearSummary(
        year=year, sections=sections, people=summaries, counts=dict(counts), docs_total=len(docs),
        open_items=open_items, auto_items=auto_items, needs_person=needs_person,
        empty_categories=[k for k in cats if k not in used], rules=rules, warnings=warnings,
        hint=joint_assessment_hint(people),
    )
    review = s.get(YearReview, year)
    if review:
        summary.signed_off_at = review.signed_off_at
        try:
            summary.changed_since_signoff = json.loads(review.snapshot) != summary.snapshot
        except ValueError:
            summary.changed_since_signoff = True
    return summary
