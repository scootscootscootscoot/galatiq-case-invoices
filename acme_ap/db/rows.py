"""Row schemas returned by :mod:`acme_ap.db.repository`.

Separated so repository.py stays below the LOC gate. The repository re-exports
these names, so callers can keep importing them from one place.
"""

from __future__ import annotations

from typing import TypedDict


class InventoryRow(TypedDict):
    """One row of the ``inventory`` table."""

    item: str
    stock: int
    unit_price: float | None
    category: str | None


class RunSummary(TypedDict):
    """One row of ``list_runs``: the run plus its joined invoice facts."""

    id: str
    source_path: str
    status: str
    provider: str | None
    model: str | None
    degraded: int
    started_at: str
    finished_at: str | None
    content_hash: str | None
    error: str | None
    invoice_number: str | None
    vendor_name: str | None
    total: float | None
    currency: str | None


class RunDetail(TypedDict, total=False):
    """One row of ``get_run``, a raw ``runs`` table row."""

    id: str
    source_path: str
    status: str
    provider: str | None
    model: str | None
    degraded: int
    started_at: str
    finished_at: str | None
    content_hash: str | None
    error: str | None


class EventRow(TypedDict):
    """One row of ``agent_events`` with a decoded payload."""

    id: int
    run_id: str
    seq: int
    agent: str
    kind: str
    message: str | None
    payload: dict[str, object] | None
    latency_ms: int | None
    created_at: str


class FindingRow(TypedDict, total=False):
    """One row of ``validation_findings``."""

    id: int
    run_id: str
    code: str
    severity: str
    item: str | None
    message: str
    evidence: str | None


class DecisionRow(TypedDict, total=False):
    """One row of ``decisions`` with critique rounds decoded."""

    id: int
    run_id: str
    outcome: str
    approved: int
    rationale: str
    critique_rounds: list[dict[str, object]] | None
    policy_version: str
    hard_gate: str | None
    created_at: str


class PaymentRow(TypedDict, total=False):
    """One row of ``payments``."""

    id: int
    run_id: str
    idempotency_key: str
    vendor: str
    amount: float
    currency: str
    status: str
    detail: str | None
    paid_at: str | None
