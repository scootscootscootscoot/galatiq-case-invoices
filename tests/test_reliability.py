"""Adversarial extraction and payment tests, independent of the sample golden set."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from acme_ap.agents.base import AgentContext
from acme_ap.agents.payment import PaymentAgent
from acme_ap.config import Settings
from acme_ap.db.repository import Repository
from acme_ap.eval import FileScore, Scorecard
from acme_ap.ingestion.heuristics import parse_document, parse_money
from acme_ap.ingestion.heuristics.schema import RawExtraction
from acme_ap.ingestion.quality import assess
from acme_ap.ingestion.readers import load_document
from acme_ap.llm.stub import StubClient
from acme_ap.models import ExtractedInvoice, FindingCode, Outcome, RawDocument
from acme_ap.service import process_invoice


def invoice_data(**updates):
    data = {
        "invoice_number": "INV-8801",
        "vendor": "Evidence Supply",
        "date": "2026-09-01",
        "due_date": "2026-10-01",
        "currency": "USD",
        "line_items": [{"item": "WidgetA", "quantity": 2, "unit_price": 250, "amount": 500}],
        "subtotal": 500,
        "tax_amount": 0,
        "total": 500,
    }
    data.update(updates)
    return data


def write_invoice(tmp_path, **updates):
    path = tmp_path / "invoice.json"
    path.write_text(json.dumps(invoice_data(**updates)))
    return path


def settings(temp_db):
    return Settings(database_path=temp_db, llm_provider="stub")


def test_internally_consistent_hallucination_never_pays(tmp_path, temp_db, monkeypatch):
    class LyingExtractor(StubClient):
        def complete_structured(self, *, system, user, schema, purpose):
            if purpose == "extraction":
                return RawExtraction(
                    invoice_number="INV-8801",
                    vendor_name="Invented Vendor",
                    invoice_date="2026-09-01",
                    due_date="2026-10-01",
                    currency="USD",
                    line_items=[
                        {"name": "WidgetA", "quantity": "1", "unit_price": "250", "amount": "250"}
                    ],
                    subtotal="250",
                    tax_amount="0",
                    total="250",
                )
            return super().complete_structured(
                system=system, user=user, schema=schema, purpose=purpose
            )

    monkeypatch.setattr("acme_ap.service.build_client", lambda _settings: LyingExtractor())
    result = process_invoice(write_invoice(tmp_path), settings=settings(temp_db))
    assert result.outcome is Outcome.REVIEW_REQUIRED
    assert result.quality.score == 0
    assert result.payment is None
    with Repository(temp_db) as repo:
        assert repo.get_review(result.run_id)["status"] == "OPEN"
        assert repo.get_extraction(result.run_id)["invoice"]["vendor_name"] == "Invented Vendor"


def test_omitted_and_duplicated_lines_have_zero_confidence():
    doc = RawDocument(source_path="memory", source_format="json", text=json.dumps(invoice_data()))
    invoice = parse_document(doc.text, "json")
    invoice.line_items *= 2
    report = assess(doc, invoice, 0.9)
    assert report.requires_review and report.score == 0


def test_legitimate_identical_rows_survive_parsing(tmp_path, temp_db):
    path = tmp_path / "repeated.txt"
    path.write_text("""Invoice: INV-8820
Vendor: Repeated Supply
Date: 2026-09-01
Due date: 2026-10-01
WidgetA  8  $250.00  $2,000.00
WidgetA  8  $250.00  $2,000.00
Subtotal: $4,000.00
Total: $4,000.00
""")
    result = process_invoice(path, settings=settings(temp_db))
    assert len(result.invoice.line_items) == 2
    assert result.invoice.aggregated_quantities()["WidgetA"] == 16
    assert FindingCode.STOCK_EXCEEDED in result.validation.codes()
    assert result.outcome is Outcome.REJECTED


@pytest.mark.parametrize(
    "updates",
    [
        {"total": None},
        {"total": 0},
        {"vendor": ""},
        {"line_items": [{"item": "WidgetA", "quantity": None, "unit_price": 250, "amount": 500}]},
        {"line_items": [{"item": "WidgetA", "quantity": 0, "unit_price": 250, "amount": 500}]},
        {"line_items": [{"item": "WidgetA", "quantity": 2, "unit_price": -250, "amount": 500}]},
        {
            "line_items": [{"item": "WidgetA", "quantity": 2, "unit_price": 250, "amount": 100}],
            "subtotal": 100,
            "total": 100,
        },
        {"currency": "USDDOLLARS"},
    ],
)
def test_invalid_payment_fields_never_pay(tmp_path, temp_db, updates):
    result = process_invoice(write_invoice(tmp_path, **updates), settings=settings(temp_db))
    assert result.outcome is Outcome.REJECTED
    assert result.payment is None


@pytest.mark.parametrize(
    "extra",
    [
        "Total: $600.00",
        "Currency: EUR",
        "Due date: 03/04/2026",
    ],
)
def test_source_ambiguity_requires_review(extra):
    text = (
        """Invoice: INV-8810
