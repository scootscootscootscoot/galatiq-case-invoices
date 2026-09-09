"""Residual parser, storage, provider, and utility branches."""

from __future__ import annotations

import json
import runpy
import sqlite3
from datetime import date
from pathlib import Path

import pytest

import acme_ap.config as config_module
import acme_ap.db.migrations as migrations
import acme_ap.db.seed as seed_module
import acme_ap.ingestion.heuristics.text as text_module
import acme_ap.llm.factory as factory
from acme_ap.config import Settings
from acme_ap.ingestion.heuristics.scalars import parse_date, parse_money
from acme_ap.ingestion.heuristics.structured import (
    _cell,
    _column,
    parse_csv_table,
    parse_flat_kv,
    parse_json_invoice,
    parse_xml_invoice,
)
from acme_ap.ingestion.readers import read_csv, read_json, read_xml
from acme_ap.llm.base import LLMError
from acme_ap.llm.stub import StubClient
from acme_ap.models import RawDocument
from acme_ap.service import list_invoice_files


def test_scalar_fallbacks_and_date_month_lookup() -> None:
    assert (
        parse_money("1.2.3"),
        parse_date("2 September 2026 damaged"),
        parse_date("September 2 2026 damaged"),
        parse_date("2 Foo 2026"),
    ) == (None, date(2026, 9, 2), date(2026, 9, 2), None)


def test_json_parser_rejects_bad_shapes_and_reads_aliases() -> None:
    assert (parse_json_invoice("{"), parse_json_invoice("[]")) == (None, None)
    invoice = parse_json_invoice(
        json.dumps(
            {
                "vendor": {"name": "Nested Vendor", "address": "Main St"},
                "items": [
                    None,
                    {"quantity": 1},
                    {
                        "description": "WidgetA",
                        "qty": "2",
                        "rate": "3",
                        "line total": "6",
                        "note": "rush",
                    },
                ],
                "invoice_number": "INV-EDGE",
                "date": "2026-01-01",
                "due_date": "2026-02-01",
                "subtotal": "6",
                "tax_rate": "5",
                "tax_amount": "0.30",
                "total": "6.30",
                "currency": "EUR",
                "payment_terms": "net 30",
                "notes": "note",
            }
        )
    )
    assert invoice is not None
    assert (invoice.vendor_address, invoice.line_items[0].note, invoice.currency) == (
        "Main St",
        "rush",
        "EUR",
    )


def test_flat_and_xml_parser_edges() -> None:
    assert (
        parse_flat_kv("good: value\nnot a pair\n: empty\nblank:   ", ":"),
        parse_xml_invoice("vendor: V"),
        parse_xml_invoice("invoice: INV-1"),
    ) == ({"good": ["value"]}, None, None)
    xml_invoice = parse_xml_invoice(
        "invoice_number: INV-1\nitem: WidgetA\nquantity: 1\nprice: 2\ntax_rate: 5\ntotal: 2"
    )
    assert xml_invoice is not None
    assert xml_invoice.tax_rate == 5


def test_csv_parser_edges() -> None:
    assert (
        parse_csv_table("invoice number  vendor"),
        parse_csv_table("invoice number  vendor\nINV-1  Vendor"),
    ) == (None, None)
    table = parse_csv_table(
        "invoice number  vendor  date  due date  item  qty  unit price  line total\n"
        "INV-1  Vendor  2026-01-01  2026-02-01  WidgetA  1  2  2\n"
        "Subtotal  2\nTax  0\nTotal  2"
    )
    assert table is not None
    assert (
        (table.line_items[0].raw_name, _column(["item"], "missing")),
        (_cell(["value"], None), _cell(["value"], 3)),
    ) == (("WidgetA", None), (None, None))


def test_text_parser_rejects_false_item_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    extracted = text_module._extract_number("Invoice: Alpha Beta")
    monkeypatch.setattr(text_module, "_label_value", lambda *_args: "   ")
    assert (
        text_module._looks_like_item(""),
        text_module._looks_like_item("total"),
        extracted,
        text_module._extract_number("no number"),
        text_module._collect_items("total qty: 1 price: $2"),
    ) == (False, False, "Alpha", None, [])


