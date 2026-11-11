"""Text extraction (PDF text layer, else Tesseract OCR) and heuristic parsing of date/amount/vendor.

Everything here is a SUGGESTION. Low confidence or any flag sends the document to the review queue.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.money import AMOUNT_RE, to_cents

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


@dataclass
class Extraction:
    text: str = ""
    doc_date: date | None = None
    amount_cents: int | None = None
    vendor: str | None = None
    confidence: float = 0.0
    flags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- reading text

def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _ocr_image(path: Path, lang: str, flags: list[str]) -> str:
    if not shutil.which("tesseract"):
        flags.append("OCR unavailable (tesseract not installed) - enter values manually")
        return ""
    try:
        r = _run(["tesseract", str(path), "stdout", "-l", lang], 180)
        if r.returncode != 0 and lang != "eng":
            r = _run(["tesseract", str(path), "stdout", "-l", "eng"], 180)
            flags.append(f"OCR language '{lang}' unavailable, used English")
    except subprocess.TimeoutExpired:
        flags.append("OCR timed out")
        return ""
    if r.returncode != 0:
        flags.append(f"OCR failed: {r.stderr.strip()[:120]}")
        return ""
    return r.stdout


def _ocr_pdf(path: Path, lang: str, flags: list[str]) -> str:
    if not shutil.which("pdftoppm"):
        flags.append("Scanned PDF but pdftoppm (poppler) not installed - enter values manually")
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            r = _run(["pdftoppm", "-r", "200", "-l", "3", "-png", str(path), str(Path(tmp) / "p")], 180)
        except subprocess.TimeoutExpired:
            flags.append("PDF rasterising timed out")
            return ""
        if r.returncode != 0:
            flags.append("Could not rasterise PDF for OCR")
            return ""
        return "\n".join(_ocr_image(p, lang, flags) for p in sorted(Path(tmp).glob("p*.png")))


def read_text(path: Path, lang: str) -> tuple[str, list[str]]:
    flags: list[str] = []
    ext = path.suffix.lower()
    text = ""
    if ext == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:10])
        except Exception as exc:  # corrupt / encrypted PDFs
            flags.append(f"PDF unreadable: {type(exc).__name__}")
        if len(text.strip()) < 20:
            text = _ocr_pdf(path, lang, flags) or text
    elif ext in IMAGE_EXT:
        text = _ocr_image(path, lang, flags)
    elif ext == ".txt":
        text = path.read_text(encoding="utf-8", errors="ignore")
    else:
        flags.append(f"Unsupported file type {ext or '(none)'}")
    return text, flags


# ---------------------------------------------------------------- parsing

_TIER1 = ("gesamtbetrag", "rechnungsbetrag", "endbetrag", "zahlbetrag", "zahlungsbetrag", "zu zahlen",
          "gesamtsumme", "amount due", "total due", "grand total", "total")
_TIER2 = ("summe", "gesamt", "betrag", "brutto", "beitrag", "amount", "sum")
_SKIP = ("zwischensumme", "subtotal", "netto", "davon")


def _amounts(line: str) -> list[int]:
    return [c for m in AMOUNT_RE.finditer(line) if (c := to_cents(m.group())) is not None]


def parse_amount(text: str) -> tuple[int | None, float, list[str]]:
    lines = [ln.strip() for ln in text.splitlines()]
    tiers: dict[int, list[int]] = {1: [], 2: []}
    for i, raw in enumerate(lines):
        low = raw.lower()
        if not low or any(s in low for s in _SKIP):
            continue
        tier = 1 if any(k in low for k in _TIER1) else 2 if any(k in low for k in _TIER2) else 0
        if not tier:
            continue
        found = _amounts(raw) or (_amounts(lines[i + 1]) if i + 1 < len(lines) else [])
        if found:
            tiers[tier].append(max(found))
    if tiers[1]:
        vals = tiers[1]
        if len(set(vals)) == 1:
            return vals[0], 0.9, []
        return max(vals), 0.75, ["Several different totals found - check the amount"]
    if tiers[2]:
        return max(tiers[2]), 0.6, []
    allv = _amounts(text)
    if allv:
        return max(allv), 0.4, ["No total label found - took the largest amount"]
    return None, 0.0, ["No amount found"]


_DMY = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?!\d)")
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DATE_KEYS = ("rechnungsdatum", "belegdatum", "datum", "date", "rechnung vom", "ausgestellt")


def _dates_in(line: str) -> list[date]:
    out: list[date] = []
    limit = date.today().year + 1
    for m in _DMY.finditer(line):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
        out.append((y, mo, d))
    for m in _ISO.finditer(line):
        out.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    valid = []
    for y, mo, d in out:
        try:
            dt = date(y, mo, d)
        except ValueError:
            continue
        if 2000 <= dt.year <= limit:
            valid.append(dt)
    return valid


def parse_date(text: str) -> tuple[date | None, float, list[str]]:
    lines = [ln.strip() for ln in text.splitlines()]
    for i, raw in enumerate(lines):
        if any(k in raw.lower() for k in _DATE_KEYS):
            found = _dates_in(raw) or (_dates_in(lines[i + 1]) if i + 1 < len(lines) else [])
            if found:
                return found[0], 0.9, []
    for raw in lines:
        found = _dates_in(raw)
        if found:
            return found[0], 0.6, []
    return None, 0.0, ["No date found"]


_VENDOR_SKIP = re.compile(
    r"^(rechnung|invoice|datum|date|kunde|kundennummer|seite|page|tel|fax|www|http|iban|bic|ust|steuer|"
    r"beleg|quittung|an:|to:)", re.I)


def parse_vendor(text: str, fallback_name: str = "") -> str | None:
    seen = 0
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        seen += 1
        if seen > 8:
            break
        letters = sum(c.isalpha() for c in ln)
        digits = sum(c.isdigit() for c in ln)
        if letters < 3 or digits > letters * 0.4 or _VENDOR_SKIP.match(ln):
            continue
        return ln[:60]
    return Path(fallback_name).stem[:60] or None


# ---------------------------------------------------------------- entry point

def extract_document(path: Path, lang: str, tax_year: int) -> Extraction:
    text, flags = read_text(path, lang)
    ex = Extraction(text=text, flags=list(flags))
    if not text.strip():
        if not ex.flags:
            ex.flags.append("No text found in document")
        ex.vendor = parse_vendor("", path.name)
        return ex
    ex.amount_cents, a_conf, a_flags = parse_amount(text)
    ex.doc_date, d_conf, d_flags = parse_date(text)
    ex.vendor = parse_vendor(text, path.name)
    ex.flags += a_flags + d_flags
    if ex.doc_date and ex.doc_date.year != tax_year:
        ex.flags.append(f"Date is outside {tax_year} - expenses count in the year they were paid, check")
    ex.confidence = round(min(a_conf, d_conf), 2)
    return ex
