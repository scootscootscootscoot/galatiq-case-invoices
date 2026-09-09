"""LangGraph orchestration.

The graph is the control flow, visibly. Extraction and its critique are two
nodes joined by a conditional edge that loops back, so the self-correction cycle
is something you can point at in the topology rather than a while loop hidden
inside a function. Routing after validation and approval is likewise explicit.

Checkpoints are written into the same SQLite file as everything else, which
makes a run resumable and keeps the "one system of record" promise honest.

Node handlers live in :class:`_Nodes`, one method per node. Keeping them in a
class rather than as closures inside ``build_graph`` makes each node's
complexity measurable on its own, which the complexity gate requires.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, TypedDict, TypeVar

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from acme_ap.agents.approval import ApprovalAgent
from acme_ap.agents.base import AgentContext
from acme_ap.agents.ingestion import IngestionAgent
from acme_ap.agents.payment import PaymentAgent
from acme_ap.agents.validation import ValidationAgent
from acme_ap.ingestion.quality import assess
from acme_ap.ingestion.readers import load_document
from acme_ap.llm.base import LLMError
from acme_ap.logging import get_logger
from acme_ap.models import (
    ApprovalDecision,
    ExtractedInvoice,
    ExtractionQuality,
    Finding,
    FindingCode,
    Outcome,
    PaymentReceipt,
    RawDocument,
    Severity,
    ValidationReport,
)

logger = get_logger(__name__)

T = TypeVar("T")


def _replace(_old: T | None, new: T) -> T:
    """LangGraph reducer: the last write wins on a channel."""
    return new


class PipelineState(TypedDict, total=False):
    """State carried between nodes, all channels last-write-wins."""

    source_path: str
    document: Annotated[RawDocument | None, _replace]
    invoice: Annotated[ExtractedInvoice | None, _replace]
    critique: Annotated[str | None, _replace]
    problems: Annotated[list[str], _replace]
    attempts: Annotated[int, _replace]
    validation: Annotated[ValidationReport | None, _replace]
    approval: Annotated[ApprovalDecision | None, _replace]
    payment: Annotated[PaymentReceipt | None, _replace]
    outcome: Annotated[Outcome | None, _replace]
    error: Annotated[str | None, _replace]
    quality: Annotated[ExtractionQuality | None, _replace]
    correction: ExtractedInvoice
    source_snapshot: RawDocument


class _Nodes:
    """One method per graph node, with the agent stack available."""

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx
        self.ingestion = IngestionAgent(ctx)
        self.validation = ValidationAgent(ctx)
        self.approval = ApprovalAgent(ctx)
        self.payment = PaymentAgent(ctx)

    # -------------------------------------------------------------- handlers

    def load(self, state: PipelineState) -> PipelineState:
        document = state.get("source_snapshot") or load_document(state["source_path"])
        self.ctx.emit(
            "loader",
            "document_loaded",
            f"read {document.source_format} document ({len(document.text)} chars)",
            {
                "format": document.source_format,
                "chars": len(document.text),
                "content_hash": document.content_hash,
            },
        )
        return {"document": document, "attempts": 0, "problems": []}

    def extract(self, state: PipelineState) -> PipelineState:
        document = state["document"]
        assert document is not None
        attempt = state.get("attempts", 0) + 1
        try:
            if "correction" in state:
                invoice, latency = state["correction"].model_copy(deep=True), 0
            elif not document.text.strip():
                invoice, latency = ExtractedInvoice(), 0
            else:
                invoice, latency = self.ingestion.attempt(document, state.get("critique"))
        except LLMError as exc:
            self.ctx.emit("ingestion", "extraction_failed", f"attempt {attempt}: {exc}")
            return {"attempts": attempt, "error": str(exc), "outcome": Outcome.FAILED}

        self.ctx.emit(
            "ingestion",
            "extraction_attempt",
            f"attempt {attempt}: {invoice.invoice_number or 'unnumbered'}, "
            f"{len(invoice.line_items)} line item(s)",
            {
                "attempt": attempt,
                "invoice_number": invoice.invoice_number,
                "vendor": invoice.vendor_name,
                "total": invoice.total,
                "currency": invoice.currency,
                "line_items": [
                    {"name": li.raw_name, "quantity": li.quantity, "unit_price": li.unit_price}
                    for li in invoice.line_items
                ],
            },
            latency,
        )
        return {"invoice": invoice, "attempts": attempt}

    def critique(self, state: PipelineState) -> PipelineState:
        invoice = state.get("invoice")
        if invoice is None:
            return {"problems": []}
        problems = self.ingestion.critique(invoice)
        if problems:
            self.ctx.emit(
                "ingestion",
                "critique",
                f"{len(problems)} problem(s) found in attempt {state.get('attempts', 0)}",
                {"problems": problems},
            )
        elif state.get("attempts", 0) > 1:
            self.ctx.emit(
                "ingestion",
                "self_correction",
                f"extraction converged on attempt {state.get('attempts')} after critique",
                {"attempts": state.get("attempts")},
            )
        return {
            "problems": problems,
            "critique": "\n".join(f"- {p}" for p in problems) if problems else None,
        }

    def validate(self, state: PipelineState) -> PipelineState:
        invoice, document = state.get("invoice"), state.get("document")
        assert invoice is not None and document is not None
        report = self.validation.run(invoice, document, state.get("problems", []))
        quality = assess(
            document,
            invoice,
            self.ctx.settings.extraction_confidence_threshold,
            state.get("problems", []),
            human_verified="correction" in state,
        )
        self.ctx.repo.save_extraction(self.ctx.run_id, document, invoice, quality)
        self.ctx.emit(
            "quality",
            "confidence_assessed",
            f"Source evidence score {quality.score:.0%}; review threshold {quality.threshold:.0%}",
            quality.model_dump(mode="json"),
        )
        if quality.requires_review:
            report.findings.append(
                Finding(
                    code=FindingCode.EXTRACTION_UNCERTAIN,
                    severity=Severity.WARN,
                    message="Extraction requires manual verification before payment.",
                    evidence={
                        "score": quality.score,
                        "threshold": quality.threshold,
                        "reasons": quality.reasons,
                    },
                )
            )
            self.ctx.repo.open_review(self.ctx.run_id, quality.reasons)
            self.ctx.emit(
                "review",
                "review_required",
                "Review alert opened; automatic payment is on hold.",
                {"reasons": quality.reasons, "score": quality.score},
            )
        self.ctx.repo.save_invoice(self.ctx.run_id, invoice, document.content_hash)
        self.ctx.repo.save_validation(self.ctx.run_id, report)
        return {"validation": report, "quality": quality}

    def after_validation(self, state: PipelineState) -> str:
        quality, report = state.get("quality"), state.get("validation")
        # Preserve definite business rejections while also opening a review alert.
        if quality and quality.requires_review and report and not report.has_blocking:
            return "review"
        return "approve"

    def review(self, state: PipelineState) -> PipelineState:
        decision = ApprovalDecision(
            approved=False,
            rationale="Extraction needs human verification. No payment issued.",
            policy_version=self.ctx.settings.policy_version,
            hard_gate_triggered="EXTRACTION_UNCERTAIN",
        )
        self.ctx.repo.save_decision(self.ctx.run_id, Outcome.REVIEW_REQUIRED, decision)
        return {"approval": decision, "outcome": Outcome.REVIEW_REQUIRED}

    def approve(self, state: PipelineState) -> PipelineState:
        invoice, report = state.get("invoice"), state.get("validation")
        assert invoice is not None and report is not None
        decision = self.approval.run(invoice, report)
        if decision.critique_rounds and not decision.critique_rounds[-1].accepted:
            self.ctx.repo.open_review(self.ctx.run_id, [decision.rationale])
            self.ctx.emit(
                "review", "review_required", "Approval review did not converge; alert opened."
            )
        return {"approval": decision}

    def pay(self, state: PipelineState) -> PipelineState:
        invoice, document, decision = (
            state["invoice"],
            state["document"],
            state["approval"],
        )
        assert invoice is not None and document is not None and decision is not None
        quality = state.get("quality")
        if quality is None or quality.requires_review:
            raise ValueError("Payment refused: source verification is required")
        receipt = self.payment.run(invoice, document.content_hash)
        outcome = Outcome.PAID if receipt.status == "success" else Outcome.REJECTED
        if outcome is Outcome.REJECTED:
            decision = decision.model_copy(
                update={"approved": False, "rationale": receipt.detail or "Payment was not issued."}
            )
        self.ctx.repo.save_decision(self.ctx.run_id, outcome, decision)
        return {"payment": receipt, "outcome": outcome, "approval": decision}

    def reject(self, state: PipelineState) -> PipelineState:
        decision = state.get("approval")
        if decision is not None:
            self.ctx.repo.save_decision(self.ctx.run_id, Outcome.REJECTED, decision)
            self.ctx.emit(
                "approval",
                "rejected",
                decision.rationale,
                {
                    "policy_version": decision.policy_version,
                    "hard_gate": decision.hard_gate_triggered,
                },
            )
        return {"outcome": Outcome.REJECTED}

    # -------------------------------------------------------------- routers

    def after_critique(self, state: PipelineState) -> str:
        """Loop back for another extraction, or move on.

        Bounded by ``max_extraction_attempts``. Unbounded self-correction is how
        an agent spends forty dollars refusing to admit a field is genuinely
        absent from the page.
        """
        if state.get("outcome") is Outcome.FAILED:
            return "validate"
        problems = state.get("problems", [])
        attempts = state.get("attempts", 0)
        if (
            problems
            and attempts < self.ctx.settings.max_extraction_attempts
            and "correction" not in state
        ):
            return "extract"
        return "validate"

    def after_extract(self, state: PipelineState) -> str:
        return "failed" if state.get("outcome") is Outcome.FAILED else "critique"

    def after_approval(self, state: PipelineState) -> str:
        decision = state.get("approval")
        return "pay" if decision is not None and decision.approved else "reject"


def build_graph(
    ctx: AgentContext, checkpointer: SqliteSaver | None = None
) -> CompiledStateGraph[PipelineState]:
    """Compile the invoice pipeline."""
    nodes = _Nodes(ctx)

    graph = StateGraph(PipelineState)
    graph.add_node("load", nodes.load)
    graph.add_node("extract", nodes.extract)
    graph.add_node("critique", nodes.critique)
    graph.add_node("validate", nodes.validate)
    graph.add_node("approve", nodes.approve)
    graph.add_node("pay", nodes.pay)
    graph.add_node("reject", nodes.reject)
    graph.add_node("review", nodes.review)

    graph.set_entry_point("load")
    graph.add_edge("load", "extract")
    graph.add_conditional_edges(
        "extract", nodes.after_extract, {"critique": "critique", "failed": END}
    )
    graph.add_conditional_edges(
        "critique", nodes.after_critique, {"extract": "extract", "validate": "validate"}
    )
    graph.add_conditional_edges(
        "validate", nodes.after_validation, {"approve": "approve", "review": "review"}
    )
    graph.add_edge("review", END)
    graph.add_conditional_edges("approve", nodes.after_approval, {"pay": "pay", "reject": "reject"})
    graph.add_edge("pay", END)
    graph.add_edge("reject", END)

    return graph.compile(checkpointer=checkpointer)


def make_checkpointer(database_path: str) -> SqliteSaver:
    """Checkpoint into the same database as everything else."""
    conn = sqlite3.connect(database_path, check_same_thread=False)
    return SqliteSaver(conn)
