"""Focused coverage for agent retry, policy, and payment edges."""

from __future__ import annotations

from datetime import date

import pytest

import acme_ap.agents.payment as payment_module
from acme_ap.agents.approval import ApprovalAgent
from acme_ap.agents.base import AgentContext
from acme_ap.agents.ingestion import IngestionAgent
from acme_ap.agents.payment import PaymentAgent
from acme_ap.agents.validation import ValidationAgent
from acme_ap.config import Settings
from acme_ap.ingestion.heuristics.schema import RawExtraction, RawLineItem
from acme_ap.llm.base import LLMError
from acme_ap.models import (
    ApprovalCritique,
    ApprovalProposal,
    ExtractedInvoice,
    Finding,
    FindingCode,
    RawDocument,
    Severity,
    ValidationReport,
)


class _Events:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    def append_event(self, *args: object, **kwargs: object) -> int:
        self.events.append((*args, kwargs))
        return len(self.events)


class _SequenceLLM:
    name = "test"
    model = "test-model"

    def __init__(self, responses: list[object]) -> None:
        self.responses = responses

    def complete_structured(self, **_kwargs: object) -> object:
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _context(llm: object, repo: object | None = None, **settings: object) -> AgentContext:
    return AgentContext(
        run_id="agent-run",
        repo=repo or _Events(),
        llm=llm,
        settings=Settings(**settings),
    )


def _document() -> RawDocument:
    return RawDocument(source_path="invoice.txt", source_format="txt", text="invoice")


def _good_raw() -> RawExtraction:
    return RawExtraction(
        invoice_number="INV-1",
        vendor_name="Vendor",
        invoice_date="2026-01-01",
        due_date="2026-02-01",
        line_items=[RawLineItem(name="WidgetA", quantity="2", unit_price="3", amount="6")],
        subtotal="6",
        total="6",
    )


def _bad_raw() -> RawExtraction:
    return RawExtraction(
        invoice_number="INV-1",
        vendor_name="Vendor",
        invoice_date="yesterday",
        line_items=[
            RawLineItem(name="No numbers"),
            RawLineItem(name="Wrong", quantity=2, unit_price=3, amount=5),
        ],
        subtotal="99",
    )


def test_ingestion_retries_error_then_critique_and_converges() -> None:
    llm = _SequenceLLM([LLMError("temporary"), _bad_raw(), _good_raw()])
    agent = IngestionAgent(_context(llm, max_extraction_attempts=3))
    invoice, problems = agent.run(_document())
    assert invoice.invoice_number == "INV-1"
    assert problems == []
    assert agent.attempts == 3


def test_ingestion_raises_when_last_attempt_is_unusable() -> None:
    llm = _SequenceLLM([LLMError("permanent")])
    agent = IngestionAgent(_context(llm, max_extraction_attempts=1))
    with pytest.raises(LLMError, match="permanent"):
        agent.run(_document())


def test_ingestion_returns_best_invoice_after_critique_exhaustion() -> None:
    llm = _SequenceLLM([_bad_raw(), _bad_raw()])
    agent = IngestionAgent(_context(llm, max_extraction_attempts=2))
    invoice, problems = agent.run(_document())
    assert invoice.invoice_number == "INV-1"
    assert problems
    assert any("No invoice total" in problem for problem in problems)


def test_ingestion_critique_reports_empty_and_unparseable_fields() -> None:
    agent = IngestionAgent(_context(_SequenceLLM([])))
    problems = agent.critique(ExtractedInvoice(raw_date_text="bad", total=None))
    assert any("No line items" in problem for problem in problems)
    assert any("could not be parsed" in problem for problem in problems)
    assert any("No invoice total" in problem for problem in problems)


def test_approval_hard_gates_blocking() -> None:
    blocking = ValidationReport(
        findings=[Finding(code=FindingCode.UNKNOWN_ITEM, severity=Severity.BLOCK, message="bad")]
    )
    gated = ApprovalAgent(_context(_SequenceLLM([]))).run(ExtractedInvoice(total=4), blocking)
    assert (gated.approved, bool(gated.hard_gate_triggered)) == (False, True)


def test_approval_hard_gates_missing_total() -> None:
    missing = ApprovalAgent(_context(_SequenceLLM([]))).run(
        ExtractedInvoice(total=None), ValidationReport()
    )
    assert (missing.approved, missing.hard_gate_triggered) == (
        False,
        "no invoice total could be established",
    )


