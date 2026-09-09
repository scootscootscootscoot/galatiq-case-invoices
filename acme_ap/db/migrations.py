"""Idempotent schema management.

``apply()`` is safe to call on every process start: a fresh database gets built,
an existing one is left alone. That means no separate setup step to forget and
no divergence between a developer's box and CI.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from acme_ap.config import get_settings
from acme_ap.logging import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 2
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a connection with the conventions the rest of the app expects."""
    settings = get_settings()
    target = path or settings.database_path
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply(path: Path | None = None) -> int:
    """Create or upgrade the schema. Returns the resulting version."""
    conn = connect(path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        version = int(row["v"] or 0)
        logger.info("schema ready", extra={"version": version, "path": str(path or "default")})
        return version
    finally:
        conn.close()


if __name__ == "__main__":
    from acme_ap.logging import configure_logging

    configure_logging("console")
    print(f"schema version {apply()}")
