"""Domain models — the contract between agents.

These types are the reason self-correction is possible at all. An agent cannot be
asked to fix its output unless something can state precisely what was wrong with
it, and these schemas are that something: a failed validation here becomes the
critique text that drives the next extraction attempt.
"""

from __future__ import annotations

import hashlib
from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from acme_ap.money import product


class Severity(StrEnum):
    """How much a finding should weigh on the decision."""

    INFO = "INFO"
    WARN = "WARN"
    BLOCK = "BLOCK"


class FindingCode(StrEnum):
    """Stable identifiers for every defect the system can detect.

    Codes rather than free text so the eval harness can assert on them and so
    operators can build alerts without pattern-matching English.
    """

    MISSING_INVOICE_NUMBER = "MISSING_INVOICE_NUMBER"
    MISSING_VENDOR = "MISSING_VENDOR"
    MISSING_DUE_DATE = "MISSING_DUE_DATE"
    UNPARSEABLE_DATE = "UNPARSEABLE_DATE"
    PAST_DUE_DATE = "PAST_DUE_DATE"
    NO_LINE_ITEMS = "NO_LINE_ITEMS"
    NEGATIVE_QUANTITY = "NEGATIVE_QUANTITY"
    NEGATIVE_TOTAL = "NEGATIVE_TOTAL"
    ARITHMETIC_MISMATCH = "ARITHMETIC_MISMATCH"
    UNKNOWN_ITEM = "UNKNOWN_ITEM"
    ZERO_STOCK = "ZERO_STOCK"
    STOCK_EXCEEDED = "STOCK_EXCEEDED"
    STOCK_AT_LIMIT = "STOCK_AT_LIMIT"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    NON_BASE_CURRENCY = "NON_BASE_CURRENCY"
    HIGH_VALUE = "HIGH_VALUE"
    URGENCY_PRESSURE = "URGENCY_PRESSURE"
    PRICE_DEVIATION = "PRICE_DEVIATION"
    ITEM_NAME_REPAIRED = "ITEM_NAME_REPAIRED"
    EXTRACTION_DEGRADED = "EXTRACTION_DEGRADED"
    EXTRACTION_UNCERTAIN = "EXTRACTION_UNCERTAIN"
    INVALID_PAYMENT_DATA = "INVALID_PAYMENT_DATA"


class Outcome(StrEnum):
    """Terminal state of a run."""

    PAID = "PAID"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #


