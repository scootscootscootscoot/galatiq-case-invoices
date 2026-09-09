"""The application service.

One function processes one invoice, and both the CLI and the HTTP API call it.
There is exactly one code path through the business logic, so the terminal and
the browser can never drift into disagreeing about what the system decided.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from acme_ap.agents.base import AgentContext
from acme_ap.config import Settings, get_settings
from acme_ap.db.migrations import apply as apply_migrations
from acme_ap.db.repository import Repository
from acme_ap.db.seed import seed as seed_inventory
from acme_ap.graph import PipelineState, build_graph, make_checkpointer
from acme_ap.llm.factory import build_client
from acme_ap.logging import get_logger, new_run_id, run_context
from acme_ap.models import ExtractedInvoice, Outcome, RawDocument, RunResult

logger = get_logger(__name__)


def ensure_database(settings: Settings | None = None) -> None:
    """Migrate and seed. Safe to call on every start."""
    cfg = settings or get_settings()
    apply_migrations(cfg.database_path)
    seed_inventory(cfg.database_path)


def process_invoice(
    source_path: str | Path,
    *,
    settings: Settings | None = None,
    repo: Repository | None = None,
    run_id: str | None = None,
    correction: ExtractedInvoice | None = None,
    source_snapshot: RawDocument | None = None,
) -> RunResult:
    """Run one invoice end to end and return the result.

    Every exit path -- success, rejection, or an unexpected failure -- closes the
    run out in the database. A run left in RUNNING forever is worse than a run
    marked FAILED, because only one of those is visible.
    """
    cfg = settings or get_settings()
    path = str(source_path)
    rid = run_id or new_run_id()

    owns_repo = repo is None
    repository = repo or Repository(cfg.database_path)
    llm = build_client(cfg)
    actual_degraded = llm.name == "stub"

    with run_context(rid):
        repository.create_run(rid, path, llm.name, llm.model, actual_degraded)
        ctx = AgentContext(run_id=rid, repo=repository, llm=llm, settings=cfg)

        result = RunResult(
            run_id=rid,
            source_path=path,
            outcome=Outcome.FAILED,
            provider=llm.name,
            model=llm.model,
            degraded=actual_degraded,
        )

        checkpointer = None
        try:
            checkpointer = make_checkpointer(str(repository.path))
            graph = build_graph(ctx, checkpointer)
            initial: PipelineState = {"source_path": path}
            if correction is not None:
                initial["correction"] = correction
            if source_snapshot is not None:
                initial["source_snapshot"] = source_snapshot
            final = cast(
                PipelineState,
                graph.invoke(
                    initial,
                    config={"configurable": {"thread_id": rid}},
                ),
            )

            result.document = final.get("document")
            result.invoice = final.get("invoice")
            result.validation = final.get("validation")
            result.approval = final.get("approval")
            result.payment = final.get("payment")
            result.extraction_attempts = int(final.get("attempts") or 0)
            result.outcome = final.get("outcome") or Outcome.FAILED
            result.error = final.get("error")
            result.quality = final.get("quality")
            if result.outcome is Outcome.FAILED:
                repository.open_review(
                    rid, [result.error or "Extraction failed; inspect the document."]
                )

            content_hash = result.document.content_hash if result.document else None
            repository.finish_run(rid, result.outcome, content_hash, result.error)
            logger.info(
                "run complete",
                extra={"outcome": result.outcome.value, "attempts": result.extraction_attempts},
            )

        except Exception as exc:  # noqa: BLE001 - the run must always close out
            result.outcome = Outcome.FAILED
            result.error = f"{type(exc).__name__}: {exc}"
            ctx.emit("pipeline", "run_failed", result.error)
            repository.finish_run(rid, Outcome.FAILED, None, result.error)
            repository.open_review(rid, [result.error])
            logger.exception("run failed")

        finally:
            if checkpointer is not None:
                checkpointer.conn.close()
            close_client = getattr(llm, "close", None)
            if callable(close_client):
                close_client()
            if owns_repo:
                repository.close()

    return result


def list_invoice_files(settings: Settings | None = None) -> list[dict[str, str | int]]:
    """Everything in the invoice directory a reader can handle."""
    from acme_ap.ingestion.readers import supported_extensions

    cfg = settings or get_settings()
    extensions = set(supported_extensions())
    if not cfg.invoice_dir.exists():
        return []
    return [
        {"name": p.name, "path": str(p), "format": p.suffix.lstrip("."), "bytes": p.stat().st_size}
        for p in sorted(cfg.invoice_dir.iterdir())
        if p.suffix.lower() in extensions
    ]
