"""Evaluation scorecard and renderer branches."""

from __future__ import annotations

from pathlib import Path

import pytest

import acme_ap.eval as eval_module
from acme_ap.config import Settings
from acme_ap.models import Outcome, RunResult


def _result(outcome: Outcome) -> RunResult:
    return RunResult(
        run_id="eval-run",
        source_path="invoice.txt",
        outcome=outcome,
        provider="stub",
        model="rules",
    )


def test_empty_scorecard_properties() -> None:
    empty = eval_module.Scorecard()
    assert (
        empty.field_accuracy,
        empty.decision_accuracy,
        empty.false_pay_rate,
        empty.passed,
    ) == (100.0, 100.0, 0.0, False)


def test_invalid_quantity_checks() -> None:
    result = _result(Outcome.PAID)
    assert eval_module._quantities_checks(result, {"quantities": {1: 2, "WidgetA": "bad"}})
    assert eval_module._quantities_checks(result, {"quantities": []}) == []


def test_invalid_flag_and_field_checks() -> None:
    result = _result(Outcome.PAID)
    flags = eval_module._flag_checks(
        result,
        {"must_flag": [1, "MISSING"], "must_not_flag": ["OTHER"]},
    )
    checks = eval_module._field_checks(
        result, {"invoice_number": "INV", "vendor": "Vendor", "total": "bad"}
    )
    assert (len(flags), len(checks)) == (2, 3)


def test_score_file_records_decision_and_field_failures() -> None:
    score = eval_module.score_file(
        "invoice.txt", {"outcome": "REJECTED", "total": 8}, _result(Outcome.PAID)
    )
    assert score.failures


def test_evaluate_records_missing_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(eval_module, "get_settings", lambda: Settings(invoice_dir=tmp_path))
    card = eval_module.evaluate({"missing.txt": {"outcome": "REJECTED"}})
    assert card.scores[0].actual == "MISSING"
    assert not card.passed


def test_render_prints_all_row_marks_and_pass_fail_banners(
    capsys: pytest.CaptureFixture[str],
) -> None:
    eval_module.render(
        eval_module.Scorecard(
            scores=[
                eval_module.FileScore("clean", "PAID", "PAID"),
                eval_module.FileScore("false", "REJECTED", "PAID"),
                eval_module.FileScore("wrong", "PAID", "REJECTED"),
                eval_module.FileScore("field", "PAID", "PAID", failures=["bad field"]),
            ]
        )
    )
    eval_module.render(
        eval_module.Scorecard(scores=[eval_module.FileScore("clean", "PAID", "PAID")])
    )
    eval_module.render(eval_module.Scorecard())
    output = capsys.readouterr().out
    assert all(marker in output for marker in ("FALSE PAY", "PASS", "FAIL"))


def test_eval_main_returns_the_scorecard_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import acme_ap.logging as logging_module

    monkeypatch.setattr(logging_module, "configure_logging", lambda *_args: None)
    cards = [
        eval_module.Scorecard(scores=[eval_module.FileScore("x", "PAID", "PAID")]),
        eval_module.Scorecard(scores=[eval_module.FileScore("bad", "PAID", "REJECTED")]),
    ]
    monkeypatch.setattr(eval_module, "evaluate", lambda: cards.pop(0))
    monkeypatch.setattr(eval_module, "render", lambda _card: None)
    assert eval_module.main() == 0
    assert eval_module.main() == 1
