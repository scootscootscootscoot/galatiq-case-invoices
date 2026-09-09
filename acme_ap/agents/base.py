"""Shared agent scaffolding.

Every agent gets the same context object and emits events through the same
channel. That uniformity is what makes the trace complete: there is no way for an
agent to do work without it appearing in ``agent_events``, because emitting is
how an agent talks at all.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from acme_ap.config import Settings
from acme_ap.db.repository import Repository
from acme_ap.llm.base import LLMClient
from acme_ap.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class AgentContext:
    """Everything an agent is allowed to touch."""

    run_id: str
    repo: Repository
    llm: LLMClient
    settings: Settings

    def emit(
        self,
        agent: str,
        kind: str,
        message: str | None = None,
        payload: dict[str, object] | None = None,
        latency_ms: int | None = None,
    ) -> int:
        """Record one step, to the database and the log, in one call."""
        seq = self.repo.append_event(
            self.run_id, agent, kind, message=message, payload=payload, latency_ms=latency_ms
        )
        logger.info(message or kind, extra={"agent": agent, "kind": kind, "seq": seq})
        return seq


class Agent:
    """Base class for the four pipeline stages.

    The interface is deliberately loose: each concrete agent defines its own
    ``run`` signature (payment takes the invoice plus a hash, validation takes
    the invoice and document, and so on). Forcing one abstract signature would
    only widen it to ``Any`` -- the trade that keeps them honest is a docstring
    and convention, not ABC plumbing.
    """

    name: str = "agent"

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    def emit(
        self,
        kind: str,
        message: str | None = None,
        payload: dict[str, object] | None = None,
        latency_ms: int | None = None,
    ) -> int:
        return self.ctx.emit(self.name, kind, message, payload, latency_ms)

    @contextmanager
    def timed(self, kind: str, message: str) -> Iterator[dict[str, object]]:
        """Emit a completion event carrying how long the block took."""
        payload: dict[str, object] = {}
        started = time.perf_counter()
        try:
            yield payload
        finally:
            elapsed = int((time.perf_counter() - started) * 1000)
            self.emit(kind, message, payload or None, elapsed)
        """Do the agent's work."""
