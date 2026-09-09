"""Approval agent: simulated VP review with a critique loop.

Two things happen here, in a deliberate order.

First a deterministic policy gate. If validation raised anything blocking, the
invoice is rejected and no model is consulted at all. This is the answer to
"would you let a language model approve a hundred-thousand-dollar payment?" --
it never gets the opportunity. The model reasons *within* the rules, never
around them.

Then, for invoices that clear the gate, a propose/critique/revise loop. The
proposer states a decision; a reviewer with the same facts challenges it; the
proposer may revise. Every round is persisted, so the audit trail shows the
reasoning, not just the verdict.
"""

from __future__ import annotations

import json

from acme_ap.agents.base import Agent
from acme_ap.llm.base import LLMError
from acme_ap.models import (
    ApprovalCritique,
    ApprovalDecision,
    ApprovalProposal,
    CritiqueRound,
    ExtractedInvoice,
    ValidationReport,
)

PROPOSER_SYSTEM = """You are a VP of Finance at Acme Corp deciding whether to pay an invoice.

You are bound by written policy and cannot override it:
1. Any BLOCKING finding means rejection. No exceptions, no matter how small the amount.
2. At or above the high-value threshold, approval requires zero warnings.
3. Below the threshold, warnings are advisory: weigh them and explain your reasoning.
4. Approve only what you can justify to an auditor in one paragraph.

State a decision and the reasoning behind it. Cite specific findings and figures.
Be concise and concrete."""

REVIEWER_SYSTEM = """You review a VP's invoice decision before it takes effect.

Check the decision against the same written policy:
1. Any BLOCKING finding requires rejection.
2. At or above the high-value threshold, approval requires zero warnings.
3. A rejection needs an actual finding to rest on.

Challenge the decision only where it departs from policy or ignores evidence.
If it is sound, say so plainly. Do not manufacture objections."""


class ApprovalAgent(Agent):
    """Decides pay or reject, and shows its work."""

    name = "approval"

    def run(self, invoice: ExtractedInvoice, report: ValidationReport) -> ApprovalDecision:
        settings = self.ctx.settings

        gate = self._hard_gate(report, invoice)
        if gate is not None:
            self.emit(
                "policy_gate",
                f"hard gate: {gate}",
                {"gate": gate, "blocking": [str(f) for f in report.blocking]},
            )
            return ApprovalDecision(
                approved=False,
                rationale=(
                    f"Rejected by policy gate before review: {gate}. "
                    + " ".join(f.message for f in report.blocking[:4])
                ),
                policy_version=settings.policy_version,
                hard_gate_triggered=gate,
            )

        facts = self._facts(invoice, report)
        rounds: list[CritiqueRound] = []
        proposal = self._propose(facts, None)

        for round_number in range(1, settings.max_critique_rounds + 1):
            critique = self._critique(facts, proposal)
            record = CritiqueRound(
                round_number=round_number,
                proposal=proposal.rationale,
                decision=proposal.approve,
                critique=critique.critique,
                accepted=critique.agrees,
            )
            rounds.append(record)
            self.emit(
                "critique_round",
                f"round {round_number}: proposed {'APPROVE' if proposal.approve else 'REJECT'}"
                f" — reviewer {'agrees' if critique.agrees else 'objects'}",
                record.model_dump(),
            )

            if critique.agrees:
                break
            if round_number < settings.max_critique_rounds:
                proposal = self._propose(facts, critique.critique)

        converged = bool(rounds and rounds[-1].accepted)
        approved = proposal.approve

        # Belt and braces: the loop must never be able to approve something the
        # policy forbids, whatever the model concluded.
        if approved and report.has_blocking:
            approved = False
            self.emit("policy_override", "approval overridden: blocking findings present")

        if not converged:
            self.emit(
                "escalation",
                "proposer and reviewer did not converge; defaulting to the safe outcome",
                {"rounds": len(rounds)},
            )
            approved = False

        return ApprovalDecision(
            approved=approved,
            rationale=proposal.rationale
            if converged
            else (
                f"{proposal.rationale} [Reviewer did not sign off after "
                f"{len(rounds)} rounds; held for human review.]"
            ),
            policy_version=settings.policy_version,
            critique_rounds=rounds,
        )

    # ------------------------------------------------------------------ gates

    def _hard_gate(self, report: ValidationReport, invoice: ExtractedInvoice) -> str | None:
        """Deterministic rejections, evaluated before any model call."""
        if report.has_blocking:
            codes = sorted({f.code.value for f in report.blocking})
            return f"{len(report.blocking)} blocking finding(s): {', '.join(codes)}"
        if invoice.total is None:
            return "no invoice total could be established"
        if invoice.total >= self.ctx.settings.high_value_threshold and report.warnings:
            return "high-value invoice has unresolved warnings"
        return None

    def _facts(self, invoice: ExtractedInvoice, report: ValidationReport) -> dict[str, object]:
        """The evidence packet both roles reason over. Identical for each."""
        return {
            "invoice_number": invoice.invoice_number,
            "vendor": invoice.vendor_name,
            "total": invoice.total,
            "currency": invoice.currency,
            "due_date": str(invoice.due_date) if invoice.due_date else None,
            "line_items": [
                {
                    "item": li.canonical_item or li.raw_name,
                    "quantity": li.quantity,
                    "unit_price": li.unit_price,
                }
                for li in invoice.line_items
            ],
            "aggregated_quantities": invoice.aggregated_quantities(),
            "blocking_findings": [str(f) for f in report.blocking],
            "warning_findings": [str(f) for f in report.warnings],
            "high_value_threshold": self.ctx.settings.high_value_threshold,
            "policy_version": self.ctx.settings.policy_version,
        }

    # ------------------------------------------------------------------- loop

    def _propose(self, facts: dict[str, object], critique: str | None) -> ApprovalProposal:
        payload = dict(facts)
        user = f"<facts>\n{json.dumps(payload, indent=2, default=str)}\n</facts>"
        if critique:
            user += (
                f"\n\nA reviewer challenged your previous decision:\n{critique}\n\n"
                "Revise your decision, or restate it with a stronger justification."
            )
        try:
            return self.ctx.llm.complete_structured(
                system=PROPOSER_SYSTEM,
                user=user,
                schema=ApprovalProposal,
                purpose="approval_proposal",
            )
        except LLMError as exc:
            self.emit("approval_degraded", f"proposer unavailable: {exc}")
            return ApprovalProposal(
                approve=False,
                rationale=f"Held: the reasoning model was unavailable ({exc}).",
            )

    def _critique(self, facts: dict[str, object], proposal: ApprovalProposal) -> ApprovalCritique:
        payload = dict(facts)
        payload["proposed_approval"] = proposal.approve
        payload["proposed_rationale"] = proposal.rationale
        user = f"<facts>\n{json.dumps(payload, indent=2, default=str)}\n</facts>"
        try:
            return self.ctx.llm.complete_structured(
                system=REVIEWER_SYSTEM,
                user=user,
                schema=ApprovalCritique,
                purpose="approval_critique",
            )
        except LLMError as exc:
            self.emit("critique_degraded", f"reviewer unavailable: {exc}")
            return ApprovalCritique(
                agrees=False, critique=f"Reviewer unavailable ({exc}); cannot sign off."
            )
