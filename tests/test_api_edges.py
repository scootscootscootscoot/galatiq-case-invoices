"""API worker and streaming edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import acme_ap.api.app as app_module
from acme_ap.config import get_settings


@pytest.fixture
def api_client(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    settings = get_settings()
    monkeypatch.setattr(settings, "database_path", temp_db)
    with TestClient(app_module.app) as client:
        yield client


def test_runs_endpoint_lists_reserved_history(api_client: TestClient) -> None:
    response = api_client.get("/api/runs")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_background_worker_logs_and_survives_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "invoice.txt"
    source.write_text("invoice", encoding="utf-8")

    class _Repo:
        def create_run(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            return None

    class _Thread:
        def __init__(self, target: object, **_kwargs: object) -> None:
            self.target = target

        def start(self) -> None:
            self.target()  # type: ignore[operator]

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("worker failed")

    monkeypatch.setattr(app_module, "_resolve_invoice", lambda _raw: source)
    monkeypatch.setattr(app_module, "new_run_id", lambda: "worker-run")
    monkeypatch.setattr(app_module, "_repo", lambda: _Repo())
    monkeypatch.setattr(app_module, "process_invoice", fail)
    monkeypatch.setattr(app_module.threading, "Thread", _Thread)
    accepted = app_module.start_run(app_module.RunRequest(invoice_path="invoice.txt"))
    assert accepted.run_id == "worker-run"


@pytest.mark.asyncio
async def test_event_stream_times_out_when_run_never_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Repo:
        def events_since(self, *_args: object) -> list[object]:
            return []

        def get_run(self, *_args: object) -> dict[str, str]:
            return {"status": "RUNNING"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def close(self) -> None:
            return None

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(app_module, "_repo", lambda: _Repo())
    monkeypatch.setattr(app_module.asyncio, "sleep", no_sleep)
    response = await app_module.stream_events("never")
    chunks = [chunk async for chunk in response.body_iterator]
    assert "TIMEOUT" in chunks[-1]
