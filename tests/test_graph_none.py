"""Graph no-invoice router edge."""

from __future__ import annotations

from tests.test_graph_edges import _nodes


def test_critique_without_invoice_returns_empty_problems() -> None:
    assert _nodes().critique({"invoice": None}) == {"problems": []}
