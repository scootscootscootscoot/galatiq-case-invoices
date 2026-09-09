"""Durable review workflow: corrections create a new, fully validated run."""

from __future__ import annotations

from typing import Literal, Self

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from acme_ap.config import get_settings
from acme_ap.db.repository import Repository
from acme_ap.models import ExtractedInvoice, RawDocument
from acme_ap.service import process_invoice

router = APIRouter(prefix="/api/reviews", tags=["review"])


class ReviewAction(BaseModel):  # type: ignore[explicit-any]
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    action: Literal["correct", "dismiss"]
    reviewer: str = Field(min_length=2, max_length=120)
    note: str = Field(min_length=5, max_length=2000)
    source_verified: bool = False
    invoice: ExtractedInvoice | None = None

    @model_validator(mode="after")
    def correction_requires_verification(self) -> Self:
        if self.action == "correct" and (not self.source_verified or self.invoice is None):
            raise ValueError("A corrected invoice and explicit source verification are required")
        return self


@router.get("")
def reviews(
    status: Literal["OPEN", "IN_PROGRESS", "RESOLVED", "DISMISSED"] = "OPEN",
) -> list[dict[str, object]]:
    with Repository(get_settings().database_path) as repo:
        return repo.list_reviews(status)


@router.post("/{run_id}/resolve")
def resolve_review(run_id: str, action: ReviewAction) -> dict[str, object]:
    with Repository(get_settings().database_path) as repo:
        review, run = repo.get_review(run_id), repo.get_run(run_id)
        if review is None or run is None:
            raise HTTPException(404, "Unknown review")
        if run.get("status") == "RUNNING":
            raise HTTPException(409, "Wait for the original run to finish")
        extraction = repo.get_extraction(run_id)
        if action.action == "correct" and extraction is None:
            raise HTTPException(
                409,
                "No readable source snapshot. Upload a readable replacement to start a new run.",
            )
        if not repo.claim_review(run_id, action.reviewer, action.note, action.invoice):
            raise HTTPException(409, "This review is already being handled or has been resolved")
        repo.append_event(
            run_id,
            "review",
            "review_claimed",
            action.note,
            {
                "reviewer": action.reviewer,
                "action": action.action,
                "source_verified": action.source_verified,
            },
        )
        if action.action == "dismiss":
            repo.finish_review(run_id, "DISMISSED")
            repo.append_event(
                run_id, "review", "review_dismissed", "Alert closed without issuing payment."
            )
            return {"status": "DISMISSED", "run_id": run_id}
        assert extraction is not None and action.invoice is not None
        try:
            document = RawDocument.model_validate(extraction["document"])
            result = process_invoice(
                str(run["source_path"]),
                repo=repo,
                correction=action.invoice,
                source_snapshot=document,
            )
        except Exception:  # noqa: BLE001 - never strand a review on a request failure
            repo.finish_review(run_id, "OPEN")
            raise
        repo.finish_review(run_id, "RESOLVED", result.run_id)
        repo.append_event(
            result.run_id,
            "review",
            "human_correction",
            action.note,
            {
                "original_run_id": run_id,
                "reviewer": action.reviewer,
                "before": extraction["invoice"],
                "after": action.invoice.model_dump(mode="json"),
            },
        )
        repo.append_event(
            run_id,
            "review",
            "review_resolved",
            f"Correction revalidated: {result.outcome.value}",
            {"resolution_run_id": result.run_id, "reviewer": action.reviewer},
        )
        return {
            "status": "RESOLVED",
            "run_id": run_id,
            "resolution_run_id": result.run_id,
            "outcome": result.outcome.value,
        }
