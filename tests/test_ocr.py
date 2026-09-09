"""Real image-only PDF coverage, with optional local Tesseract integration."""

from __future__ import annotations

import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

from acme_ap.ingestion.heuristics import parse_document
from acme_ap.ingestion.quality import assess
from acme_ap.ingestion.readers import load_document


def scanned_invoice(path):
    image = Image.new("RGB", (1800, 1800), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("DejaVuSans.ttf", 40)
    lines = [
        "Invoice: INV-8830",
        "Vendor: Scan Supply",
        "Date: 2026-09-01",
        "Due date: 2026-10-01",
        "Currency: USD",
        "",
        "WidgetA    2    $250.00    $500.00",
        "",
        "Subtotal: $500.00",
        "Tax: $0.00",
        "Total: $500.00",
    ]
    for index, line in enumerate(lines):
        draw.text((100, 120 + index * 95), line, fill="black", font=font)
    image.save(path, "PDF", resolution=150)


def test_no_ocr_binary_is_an_explicit_hold(tmp_path, monkeypatch):
    path = tmp_path / "scan.pdf"
    scanned_invoice(path)
    monkeypatch.setattr("acme_ap.ingestion.ocr.shutil.which", lambda _name: None)
    doc = load_document(path)
    assert doc.metadata["pages"][0]["method"] == "unreadable"
    assert any("Tesseract is unavailable" in issue for issue in doc.metadata["quality_issues"])
    assert assess(doc, parse_document(doc.text, "pdf"), 0.9).requires_review


@pytest.mark.skipif(
    not shutil.which("tesseract"), reason="Install Tesseract to run image-only PDF integration"
)
def test_real_scanned_pdf_extracts_and_requires_verification(tmp_path):
    path = tmp_path / "scan.pdf"
    scanned_invoice(path)
    doc = load_document(path)
    assert doc.metadata["pages"][0]["method"] == "ocr"
    invoice = parse_document(doc.text, "pdf")
    assert invoice.invoice_number == "INV-8830"
    assert invoice.vendor_name == "Scan Supply"
    assert invoice.total == 500
    assert invoice.line_items[0].quantity == 2
    assert assess(doc, invoice, 0.9).requires_review
