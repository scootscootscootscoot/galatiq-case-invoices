"""Validation agent: check an extracted invoice against the business.

Tool use against real data rather than a model opining about plausibility. Every
finding here is reproducible, cites its evidence, and carries a severity that the
approval policy consumes mechanically.
"""

from __future__ import annotations

import re
from datetime import date

from acme_ap.agents.base import Agent
from acme_ap.models import (
    ExtractedInvoice,
    Finding,
    FindingCode,
    RawDocument,
    Severity,
    ValidationReport,
)
from acme_ap.money import difference as money_difference
from acme_ap.money import total as money_total

# Pressure tactics. Legitimate invoices state terms; they do not shout.
_URGENCY_PATTERNS = (
    r"\burgent\b",
    r"\bimmediate(?:ly)?\b",
    r"wire transfer",
    r"avoid penalt",
    r"final notice",
    r"pay (?:now|today)",
    r"!!!",
)


class ValidationAgent(Agent):
    """Resolves items, checks stock, and recomputes the money."""

    name = "validation"

    def run(
        self,
        invoice: ExtractedInvoice,
        document: RawDocument,
        extraction_problems: list[str] | None = None,
    ) -> ValidationReport:
        report = ValidationReport()

        with self.timed("validation_complete", "validation finished") as payload:
            self._check_completeness(invoice, report)
            self._resolve_and_check_stock(invoice, report)
            self._check_arithmetic(invoice, report)
            self._check_duplicates(invoice, report)
            self._check_currency(invoice, report)
            self._check_dates(invoice, report)
            self._check_pressure(document, report)
            self._carry_extraction_problems(extraction_problems or [], report)

            payload.update(
                {
                    "findings": len(report.findings),
                    "blocking": len(report.blocking),
                    "warnings": len(report.warnings),
                    "codes": sorted(c.value for c in report.codes()),
                }
            )

        for finding in report.findings:
            self.emit(
                "finding",
                str(finding),
                {
                    "code": finding.code.value,
                    "severity": finding.severity.value,
                    "item": finding.item,
                    "message": finding.message,
                    "evidence": finding.evidence,
                },
            )
        return report

    # ----------------------------------------------------------- completeness

    @staticmethod
    def _check_completeness(invoice: ExtractedInvoice, report: ValidationReport) -> None:
        invalid = []
        if invoice.total is None or invoice.total == 0:
            invalid.append("A positive invoice total is required")
        if not re.fullmatch(r"[A-Z]{3}", invoice.currency):
            invalid.append("Currency must be a three-letter code")
        for index, item in enumerate(invoice.line_items, 1):
            if item.quantity is None or item.quantity == 0:
                invalid.append(f"Line {index} needs a non-zero quantity")
            if item.unit_price is None and item.amount is None:
                invalid.append(f"Line {index} needs a price or amount")
            if any(value is not None and value < 0 for value in (item.unit_price, item.amount)):
                invalid.append(f"Line {index} has a negative price or amount")
        for problem in invalid:
            report.findings.append(
                Finding(
                    code=FindingCode.INVALID_PAYMENT_DATA, severity=Severity.BLOCK, message=problem
                )
            )
        if not invoice.invoice_number:
            report.findings.append(
                Finding(
                    code=FindingCode.MISSING_INVOICE_NUMBER,
                    severity=Severity.BLOCK,
                    message="No invoice number; duplicate detection cannot run.",
                )
            )
        if not (invoice.vendor_name or "").strip():
            report.findings.append(
                Finding(
                    code=FindingCode.MISSING_VENDOR,
                    severity=Severity.BLOCK,
                    message="No vendor name. There is nobody to pay.",
                )
            )
        if not invoice.line_items:
            report.findings.append(
                Finding(
                    code=FindingCode.NO_LINE_ITEMS,
                    severity=Severity.BLOCK,
                    message="No line items; nothing to verify against inventory.",
                )
            )
        if invoice.total is not None and invoice.total < 0:
            report.findings.append(
                Finding(
                    code=FindingCode.NEGATIVE_TOTAL,
                    severity=Severity.BLOCK,
                    message=f"Invoice total is negative ({invoice.total:,.2f}).",
                    evidence={"total": invoice.total},
                )
            )

    # ------------------------------------------------------------------ stock

    def _resolve_and_check_stock(self, invoice: ExtractedInvoice, report: ValidationReport) -> None:
        """Resolve every line to the catalog, then check aggregated demand.

        Aggregation before comparison is the whole point. Invoice 1010 orders
        WidgetA twice -- 8 on one line, 4 as a rush order on another. Checked per
        line both pass against stock of 15; checked as the 12 units actually
        being ordered, the answer is still yes but for the right reason. Invoice
        1013 spreads 22 WidgetA across three lines and only fails when summed.
        """
        for item in invoice.line_items:
            record = self.ctx.repo.lookup_item(item.raw_name)
            if record is None:
                report.findings.append(
                    Finding(
                        code=FindingCode.UNKNOWN_ITEM,
                        severity=Severity.BLOCK,
                        message=f"'{item.raw_name}' is not in the inventory catalog.",
                        item=item.raw_name,
                        evidence={"raw_name": item.raw_name},
                    )
                )
                continue

            item.canonical_item = record.item
            report.resolved_items[item.raw_name] = record.item
            if record.matched_via not in {"exact", "alias"}:
                report.findings.append(
                    Finding(
                        code=FindingCode.ITEM_NAME_REPAIRED,
                        severity=Severity.INFO,
                        message=(
                            f"'{item.raw_name}' resolved to '{record.item}' "
                            f"via {record.matched_via}."
                        ),
                        item=record.item,
                        evidence={"raw_name": item.raw_name, "method": record.matched_via},
                    )
                )
            if (
                record.unit_price is not None
                and item.unit_price is not None
                and item.unit_price > record.unit_price
            ):
                report.findings.append(
                    Finding(
                        code=FindingCode.PRICE_DEVIATION,
                        severity=Severity.WARN,
                        message=(
                            f"{record.item} billed at {item.unit_price:,.2f}, above the "
                            f"catalog price of {record.unit_price:,.2f}."
                        ),
                        item=record.item,
                        evidence={"billed": item.unit_price, "catalog": record.unit_price},
                    )
                )

        for item in invoice.line_items:
            if item.quantity is not None and item.quantity < 0:
                report.findings.append(
                    Finding(
                        code=FindingCode.NEGATIVE_QUANTITY,
                        severity=Severity.BLOCK,
                        message=(f"'{item.raw_name}' has a negative quantity ({item.quantity:g})."),
                        item=item.canonical_item or item.raw_name,
                        evidence={"quantity": item.quantity},
                    )
                )

        for canonical, quantity in invoice.aggregated_quantities().items():
            record = self.ctx.repo.lookup_item(canonical)
            if record is None or quantity <= 0:
                continue
            if record.stock == 0:
                report.findings.append(
                    Finding(
                        code=FindingCode.ZERO_STOCK,
                        severity=Severity.BLOCK,
                        message=(
                            f"{record.item} has zero stock on hand but {quantity:g} "
                            "were invoiced. This product cannot have shipped."
                        ),
                        item=record.item,
                        evidence={"requested": quantity, "stock": 0},
                    )
                )
            elif quantity > record.stock:
                report.findings.append(
                    Finding(
                        code=FindingCode.STOCK_EXCEEDED,
                        severity=Severity.BLOCK,
                        message=(
                            f"{record.item}: {quantity:g} invoiced against {record.stock} in stock."
                        ),
                        item=record.item,
                        evidence={"requested": quantity, "stock": record.stock},
                    )
                )
            elif quantity == record.stock:
                report.findings.append(
                    Finding(
                        code=FindingCode.STOCK_AT_LIMIT,
                        severity=Severity.INFO,
                        message=(
                            f"{record.item}: {quantity:g} invoiced, exactly the quantity on hand."
                        ),
                        item=record.item,
                        evidence={"requested": quantity, "stock": record.stock},
                    )
                )

    # ------------------------------------------------------------- arithmetic

    def _check_arithmetic(self, invoice: ExtractedInvoice, report: ValidationReport) -> None:
        """Recompute the invoice from its own numbers.

        Independent recomputation is what catches a total that does not follow
        from the parts -- invoice 1013 states a subtotal and tax that sum to
        22,512.80 while claiming a total of 22,562.80. Fifty dollars with no
        stated cause is exactly the kind of quiet leak manual review misses.
        """
        tolerance = self.ctx.settings.arithmetic_tolerance
        for index, item in enumerate(invoice.line_items, 1):
            computed = item.computed_amount
            if (
                computed is not None
                and item.amount is not None
                and abs(money_difference(computed, item.amount)) > tolerance
            ):
                report.findings.append(
                    Finding(
                        code=FindingCode.ARITHMETIC_MISMATCH,
                        severity=Severity.BLOCK,
                        message=f"Line {index}: quantity × unit price is {computed:.2f}, stated amount is {item.amount:.2f}.",
                        evidence={"computed": computed, "stated": item.amount},
                    )
                )

        line_sum = money_total(
            (item.amount if item.amount is not None else item.computed_amount) or 0.0
            for item in invoice.line_items
        )
        report.recomputed_subtotal = round(line_sum, 2) if invoice.line_items else None

        if invoice.subtotal is not None and report.recomputed_subtotal is not None:
            if abs(money_difference(invoice.subtotal, report.recomputed_subtotal)) > tolerance:
                report.findings.append(
                    Finding(
                        code=FindingCode.ARITHMETIC_MISMATCH,
                        severity=Severity.BLOCK,
                        message=(
                            f"Line items sum to {report.recomputed_subtotal:,.2f} but the "
                            f"stated subtotal is {invoice.subtotal:,.2f}."
                        ),
                        evidence={
                            "computed": report.recomputed_subtotal,
                            "stated": invoice.subtotal,
                            "difference": round(invoice.subtotal - report.recomputed_subtotal, 2),
                        },
                    )
                )

        base = invoice.subtotal if invoice.subtotal is not None else report.recomputed_subtotal
        if base is not None:
            expected = money_total([base, invoice.tax_amount or 0.0, invoice.shipping or 0.0])
            if (
                invoice.total is not None
                and abs(money_difference(invoice.total, expected)) > tolerance
            ):
                difference = round(invoice.total - expected, 2)
                report.findings.append(
                    Finding(
                        code=FindingCode.ARITHMETIC_MISMATCH,
                        severity=Severity.BLOCK,
                        message=(
                            f"Subtotal {base:,.2f} + tax {(invoice.tax_amount or 0.0):,.2f}"
                            f" + shipping {(invoice.shipping or 0.0):,.2f} = {expected:,.2f}, "
                            f"but the invoice total is {invoice.total:,.2f}. "
                            f"{abs(difference):,.2f} is unaccounted for."
                        ),
                        evidence={
                            "computed": round(expected, 2),
                            "stated": invoice.total,
                            "difference": difference,
                        },
                    )
                )

    # -------------------------------------------------------------- duplicates

    def _check_duplicates(self, invoice: ExtractedInvoice, report: ValidationReport) -> None:
        """Has this invoice number already been paid?

        Invoice 1004 ships twice in the sample data: an original and a revision
        that adds a line. Both are plausible documents. Paying both is a real
        loss, so a number already settled blocks unconditionally and goes to a
        human.
        """
        if not invoice.invoice_number:
            return
        prior = self.ctx.repo.find_paid_invoice(invoice.invoice_number, self.ctx.run_id)
        if prior:
            report.findings.append(
                Finding(
                    code=FindingCode.DUPLICATE_INVOICE,
                    severity=Severity.BLOCK,
                    message=(
                        f"Invoice {invoice.invoice_number} was already paid on "
                        f"{prior.get('paid_at')} (run {prior.get('run_id')}). "
                        "Possible duplicate or revision."
                    ),
                    evidence=dict(prior),
                )
            )

    # ---------------------------------------------------------------- context

    def _check_currency(self, invoice: ExtractedInvoice, report: ValidationReport) -> None:
        base = self.ctx.settings.base_currency
        if invoice.currency and invoice.currency != base:
            report.findings.append(
                Finding(
                    code=FindingCode.NON_BASE_CURRENCY,
                    severity=Severity.WARN,
                    message=(
                        f"Invoiced in {invoice.currency}, not {base}. Stock and catalog "
                        f"prices are held in {base}; conversion is not automated."
                    ),
                    evidence={"currency": invoice.currency, "base": base},
                )
            )
        if invoice.total is not None and invoice.total >= self.ctx.settings.high_value_threshold:
            report.findings.append(
                Finding(
                    code=FindingCode.HIGH_VALUE,
                    severity=Severity.INFO,
                    message=(
                        f"Total {invoice.total:,.2f} is at or above the "
                        f"{self.ctx.settings.high_value_threshold:,.0f} scrutiny threshold."
                    ),
                    evidence={"total": invoice.total},
                )
            )

    @staticmethod
    def _check_dates(invoice: ExtractedInvoice, report: ValidationReport) -> None:
        """Date sanity, judged against the invoice's own timeline.

        Comparing due dates to today would flag every historical invoice as
        overdue and say nothing useful. A due date that precedes the invoice date
        is different: that is impossible on its face, and it is how invoice 1003
        manufactures urgency.
        """
        if invoice.due_date is None:
            if invoice.raw_due_date_text:
                report.findings.append(
                    Finding(
                        code=FindingCode.UNPARSEABLE_DATE,
                        severity=Severity.BLOCK,
                        message=(
                            f"Due date '{invoice.raw_due_date_text}' is not a date. "
                            "Payment timing cannot be established."
                        ),
                        evidence={"raw": invoice.raw_due_date_text},
                    )
                )
            else:
                report.findings.append(
                    Finding(
                        code=FindingCode.MISSING_DUE_DATE,
                        severity=Severity.WARN,
                        message="No due date stated.",
                    )
                )
            return

        if invoice.invoice_date and invoice.due_date < invoice.invoice_date:
            report.findings.append(
                Finding(
                    code=FindingCode.PAST_DUE_DATE,
                    severity=Severity.BLOCK,
                    message=(
                        f"Due date {invoice.due_date} precedes the invoice date "
                        f"{invoice.invoice_date}."
                    ),
                    evidence={
                        "due_date": str(invoice.due_date),
                        "invoice_date": str(invoice.invoice_date),
                    },
                )
            )
        elif invoice.due_date < date.today():
            report.findings.append(
                Finding(
                    code=FindingCode.PAST_DUE_DATE,
                    severity=Severity.INFO,
                    message=f"Due date {invoice.due_date} has passed.",
                    evidence={"due_date": str(invoice.due_date)},
                )
            )

    @staticmethod
    def _check_pressure(document: RawDocument, report: ValidationReport) -> None:
        """Flag payment-pressure language in the document body."""
        text = document.text.lower()
        hits = [p for p in _URGENCY_PATTERNS if re.search(p, text)]
        if hits:
            report.findings.append(
                Finding(
                    code=FindingCode.URGENCY_PRESSURE,
                    severity=Severity.WARN,
                    message=(
                        f"Document uses payment-pressure language ({len(hits)} marker(s)). "
                        "Common in invoice fraud."
                    ),
                    evidence={"patterns": hits},
                )
            )

    @staticmethod
    def _carry_extraction_problems(problems: list[str], report: ValidationReport) -> None:
        """Surface defects the extraction loop could not resolve."""
        for problem in problems:
            report.findings.append(
                Finding(
                    code=FindingCode.EXTRACTION_DEGRADED,
                    severity=Severity.WARN,
                    message=f"Unresolved after re-extraction: {problem}",
                )
            )
