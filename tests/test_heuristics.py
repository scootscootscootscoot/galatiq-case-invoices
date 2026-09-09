"""Scalar normalisers and the deterministic parsers."""

from __future__ import annotations

from datetime import date

import pytest

from acme_ap.ingestion.heuristics import parse_date, parse_money, parse_quantity


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$5,000.00", 5000.0),
        ("15000", 15000.0),
        ("$3,500.O0", 3500.0),  # capital O for zero -- the scan artifact
        ("$3,5OO.00", 3500.0),
        ("-250.00", -250.0),
        ("(250.00)", -250.0),  # accounting negative
        ("", None),
        (None, None),
        ("n/a", None),
        (1890.0, 1890.0),
    ],
)
def test_parse_money(raw: object, expected: float | None) -> None:
    assert parse_money(raw) == expected  # type: ignore[arg-type]


def test_negative_quantity_survives_parsing() -> None:
    """A negative quantity must reach validation, not be clamped to zero."""
    assert parse_quantity("-5") == -5.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-01-15", date(2026, 1, 15)),
        ("26-Jan-2O26", date(2026, 1, 26)),  # OCR damage in the year
        ("January 27, 2026", date(2026, 1, 27)),
        ("Jan 30 2026", date(2026, 1, 30)),
        ("01/28/2026", date(2026, 1, 28)),
        ("25-Feb-2026", date(2026, 2, 25)),
    ],
)
def test_parse_date(raw: str, expected: date) -> None:
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["yesterday", "", "null", "n/a", "soon", None])
def test_unparseable_dates_return_none(raw: str | None) -> None:
    """None is signal. Coercing "yesterday" to a real date would hide the defect."""
    assert parse_date(raw) is None
