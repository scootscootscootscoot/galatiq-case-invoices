"""Data access. The only module in the system that touches ``sqlite3``.

Agents receive a ``Repository`` and call methods on it. They never write SQL and
never learn what storage looks like, so swapping SQLite for Postgres is a change
to this file alone. Item-name resolution is delegated to :class:`Catalog`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from acme_ap.db.catalog import Catalog, InventoryRecord, normalize
from acme_ap.db.migrations import connect
from acme_ap.db.rows import (
    DecisionRow,
    EventRow,
    FindingRow,
    InventoryRow,
    PaymentRow,
    RunDetail,
    RunSummary,
)
from acme_ap.logging import get_logger
from acme_ap.models import (
    ApprovalDecision,
    ExtractedInvoice,
    ExtractionQuality,
    Finding,
    Outcome,
    PaymentReceipt,
    RawDocument,
    ValidationReport,
)

__all__ = [
    "DecisionRow",
    "EventRow",
    "FindingRow",
    "InventoryRecord",
    "InventoryRow",
    "PaymentRow",
    "Repository",
    "RunDetail",
    "RunSummary",
    "normalize",
]

logger = get_logger(__name__)


class Repository:
    """Typed access to the one database."""

    def __init__(self, path: Path | None = None) -> None:
        self._conn = connect(path)
        self.path = Path(self._conn.execute("PRAGMA database_list").fetchone()["file"])
        self._catalog = Catalog(self._conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Repository:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- catalog

    def lookup_item(self, raw_name: str) -> InventoryRecord | None:
        """Resolve a document's item name against the catalog."""
        return self._catalog.lookup(raw_name)

    def all_inventory(self) -> list[InventoryRow]:
        rows = self._conn.execute("SELECT * FROM inventory ORDER BY item")
        return [cast(InventoryRow, dict(r)) for r in rows]

    # ------------------------------------------------------------------- runs

    def create_run(
        self,
        run_id: str,
        source_path: str,
        provider: str | None = None,
        model: str | None = None,
        degraded: bool = False,
    ) -> None:
        """Open a run, or fill in the details of one already reserved.

        The API reserves a run id synchronously before handing it to the client
        and starting the work in the background, so that the id it returns is
        immediately queryable. The worker then calls this again with the provider
        it actually resolved. Upserting keeps both callers honest without either
        needing to know about the other.
        """
        self._conn.execute(
            """
            INSERT INTO runs (id, source_path, status, provider, model, degraded)
            VALUES (?, ?, 'RUNNING', ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                provider = COALESCE(excluded.provider, runs.provider),
                model    = COALESCE(excluded.model, runs.model),
                degraded = excluded.degraded
            """,
            (run_id, source_path, provider, model, int(degraded)),
        )
        self._conn.commit()

    def finish_run(
        self, run_id: str, status: Outcome, content_hash: str | None, error: str | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, content_hash = ?, error = ? WHERE id = ?",
            (
                status.value,
                datetime.now(UTC).isoformat(timespec="seconds"),
                content_hash,
                error,
                run_id,
            ),
        )
        self._conn.commit()

    def list_runs(self, limit: int = 50) -> list[RunSummary]:
        rows = self._conn.execute(
            """
            SELECT r.*, i.invoice_number, i.vendor_name, i.total, i.currency
            FROM runs r
            LEFT JOIN invoices i ON i.run_id = r.id
            ORDER BY r.started_at DESC, r.rowid DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [cast(RunSummary, dict(r)) for r in rows]

    def get_run(self, run_id: str) -> RunDetail | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return cast(RunDetail, dict(row)) if row else None

    # ----------------------------------------------------------------- events

    def append_event(
        self,
        run_id: str,
        agent: str,
        kind: str,
        message: str | None = None,
        payload: dict[str, object] | None = None,
        latency_ms: int | None = None,
    ) -> int:
        """Record one step. Returns the sequence number within the run."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM agent_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        seq = int(row["next"])
        self._conn.execute(
            "INSERT INTO agent_events (run_id, seq, agent, kind, message, payload, latency_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                seq,
                agent,
                kind,
                message,
                json.dumps(payload, default=str) if payload else None,
                latency_ms,
            ),
        )
        self._conn.commit()
        return seq

    def events_since(self, run_id: str, after_seq: int = 0) -> list[EventRow]:
        rows = self._conn.execute(
            "SELECT * FROM agent_events WHERE run_id = ? AND seq > ? ORDER BY seq",
            (run_id, after_seq),
        )
        out: list[EventRow] = []
        for r in rows:
            event = dict(r)
            if event.get("payload"):
                event["payload"] = dict(json.loads(event["payload"]))
            out.append(cast(EventRow, event))
        return out

    # --------------------------------------------------------------- invoices

    def upsert_vendor(self, name: str) -> int:
        key = normalize(name)
        self._conn.execute(
            "INSERT OR IGNORE INTO vendors (name, normalized_name) VALUES (?, ?)", (name, key)
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM vendors WHERE normalized_name = ?", (key,)
        ).fetchone()
        return int(row["id"])

    def save_invoice(self, run_id: str, invoice: ExtractedInvoice, content_hash: str) -> int:
        vendor_id = self.upsert_vendor(invoice.vendor_name) if invoice.vendor_name else None
        cursor = self._conn.execute(
            """
            INSERT INTO invoices (run_id, invoice_number, vendor_id, vendor_name, invoice_date,
                                  due_date, currency, subtotal, tax_amount, total,
                                  payment_terms, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                invoice.invoice_number,
                vendor_id,
                invoice.vendor_name,
                invoice.invoice_date.isoformat() if invoice.invoice_date else None,
                invoice.due_date.isoformat() if invoice.due_date else None,
                invoice.currency,
                invoice.subtotal,
                invoice.tax_amount,
                invoice.total,
                invoice.payment_terms,
                content_hash,
            ),
        )
        invoice_id = int(cursor.lastrowid or 0)
        self._conn.executemany(
            "INSERT INTO line_items (invoice_id, raw_name, canonical_item, quantity,"
            " unit_price, amount, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    invoice_id,
                    li.raw_name,
                    li.canonical_item,
                    li.quantity,
                    li.unit_price,
                    li.amount,
                    li.note,
                )
                for li in invoice.line_items
            ],
        )
        self._conn.commit()
        return invoice_id

    def find_paid_invoice(self, invoice_number: str, exclude_run: str) -> PaymentRow | None:
        """Has this invoice number already been paid on an earlier run?

        This is what stops a revised copy of an invoice from being paid twice.
        """
        row = self._conn.execute(
            """
            SELECT i.invoice_number, i.total, p.paid_at, p.run_id
            FROM invoices i
            JOIN payments p ON p.run_id = i.run_id
            WHERE i.invoice_number = ? AND i.run_id != ? AND p.status = 'success'
            ORDER BY p.paid_at DESC LIMIT 1
            """,
            (invoice_number, exclude_run),
        ).fetchone()
        return cast(PaymentRow, dict(row)) if row else None

    # -------------------------------------------------------------- findings

    def save_findings(self, run_id: str, findings: list[Finding]) -> None:
        self._conn.executemany(
            "INSERT INTO validation_findings (run_id, code, severity, item, message, evidence)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    f.code.value,
                    f.severity.value,
                    f.item,
                    f.message,
                    json.dumps(f.evidence, default=str),
                )
                for f in findings
            ],
        )
        self._conn.commit()

    def get_findings(self, run_id: str) -> list[FindingRow]:
        rows = self._conn.execute(
            "SELECT * FROM validation_findings WHERE run_id = ? ORDER BY id", (run_id,)
        )
        return [cast(FindingRow, dict(r)) for r in rows]

    # -------------------------------------------------------------- decisions

    def save_decision(self, run_id: str, outcome: Outcome, decision: ApprovalDecision) -> None:
        self._conn.execute(
            "INSERT INTO decisions (run_id, outcome, approved, rationale, critique_rounds,"
            " policy_version, hard_gate) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                outcome.value,
                int(decision.approved),
                decision.rationale,
                json.dumps([r.model_dump() for r in decision.critique_rounds]),
                decision.policy_version,
                decision.hard_gate_triggered,
            ),
        )
        self._conn.commit()

    def get_decision(self, run_id: str) -> DecisionRow | None:
        row = self._conn.execute(
            "SELECT * FROM decisions WHERE run_id = ? ORDER BY id DESC LIMIT 1", (run_id,)
        ).fetchone()
        if not row:
            return None
        decision = dict(row)
        if decision.get("critique_rounds"):
            decision["critique_rounds"] = json.loads(decision["critique_rounds"])
        return cast(DecisionRow, decision)

    # --------------------------------------------------------------- payments

    def record_payment(self, run_id: str, receipt: PaymentReceipt) -> bool:
        """Write a payment. Returns False if the idempotency key already exists."""
        try:
            self._conn.execute(
                "INSERT INTO payments (run_id, idempotency_key, vendor, amount, currency,"
                " status, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    receipt.idempotency_key,
                    receipt.vendor,
                    receipt.amount,
                    receipt.currency,
                    receipt.status,
                    receipt.detail,
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            self._conn.rollback()
            logger.warning(
                "duplicate payment suppressed",
                extra={"idempotency_key": receipt.idempotency_key},
            )
            return False

    def get_payment(self, run_id: str) -> PaymentRow | None:
        row = self._conn.execute("SELECT * FROM payments WHERE run_id = ?", (run_id,)).fetchone()
        return cast(PaymentRow, dict(row)) if row else None

    def payment_by_key(self, idempotency_key: str) -> PaymentRow | None:
        row = self._conn.execute(
            "SELECT * FROM payments WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return cast(PaymentRow, dict(row)) if row else None

    # ------------------------------------------------------------------ views

    def save_validation(self, run_id: str, report: ValidationReport) -> None:
        self.save_findings(run_id, report.findings)

    def save_extraction(
        self,
        run_id: str,
        document: RawDocument,
        invoice: ExtractedInvoice,
        quality: ExtractionQuality,
    ) -> None:
        self._conn.execute(
            "INSERT INTO extraction_records VALUES (?, ?, ?, ?)",
            (
                run_id,
                document.model_dump_json(),
                invoice.model_dump_json(),
                quality.model_dump_json(),
            ),
        )
        self._conn.commit()

    def get_extraction(self, run_id: str) -> dict[str, object] | None:
        row = self._conn.execute(
            "SELECT * FROM extraction_records WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row:
            return None
        return {key: json.loads(row[f"{key}_json"]) for key in ("document", "invoice", "quality")}

    def open_review(self, run_id: str, reasons: list[str]) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO reviews (run_id, reasons_json) VALUES (?, ?)",
            (run_id, json.dumps(reasons)),
        )
        self._conn.commit()

    def get_review(self, run_id: str) -> dict[str, object] | None:
        row = self._conn.execute("SELECT * FROM reviews WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["reasons"] = json.loads(result.pop("reasons_json"))
        return result

    def list_reviews(self, status: str = "OPEN") -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT v.run_id, v.status, v.created_at, v.reasons_json, r.source_path,"
            " i.invoice_number, i.vendor_name, i.total, i.currency FROM reviews v"
            " JOIN runs r ON r.id = v.run_id LEFT JOIN invoices i ON i.run_id = v.run_id"
            " WHERE v.status = ? ORDER BY v.created_at, v.rowid",
            (status,),
        )
        results = []
        for row in rows:
            result = dict(row)
            result["reasons"] = json.loads(result.pop("reasons_json"))
            results.append(result)
        return results

    def claim_review(
        self, run_id: str, reviewer: str, note: str, invoice: ExtractedInvoice | None
    ) -> bool:
        cursor = self._conn.execute(
            "UPDATE reviews SET status = 'IN_PROGRESS', reviewer = ?, note = ?, corrected_json = ?"
            " WHERE run_id = ? AND status = 'OPEN'",
            (reviewer, note, invoice.model_dump_json() if invoice else None, run_id),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def finish_review(self, run_id: str, status: str, child_run_id: str | None = None) -> None:
        self._conn.execute(
            "UPDATE reviews SET status = ?, resolution_run_id = ?, resolved_at = datetime('now')"
            " WHERE run_id = ? AND status = 'IN_PROGRESS'",
            (status, child_run_id, run_id),
        )
        self._conn.commit()

    def claim_payment(self, run_id: str, invoice: ExtractedInvoice) -> bool:
        # Keep the policy's invoice-number namespace; revisions share this key.
        key = normalize(invoice.invoice_number or "")
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            prior = self._conn.execute(
                "SELECT i.invoice_number FROM invoices i JOIN payments p ON p.run_id = i.run_id"
                " WHERE p.status = 'success'",
            ).fetchall()
            if any(normalize(row["invoice_number"] or "") == key for row in prior):
                return False
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO payment_claims (business_key, run_id) VALUES (?, ?)",
                (key, run_id),
            )
            return cursor.rowcount == 1
