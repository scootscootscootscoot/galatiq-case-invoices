"""Catalog resolution -- where a wrong match costs real money."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("WidgetA", "WidgetA"),
        ("Widget A", "WidgetA"),  # spacing drift
        ("widgeta", "WidgetA"),
        ("WidgetA ", "WidgetA"),
        ("Gadget X", "GadgetX"),
        ("WidgetA (rush order)", "WidgetA"),  # trailing qualifier
        ("W1dgetA", "WidgetA"),  # OCR digit/letter confusion
        ("FakeItem", "FakeItem"),
    ],
)
def test_known_names_resolve(repo: Any, raw: str, expected: str) -> None:
    record = repo.lookup_item(raw)
    assert record is not None, f"{raw} failed to resolve"
    assert record.item == expected


@pytest.mark.parametrize("raw", ["WidgetC", "SuperGizmo", "MegaSprocket", "Sprocket9"])
def test_unknown_items_stay_unknown(repo: Any, raw: str) -> None:
    """WidgetC must not resolve to WidgetB.

    They differ by one character, so generic similarity matching maps one to the
    other at roughly 0.86 confidence. That would pay for a product the catalog
    has never heard of. An unknown item costs a human thirty seconds; a wrong
    match costs the invoice amount.
    """
    assert repo.lookup_item(raw) is None


def test_seed_matches_the_case_specification(repo: Any) -> None:
    stock = {row["item"]: row["stock"] for row in repo.all_inventory()}
    assert stock == {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}
