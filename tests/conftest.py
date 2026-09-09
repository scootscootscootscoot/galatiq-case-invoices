from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("LLM_PROVIDER", "stub")
os.environ.setdefault("LOG_LEVEL", "WARNING")

REPO_ROOT = Path(__file__).resolve().parent.parent
INVOICE_DIR = REPO_ROOT / "data" / "invoices"


@pytest.fixture
def invoice_dir() -> Path:
    return INVOICE_DIR


@pytest.fixture
def temp_db(tmp_path: Path) -> Iterator[Path]:
    """A migrated, seeded database isolated to one test."""
    from acme_ap.db.migrations import apply
    from acme_ap.db.seed import seed

    path = tmp_path / "test.db"
    apply(path)
    seed(path)
    yield path


@pytest.fixture
def repo(temp_db: Path) -> Iterator[object]:
    from acme_ap.db.repository import Repository

    r = Repository(temp_db)
    yield r
    r.close()
