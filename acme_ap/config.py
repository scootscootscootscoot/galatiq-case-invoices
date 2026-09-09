"""Typed application configuration.

Every tunable in the system resolves here, once, at startup. Nothing calls
``os.getenv`` at the point of use: a missing key or a nonsensical threshold fails
loudly when the process boots rather than silently ten minutes into a batch.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent

Provider = Literal["xai", "stub", "auto"]


class Settings(BaseSettings):  # type: ignore[explicit-any]
    """Runtime configuration, sourced from the environment and ``.env``."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM -----------------------------------------------------------------
    xai_api_key: str | None = Field(default=None, description="xAI API key.")
    xai_base_url: str = "https://api.x.ai/v1"
    xai_model: str = "grok-4"
    llm_provider: Provider = Field(
        default="auto",
        description="'auto' uses xAI when a key is present and falls back to the stub.",
    )
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3
    llm_temperature: float = 0.0

    # --- Agent loops ---------------------------------------------------------
    max_extraction_attempts: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Initial extraction plus critique-driven retries.",
    )
    max_critique_rounds: int = Field(
        default=2,
        ge=1,
        le=5,
        description="Propose/critique/revise rounds in the approval agent.",
    )

    # --- Approval policy -----------------------------------------------------
    policy_version: str = "2026.09.2"
    extraction_confidence_threshold: float = Field(default=0.90, gt=0, le=1)
    max_upload_bytes: int = Field(default=10_000_000, ge=1, le=50_000_000)
    max_pdf_pages: int = Field(default=30, ge=1, le=100)
    ocr_timeout_seconds: int = Field(default=30, ge=1, le=120)
    high_value_threshold: float = Field(
        default=10_000.00,
        description="Invoices at or above this total get heightened scrutiny.",
    )
    arithmetic_tolerance: float = Field(
        default=0.01,
        description="Dollar tolerance when recomputing invoice totals.",
    )
    base_currency: str = "USD"

    # --- Storage -------------------------------------------------------------
    database_path: Path = REPO_ROOT / "acme.db"
    invoice_dir: Path = REPO_ROOT / "data" / "invoices"
    upload_dir: Path | None = None

    @property
    def resolved_upload_dir(self) -> Path:
        return self.upload_dir or self.invoice_dir / "uploads"

    # --- Observability -------------------------------------------------------
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    @field_validator("high_value_threshold", "arithmetic_tolerance")
    @classmethod
    def _must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("must be greater than zero")
        return v

    @property
    def resolved_provider(self) -> Literal["xai", "stub"]:
        """Which provider will actually be used for this process."""
        if self.llm_provider == "auto":
            return "xai" if self.xai_api_key else "stub"
        return self.llm_provider

    @property
    def degraded(self) -> bool:
        """True when we are reasoning without a live model."""
        return self.resolved_provider == "stub"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
