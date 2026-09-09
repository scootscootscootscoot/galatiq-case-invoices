"""Provider selection and graceful degradation."""

from __future__ import annotations

from acme_ap.config import Settings, get_settings
from acme_ap.llm.base import LLMClient, LLMError
from acme_ap.llm.stub import StubClient
from acme_ap.logging import get_logger

logger = get_logger(__name__)


def build_client(settings: Settings | None = None) -> LLMClient:
    """Return the configured provider, falling back to the stub if need be.

    A missing key or an unreachable API degrades the run rather than killing it:
    the invoice still gets validated, decided and recorded. Losing the reasoning
    model should cost you nuance, not the audit trail.
    """
    cfg = settings or get_settings()
    if cfg.resolved_provider == "stub":
        if cfg.llm_provider == "auto":
            logger.warning("no XAI_API_KEY found - running on the deterministic offline provider")
        return StubClient()

    try:
        from acme_ap.llm.xai import XAIClient

        client = XAIClient(cfg)
        logger.info("llm provider ready", extra={"provider": "xai", "model": client.model})
        return client
    except LLMError as exc:
        logger.error(
            "xAI unavailable - degrading to the offline provider",
            extra={"error": str(exc)[:300]},
        )
        return StubClient()
