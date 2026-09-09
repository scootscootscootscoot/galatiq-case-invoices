"""End-to-end behaviour, plus the eval harness gate."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from typing import Any

from acme_ap.db.repository import Repository
from acme_ap.eval import evaluate, load_expectations
from acme_ap.models import FindingCode, Outcome
from acme_ap.service import process_invoice


def run(path: Path, repo: Any) -> Any:
    """Process an invoice, swallowing the mock payment's stdout."""
    with contextlib.redirect_stdout(io.StringIO()):
        return process_invoice(path, repo=repo)


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def _assert_golden_set(card: Any) -> None:
    failures = [f"{s.filename}: {'; '.join(s.failures)}" for s in card.scores if s.failures]
    assert (not card.false_pays, not failures, card.passed) == (True, True, True)


def test_golden_set_passes() -> None:
    """Every invoice reaches its expected outcome with no false pays."""
    _assert_golden_set(evaluate())


def test_every_sample_invoice_is_in_the_golden_set(invoice_dir: Path) -> None:
    """No sample file may quietly go unasserted."""
    on_disk = {
        p.name
        for p in invoice_dir.iterdir()
        if p.suffix.lower() in {".txt", ".json", ".csv", ".xml", ".pdf"}
    }
    assert on_disk - set(load_expectations()) == set()


# --------------------------------------------------------------------------- #
# Specific behaviours
# --------------------------------------------------------------------------- #


def test_clean_invoice_is_paid(invoice_dir: Path, repo: Any) -> None:
    result = run(invoice_dir / "invoice_1001.txt", repo)
    assert result.outcome is Outcome.PAID
    assert result.payment is not None
    assert result.payment.status == "success"


def test_line_items_aggregate_before_the_stock_check(invoice_dir: Path, repo: Any) -> None:
    """1010 orders WidgetA twice: 8 plus a 4-unit rush order."""
    result = run(invoice_dir / "invoice_1010.txt", repo)
    assert result.invoice is not None
    invoice = result.invoice
    assert (
        len(invoice.line_items),
        invoice.aggregated_quantities()["WidgetA"],
        result.outcome,
    ) == (
        4,
        12,
        Outcome.PAID,
    )


def test_aggregation_catches_stock_spread_across_lines(invoice_dir: Path, repo: Any) -> None:
    """1013 hides 22 WidgetA across three lines against stock of 15."""
    result = run(invoice_dir / "invoice_1013.json", repo)
    assert result.invoice is not None
    assert (
        result.invoice.aggregated_quantities()["WidgetA"],
        FindingCode.STOCK_EXCEEDED in result.validation.codes(),
        result.outcome,
    ) == (22, True, Outcome.REJECTED)


def test_unexplained_total_is_blocked(invoice_dir: Path, repo: Any) -> None:
    """1013 states a total fifty dollars above subtotal plus tax."""
    result = run(invoice_dir / "invoice_1013.json", repo)
    mismatches = [
        f for f in result.validation.findings if f.code is FindingCode.ARITHMETIC_MISMATCH
    ]
    assert (
        bool(mismatches),
        any(abs(f.evidence.get("difference", 0)) == 50.0 for f in mismatches),
    ) == (
        True,
        True,
    )


def test_ocr_damage_is_repaired(invoice_dir: Path, repo: Any) -> None:
    """1012 carries letter O where digits belong, in a date and a line amount."""
    result = run(invoice_dir / "invoice_1012.pdf", repo)
    assert result.invoice is not None
    assert (
        str(result.invoice.invoice_date),
        result.invoice.total,
        FindingCode.ARITHMETIC_MISMATCH not in result.validation.codes(),
        result.outcome,
    ) == ("2026-01-26", 9975.00, True, Outcome.PAID)


def _assert_pdf_text_twin(invoice_dir: Path, temp_db: Path, stem: str) -> None:
    results = []
    for suffix in (".txt", ".pdf"):
        repo = Repository(temp_db)
        results.append(run(invoice_dir / f"{stem}{suffix}", repo))
        repo.close()
    text, pdf = results
    assert (
        text.invoice.invoice_number,
        text.invoice.total,
        text.invoice.aggregated_quantities(),
    ) == (
        pdf.invoice.invoice_number,
        pdf.invoice.total,
        pdf.invoice.aggregated_quantities(),
    )


def test_pdf_and_text_twins_agree(invoice_dir: Path, temp_db: Path) -> None:
    """The same invoice in two formats must produce the same decision."""
    _assert_pdf_text_twin(invoice_dir, temp_db, "invoice_1011")
    _assert_pdf_text_twin(invoice_dir, temp_db, "invoice_1012")


