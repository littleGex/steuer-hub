"""Amount parsing/formatting. All money is stored as integer cents (EUR)."""
from __future__ import annotations

import re
from decimal import Decimal

# Matches 1.234,56 | 12,50 | 1,234.56 | 12.50 -- but not dates like 12.05.2026
AMOUNT_RE = re.compile(
    r"(?<![\d.,])"
    r"(?:\d{1,3}(?:\.\d{3})+,\d{2}"
    r"|\d+,\d{2}"
    r"|\d{1,3}(?:,\d{3})+\.\d{2}"
    r"|\d+\.\d{2})"
    r"(?!\d|[.,]\d)"
)
_VALID = re.compile(r"^\d+(\.\d{1,2})?$")


def to_cents(text: str | None) -> int | None:
    """Parse user/OCR input like '119,00', '1.234,56', '1,234.56', '1234' into cents."""
    if text is None:
        return None
    t = (
        text.strip().replace("€", "").replace("EUR", "").replace(" ", "").replace(" ", "")
    )
    if not t:
        return None
    neg = t.startswith("-")
    t = t.lstrip("+-")
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
    elif "," in t:
        dec = ","
    elif "." in t:
        dec = "." if len(t.rsplit(".", 1)[1]) != 3 else None  # "1.234" = thousands
    else:
        dec = None
    if dec is None:
        t = t.replace(".", "").replace(",", "")
    elif dec == ",":
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", "")
    if not _VALID.match(t):
        return None
    cents = int((Decimal(t) * 100).to_integral_value())
    return -cents if neg else cents


def fmt_eur(cents: int | None) -> str:
    if cents is None:
        return "–"
    s = f"{Decimal(cents) / 100:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def fmt_plain(cents: int | None, decimal: str = ",") -> str:
    """Number without currency, for CSV. decimal=',' (German Excel) or '.'."""
    if cents is None:
        return ""
    s = f"{Decimal(cents) / 100:.2f}"
    return s.replace(".", ",") if decimal == "," else s
