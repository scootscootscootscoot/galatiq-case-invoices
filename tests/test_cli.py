"""CLI rendering and command behavior."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import acme_ap.cli as cli
from acme_ap.config import Settings
from acme_ap.models import (
    ApprovalDecision,
    CritiqueRound,
    ExtractedInvoice,
    Finding,
    FindingCode,
    LineItem,
    Outcome,
    PaymentReceipt,
    RunResult,
    Severity,
    ValidationReport,
)

RUNNER = CliRunner()


def _result(outcome: Outcome = Outcome.PAID, **kwargs: object) -> RunResult:
    return RunResult(
        run_id="run-cli",
        source_path="/tmp/invoice.txt",
        outcome=outcome,
        provider="stub",
        model="deterministic-rules",
        **kwargs,
    )


def _invoice(*, with_items: bool = True) -> ExtractedInvoice:
    items = (
        [
            LineItem(raw_name="WidgetA", quantity=2, unit_price=3),
            LineItem(raw_name="WidgetA", canonical_item="WidgetA", quantity=None),
        ]
        if with_items
        else []
    )
    return ExtractedInvoice(
        invoice_number="INV-CLI",
        vendor_name="CLI Vendor",
        invoice_date="2026-01-01",
        due_date="2026-02-01",
        line_items=items,
        total=6,
    )


def test_render_extraction_handles_empty_invoice_and_item_variants() -> None:
    cli._render_extraction(_result())
    cli._render_extraction(_result(invoice=_invoice(with_items=False)))
    cli._render_extraction(_result(invoice=_invoice()))


def test_render_validation_handles_empty_and_all_severities() -> None:
    cli._render_validation(_result())
    cli._render_validation(_result(validation=ValidationReport()))
    findings = [
        Finding(code=FindingCode.HIGH_VALUE, severity=Severity.INFO, message="info"),
        Finding(code=FindingCode.MISSING_DUE_DATE, severity=Severity.WARN, message="warn"),
        Finding(code=FindingCode.UNKNOWN_ITEM, severity=Severity.BLOCK, message="block"),
    ]
    cli._render_validation(_result(validation=ValidationReport(findings=findings)))


def test_render_approval_and_payment_show_optional_detail() -> None:
    cli._render_approval(_result(), verbose=True)
    decision = ApprovalDecision(
        approved=False,
        rationale="held",
        policy_version="p1",
        hard_gate_triggered="policy gate",
        critique_rounds=[
            CritiqueRound(
                round_number=1,
                proposal="reject",
                decision=False,
                critique="reviewed",
                accepted=False,
            )
        ],
    )
    cli._render_approval(_result(approval=decision), verbose=True)
    cli._render_payment(_result())
    receipt = PaymentReceipt(
        status="success",
        vendor="CLI Vendor",
        amount=6,
        currency="USD",
        idempotency_key="key",
    )
    cli._render_payment(_result(payment=receipt))
    cli._render_payment(
        _result(
            payment=receipt.model_copy(update={"detail": "already paid"}),
        )
    )


def test_render_degraded_error_and_each_outcome(capsys: pytest.CaptureFixture[str]) -> None:
    report = ValidationReport(
        findings=[Finding(code=FindingCode.HIGH_VALUE, severity=Severity.INFO, message="info")]
    )
    decision = ApprovalDecision(approved=True, rationale="approved", policy_version="p1")
    receipt = PaymentReceipt(
        status="success",
        vendor="CLI Vendor",
        amount=6,
        currency="USD",
        idempotency_key="key",
        detail="bank trace",
    )
    cli._render(
        _result(
            degraded=True,
            error="something failed",
            invoice=_invoice(),
            validation=report,
            approval=decision,
            payment=receipt,
        ),
        verbose=True,
    )
    cli._render(_result(Outcome.REJECTED), verbose=False)
    cli._render(_result(Outcome.FAILED), verbose=False)
    assert "degraded" in capsys.readouterr().out


def test_run_command_json_success_and_report_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(cli, "ensure_database", lambda: None)
    results = [_result(), _result(Outcome.REJECTED)]
    monkeypatch.setattr(cli, "process_invoice", lambda _path: results.pop(0))
    json_response = RUNNER.invoke(cli.app, ["run", "--invoice-path", "invoice.txt", "--json"])
    report_response = RUNNER.invoke(cli.app, ["run", "--invoice-path", "invoice.txt", "--verbose"])
    assert (
        json_response.exit_code,
        '"outcome": "PAID"' in json_response.stdout,
        report_response.exit_code,
        "REJECTED" in report_response.stdout,
    ) == (0, True, 1, True)


def _configure_cli_database(monkeypatch: pytest.MonkeyPatch, temp_db: Path) -> None:
    from acme_ap.db.repository import Repository

    repo = Repository(temp_db)
    repo.create_run("history-run", "invoice.txt", "stub", "deterministic-rules", True)
    repo.save_invoice("history-run", _invoice(with_items=False), "hash")
    repo.finish_run("history-run", Outcome.PAID, "hash")
    repo.close()
    settings = Settings(database_path=temp_db)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "ensure_database", lambda: None)


def test_list_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda *_args: None)
    monkeypatch.setattr(
        cli,
        "list_invoice_files",
        lambda: [{"name": "a.txt", "format": "txt", "bytes": 12, "path": "/tmp/a.txt"}],
    )
    listed = RUNNER.invoke(cli.app, ["list"])
    assert (listed.exit_code, "a.txt" in listed.stdout) == (0, True)


def test_inventory_and_history_commands(monkeypatch: pytest.MonkeyPatch, temp_db: Path) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda *_args: None)
    _configure_cli_database(monkeypatch, temp_db)
    inventory = RUNNER.invoke(cli.app, ["inventory"])
    history = RUNNER.invoke(cli.app, ["history", "--limit", "1"])
    assert (
        inventory.exit_code,
        "WidgetA" in inventory.stdout,
        history.exit_code,
        "history-run" not in history.stdout,
        "INV-CLI" in history.stdout,
    ) == (0, True, 0, True, True)


def test_reset_confirmation_and_yes_path(monkeypatch: pytest.MonkeyPatch, temp_db: Path) -> None:
    settings = Settings(database_path=temp_db)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    cancelled = RUNNER.invoke(cli.app, ["reset"], input="n\n")
    cleared = RUNNER.invoke(cli.app, ["reset", "--yes"])
    assert (
        cancelled.exit_code,
        "cancelled" in cancelled.stdout,
        cleared.exit_code,
        "history cleared" in cleared.stdout,
    ) == (1, True, 0, True)


def test_main_converts_keyboard_interrupt_to_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "app", lambda: (_ for _ in ()).throw(KeyboardInterrupt))
    with pytest.raises(SystemExit, match="130"):
        cli.main()