def test_approval_revises_after_review_and_rejects() -> None:
    llm = _SequenceLLM(
        [
            ApprovalProposal(approve=True, rationale="first"),
            ApprovalCritique(agrees=False, critique="add evidence"),
            ApprovalProposal(approve=False, rationale="revised"),
            ApprovalCritique(agrees=True, critique="sound"),
        ]
    )
    agent = ApprovalAgent(_context(llm, max_critique_rounds=2))
    decision = agent.run(ExtractedInvoice(total=5), ValidationReport())
    assert not decision.approved
    assert len(decision.critique_rounds) == 2


def test_approval_escalates_when_review_never_converges() -> None:
    llm = _SequenceLLM(
        [
            ApprovalProposal(approve=True, rationale="maybe"),
            ApprovalCritique(agrees=False, critique="not enough"),
        ]
    )
    agent = ApprovalAgent(_context(llm, max_critique_rounds=1))
    decision = agent.run(ExtractedInvoice(total=5), ValidationReport())
    assert not decision.approved
    assert "did not sign off" in decision.rationale


def test_approval_override_catches_model_approval_with_blocking_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _SequenceLLM(
        [
            ApprovalProposal(approve=True, rationale="unsafe"),
            ApprovalCritique(agrees=True, critique="agreed"),
        ]
    )
    agent = ApprovalAgent(_context(llm))
    monkeypatch.setattr(agent, "_hard_gate", lambda _report, _invoice: None)
    report = ValidationReport(
        findings=[Finding(code=FindingCode.UNKNOWN_ITEM, severity=Severity.BLOCK, message="bad")]
    )
    decision = agent.run(ExtractedInvoice(total=5), report)
    assert not decision.approved


def test_approval_degrades_when_proposer_or_reviewer_fails() -> None:
    class _Failing:
        name = "test"
        model = "test"

        def complete_structured(self, **_kwargs: object) -> object:
            raise LLMError("offline")

    agent = ApprovalAgent(_context(_Failing()))
    facts = {"total": 5}
    proposal = agent._propose(facts, "challenge")
    critique = agent._critique(facts, proposal)
    assert not proposal.approve
    assert not critique.agrees


def test_payment_suppresses_existing_key() -> None:
    existing_repo = _Events()
    existing_repo.payment_by_key = lambda _key: {"paid_at": "today", "run_id": "old"}
    agent = PaymentAgent(_context(_SequenceLLM([]), existing_repo))
    duplicate = agent.run(
        ExtractedInvoice(invoice_number="INV-1", vendor_name="V", total=4), "hash"
    )
    assert (duplicate.status, bool(existing_repo.events)) == ("duplicate_suppressed", True)


def test_payment_handles_ledger_race(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger_repo = _Events()
    ledger_repo.payment_by_key = lambda _key: None
    ledger_repo.record_payment = lambda _run_id, _receipt: False
    ledger_repo.claim_payment = lambda _run_id, _invoice: True
    monkeypatch.setattr(
        payment_module, "mock_payment", lambda _vendor, _amount: {"status": "success"}
    )
    agent = PaymentAgent(_context(_SequenceLLM([]), ledger_repo))
    receipt = agent.run(ExtractedInvoice(invoice_number="INV-2", vendor_name="V", total=4), "hash")
    assert (receipt.status, receipt.detail) == (
        "duplicate_suppressed",
        "Ledger already held this idempotency key.",
    )


def test_validation_completeness_edges() -> None:
    report = ValidationReport()
    ValidationAgent._check_completeness(ExtractedInvoice(total=-1), report)
    codes = {finding.code for finding in report.findings}
    assert codes >= {
        FindingCode.MISSING_INVOICE_NUMBER,
        FindingCode.NO_LINE_ITEMS,
        FindingCode.NEGATIVE_TOTAL,
    }


def test_validation_duplicate_skip_and_past_due() -> None:
    class _NoDuplicate:
        def find_paid_invoice(self, *_args: object) -> None:
            return None

    agent = ValidationAgent(_context(_SequenceLLM([]), _NoDuplicate()))
    agent._check_duplicates(ExtractedInvoice(), ValidationReport())
    dated = ExtractedInvoice(invoice_date=date(2026, 3, 2), due_date=date(2026, 3, 1))
    dated_report = ValidationReport()
    agent._check_dates(dated, dated_report)
    assert dated_report.findings[0].code is FindingCode.PAST_DUE_DATE
