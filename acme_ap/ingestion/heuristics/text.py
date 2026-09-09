"""Free-text invoice parsing.

Labelled plain text is parsed with a priority ladder of label aliases, which
is where the messier documents (emails, flat text, school-report reports) come
from. Structured parsers sit in :mod:`~acme_ap.ingestion.heuristics.structured`.
"""

from __future__ import annotations

import re

from acme_ap.ingestion.heuristics.scalars import (
    detect_currency,
    parse_date,
    parse_money,
    parse_quantity,
)
from acme_ap.models import ExtractedInvoice, LineItem

# Label spellings observed across the sample documents, including the typo'd
# variants on invoice 1002. Kept as data so a new dialect is a one-line change.
_LABELS: dict[str, tuple[str, ...]] = {
    "invoice_number": (
        "invoice number",
        "invoice #",
        "inv #",
        "inv no",
        "inv#",
        "invoice",
        "inv",
        "invoce",
        "invoice_number",
    ),
    "vendor": ("vendor", "vndr", "from", "supplier", "seller", "bill from"),
    "date": ("date", "dt", "invoice date", "issued"),
    "due_date": ("due date", "due dt", "due", "payment due"),
    "subtotal": ("subtotal", "sub total"),
    "tax": ("tax", "sales tax", "tax amount", "vat"),
    "shipping": ("shipping", "freight", "delivery"),
    "total": ("total", "total amount", "amt", "amount due", "grand total", "amount"),
    "payment_terms": ("payment terms", "terms", "pymnt terms", "pymt terms"),
    "notes": ("notes", "note", "remarks", "memo"),
}

_NOT_AN_ITEM = {
    "subtotal",
    "sub total",
    "tax",
    "sales tax",
    "total",
    "grand total",
    "shipping",
    "freight",
    "item",
    "description",
    "qty",
    "quantity",
    "amount",
    "terms",
    "notes",
    "invoice",
    "vendor",
    "date",
    "due",
    "from",
    "to",
    "bill to",
    "attn",
    "items",
    "items ordered",
    "payment terms",
    "unit price",
    "rate",
    "line total",
    "price",
}

_MONEY_RE = r"[$€£]?\s*[\d][\d,OoIlSs]*(?:\.[\d OoIlSs]{1,2})?"

_ITEM_PATTERNS: tuple[re.Pattern[str], ...] = (
    # WidgetA  qty: 10  unit price: $250.00
    re.compile(
        rf"^\s*[-*•]?\s*(?P<name>[A-Za-z][\w \-]*?)\s+qty:?\s*(?P<qty>-?[\d,]+)\s*"
        rf"(?:unit\s*price|price|@)\s*:?\s*(?P<price>{_MONEY_RE})",
        re.IGNORECASE,
    ),
    # SuperGizmo  x12  $400.00 each -- the quantity marker must be attached to
    # its digits, or "Gadget X   4" parses as item "Gadget", quantity 4.
    re.compile(
        rf"^\s*[-*•]?\s*(?P<name>[A-Za-z][\w \-]*?)\s+x(?P<qty>-?[\d,]+)\b\s+"
        rf"(?P<price>{_MONEY_RE})",
        re.IGNORECASE,
    ),
    # WidgetA (rush order)  4  $300.00  $1,200.00
    re.compile(
        rf"^\s*(?P<name>[A-Za-z][\w \-()]*?)\s{{1,}}(?P<qty>-?[\d,]+)\s+"
        rf"(?P<price>{_MONEY_RE})\s+(?P<amount>{_MONEY_RE})\s*(?P<note>[A-Za-z][\w ]*)?$",
        re.IGNORECASE,
    ),
    # GadgetX  qty 20  @ $750 ea
    re.compile(
        rf"^\s*[-*•]?\s*(?P<name>[A-Za-z][\w \-]*?)\s+qty\s+(?P<qty>-?[\d,]+)\s+@?\s*"
        rf"(?P<price>{_MONEY_RE})",
        re.IGNORECASE,
    ),
)


# The separator must stay on the label's own line. Using \s here lets a bare
# "Amount" at the end of a table header bind to the first dash of the rule line
# below it, which silently captures a row of hyphens as the invoice total.
_SEPARATOR = r"[ \t]*(?:\([^)]*\))?[ \t]*[#]?[ \t]*[:\-](?!-)[ \t]*"

# A value runs to end of line, or stops where the next *known* label begins.
# Invoice 1013's PDF puts two fields on one line -- "Vendor: Atlas Industrial
# Supply Due: 2026-03-24" -- and PDF text extraction collapses the column
# spacing that separated them. Stopping at any capitalised word followed by a
# colon would truncate the vendor to "Atlas"; stopping only at a label we
# actually recognise gets both fields right.
_KNOWN_LABELS = sorted(
    {alias for aliases in _LABELS.values() for alias in aliases}, key=len, reverse=True
)
_NEXT_LABEL = "|".join(re.escape(a) for a in _KNOWN_LABELS)
_VALUE_TAIL = rf"(?P<value>.+?)(?=\s+(?:{_NEXT_LABEL})[ \t]*(?:\([^)]*\))?[ \t]*:|$)"