def test_unknown_item_blocks(invoice_dir: Path, repo: Any) -> None:
    result = run(invoice_dir / "invoice_1016.json", repo)
    assert FindingCode.UNKNOWN_ITEM in result.validation.codes()
    assert result.outcome is Outcome.REJECTED


def test_fraud_shape_is_rejected(invoice_dir: Path, repo: Any) -> None:
    """1003: zero-stock product, unparseable due date, pressure language."""
    result = run(invoice_dir / "invoice_1003.txt", repo)
    codes = result.validation.codes()
    assert (
        FindingCode.ZERO_STOCK in codes,
        FindingCode.URGENCY_PRESSURE in codes,
        FindingCode.UNPARSEABLE_DATE in codes,
        result.outcome,
    ) == (True, True, True, Outcome.REJECTED)


def test_data_integrity_failure_is_rejected(invoice_dir: Path, repo: Any) -> None:
    result = run(invoice_dir / "invoice_1009.json", repo)
    codes = result.validation.codes()
    assert FindingCode.NEGATIVE_QUANTITY in codes
    assert FindingCode.MISSING_VENDOR in codes
    assert result.outcome is Outcome.REJECTED


def test_extraction_loop_is_bounded(invoice_dir: Path, repo: Any) -> None:
    """An unrepairable document must not retry forever.

    1009 is internally inconsistent, so the critique never clears. The loop stops
    at the configured limit and hands the remaining problems to validation as findings.
    """
    from acme_ap.config import get_settings

    result = run(invoice_dir / "invoice_1009.json", repo)
    assert result.extraction_attempts == get_settings().max_extraction_attempts
    assert FindingCode.EXTRACTION_DEGRADED in result.validation.codes()


# --------------------------------------------------------------------------- #
# Money safety
# --------------------------------------------------------------------------- #


def test_same_invoice_paid_once(invoice_dir: Path, repo: Any) -> None:
    """A repeated run of a paid invoice must not pay again."""
    first = run(invoice_dir / "invoice_1001.txt", repo)
    assert first.outcome is Outcome.PAID

    second = run(invoice_dir / "invoice_1001.txt", repo)
    assert second.outcome is Outcome.REJECTED
    assert FindingCode.DUPLICATE_INVOICE in second.validation.codes()


def test_revision_of_a_paid_invoice_is_blocked(invoice_dir: Path, repo: Any) -> None:
    """1004 and 1004_revised share an invoice number.

    Both are plausible documents and each is payable alone. Once the original has
    been paid, the revision must stop for a human rather than pay a second time.
    """
    original = run(invoice_dir / "invoice_1004.json", repo)
    assert original.outcome is Outcome.PAID

    revision = run(invoice_dir / "invoice_1004_revised.json", repo)
    assert revision.outcome is Outcome.REJECTED
    assert FindingCode.DUPLICATE_INVOICE in revision.validation.codes()


def test_payment_ledger_is_idempotent(invoice_dir: Path, repo: Any) -> None:
    """The unique idempotency key is what makes a retry safe."""
    result = run(invoice_dir / "invoice_1001.txt", repo)
    key = result.payment.idempotency_key
    assert repo.payment_by_key(key) is not None
    assert not repo.record_payment(result.run_id, result.payment)


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #


def test_every_run_is_recorded(invoice_dir: Path, repo: Any) -> None:
    result = run(invoice_dir / "invoice_1002.txt", repo)
    stored = repo.get_run(result.run_id)
    events = repo.events_since(result.run_id)
    agents = {e["agent"] for e in events}
    decision = repo.get_decision(result.run_id)
    assert all(
        (
            stored is not None,
            stored["status"] == Outcome.REJECTED.value,
            bool(stored["finished_at"]),
            bool(events),
            {"loader", "ingestion", "validation"} <= agents,
            decision is not None,
            bool(decision["rationale"]),
        )
    )


def test_rejection_reasoning_is_persisted(invoice_dir: Path, repo: Any) -> None:
    result = run(invoice_dir / "invoice_1005.json", repo)
    decision = repo.get_decision(result.run_id)
    assert decision["approved"] == 0
    assert "STOCK_EXCEEDED" in decision["hard_gate"]
    assert repo.get_findings(result.run_id)


def test_unreadable_file_fails_cleanly(tmp_path: Path, repo: Any) -> None:
    """A failure must still close the run out, not leave it RUNNING."""
    bad = tmp_path / "invoice.txt"
    bad.write_text("   ")
    result = run(bad, repo)
    assert result.outcome is Outcome.FAILED
    assert result.error
    assert repo.get_run(result.run_id)["status"] == "FAILED"
