from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

OPEN_STATUSES = ("pending", "processing", "needs_review", "error")
UNSORTED = "unsorted"  # category of files dropped in _inbox until the user picks one
WORK_KINDS = ("office", "home", "away")


class Base(DeclarativeBase):
    pass


class Person(Base):
    """A person on the tax return, per tax year (Steuerklasse can change)."""

    __tablename__ = "persons"
    id: Mapped[int] = mapped_column(primary_key=True)
    tax_year: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(80))
    steuerklasse: Mapped[int] = mapped_column(Integer, default=1)  # 1..6
    faktor: Mapped[float | None] = mapped_column(Float, nullable=True)  # Steuerklasse IV mit Faktor
    commute_km: Mapped[int] = mapped_column(Integer, default=0)  # one-way, full km
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("tax_year", "sha256", name="uq_doc_year_sha"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tax_year: Mapped[int] = mapped_column(Integer, index=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id"), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))  # relative to NAS root
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(120), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    flags: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    text_excerpt: Mapped[str] = mapped_column(Text, default="")
    manually_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def flag_list(self) -> list[str]:
        return [f for f in (self.flags or "").split("\n") if f]

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES or self.category == UNSORTED


class WorkDay(Base):
    """One row per person per day: office, home or away (leave/travel/sick)."""

    __tablename__ = "workdays"
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))


class YearReview(Base):
    __tablename__ = "year_reviews"
    tax_year: Mapped[int] = mapped_column(Integer, primary_key=True)
    signed_off_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    snapshot: Mapped[str] = mapped_column(Text, default="{}")