def _label_value(text: str, aliases: tuple[str, ...]) -> str | None:
    """Find ``alias: value``, longest alias first.

    Two passes. The first requires the label to start a line, which is the
    common and unambiguous case. The second allows it mid-line but only after
    two or more spaces, which is how a column boundary reads in plain text and
    keeps the pattern from matching the word "total" inside a sentence.

    An optional parenthetical between label and colon is tolerated: real
    documents write "Tax (5%):", and treating that as unlabelled loses the tax
    amount and manufactures a phantom arithmetic mismatch.
    """
    for alias in sorted(aliases, key=len, reverse=True):
        # Line-start first (unambiguous), then mid-line. The loose pass is
        # restricted to aliases of three characters or more so that a two-letter
        # abbreviation like "dt" cannot match inside unrelated text.
        anchors = [r"^[\s#>*\-]*"]
        if len(alias) >= 3:
            anchors.append(r"(?:^|\s)")
        for anchor in anchors:
            pattern = re.compile(
                rf"{anchor}{re.escape(alias)}{_SEPARATOR}{_VALUE_TAIL}",
                re.IGNORECASE | re.MULTILINE,
            )
            if match := pattern.search(text):
                value = match.group("value").strip()
                if value and value not in {"-", ":"}:
                    return value
    return None


def _looks_like_item(name: str) -> bool:
    """Reject label fragments the regex accidentally captured as a row."""
    cleaned = name.strip().lower().strip(":-")
    if not cleaned or len(cleaned) > 60:
        return False
    if cleaned in _NOT_AN_ITEM:
        return False
    return any(ch.isalpha() for ch in cleaned)


def _extract_number(text: str) -> str | None:
    """Pull and normalise the invoice number.

    ``INV 1012``, ``INV-1012`` and a bare ``1002`` all denote the same shape of
    identifier. Normalising to PREFIX-DIGITS is what lets duplicate detection
    compare two documents that spell it differently.
    """
    number = _label_value(text, _LABELS["invoice_number"])
    if number:
        number = re.sub(r"^(?:number|no\.?|#)\s*[:\-]?\s*", "", number, flags=re.IGNORECASE).strip()
        if m := re.match(r"^([A-Za-z]{2,6})[\s\-#]*(\d{3,})\b", number):
            number = f"{m.group(1).upper()}-{m.group(2)}"
        elif m := re.match(r"^(\d{3,})\b", number):
            number = f"INV-{m.group(1)}"
        else:
            parts = number.split()
            number = parts[0] if parts else None
    if not number and (m := re.search(r"\bINV[\s\-]?(\d{3,})\b", text, re.IGNORECASE)):
        number = f"INV-{m.group(1)}"
    return number


def _collect_items(text: str) -> list[LineItem]:
    """Preserve every source row, including legitimate identical charges."""
    items: list[LineItem] = []
    for line in text.splitlines():
        if not line.strip() or set(line.strip()) <= {"-", "=", "_", " "}:
            continue
        for pattern in _ITEM_PATTERNS:
            match = pattern.match(line)
            if not match:
                continue
            name = match.group("name").strip()
            if not _looks_like_item(name):
                break
            groups = match.groupdict()
            item = LineItem(
                raw_name=name,
                quantity=parse_quantity(groups["qty"]),
                unit_price=parse_money(groups["price"]),
                amount=parse_money(groups.get("amount")),
                note=(groups.get("note") or "").strip() or None,
            )
            items.append(item)
            break
    return items


def parse_text_invoice(text: str) -> ExtractedInvoice:
    """Parse a labelled or tabular plain-text invoice."""
    raw_date = _label_value(text, _LABELS["date"])
    raw_due = _label_value(text, _LABELS["due_date"])

    vendor = _label_value(text, _LABELS["vendor"])
    if vendor:
        vendor = re.sub(r"\s*\(formerly.*?\)\s*", "", vendor, flags=re.IGNORECASE).strip()

    return ExtractedInvoice(
        invoice_number=_extract_number(text),
        vendor_name=vendor,
        invoice_date=parse_date(raw_date),
        due_date=parse_date(raw_due),
        line_items=_collect_items(text),
        subtotal=parse_money(_label_value(text, _LABELS["subtotal"])),
        tax_amount=parse_money(_label_value(text, _LABELS["tax"])),
        shipping=parse_money(_label_value(text, _LABELS["shipping"])),
        total=parse_money(_label_value(text, _LABELS["total"])),
        currency=detect_currency(text),
        payment_terms=_label_value(text, _LABELS["payment_terms"]),
        notes=_label_value(text, _LABELS["notes"]),
        raw_date_text=raw_date,
        raw_due_date_text=raw_due,
    )