Vendor: Evidence Supply
Due date: 2026-10-01
WidgetA  2  $250.00  $500.00
Total: $500.00
Currency: USD
"""
        + extra
    )
    doc = RawDocument(source_path="memory", source_format="txt", text=text)
    assert assess(doc, parse_document(text, "txt"), 0.9).requires_review


def test_confidence_boundary_and_missing_currency():
    doc = RawDocument(source_path="memory", source_format="json", text=json.dumps(invoice_data()))
    invoice = parse_document(doc.text, "json")
    assert not assess(doc, invoice, 0.99).requires_review
    assert assess(doc, invoice, 1.0).requires_review
    doc = doc.model_copy(update={"text": doc.text.replace('"currency": "USD", ', "")})
    assert assess(doc, invoice, 0.9).requires_review


def test_eval_fails_on_wrong_fields_even_when_decision_matches():
    card = Scorecard([FileScore("example", "PAID", "PAID", 2, 1, ["wrong vendor"])])
    assert not card.passed
    assert not Scorecard().passed
    assert FileScore("example", "REVIEW_REQUIRED", "PAID").false_pay


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_numbers_are_not_money(value):
    assert parse_money(value) is None
    with pytest.raises(ValueError):
        ExtractedInvoice(total=value)


def test_money_rounding_and_tolerance_are_decimal():
    from acme_ap.models import LineItem
    from acme_ap.money import difference, total

    assert LineItem(raw_name="WidgetA", quantity=1, unit_price=2.675).computed_amount == 2.68
    assert difference(100.01, 100) == 0.01
    assert total([0.1, 0.2]) == 0.3


@pytest.mark.parametrize(
    "raw, expected",
    [("USD 500.00", 500), ("500 pending", None), ("1.234,56", None), ("1,23", None)],
)
def test_currency_and_unknown_money_formats_are_not_silently_corrupted(raw, expected):
    assert parse_money(raw) == expected


def test_payment_reserves_before_call_across_connections(temp_db, monkeypatch):
    calls = []
    barrier = threading.Barrier(2)
    monkeypatch.setattr(
        "acme_ap.agents.payment.mock_payment",
        lambda vendor, amount: calls.append((vendor, amount)) or {"status": "success"},
    )

    def pay(run_id):
        with Repository(temp_db) as repo:
            repo.create_run(run_id, "synthetic.json")
            invoice = ExtractedInvoice(
                invoice_number="INV-DOUBLE", vendor_name="Evidence Supply", total=500
            )
            context = AgentContext(
                run_id=run_id, repo=repo, settings=settings(temp_db), llm=StubClient()
            )
            barrier.wait(timeout=10)
            return PaymentAgent(context).run(invoice, run_id).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(pay, ["race-one", "race-two"]))
    assert sorted(statuses) == ["duplicate_suppressed", "success"]
    assert len(calls) == 1


def test_corrupt_pdf_opens_persistent_alert(tmp_path, temp_db):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a PDF")
    result = process_invoice(path, settings=settings(temp_db))
    assert result.outcome is Outcome.FAILED
    with Repository(temp_db) as repo:
        assert repo.get_review(result.run_id)["status"] == "OPEN"


def test_pdf_table_is_read_once(tmp_path):
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    path = tmp_path / "table.pdf"
    table = Table([["Item", "Qty", "Unit", "Amount"], ["WidgetA", "2", "$250.00", "$500.00"]])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 1, colors.black)]))
    SimpleDocTemplate(str(path)).build([table])
    doc = load_document(path)
    assert doc.text.count("WidgetA") == 1
    assert doc.metadata["pdf_tables"] == 1
    assert doc.metadata["pages"][0]["page"] == 1


def test_missing_pdf_page_cannot_hide_behind_readable_page(tmp_path, monkeypatch):
    from reportlab.pdfgen.canvas import Canvas

    path = tmp_path / "partial.pdf"
    canvas = Canvas(str(path))
    canvas.drawString(50, 750, "Invoice INV-8801 Vendor Evidence Supply Total $500.00")
    canvas.showPage()
    canvas.showPage()
    canvas.save()
    monkeypatch.setattr("acme_ap.ingestion.ocr.recognize", lambda *_args: ("", "OCR unavailable"))
    doc = load_document(path)
    assert len(doc.metadata["pages"]) == 2
    assert doc.metadata["pages"][1]["method"] == "unreadable"
    assert assess(doc, parse_document(doc.text, "pdf"), 0.9).requires_review
