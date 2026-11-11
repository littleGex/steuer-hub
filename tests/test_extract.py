from datetime import date

import pytest

from app.money import fmt_eur, to_cents
from worker.extract import extract_document, parse_amount, parse_date, parse_vendor

from .helpers import INVOICE_LINES, make_pdf


@pytest.mark.parametrize("text,cents", [
    ("119,00", 11900), ("1.234,56", 123456), ("1,234.56", 123456), ("1234", 123400),
    ("1.234", 123400), ("12,5", 1250), ("12.50", 1250), ("119,00 €", 11900), ("abc", None), ("", None),
    ("1,2345", None),
])
def test_to_cents(text, cents):
    assert to_cents(text) == cents


def test_fmt_eur():
    assert fmt_eur(123456) == "1.234,56 €"
    assert fmt_eur(None) == "–"


def test_dates_are_not_amounts():
    cents, conf, flags = parse_amount("Datum 12.05.2026\nGesamtbetrag 1.234,56 EUR")
    assert cents == 123456 and conf == 0.9 and not flags


def test_amount_prefers_total_over_subtotal():
    text = "Zwischensumme 100,00\nMwSt 19,00\nGesamtbetrag 119,00"
    assert parse_amount(text)[0] == 11900


def test_amount_fallback_flags():
    cents, conf, flags = parse_amount("Kaffee 3,50\nKuchen 4,20")
    assert cents == 420 and conf < 0.7 and flags


def test_date_keyword_and_fallback():
    assert parse_date("Rechnungsdatum: 03.03.2026")[0] == date(2026, 3, 3)
    d, conf, _ = parse_date("irgendwas 2026-04-05 text")
    assert d == date(2026, 4, 5) and conf == 0.6
    assert parse_date("nothing here")[0] is None


def test_vendor():
    assert parse_vendor("\n\nStadtwerke Bremen AG\nRechnung 1") == "Stadtwerke Bremen AG"


def test_pdf_pipeline(tmp_path):
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(make_pdf(INVOICE_LINES))
    ex = extract_document(pdf, "deu+eng", 2026)
    assert ex.amount_cents == 11900
    assert ex.doc_date == date(2026, 3, 3)
    assert ex.vendor == "Stadtwerke Bremen AG"
    assert ex.confidence == 0.9 and not ex.flags


def test_date_outside_year_is_flagged(tmp_path):
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(make_pdf(INVOICE_LINES))
    ex = extract_document(pdf, "deu+eng", 2025)
    assert any("outside 2025" in f for f in ex.flags)
