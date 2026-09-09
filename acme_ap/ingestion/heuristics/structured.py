"""Structured-format parsers: JSON, XML, and the two CSV dialects.

Structured formats carry their own schema, so a parser reads them exactly,
every time, for nothing. The model's job starts only where ambiguity starts.
"""

from __future__ import annotations

import json
import re

from acme_ap.ingestion.heuristics.scalars import (
    detect_currency,
    parse_date,
    parse_money,
    parse_quantity,
)
from acme_ap.models import ExtractedInvoice, LineItem

_ITEM_KEYS = ("item", "name", "description", "product", "sku")
_QTY_KEYS = ("quantity", "qty", "units")
_PRICE_KEYS = ("unit_price", "price", "rate", "unit price")
_AMOUNT_KEYS = ("amount", "line_total", "total", "line total")


def _first(mapping: dict[str, object], keys: tuple[str, ...]) -> object | None:
    """First non-empty value among candidate key spellings, case-insensitive."""
    lowered = {k.lower().replace(" ", "_"): v for k, v in mapping.items()}
    for key in keys:
        normalized = key.replace(" ", "_")
        if normalized in lowered and lowered[normalized] not in (None, ""):
            return lowered[normalized]
    return None


def parse_json_invoice(text: str) -> ExtractedInvoice | None:
    """Map a JSON invoice onto the domain model."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    vendor = data.get("vendor")
    vendor_name: str | None
    vendor_address: str | None = None
    if isinstance(vendor, dict):
        vendor_name = vendor.get("name") or None
        vendor_address = vendor.get("address") or None
    else:
        vendor_name = str(vendor) if vendor else None

    items: list[LineItem] = []
    for entry in data.get("line_items") or data.get("items") or []:
        if not isinstance(entry, dict):
            continue
        raw_name = _first(entry, _ITEM_KEYS)
        if raw_name is None:
            continue
        items.append(
            LineItem(
                raw_name=str(raw_name),
                quantity=parse_quantity(_first(entry, _QTY_KEYS)),  # type: ignore[arg-type]
                unit_price=parse_money(_first(entry, _PRICE_KEYS)),  # type: ignore[arg-type]
                amount=parse_money(_first(entry, _AMOUNT_KEYS)),  # type: ignore[arg-type]
                note=str(entry["note"]) if entry.get("note") else None,
            )
        )

    return ExtractedInvoice(
        invoice_number=str(data["invoice_number"]) if data.get("invoice_number") else None,
        vendor_name=vendor_name,
        vendor_address=vendor_address,
        invoice_date=parse_date(data.get("date")),
        due_date=parse_date(data.get("due_date")),
        line_items=items,
        subtotal=parse_money(data.get("subtotal")),
        tax_rate=parse_money(data.get("tax_rate")),
        tax_amount=parse_money(data.get("tax_amount")),
        shipping=parse_money(data.get("shipping")),
        total=parse_money(data.get("total")),
        currency=str(data.get("currency") or "USD"),
        payment_terms=str(data["payment_terms"]) if data.get("payment_terms") else None,
        notes=str(data["notes"]) if data.get("notes") else None,
        raw_date_text=str(data.get("date")) if data.get("date") else None,
        raw_due_date_text=str(data.get("due_date")) if data.get("due_date") else None,
    )


def parse_flat_kv(text: str, separator: str) -> dict[str, list[str]]:
    """Collect ``key<sep>value`` lines, keeping repeats in order."""
    collected: dict[str, list[str]] = {}
    for line in text.splitlines():
        if separator not in line:
            continue
        key, _, value = line.partition(separator)
        key = key.strip().lower().replace(" ", "_")
        value = value.strip()
        if key and value:
            collected.setdefault(key, []).append(value)
    return collected


def _invoice_from_kv(kv: dict[str, list[str]], text: str) -> ExtractedInvoice:
    """Build an invoice from collected key/value pairs.

    Item, quantity and price keys repeat once per line item and stay in document
    order, so zipping the three lists reconstructs the rows.
    """

    def one(*keys: str) -> str | None:
        for key in keys:
            if values := kv.get(key):
                return values[0]
        return None

    def many(*keys: str) -> list[str]:
        for key in keys:
            if values := kv.get(key):
                return values
        return []

    names = many("item", "name", "description", "product")
    quantities = many("quantity", "qty", "units")
    prices = many("unit_price", "price", "rate")

    items = [
        LineItem(
            raw_name=name,
            quantity=parse_quantity(quantities[i]) if i < len(quantities) else None,
            unit_price=parse_money(prices[i]) if i < len(prices) else None,
        )
        for i, name in enumerate(names)
    ]

    raw_date = one("date", "invoice_date", "dt")
    raw_due = one("due_date", "due", "due_dt")
    return ExtractedInvoice(
        invoice_number=one("invoice_number", "invoice", "inv_#", "inv_no", "invoice_#", "inv"),
        vendor_name=one("vendor", "vndr", "from", "supplier"),
        invoice_date=parse_date(raw_date),
        due_date=parse_date(raw_due),
        line_items=items,
        subtotal=parse_money(one("subtotal")),
        tax_amount=parse_money(one("tax", "tax_amount", "sales_tax")),
        shipping=parse_money(one("shipping", "freight")),
        total=parse_money(one("total", "total_amount", "amt", "grand_total")),
        currency=detect_currency(text),
        payment_terms=one("payment_terms", "terms", "pymnt_terms"),
        raw_date_text=raw_date,
        raw_due_date_text=raw_due,
    )


def parse_xml_invoice(text: str) -> ExtractedInvoice | None:
    """Parse the reader's flattened ``tag: value`` XML rendering."""
    if "invoice_number:" not in text and "invoice:" not in text:
        return None
    kv = parse_flat_kv(text, ":")
    if not kv.get("invoice_number"):
        return None
    invoice = _invoice_from_kv(kv, text)
    if tax_rate := kv.get("tax_rate"):
        invoice.tax_rate = parse_money(tax_rate[0])
    return invoice


