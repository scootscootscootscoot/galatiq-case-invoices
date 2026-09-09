"""Ingestion agent: extract structured data, then critique and retry.

This is the self-correction core of the system. The loop is not "call the model
again and hope" -- each retry carries a specific, machine-generated list of what
was wrong with the previous attempt, so the second call is informed rather than a
reroll of the dice.

The critique is deterministic. It is our own code checking arithmetic, dates and
item names, never the model grading itself. A model asked to critique its own
extraction tends to agree with it.
"""

from __future__ import annotations

import time

from acme_ap.agents.base import Agent, AgentContext
from acme_ap.ingestion.heuristics import RawExtraction
from acme_ap.llm.base import LLMError
from acme_ap.models import ExtractedInvoice, RawDocument
from acme_ap.money import difference as money_difference
from acme_ap.money import total as money_total

SYSTEM_PROMPT = """You extract invoice data for an accounts-payable system.

Read the document and report exactly what it says. Transcribe values verbatim,
including anything that looks damaged, misspelled or wrong -- a scanned "$3,500.O0"
must be reported as "$3,500.O0", not silently corrected to 3500. Downstream code
repairs and reconciles; your job is faithful transcription.

Rules:
- Never invent a value. If a field is absent, return null.
- Report every line item separately, even when the same product appears twice.
- Preserve item names exactly as printed, including spacing.
- Do not compute totals the document does not state.
"""


