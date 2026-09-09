"""Model-facing extraction schema.

Every field is a string. That is deliberate: the model's job is to *read* the
document, and normalisation is our job. Asking a model to also emit ISO dates
and unpunctuated floats invites it to silently correct ``$3,500.O0`` to ``3500``
-- losing the evidence the document was damaged. Here the damage arrives intact
and our own normalisers repair it, visibly and testably.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from acme_ap.ingestion.heuristics.scalars import parse_date, parse_money, parse_quantity
from acme_ap.models import ExtractedInvoice, LineItem


class RawLineItem(BaseModel):  # type: ignore[explicit-any]
    """A line item exactly as a model reported it, before normalisation."""

    name: str
    quantity: str | float | int | None = None
    unit_price: str | float | int | None = None
    amount: str | float | int | None = None
    note: str | None = None


class RawExtraction(BaseModel):  # type: ignore[explicit-any]
    """What the model is asked to return, verbatim.

    Every scalar is ``str | float | int | None`` so the repair path is forced to
    normalise deliberately rather than accept whatever the model wrote.
    """

    invoice_number: str | None = None
    vendor_name: str | None = None
    vendor_address: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    line_items: list[RawLineItem] = Field(default_factory=list)
    subtotal: str | float | int | None = None
    tax_rate: str | float | int | None = None
    tax_amount: str | float | int | None = None
    shipping: str | float | int | None = None
    total: str | float | int | None = None
    currency: str | None = None
    payment_terms: str | None = None
    notes: str | None = None

    def to_invoice(self) -> ExtractedInvoice:
        """Normalise into the domain model using the shared parsers."""
        return ExtractedInvoice(
            invoice_number=self.invoice_number or None,
            vendor_name=self.vendor_name or None,
            vendor_address=self.vendor_address or None,
            invoice_date=parse_date(self.invoice_date),
            due_date=parse_date(self.due_date),
            line_items=[
                LineItem(
                    raw_name=item.name,
                    quantity=parse_quantity(item.quantity),
                    unit_price=parse_money(item.unit_price),
                    amount=parse_money(item.amount),
                    note=item.note,
                )
                for item in self.line_items
                if item.name
            ],
            subtotal=parse_money(self.subtotal),
            tax_rate=parse_money(self.tax_rate),
            tax_amount=parse_money(self.tax_amount),
            shipping=parse_money(self.shipping),
            total=parse_money(self.total),
            currency=(self.currency or "USD"),
            payment_terms=self.payment_terms or None,
            notes=self.notes or None,
            raw_date_text=self.invoice_date,
            raw_due_date_text=self.due_date,
        )


def to_raw_extraction(invoice: ExtractedInvoice) -> RawExtraction:
    """Render a parsed invoice back into the model-facing shape."""
    return RawExtraction(
        invoice_number=invoice.invoice_number,
        vendor_name=invoice.vendor_name,
        vendor_address=invoice.vendor_address,
        invoice_date=invoice.raw_date_text
        or (invoice.invoice_date.isoformat() if invoice.invoice_date else None),
        due_date=invoice.raw_due_date_text
        or (invoice.due_date.isoformat() if invoice.due_date else None),
        line_items=[
            RawLineItem(
                name=li.raw_name,
                quantity=li.quantity,
                unit_price=li.unit_price,
                amount=li.amount,
                note=li.note,
            )
            for li in invoice.line_items
        ],
        subtotal=invoice.subtotal,
        tax_rate=invoice.tax_rate,
        tax_amount=invoice.tax_amount,
        shipping=invoice.shipping,
        total=invoice.total,
        currency=invoice.currency,
        payment_terms=invoice.payment_terms,
        notes=invoice.notes,
    )
