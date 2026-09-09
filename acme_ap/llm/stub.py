"""Deterministic offline provider.

Not a mock that returns canned strings -- a real rule-based implementation of the
same contract. It exists for three reasons:

* the case specifies the system must run with no external API available;
* CI and the eval harness need to be deterministic and free, so a regression is
  unambiguously a regression and not model variance;
* when xAI is unreachable, the pipeline degrades to this instead of failing, and
  an invoice still gets a decision and an audit record.

Because it is exercised by the whole test suite, the offline path is the
best-tested code in the system rather than a stale fallback nobody runs.
"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel

from acme_ap.ingestion.heuristics import RawExtraction, parse_document, to_raw_extraction
from acme_ap.json_types import JsonValue
from acme_ap.llm.base import LLMError
from acme_ap.logging import get_logger
from acme_ap.models import ApprovalCritique, ApprovalProposal

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

_DOCUMENT_RE = re.compile(
    r"<document format=\"(?P<format>[a-z]+)\">\n(?P<body>.*?)\n</document>", re.DOTALL
)
_FACTS_RE = re.compile(r"<facts>\n(?P<body>.*?)\n</facts>", re.DOTALL)


class StubClient:
    """Rule-based stand-in for a reasoning model."""

    def __init__(self) -> None:
        self.name = "stub"
        self.model = "deterministic-rules"

    # ------------------------------------------------------------- extraction

    @staticmethod
    def _extract(user: str) -> RawExtraction:
        match = _DOCUMENT_RE.search(user)
        if not match:
            raise LLMError("stub provider found no <document> block in the prompt")
        invoice = parse_document(match.group("body"), match.group("format"))
        return to_raw_extraction(invoice)

    # -------------------------------------------------------------- approval

    @staticmethod
    def _facts(user: str) -> dict[str, JsonValue]:
        match = _FACTS_RE.search(user)
        if not match:
            raise LLMError("stub provider found no <facts> block in the prompt")
        return dict(json.loads(match.group("body")))

    @staticmethod
    def _list(facts: dict[str, JsonValue], key: str) -> list[JsonValue]:
        """Pull a JSON list out of the facts block, defaulting to empty."""
        value = facts.get(key)
        return value if isinstance(value, list) else []

    @staticmethod
    def _float(facts: dict[str, JsonValue], key: str, default: float) -> float:
        """Pull a float out of the facts block, preserving a provided zero."""
        value = facts.get(key)
        return float(value) if isinstance(value, (int, float)) else default

    def _propose(self, user: str) -> ApprovalProposal:
        """Mirror the written policy exactly.

        The stub deliberately reasons *only* from the policy. Where a live model
        would weigh softer signals, this abstains -- which keeps the offline path
        conservative rather than confidently wrong.
        """
        facts = self._facts(user)
        blocking = self._list(facts, "blocking_findings")
        warnings = self._list(facts, "warning_findings")
        total = self._float(facts, "total", 0.0)
        threshold = self._float(facts, "high_value_threshold", 10_000.0)

        if blocking:
            return ApprovalProposal(
                approve=False,
                rationale=(
                    f"Rejecting: {len(blocking)} blocking issue(s) — "
                    + "; ".join(str(b) for b in blocking[:4])
                ),
            )
        if total >= threshold and warnings:
            return ApprovalProposal(
                approve=False,
                rationale=(
                    f"Rejecting: {total:,.2f} is at or above the {threshold:,.0f} scrutiny "
                    f"threshold and {len(warnings)} warning(s) remain unresolved — "
                    + "; ".join(str(w) for w in warnings[:3])
                ),
            )
        if total >= threshold:
            return ApprovalProposal(
                approve=True,
                rationale=(
                    f"Approving: {total:,.2f} exceeds the {threshold:,.0f} threshold, but every "
                    "item resolved to the catalog, stock covers the order and the arithmetic "
                    "reconciles. No warnings outstanding."
                ),
            )
        if warnings:
            return ApprovalProposal(
                approve=True,
                rationale=(
                    f"Approving with {len(warnings)} advisory warning(s) below the "
                    f"{threshold:,.0f} threshold: " + "; ".join(str(w) for w in warnings[:3])
                ),
            )
        return ApprovalProposal(
            approve=True,
            rationale=(
                "Approving: all items resolved, stock sufficient, arithmetic reconciles, "
                "no findings raised."
            ),
        )

    def _critique(self, user: str) -> ApprovalCritique:
        """Check the proposal against the policy it claims to follow."""
        facts = self._facts(user)
        blocking = self._list(facts, "blocking_findings")
        warnings = self._list(facts, "warning_findings")
        total = self._float(facts, "total", 0.0)
        threshold = self._float(facts, "high_value_threshold", 10_000.0)
        proposed = bool(facts.get("proposed_approval"))

        if blocking and proposed:
            return ApprovalCritique(
                agrees=False,
                critique=(
                    "Policy violation: approval proposed while blocking findings stand — "
                    + "; ".join(str(b) for b in blocking[:3])
                ),
            )
        if proposed and total >= threshold and warnings:
            return ApprovalCritique(
                agrees=False,
                critique=(
                    f"High-value invoice at {total:,.2f} requires zero warnings; "
                    f"{len(warnings)} remain."
                ),
            )
        if not proposed and not blocking and not warnings:
            return ApprovalCritique(
                agrees=False,
                critique="Rejection proposed but no findings of any severity were raised.",
            )
        return ApprovalCritique(agrees=True, critique="Consistent with policy.")

    # ------------------------------------------------------------- protocol

    def complete_structured(self, *, system: str, user: str, schema: type[T], purpose: str) -> T:
        if schema is RawExtraction:
            result: BaseModel = self._extract(user)
        elif schema is ApprovalProposal:
            result = self._propose(user)
        elif schema is ApprovalCritique:
            result = self._critique(user)
        else:
            raise LLMError(f"stub provider cannot satisfy schema {schema.__name__}")
        logger.debug("stub completion", extra={"purpose": purpose, "schema": schema.__name__})
        return schema.model_validate(result.model_dump())
