"""Deterministic invoice parsing — public dispatch.

Structured formats never need a model; free text is parsed as a labelled
document after every structural attempt. Dispatch order is significant: a
format that declares its own schema is parsed structurally, and free-text
heuristics are the last resort rather than the first attempt.
"""

from __future__ import annotations

from acme_ap.ingestion.heuristics.scalars import (
    detect_currency,
    parse_date,
    parse_money,
    parse_quantity,
)
from acme_ap.ingestion.heuristics.schema import RawExtraction, RawLineItem, to_raw_extraction
from acme_ap.ingestion.heuristics.structured import (
    parse_csv_table,
    parse_flat_kv,
    parse_json_invoice,
    parse_xml_invoice,
)
from acme_ap.ingestion.heuristics.text import parse_text_invoice
from acme_ap.models import ExtractedInvoice

__all__ = [
    "RawExtraction",
    "RawLineItem",
    "detect_currency",
    "parse_csv_table",
    "parse_date",
    "parse_document",
    "parse_flat_kv",
    "parse_json_invoice",
    "parse_money",
    "parse_quantity",
    "parse_text_invoice",
    "parse_xml_invoice",
    "to_raw_extraction",
]


def parse_document(text: str, source_format: str) -> ExtractedInvoice:
    """Dispatch to the most precise parser the format allows."""
    if source_format == "json" or text.lstrip().startswith("{"):
        if invoice := parse_json_invoice(text):
            return invoice
    if source_format == "xml":
        if invoice := parse_xml_invoice(text):
            return invoice
    if source_format == "csv":
        if invoice := parse_csv_table(text):
            return invoice
        kv = parse_flat_kv(text, "  ")
        if kv.get("invoice_number"):
            from acme_ap.ingestion.heuristics.structured import _invoice_from_kv

            return _invoice_from_kv(kv, text)
    return parse_text_invoice(text)