class IngestionAgent(Agent):
    """Turns a raw document into a validated :class:`ExtractedInvoice`."""

    name = "ingestion"

    def __init__(self, ctx: AgentContext) -> None:
        super().__init__(ctx)
        self.attempts = 0

    # ----------------------------------------------------------------- prompt

    @staticmethod
    def _user_prompt(document: RawDocument, critique: str | None) -> str:
        parts = [f'<document format="{document.source_format}">\n{document.text}\n</document>']
        if critique:
            parts.append(
                "\nYour previous extraction had these problems. Re-read the document and "
                f"correct them:\n{critique}"
            )
        return "\n".join(parts)

    # ---------------------------------------------------------- single attempt

    def attempt(
        self, document: RawDocument, critique: str | None = None
    ) -> tuple[ExtractedInvoice, int]:
        """One extraction pass. Returns the invoice and the call latency.

        Split out from :meth:`run` so the orchestration graph can own the retry
        cycle as explicit edges rather than burying it in a while loop. The graph
        is then the honest picture of the control flow.
        """
        started = time.perf_counter()
        raw = self.ctx.llm.complete_structured(
            system=SYSTEM_PROMPT,
            user=self._user_prompt(document, critique),
            schema=RawExtraction,
            purpose="extraction",
        )
        return raw.to_invoice(), int((time.perf_counter() - started) * 1000)

    # ------------------------------------------------------------------- loop

    def run(self, document: RawDocument) -> tuple[ExtractedInvoice, list[str]]:
        """Extract, critique, and retry until clean or out of attempts.

        Returns the best invoice obtained plus any critique that still stands, so
        an unresolvable defect becomes a downstream finding instead of vanishing.
        """
        max_attempts = self.ctx.settings.max_extraction_attempts
        critique: str | None = None
        best: ExtractedInvoice | None = None
        outstanding: list[str] = []

        for attempt in range(1, max_attempts + 1):
            self.attempts = attempt
            started = time.perf_counter()
            try:
                raw = self.ctx.llm.complete_structured(
                    system=SYSTEM_PROMPT,
                    user=self._user_prompt(document, critique),
                    schema=RawExtraction,
                    purpose="extraction",
                )
            except LLMError as exc:
                self.emit("extraction_failed", f"attempt {attempt} failed: {exc}")
                if attempt >= max_attempts:
                    raise
                critique = f"Your previous response was unusable: {exc}"
                continue

            invoice = raw.to_invoice()
            elapsed = int((time.perf_counter() - started) * 1000)
            best = invoice

            problems = self.critique(invoice)
            self.emit(
                "extraction_attempt",
                f"attempt {attempt}: {len(invoice.line_items)} line item(s), "
                f"{len(problems)} problem(s)",
                {
                    "attempt": attempt,
                    "invoice_number": invoice.invoice_number,
                    "vendor": invoice.vendor_name,
                    "total": invoice.total,
                    "line_items": len(invoice.line_items),
                    "problems": problems,
                },
                elapsed,
            )

            if not problems:
                if attempt > 1:
                    self.emit(
                        "self_correction",
                        f"extraction converged on attempt {attempt} after critique",
                        {"attempts": attempt},
                    )
                return invoice, []

            outstanding = problems
            if attempt < max_attempts:
                critique = "\n".join(f"- {p}" for p in problems)
                self.emit(
                    "critique",
                    f"re-extracting: {len(problems)} unresolved problem(s)",
                    {"problems": problems, "attempt": attempt},
                )

        if best is None:
            raise LLMError("extraction produced no result")

        self.emit(
            "critique_exhausted",
            f"{len(outstanding)} problem(s) survived {max_attempts} attempts; "
            "passing to validation as findings",
            {"problems": outstanding},
        )
        return best, outstanding

    # -------------------------------------------------------------- critiques

    def critique(self, invoice: ExtractedInvoice) -> list[str]:
        """Deterministic checks that produce actionable retry instructions.

        Only defects a re-read could plausibly fix belong here. A genuinely
        missing due date is a business finding for the validation agent, not
        something to burn three model calls on.
        """
        problems: list[str] = []
        tolerance = self.ctx.settings.arithmetic_tolerance

        if not invoice.line_items:
            problems.append(
                "No line items were extracted. The document lists products; find and report them."
            )

        problems.extend(self._check_lines(invoice, tolerance))
        problems.extend(self._check_subtotal(invoice, tolerance))
        problems.extend(self._check_date(invoice))

        if invoice.total is None:
            problems.append("No invoice total was extracted.")
        return problems

    def _check_lines(self, invoice: ExtractedInvoice, tolerance: float) -> list[str]:
        """Per-line checks: quantity, price, and amount consistency."""
        problems: list[str] = []
        for index, item in enumerate(invoice.line_items, start=1):
            if item.quantity is None:
                problems.append(f"Line {index} ('{item.raw_name}') has no quantity.")
            if item.unit_price is None and item.amount is None:
                problems.append(
                    f"Line {index} ('{item.raw_name}') has neither a unit price nor an amount."
                )
            stated, computed = item.amount, item.computed_amount
            if stated is not None and computed is not None:
                if abs(money_difference(stated, computed)) > tolerance:
                    problems.append(
                        f"Line {index} ('{item.raw_name}'): quantity {item.quantity:g} x "
                        f"{item.unit_price:,.2f} = {computed:,.2f}, but the line total reads "
                        f"{stated:,.2f}. Re-read those three numbers."
                    )
        return problems

    def _check_subtotal(self, invoice: ExtractedInvoice, tolerance: float) -> list[str]:
        """A stated subtotal that disagrees with the sum of its own lines."""
        line_sum = money_total(
            (item.amount if item.amount is not None else item.computed_amount) or 0.0
            for item in invoice.line_items
        )
        if invoice.subtotal is not None and invoice.line_items and line_sum:
            if abs(money_difference(invoice.subtotal, line_sum)) > tolerance:
                return [
                    f"Line items sum to {line_sum:,.2f} but the subtotal reads "
                    f"{invoice.subtotal:,.2f}. Re-read the line amounts and the subtotal."
                ]
        return []

    def _check_date(self, invoice: ExtractedInvoice) -> list[str]:
        """Raw date text that survived without parsing to a real date."""
        if invoice.invoice_date is None and invoice.raw_date_text:
            return [
                f"The invoice date '{invoice.raw_date_text}' could not be parsed. "
                "Report it in an unambiguous form if the document allows."
            ]
        return []
