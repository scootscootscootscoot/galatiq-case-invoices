"""Structured logging with a run correlation ID.

Every log line emitted anywhere in the system carries the ``run_id`` of the
invoice being processed. When one invoice out of four hundred fails overnight,
``grep <run_id>`` reconstructs its entire journey through the graph.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from acme_ap.config import get_settings

_run_id: ContextVar[str | None] = ContextVar("run_id", default=None)

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
    "message",
    "asctime",
}


def current_run_id() -> str | None:
    """The correlation ID for the run in progress, if any."""
    return _run_id.get()


def new_run_id() -> str:
    """Mint a fresh correlation ID."""
    return uuid.uuid4().hex[:12]


@contextmanager
def run_context(run_id: str) -> Iterator[str]:
    """Bind ``run_id`` to every log line emitted inside this block."""
    token = _run_id.set(run_id)
    try:
        yield run_id
    finally:
        _run_id.reset(token)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the correlation ID promoted to a field."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if run_id := current_run_id():
            payload["run_id"] = run_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Human-readable single line, for interactive use."""

    def format(self, record: logging.LogRecord) -> str:
        run_id = current_run_id()
        prefix = f"[{run_id}] " if run_id else ""
        extras = " ".join(
            f"{k}={v}"
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        )
        line = f"{record.levelname:<7} {prefix}{record.getMessage()}"
        return f"{line}  {extras}" if extras else line


def configure_logging(force_format: str | None = None) -> None:
    """Install the root handler. Idempotent."""
    settings = get_settings()
    fmt = force_format or settings.log_format
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Module-level logger."""
    return logging.getLogger(name)
