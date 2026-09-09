"""Payment agent: the only component that moves money.

The case supplies a two-line ``mock_payment``. It is treated here as what it
stands in for -- a banking API -- because the failure modes that matter are the
ones a mock hides. A retried request that pays a vendor twice is the most
expensive bug this system could ship, so payment is idempotent by construction:
the key is derived from the invoice's content, and the database rejects a second
insert of the same key.
"""

from __future__ import annotations

from acme_ap.agents.base import Agent
from acme_ap.logging import get_logger
from acme_ap.models import ExtractedInvoice, PaymentReceipt

logger = get_logger(__name__)


def mock_payment(vendor: str, amount: float) -> dict[str, object]:
    """The case's mock banking call, unchanged in behaviour."""
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}


class PaymentAgent(Agent):
    """Executes an approved payment, exactly once."""

    name = "payment"

    def run(self, invoice: ExtractedInvoice, content_hash: str) -> PaymentReceipt:
        vendor = (invoice.vendor_name or "unknown").strip()
        amount = float(invoice.total or 0.0)
        if amount <= 0 or not invoice.invoice_number or not invoice.vendor_name:
            raise ValueError("Payment requires an invoice number, vendor and positive total")

        # Content hash rather than invoice number: a re-run of the same document
        # is the same payment, while a genuinely revised document has different
        # content and is allowed to proceed to its own duplicate check.
        idempotency_key = f"{invoice.invoice_number or 'NO-NUMBER'}:{content_hash}"

        if existing := self.ctx.repo.payment_by_key(idempotency_key):
            receipt = PaymentReceipt(
                status="duplicate_suppressed",
                vendor=vendor,
                amount=amount,
                currency=invoice.currency,
                idempotency_key=idempotency_key,
                detail=(
                    f"Already paid on {existing.get('paid_at')} under run "
                    f"{existing.get('run_id')}. No second payment issued."
                ),
            )
            self.emit("payment_suppressed", receipt.detail or "duplicate", dict(existing))
            return receipt

        if not self.ctx.repo.claim_payment(self.ctx.run_id, invoice):
            receipt = PaymentReceipt(
                status="duplicate_suppressed",
                vendor=vendor,
                amount=amount,
                currency=invoice.currency,
                idempotency_key=idempotency_key,
                detail="This invoice is already paid or reserved by another run. No payment issued.",
            )
            self.emit("payment_suppressed", receipt.detail or "duplicate")
            return receipt

        with self.timed("payment_complete", f"paid {invoice.currency} {amount:,.2f} to {vendor}"):
            result = mock_payment(vendor, amount)

        receipt = PaymentReceipt(
            status=str(result.get("status", "unknown")),
            vendor=vendor,
            amount=amount,
            currency=invoice.currency,
            idempotency_key=idempotency_key,
        )
        if not self.ctx.repo.record_payment(self.ctx.run_id, receipt):
            receipt.status = "duplicate_suppressed"
            receipt.detail = "Ledger already held this idempotency key."
        return receipt