def test_reader_fallbacks_for_invalid_json_csv_and_xml(tmp_path: Path) -> None:
    invalid_json = tmp_path / "bad.json"
    invalid_json.write_text("{", encoding="utf-8")
    assert read_json(invalid_json)[1] == {"json_valid": False}

    empty_csv = tmp_path / "empty.csv"
    empty_csv.write_text("", encoding="utf-8")
    assert read_csv(empty_csv)[1] == {"csv_rows": 0}

    invalid_xml = tmp_path / "bad.xml"
    invalid_xml.write_text("<invoice>", encoding="utf-8")
    assert read_xml(invalid_xml)[1] == {"xml_valid": False}


def test_seed_reset_is_idempotent_and_rolls_back_on_sql_error(
    temp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_module.seed(temp_db, reset=True)
    seed_module.seed(temp_db)
    conn = sqlite3.connect(temp_db)
    assert conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM item_aliases").fetchone()[0] == 14
    conn.close()

    class _Broken:
        rolled_back = False
        closed = False

        def executemany(self, *_args: object) -> None:
            raise sqlite3.IntegrityError("broken")

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    broken = _Broken()
    monkeypatch.setattr(seed_module, "connect", lambda _path=None: broken)
    with pytest.raises(sqlite3.IntegrityError):
        seed_module.seed(temp_db)
    assert broken.rolled_back and broken.closed


def test_module_entry_points_use_the_database_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _MainConnection:
        def execute(self, statement: str, *_args: object) -> list[dict[str, object]]:
            if statement.startswith("SELECT item"):
                return [{"item": "WidgetA", "stock": 15}]
            return []

        def executemany(self, *_args: object) -> None:
            return None

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    connection = _MainConnection()
    monkeypatch.setattr(migrations, "connect", lambda _path=None: connection)
    monkeypatch.setattr(migrations, "apply", lambda: None)
    runpy.run_module("acme_ap.db.seed", run_name="__main__")

    database = tmp_path / "migration.db"
    monkeypatch.setattr(config_module, "get_settings", lambda: Settings(database_path=database))
    runpy.run_module("acme_ap.db.migrations", run_name="__main__")
    assert database.exists()


def test_repository_context_closes_connection(temp_db: Path) -> None:
    from acme_ap.db.repository import Repository

    with Repository(temp_db) as repo:
        assert repo.all_inventory()


def test_stub_and_configuration_edges() -> None:
    assert (
        isinstance(factory.build_client(Settings(llm_provider="stub")), StubClient),
        isinstance(
            factory.build_client(Settings(llm_provider="auto", xai_api_key=None)), StubClient
        ),
        Settings(llm_provider="auto", xai_api_key="key").resolved_provider,
    ) == (True, True, "xai")
    with pytest.raises(ValueError, match="greater than zero"):
        Settings(high_value_threshold=0)


def test_factory_selects_xai(monkeypatch: pytest.MonkeyPatch) -> None:
    class _XAI:
        model = "fake"

        def __init__(self, _settings: Settings) -> None:
            self.name = "xai"

    monkeypatch.setattr("acme_ap.llm.xai.XAIClient", _XAI)
    assert factory.build_client(Settings(llm_provider="xai", xai_api_key="key")).name == "xai"


def test_factory_falls_back_when_xai_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Unavailable:
        def __init__(self, _settings: Settings) -> None:
            raise LLMError("offline")

    monkeypatch.setattr("acme_ap.llm.xai.XAIClient", _Unavailable)
    assert isinstance(
        factory.build_client(Settings(llm_provider="xai", xai_api_key="key")), StubClient
    )


def test_empty_invoice_directory_returns_no_files(tmp_path: Path) -> None:
    assert list_invoice_files(Settings(invoice_dir=tmp_path / "missing")) == []


def test_stub_provider_rejects_missing_blocks_and_unknown_schema() -> None:
    stub = StubClient()
    with pytest.raises(LLMError, match="no <document>"):
        stub._extract("nothing")
    with pytest.raises(LLMError, match="no <facts>"):
        stub._facts("nothing")
    with pytest.raises(LLMError, match="cannot satisfy"):
        stub.complete_structured(system="", user="", schema=RawDocument, purpose="x")
