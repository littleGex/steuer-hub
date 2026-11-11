"""Progress layer: readiness score, work-day streaks, month stamps, achievements, pipeline status.

Everything is derived from existing data on each request and nothing is stored. The points reward
completeness and review, never the size of a deduction or the number of home-office days.
"""
from __future__ import annotations

import hashlib
import json
from calendar import month_name, monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Document, WorkDay
from .settings import get_settings

# Document pipeline: where a document "is", by status. Single source of truth for server and browser.
STATIONS = (
    ("dropoff", "Drop-off", "📥"),
    ("reading", "Reading desk", "🔍"),
    ("review", "Review", "👀"),
    ("filed", "Filed", "🗂️"),
)
STATION_OF = {
    "pending": "dropoff",
    "processing": "reading",
    "processed": "review",
    "needs_review": "review",
    "error": "review",
    "approved": "filed",
}


def station_for(status: str) -> str:
    return STATION_OF.get(status, "review")


def weekdays(a: date, b: date) -> list[date]:
    if b < a:
        return []
    out = []
    for i in range((b - a).days + 1):
        d = a + timedelta(days=i)
        if d.weekday() < 5:
            out.append(d)
    return out


@dataclass
class Component:
    key: str
    label: str
    weight: int
    score: float          # 0..1
    hint: str
    action: str           # call to action shown as "Next: ..."
    href: str

    @property
    def points(self) -> int:
        return int(self.weight * self.score + 0.5)


@dataclass
class PersonStreak:
    name: str
    person_id: int = 0
    current: int = 0
    longest: int = 0
    logged: int = 0
    elapsed: int = 0
    missing_total: int = 0
    missing: list[date] = field(default_factory=list)   # most recent first, capped


@dataclass
class MonthStatus:
    month: int
    name: str
    state: str            # closed | partial | current | future
    logged: int = 0
    expected: int = 0
    open_docs: int = 0


@dataclass
class Achievement:
    key: str
    title: str
    desc: str
    icon: str
    earned: bool
    progress: str = ""


@dataclass
class Progress:
    year: int
    score: int
    components: list[Component]
    next_step: Component | None
    streaks: list[PersonStreak]
    months: list[MonthStatus]
    closed_months: int
    achievements: list[Achievement]

    @property
    def earned(self) -> list[Achievement]:
        return [a for a in self.achievements if a.earned]

    @property
    def best_streak(self) -> int:
        return max((s.current for s in self.streaks), default=0)


def _backup_count() -> int:
    try:
        return len(list(get_settings().backup_dir.glob("steuer-*.db")))
    except OSError:
        return 0


