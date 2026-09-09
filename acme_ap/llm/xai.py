"""xAI Grok client.

The case README shows ``from xai import Grok``. No such package exists -- that
snippet is pseudo-code. xAI's real API is OpenAI-compatible, so this talks to it
over plain HTTP with ``httpx``: no SDK to go stale, and full control of the
timeout, retry and parsing behaviour that actually determines whether this
survives contact with production.
"""

from __future__ import annotations

import json
import random
import time
from typing import TypeVar, cast

import httpx
from pydantic import BaseModel, ValidationError

from acme_ap.config import Settings, get_settings
from acme_ap.json_types import JsonDict, JsonValue
from acme_ap.llm.base import LLMError
from acme_ap.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

# Transient conditions worth another attempt. A 400 means our request was wrong
# and retrying it just spends money to fail again.
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class XAIClient:
    """Grok over the OpenAI-compatible chat completions endpoint."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        if not self._settings.xai_api_key:
            raise LLMError("XAI_API_KEY is not set")
        self.name = "xai"
        self.model = self._settings.xai_model
        self._client = httpx.Client(
            base_url=self._settings.xai_base_url,
            timeout=httpx.Timeout(self._settings.llm_timeout_seconds),
            headers={
                "Authorization": f"Bearer {self._settings.xai_api_key}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ calls

    def _post(self, payload: dict[str, JsonValue], purpose: str) -> JsonDict:
        """POST with bounded retries and exponential backoff plus jitter.

        Jitter matters when a batch is running: without it, every concurrent
        invoice retries in lockstep and hammers an already-struggling API.
        """
        last_error: Exception | None = None
        for attempt in range(1, self._settings.llm_max_retries + 1):
            started = time.perf_counter()
            try:
                response = self._client.post("/chat/completions", json=payload)
                elapsed_ms = int((time.perf_counter() - started) * 1000)

                if response.status_code in _RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"retryable status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                if response.status_code >= 400:
                    detail = response.text[:400]
                    if response.status_code == 404:
                        raise LLMError(
                            f"model '{self.model}' was rejected by {self._settings.xai_base_url}"
                            f" (404). Set XAI_MODEL to a model your key can reach. Detail: {detail}"
                        )
                    raise LLMError(f"xAI returned {response.status_code}: {detail}")

                logger.info(
                    "llm call complete",
                    extra={
                        "purpose": purpose,
                        "attempt": attempt,
                        "latency_ms": elapsed_ms,
                        "model": self.model,
                    },
                )
                return cast(JsonDict, dict(response.json()))

            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt >= self._settings.llm_max_retries:
                    break
                backoff = min(2.0 ** (attempt - 1), 8.0) + random.uniform(0, 0.5)
                logger.warning(
                    "llm call failed, retrying",
                    extra={
                        "purpose": purpose,
                        "attempt": attempt,
                        "sleep_s": round(backoff, 2),
                        "error": str(exc)[:200],
                    },
                )
                time.sleep(backoff)

        raise LLMError(
            f"xAI unreachable after {self._settings.llm_max_retries} attempts: {last_error}"
        ) from last_error

    @staticmethod
    def _content(response: JsonDict) -> str:
        """Dig the message text out of the OpenAI-shaped response."""
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
        raise LLMError(f"unexpected response shape: {json.dumps(response)[:300]}")

    def complete_structured(self, *, system: str, user: str, schema: type[T], purpose: str) -> T:
        """Ask for JSON matching ``schema`` and validate the reply.

        A validation failure here is not swallowed: it propagates so the calling
        agent can decide whether to critique and retry. Silently returning a
        half-parsed object would destroy the audit trail's credibility.
        """
        instructions = (
            f"{system}\n\nRespond with a single JSON object only -- no prose, no code fences. "
            f"It must conform to this JSON Schema:\n{json.dumps(schema.model_json_schema())}"
        )
        response = self._post(
            {
                "model": self.model,
                "temperature": self._settings.llm_temperature,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": user},
                ],
            },
            purpose,
        )
        content = self._content(response).strip()
        if content.startswith("```"):
            content = content.split("```")[1].removeprefix("json").strip()
        try:
            return schema.model_validate_json(content)
        except ValidationError as exc:
            raise LLMError(f"response did not match {schema.__name__}: {exc}") from exc