def _column(header: list[str], *names: str) -> int | None:
    """Index of the first present header alias."""
    for name in names:
        if name in header:
            return header.index(name)
    return None


def _cell(cells: list[str], index: int | None) -> str | None:
    """Safe cell access for a parsed row."""
    return cells[index] if index is not None and index < len(cells) else None


def _scan_rows(lines: list[str], columns: dict[str, int | None]) -> ExtractedInvoice | None:
    """Walk the table rows, separating line items from the summary footer."""
    number = vendor = raw_date = raw_due = None
    items: list[LineItem] = []
    subtotal = tax = total = None

    for line in lines:
        cells = [c.strip() for c in re.split(r"\s{2,}", line.strip())]
        joined = " ".join(cells).lower()
        if "subtotal" in joined:
            subtotal = parse_money(cells[-1])
            continue
        if "tax" in joined:
            tax = parse_money(cells[-1])
            continue
        if "total" in joined:
            total = parse_money(cells[-1])
            continue

        number = number or _cell(cells, columns["number"])
        vendor = vendor or _cell(cells, columns["vendor"])
        raw_date = raw_date or _cell(cells, columns["date"])
        raw_due = raw_due or _cell(cells, columns["due"])
        if (name := _cell(cells, columns["item"])) and name.lower() not in {"item", ""}:
            items.append(
                LineItem(
                    raw_name=name,
                    quantity=parse_quantity(_cell(cells, columns["qty"])),
                    unit_price=parse_money(_cell(cells, columns["price"])),
                    amount=parse_money(_cell(cells, columns["amount"])),
                )
            )

    if not items:
        return None
    return ExtractedInvoice(
        invoice_number=number,
        vendor_name=vendor,
        invoice_date=parse_date(raw_date),
        due_date=parse_date(raw_due),
        line_items=items,
        subtotal=subtotal,
        tax_amount=tax,
        total=total,
        raw_date_text=raw_date,
        raw_due_date_text=raw_due,
    )


def parse_csv_table(text: str) -> ExtractedInvoice | None:
    """Parse the wide tabular CSV dialect (one row per line item)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    header = [h.strip().lower() for h in re.split(r"\s{2,}", lines[0].strip())]
    if "invoice number" not in header and "invoice_number" not in header:
        return None

    columns = {
        "number": _column(header, "invoice number", "invoice_number"),
        "vendor": _column(header, "vendor"),
        "date": _column(header, "date"),
        "due": _column(header, "due date", "due_date"),
        "item": _column(header, "item", "description"),
        "qty": _column(header, "qty", "quantity"),
        "price": _column(header, "unit price", "unit_price", "rate"),
        "amount": _column(header, "line total", "line_total", "amount"),
    }
    invoice = _scan_rows(lines[1:], columns)
    if invoice:
        invoice.currency = detect_currency(text)
    return invoice