def compute_progress(s: Session, year: int, sm, today: date | None = None) -> Progress:
    today = today or date.today()
    start, end = date(year, 1, 1), date(year, 12, 31)
    if year < today.year:
        through = end                                   # the whole year is in the past
    elif year == today.year:
        through = today - timedelta(days=1)             # today is still in progress
    else:
        through = start - timedelta(days=1)             # future year: nothing has elapsed

    people = [ps.person for ps in sm.people]
    logged: dict[int, set[date]] = {p.id: set() for p in people}
    for pid, day in s.execute(select(WorkDay.person_id, WorkDay.day).where(WorkDay.day >= start, WorkDay.day <= end)):
        if pid in logged:
            logged[pid].add(day)

    # --- streaks (weekdays only; weekends never break a streak) ---
    wds = weekdays(start, through)
    streaks: list[PersonStreak] = []
    for p in people:
        have = logged[p.id]
        longest = run = 0
        for d in wds:
            run = run + 1 if d in have else 0
            longest = max(longest, run)
        current = 0
        if year == today.year:
            seq = wds + ([today] if today.weekday() < 5 and today in have else [])
            for d in reversed(seq):
                if d not in have:
                    break
                current += 1
            longest = max(longest, current)
        gaps = [d for d in reversed(wds) if d not in have]
        streaks.append(PersonStreak(
            name=p.name, person_id=p.id, current=current, longest=longest, logged=sum(1 for d in wds if d in have),
            elapsed=len(wds), missing_total=len(gaps), missing=gaps[:8]))

    # --- month stamps ---
    docs = list(s.scalars(select(Document).where(Document.tax_year == year, Document.excluded.is_(False))))
    months: list[MonthStatus] = []
    for m in range(1, 13):
        first, last_day = date(year, m, 1), date(year, m, monthrange(year, m)[1])
        if year > today.year or (year == today.year and first > today):
            months.append(MonthStatus(m, month_name[m], "future"))
            continue
        upto = last_day if (year < today.year or last_day < today) else through
        days = weekdays(first, min(upto, last_day))
        expected = len(days) * len(people)
        got = sum(1 for p in people for d in days if d in logged[p.id])
        open_docs = sum(1 for d in docs if d.doc_date and first <= d.doc_date <= last_day and d.is_open)
        if upto == last_day:
            state = "closed" if (got == expected and open_docs == 0 and expected > 0) else "partial"
        else:
            state = "current"
        months.append(MonthStatus(m, month_name[m], state, got, expected, open_docs))
    closed = sum(1 for m in months if m.state == "closed")

    # --- readiness score ---
    total, approved, open_items = sm.docs_total, sm.counts.get("approved", 0), sm.open_items
    logged_total, elapsed_total = sum(x.logged for x in streaks), sum(x.elapsed for x in streaks)
    wd_score = logged_total / elapsed_total if elapsed_total else (1.0 if year == today.year else 0.0)
    signed = bool(sm.signed_off_at) and not sm.changed_since_signoff
    review_url, wd_url, home_url = f"/review?year={year}", f"/workdays?year={year}", f"/?year={year}"
    comps = [
        Component(
            "approved", "Documents approved", 30, (approved / total) if total else 0.0,
            f"{approved} of {total} documents approved" if total else "No documents yet",
            f"Approve {total - approved} document(s)" if total else "Upload your first receipt",
            review_url if total else home_url),
        Component(
            "open", "Nothing left open", 20, max(0.0, 1 - min(1.0, open_items / total)) if total else 0.0,
            f"{open_items} open item(s)" if open_items else ("Nothing open" if total else "No documents yet"),
            f"Clear {open_items} open item(s)", review_url),
        Component(
            "workdays", "Work days logged", 30, wd_score,
            f"{logged_total} of {elapsed_total} past weekdays logged" if elapsed_total else "Nothing to log yet",
            f"Log {elapsed_total - logged_total} missing weekday(s)", wd_url),
        Component(
            "signoff", "Signed off", 20, 1.0 if signed else 0.0,
            "Signed off" if signed else ("Figures changed since sign-off" if sm.signed_off_at else "Not signed off yet"),
            "Sign off the year", review_url),
    ]
    score = sum(c.points for c in comps)
    candidates = [c for c in comps if c.score < 1 and not (c.key == "signoff" and open_items)]
    next_step = max(candidates, key=lambda c: c.weight * (1 - c.score), default=None)

    # --- achievements ---
    longest_any = max((x.longest for x in streaks), default=0)
    backups = _backup_count()
    achievements = [
        Achievement("first_receipt", "First receipt", "Upload your first document.", "📄",
                    total >= 1, f"{min(total, 1)}/1"),
        Achievement("ten_approved", "Ten approved", "Approve ten documents.", "✅",
                    approved >= 10, f"{min(approved, 10)}/10"),
        Achievement("eagle_eye", "Eagle eye", "Correct a reading mistake by hand.", "🦅",
                    any(d.manually_edited for d in docs)),
        Achievement("inbox_zero", "Inbox zero", "Have documents and nothing left open.", "📭",
                    total >= 1 and open_items == 0),
        Achievement("streak_5", "On a roll", "Log five weekdays in a row.", "🔥",
                    longest_any >= 5, f"{min(longest_any, 5)}/5"),
        Achievement("streak_20", "Habit formed", "Log twenty weekdays in a row.", "🚀",
                    longest_any >= 20, f"{min(longest_any, 20)}/20"),
        Achievement("perfect_month", "Perfect month", "Close a month: every weekday logged, nothing open.", "🗓️",
                    closed >= 1, f"{min(closed, 1)}/1"),
        Achievement("perfect_year", "Perfect year", "Close all twelve months.", "🏆",
                    closed == 12, f"{closed}/12"),
        Achievement("signed_off", "Signed, sealed", "Sign off the year and keep it unchanged.", "✍️", signed),
        Achievement("backup_hero", "Backup hero", "Keep seven nightly database backups.", "💾",
                    backups >= 7, f"{min(backups, 7)}/7"),
    ]
    return Progress(year, score, comps, next_step, streaks, months, closed, achievements)


def earned_payload(prog: Progress, year: int) -> dict:
    return {"year": year, "earned": [{"key": a.key, "title": a.title, "icon": a.icon} for a in prog.earned]}


def status_payload(s: Session, year: int, sm, prog: Progress, worker: str, nas_ok: bool) -> dict:
    """What the dashboard animation polls. `sig` changes whenever anything visible changes."""
    recent = list(s.scalars(select(Document).where(Document.tax_year == year, Document.excluded.is_(False))
                            .order_by(Document.id.desc()).limit(40)))
    stations = {key: 0 for key, _n, _i in STATIONS}
    for status, n in sm.counts.items():
        stations[station_for(status)] += n
    docs = [{"id": d.id, "label": (d.vendor or d.original_name)[:40], "status": d.status,
             "station": station_for(d.status)} for d in recent]
    earned = earned_payload(prog, year)["earned"]
    sig = hashlib.sha1(json.dumps(
        [[d["id"], d["status"]] for d in docs] + [stations, prog.score, sm.open_items,
                                                  bool(sm.signed_off_at), [a["key"] for a in earned]],
        sort_keys=True).encode()).hexdigest()[:12]
    return {"year": year, "worker": worker, "nas_ok": nas_ok, "docs": docs, "stations": stations,
            "open_items": sm.open_items, "score": prog.score, "signed_off": bool(sm.signed_off_at),
            "earned": earned, "sig": sig}
