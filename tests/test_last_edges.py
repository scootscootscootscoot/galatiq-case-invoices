"""Final defensive branches and module entry points."""

from __future__ import annotations

import json
import logging
import runpy
import sys
from pathlib import Path

import pytest

import acme_ap.config as config_module
import acme_ap.db.migrations as migrations_module
import acme_ap.db.repository as repository_module
import acme_ap.db.seed as seed_module
import acme_ap.eval as eval_module
import acme_ap.service as service_module
from acme_ap.config import Settings
from acme_ap.ingestion.heuristics.structured import parse_json_invoice, parse_xml_invoice
from acme_ap.llm.stub import StubClient
from acme_ap.logging import JsonFormatter, run_context
from acme_ap.models import Outcome, RunResult


def test_ingestion_defensive_no_result_path() -> None:
    from acme_ap.agents.ingestion import IngestionAgent
    from acme_ap.llm.base import LLMError
    from tests.test_agent_branches import _context, _document

    agent = IngestionAgent(_context(object(), max_extraction_attempts=1))
    agent.ctx.settings.max_extraction_attempts = 0
    with pytest.raises(LLMError, match="no result"):
        agent.run(_document())


def test_catalog_uses_normalized_match_when_no_alias_exists(repo: object) -> None:
    repository = repo
    repository._conn.execute(  # type: ignore[attr-defined]
        "INSERT INTO inventory (item, stock, unit_price, category) VALUES (?, ?, ?, ?)",
        ("PlainItem", 2, 1.0, "misc"),
    )
    repository._conn.commit()  # type: ignore[attr-defined]
    record = repository.lookup_item("plainitem")  # type: ignore[attr-defined]
    assert record is not None
    assert record.matched_via == "normalized"


def test_json_and_xml_optional_fallbacks() -> None:
    scalar_vendor = parse_json_invoice('{"vendor": "Vendor"}')
    no_items = parse_xml_invoice("invoice_number: INV-1")
    assert (
        scalar_vendor.vendor_name if scalar_vendor else None,
        no_items.line_items if no_items else None,
    ) == ("Vendor", [])


def _facts(**values: object) -> str:
    defaults = {
        "blocking_findings": [],
        "warning_findings": [],
        "total": 1,
        "high_value_threshold": 10_000,
    }
    defaults.update(values)
    return f"<facts>\n{json.dumps(defaults)}\n</facts>"


def test_stub_proposal_policy_alternatives() -> None:
    stub = StubClient()
    proposals = (
        stub._propose(_facts(blocking_findings=["bad"])).approve,
        stub._propose(_facts(total=10_000, warning_findings=["warn"])).approve,
        stub._propose(_facts(total=10_000)).approve,
        stub._propose(_facts(total=1, warning_findings=["warn"])).approve,
    )
    assert proposals == (False, False, True, True)


def test_stub_critique_policy_alternatives() -> None:
    stub = StubClient()
    critiques = (
        stub._critique(_facts(blocking_findings=["bad"], proposed_approval=True)).agrees,
        stub._critique(
            _facts(total=10_000, warning_findings=["warn"], proposed_approval=True)
        ).agrees,
        stub._critique(_facts(proposed_approval=False)).agrees,
        stub._critique(_facts(proposed_approval=True)).agrees,
    )
    assert critiques == (False, False, False, True)


def test_json_formatter_includes_run_id() -> None:
    record = logging.LogRecord("test", logging.INFO, "", 1, "message", (), None)
    with run_context("run-json"):
        body = json.loads(JsonFormatter().format(record))
    assert body["run_id"] == "run-json"


def test_cli_module_entry_point_is_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["acme-ap"])
    with pytest.raises(SystemExit):
        runpy.run_module("acme_ap.cli", run_name="__main__")


def test_eval_module_entry_point_runs_with_isolated_fakes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_invoices = eval_module.REPO_ROOT / "data" / "invoices"
    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: Settings(invoice_dir=real_invoices, database_path=tmp_path / "eval.db"),
    )
    monkeypatch.setattr(migrations_module, "apply", lambda *_args: 1)
    monkeypatch.setattr(seed_module, "seed", lambda *_args, **_kwargs: None)

    class _Repo:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(repository_module, "Repository", _Repo)
    monkeypatch.setattr(
        service_module,
        "process_invoice",
        lambda path, **_kwargs: RunResult(
            run_id="eval",
            source_path=str(path),
            outcome=Outcome.REJECTED,
            provider="stub",
            model="rules",
        ),
    )
    monkeypatch.setattr(sys, "argv", ["eval"])
    with pytest.raises(SystemExit):
        runpy.run_module("acme_ap.eval", run_name="__main__")