class RawDocument(BaseModel):  # type: ignore[explicit-any]
    """A source file, flattened to text before any model sees it."""

    model_config = ConfigDict(frozen=True)

    source_path: str
    source_format: str
    text: str
    metadata: dict[str, object] = Field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """Stable identity for the document's content, used for idempotency."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


class LineItem(BaseModel):  # type: ignore[explicit-any]
    """One row of an invoice.

    ``raw_name`` is preserved verbatim so a reviewer can always see what the
    document actually said; ``canonical_item`` is what validation resolved it to.
    """

    model_config = ConfigDict(allow_inf_nan=False)

    raw_name: str
    canonical_item: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    amount: float | None = None
    note: str | None = None

    @property
    def computed_amount(self) -> float | None:
        """Quantity times unit price, when both are known."""
        if self.quantity is None or self.unit_price is None:
            return None
        return product(self.quantity, self.unit_price)


class ExtractedInvoice(BaseModel):  # type: ignore[explicit-any]
    """Structured invoice data.

    Deliberately permissive: nearly every field is optional. An invoice with a
    missing vendor is a business problem to be flagged downstream, not a parse
    error to be raised here. Refusing to represent bad data would mean the system
    could not report on it.
    """

    model_config = ConfigDict(allow_inf_nan=False)

    invoice_number: str | None = None
    vendor_name: str | None = None
    vendor_address: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float | None = None
    tax_rate: float | None = None
    tax_amount: float | None = None
    shipping: float | None = None
    total: float | None = None
    currency: str = "USD"
    payment_terms: str | None = None
    notes: str | None = None

    raw_date_text: str | None = None
    raw_due_date_text: str | None = None

    @model_validator(mode="after")
    def _normalise_currency(self) -> Self:
        object.__setattr__(self, "currency", (self.currency or "USD").strip().upper())
        return self

    def aggregated_quantities(self) -> dict[str, float]:
        """Total quantity per canonical item across all lines.

        Invoices repeat items across lines — a base order and a rush order, a
        volume-discount tier. Stock has to be checked against the sum, not each
        line, or an invoice ordering 8 + 4 of a 15-stock item looks fine twice.
        """
        totals: dict[str, float] = {}
        for item in self.line_items:
            key = item.canonical_item or item.raw_name
            totals[key] = totals.get(key, 0.0) + (item.quantity or 0.0)
        return totals


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


class Finding(BaseModel):  # type: ignore[explicit-any]
    """A single detected defect."""

    code: FindingCode
    severity: Severity
    message: str
    item: str | None = None
    evidence: dict[str, object] = Field(default_factory=dict)

    def __str__(self) -> str:
        scope = f" [{self.item}]" if self.item else ""
        return f"{self.severity}:{self.code}{scope} {self.message}"


class ValidationReport(BaseModel):  # type: ignore[explicit-any]
    """Everything validation learned about an invoice."""

    findings: list[Finding] = Field(default_factory=list)
    resolved_items: dict[str, str] = Field(default_factory=dict)
    recomputed_subtotal: float | None = None

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCK]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARN]

    @property
    def has_blocking(self) -> bool:
        return bool(self.blocking)

    def codes(self) -> set[FindingCode]:
        return {f.code for f in self.findings}


# --------------------------------------------------------------------------- #
# Approval
# --------------------------------------------------------------------------- #


class CritiqueRound(BaseModel):  # type: ignore[explicit-any]
    """One turn of the propose/critique/revise loop, kept for the audit trail."""

    round_number: int
    proposal: str
    decision: bool
    critique: str | None = None
    accepted: bool = False


class ApprovalDecision(BaseModel):  # type: ignore[explicit-any]
    """The VP's verdict, with its reasoning."""

    approved: bool
    rationale: str
    policy_version: str
    critique_rounds: list[CritiqueRound] = Field(default_factory=list)
    hard_gate_triggered: str | None = None


# --------------------------------------------------------------------------- #
# Payment
# --------------------------------------------------------------------------- #


class PaymentReceipt(BaseModel):  # type: ignore[explicit-any]
    """Result of a payment attempt."""

    status: str
    vendor: str
    amount: float
    currency: str
    idempotency_key: str
    detail: str | None = None


# --------------------------------------------------------------------------- #
# Run state
# --------------------------------------------------------------------------- #


class RunResult(BaseModel):  # type: ignore[explicit-any]
    """The complete outcome of processing one invoice."""

    run_id: str
    source_path: str
    outcome: Outcome
    provider: str
    model: str
    degraded: bool = False
    document: RawDocument | None = None
    invoice: ExtractedInvoice | None = None
    validation: ValidationReport | None = None
    approval: ApprovalDecision | None = None
    payment: PaymentReceipt | None = None
    extraction_attempts: int = 0
    error: str | None = None
    quality: ExtractionQuality | None = None


class FieldEvidence(BaseModel):  # type: ignore[explicit-any]
    """Deterministic evidence score, never a model's self-reported probability."""

    field: str
    value: str | None
    source_value: str | None = None
    score: float = Field(ge=0, le=1)
    reason: str
    page: int | None = None
    excerpt: str | None = None


class ExtractionQuality(BaseModel):  # type: ignore[explicit-any]
    score: float = Field(ge=0, le=1)
    threshold: float
    requires_review: bool
    reasons: list[str] = Field(default_factory=list)
    fields: list[FieldEvidence] = Field(default_factory=list)
    method: str = "source-check-v1"
    human_verified: bool = False


class ApprovalProposal(BaseModel):  # type: ignore[explicit-any]
    """The VP agent's opening position."""

    approve: bool
    rationale: str


class ApprovalCritique(BaseModel):  # type: ignore[explicit-any]
    """The reviewer's challenge to a proposal."""

    agrees: bool
    critique: str
