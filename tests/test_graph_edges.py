"""Residual graph node and router branches."""

from __future__ import annotations

import pytest

import acme_ap.graph as graph_module
from acme_ap.agents.base import AgentContext
from acme_ap.config import Settings
from acme_ap.llm.base import LLMError
from acme_ap.models import ExtractedInvoice, Outcome, RawDocument


class _Events:
    def append_event(self, *_args: object, **_kwargs: object) -> int:
        return 1


class _LLM:
    name = "test"
    model = "test"

    def complete_structured(self, **_kwargs: object) -> object:
        raise LLMError("not used")


def _nodes() -> graph_module._Nodes:
    return graph_module._Nodes(
        AgentContext("graph-run", _Events(), _LLM(), Settings(max_extraction_attempts=2))
    )


def test_extract_failure_and_critique_router_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = _nodes()
    document = RawDocument(source_path="x", source_format="txt", text="body")
    monkeypatch.setattr(
        nodes.ingestion, "attempt", lambda *_args: (_ for _ in ()).throw(LLMError("bad"))
    )
    failed = nodes.extract({"document": document})
    assert (
        failed["outcome"],
        nodes.after_critique({"outcome": Outcome.FAILED}),
        nodes.after_extract(failed),
    ) == (Outcome.FAILED, "validate", "failed")


def test_critique_emits_self_correction_for_clean_second_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nodes = _nodes()
    monkeypatch.setattr(nodes.ingestion, "critique", lambda _invoice: [])
    state = nodes.critique({"invoice": ExtractedInvoice(total=1), "attempts": 2})
    assert state["problems"] == []
    assert state["critique"] is None
    assert nodes.after_critique({"problems": ["bad"], "attempts": 2}) == "validate"
