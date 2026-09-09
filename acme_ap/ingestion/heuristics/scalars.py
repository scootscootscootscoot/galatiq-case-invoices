"""Scalar normalisers: money, quantities, currency, dates.

These are the parsers that repair OCR damage in a controlled way. A capital O
becomes a zero only when we already know the field is numeric, so a vendor name
can never be corrupted by the repair.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime

_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}
_OCR_DIGITS = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "S": "5"})


def parse_money(raw: str | float | int | None) -> float | None:
    """Parse a monetary string, repairing OCR letter/digit confusion.

    ``$3,500.O0`` is a capital letter O where a zero belongs -- the single most
    common scan artifact in the sample data. Substitution happens only inside a
    field already known to be numeric, so it can never corrupt a vendor name.
    """
    if raw is None:
        return None
    if isinstance(raw, int | float):
        return float(raw) if math.isfinite(raw) else None

    text = raw.strip()
    if not text:
        return None

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1].strip()
    text = re.sub(r"\b(?:USD|EUR|GBP|CAD|JPY|AUD|CHF)\b", "", text, flags=re.IGNORECASE)
    text = text.strip().lstrip("$€£").strip().removesuffix("%").strip()
    cleaned = text.translate(_OCR_DIGITS)
    # Never manufacture money by deleting arbitrary words or joining two values.
    if not re.fullmatch(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", cleaned):
        return None
    cleaned = cleaned.replace(",", "")
    if not cleaned or cleaned in {"-", "."}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return (-value if negative else value) if math.isfinite(value) else None


def parse_quantity(raw: str | float | int | None) -> float | None:
    """Parse a quantity, preserving sign so negatives can be flagged."""
    return parse_money(raw)


def detect_currency(text: str, default: str = "USD") -> str:
    """Pick a currency from an explicit code, else a symbol, else the default."""
    if match := re.search(r"\b(USD|EUR|GBP|CAD|JPY|AUD|CHF)\b", text):
        return match.group(1)
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    return default


_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%b-%Y",
    "%d %b %Y",
    "%b %d %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%B %d, %Y",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%m-%d-%Y",
)


def _repair_ocr_digits(text: str) -> str:
    """Fix O/0 confusion inside year strings like ``2O26``."""
    text = re.sub(
        r"\b([12])([O0o])([0-9O o]{2})\b",
        lambda m: (m.group(1) + m.group(2) + m.group(3)).translate(_OCR_DIGITS),
        text,
    )
    return re.sub(r"(?<=\d)[Oo](?=\d)|(?<=\d)[Oo]\b", "0", text)


def parse_date(raw: str | None) -> date | None:
    """Parse a date in any format the sample data uses.

    Returns ``None`` for genuinely unparseable values -- ``"yesterday"`` on
    invoice 1003 is one. That ``None`` is signal, not failure: it becomes a
    finding rather than being quietly coerced into today's date.
    """
    if not raw:
        return None
    text = str(raw).strip().strip(".,;")
    if not text or text.lower() in {"null", "none", "n/a", "tbd", ""}:
        return None

    text = _repair_ocr_digits(text)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    if m := re.match(r"(\d{1,2})[-\s]([A-Za-z]{3,})[-\s](\d{4})", text):
        month = _MONTHS.get(m.group(2)[:3].lower())
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(1)))
            except ValueError:
                return None
    if m := re.match(r"([A-Za-z]{3,})\s+(\d{1,2}),?\s+(\d{4})", text):
        month = _MONTHS.get(m.group(1)[:3].lower())
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                return None
    return None
