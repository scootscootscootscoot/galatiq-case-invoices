"""HTTP API and dashboard host.

Routes call :mod:`acme_ap.service`, the same entry point the CLI uses, so the
browser and the terminal cannot disagree about what the system decided.

Live updates work by tailing ``agent_events`` in the database rather than by
holding an in-memory queue. That is a deliberate consequence of the one-database
design: a run started in one process is watchable from another, a browser that
reconnects mid-run replays cleanly from a sequence number, and a run watched
today renders identically when replayed next year from the same rows.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from acme_ap.api.review import router as review_router
from acme_ap.config import get_settings
from acme_ap.db.repository import (
    DecisionRow,
    EventRow,
    FindingRow,
    InventoryRow,
    PaymentRow,
    Repository,
    RunDetail,
    RunSummary,
)
from acme_ap.ingestion.readers import supported_extensions
from acme_ap.logging import configure_logging, get_logger, new_run_id
from acme_ap.models import Outcome
from acme_ap.service import ensure_database, list_invoice_files, process_invoice

logger = get_logger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Acme AP",
    description="Multi-agent invoice processing.",
    version="1.0.0",
)
app.include_router(review_router)


@app.on_event("startup")
def _startup() -> None:
    configure_logging()
    ensure_database()
    logger.info("api ready", extra={"database": str(get_settings().database_path)})


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class RunRequest(BaseModel):  # type: ignore[explicit-any]
    invoice_path: str


class RunAccepted(BaseModel):  # type: ignore[explicit-any]
    run_id: str
    invoice_path: str


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _repo() -> Repository:
    return Repository(get_settings().database_path)


def _resolve_invoice(raw: str) -> Path:
    """Resolve a request path, refusing anything outside the invoice directory.

    The API takes a path from the network, so it must never be able to name an
    arbitrary file on the host.
    """
    settings = get_settings()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = settings.invoice_dir / candidate.name
    resolved = candidate.resolve()
    allowed = settings.invoice_dir.resolve()
    if not (resolved.is_relative_to(allowed) or resolved.is_relative_to(settings.resolved_upload_dir.resolve())):
        raise HTTPException(status_code=400, detail="invoice must live in the invoice directory")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"no such invoice: {resolved.name}")
    return resolved


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


class HealthStatus(BaseModel):  # type: ignore[explicit-any]
    """Aggregate health fields for the dashboard header."""

    status: str
    provider: str
    model: str
    degraded: bool
    policy_version: str
    high_value_threshold: float
    extraction_confidence_threshold: float


@app.get("/api/health")
def health() -> HealthStatus:
    settings = get_settings()
    return HealthStatus(
        status="ok",
        provider=settings.resolved_provider,
        model=settings.xai_model if not settings.degraded else "deterministic-rules",
        degraded=settings.degraded,
        policy_version=settings.policy_version,
        high_value_threshold=settings.high_value_threshold,
        extraction_confidence_threshold=settings.extraction_confidence_threshold,
    )


@app.get("/api/invoices")
def invoices() -> list[dict[str, str | int]]:
    return list_invoice_files()


@app.get("/api/inventory")
def inventory() -> list[InventoryRow]:
    repo = _repo()
    try:
        return repo.all_inventory()
    finally:
        repo.close()


@app.post("/api/runs", response_model=RunAccepted, status_code=202)
def start_run(request: RunRequest) -> RunAccepted:
    """Kick off a run and return immediately.

    The run id comes back before any work happens, so the client can subscribe
    to the event stream and miss nothing.
    """
    path = _resolve_invoice(request.invoice_path)
    run_id = new_run_id()

    # Reserve the row before returning, so the id we hand back is queryable the
    # instant the client receives it.
    repo = _repo()
    try:
        repo.create_run(run_id, str(path))
    finally:
        repo.close()

    def worker() -> None:
        try:
            process_invoice(path, run_id=run_id)
        except Exception:  # noqa: BLE001 - a background failure must be logged, not lost
            logger.exception("background run failed", extra={"run_id": run_id})

    threading.Thread(target=worker, name=f"run-{run_id}", daemon=True).start()
    return RunAccepted(run_id=run_id, invoice_path=str(path))


@app.post("/api/uploads", response_model=RunAccepted, status_code=202)
async def upload_invoice(file: UploadFile) -> RunAccepted:
    settings = get_settings()
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in supported_extensions():
        raise HTTPException(415, "Unsupported invoice format")
    content = await file.read(settings.max_upload_bytes + 1)
    await file.close()
    if not content:
        raise HTTPException(400, "The uploaded file is empty")
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, "Invoice exceeds the upload size limit")
    target = settings.resolved_upload_dir / f"{uuid.uuid4().hex}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as output:
        output.write(content)
    return start_run(RunRequest(invoice_path=str(target.resolve())))


@app.get("/api/runs")
def runs(limit: int = Query(default=50, ge=1, le=500)) -> list[RunSummary]:
    repo = _repo()
    try:
        return repo.list_runs(limit)
    finally:
        repo.close()


class RunBundle(BaseModel):  # type: ignore[explicit-any]
    """Run detail plus everything attached to it, for the detail pane."""

    run: RunDetail
    events: list[EventRow]
    findings: list[FindingRow]
    decision: DecisionRow | None
    payment: PaymentRow | None
    extraction: dict[str, object] | None
    review: dict[str, object] | None


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str) -> RunBundle:
    repo = _repo()
    try:
        run = repo.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="unknown run")
        return RunBundle(
            run=run,
            events=repo.events_since(run_id),
            findings=repo.get_findings(run_id),
            decision=repo.get_decision(run_id),
            payment=repo.get_payment(run_id),
            extraction=repo.get_extraction(run_id),
            review=repo.get_review(run_id),
        )
    finally:
        repo.close()


@app.get("/api/runs/{run_id}/source")
def source_file(run_id: str) -> FileResponse:
    with _repo() as repo:
        run = repo.get_run(run_id)
        extraction = repo.get_extraction(run_id)
    if not run:
        raise HTTPException(404, "Unknown run")
    path = _resolve_invoice(str(run["source_path"]))
    if extraction:
        document = extraction.get("document")
        metadata = document.get("metadata", {}) if isinstance(document, dict) else {}
        expected = metadata.get("file_sha256") if isinstance(metadata, dict) else None
        if expected and hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise HTTPException(
                409, "Source file changed; use the saved text snapshot in the audit record"
            )
    media_type = "application/pdf" if path.suffix.lower() == ".pdf" else "text/plain"
    return FileResponse(path, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"})


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, after: int = 0) -> StreamingResponse:
    """Server-sent events, tailed from the database.

    ``after`` lets a reconnecting client resume exactly where it stopped, which
    is why the sequence number lives in the row rather than in process memory.
    """

    with _repo() as repo:
        if repo.get_run(run_id) is None:
            raise HTTPException(404, "Unknown run")

    async def generate() -> AsyncIterator[str]:
        cursor = after
        idle_ticks = 0
        while True:
            repo = _repo()
            try:
                events = repo.events_since(run_id, cursor)
                run = repo.get_run(run_id)
            finally:
                repo.close()

            for event in events:
                cursor = int(event["seq"])
                yield f"event: agent\ndata: {json.dumps(event, default=str)}\n\n"

            if run is not None and run["status"] != "RUNNING" and not events:
                done = {"run_id": run_id, "status": run["status"]}
                yield f"event: done\ndata: {json.dumps(done, default=str)}\n\n"
                return

            idle_ticks = 0 if events else idle_ticks + 1
            # Give up on a run that never reports, rather than holding the
            # connection open forever.
            if idle_ticks > 600:
                yield (
                    f"event: done\ndata: {json.dumps({'run_id': run_id, 'status': 'TIMEOUT'})}\n\n"
                )
                return
            await asyncio.sleep(0.2)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class Stats(BaseModel):  # type: ignore[explicit-any]
    """Dashboard header counters."""

    runs: int
    paid: int
    rejected: int
    paid_value: float
    held_value: float
    review_required: int
    open_alerts: int
    paid_by_currency: dict[str, float]
    held_by_currency: dict[str, float]


@app.get("/api/stats")
def stats() -> Stats:
    """Counts for the dashboard header."""
    repo = _repo()
    try:
        rows = repo.list_runs(500)
        open_alerts = len(repo.list_reviews())
    finally:
        repo.close()
    paid = [r for r in rows if r["status"] == Outcome.PAID.value]
    rejected = [r for r in rows if r["status"] == Outcome.REJECTED.value]
    paid_totals: dict[str, float] = {}
    held_totals: dict[str, float] = {}
    for group, target in ((paid, paid_totals), (rejected, held_totals)):
        for row in group:
            currency = row["currency"] or "UNKNOWN"
            target[currency] = round(target.get(currency, 0) + (row["total"] or 0), 2)
    return Stats(
        runs=len(rows),
        paid=len(paid),
        rejected=len(rejected),
        paid_value=paid_totals.get(get_settings().base_currency, 0),
        held_value=held_totals.get(get_settings().base_currency, 0),
        review_required=sum(r["status"] == Outcome.REVIEW_REQUIRED.value for r in rows),
        open_alerts=open_alerts,
        paid_by_currency=paid_totals,
        held_by_currency=held_totals,
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
