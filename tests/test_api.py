"""HTTP surface, including the path guard and the event stream."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from acme_ap.api.app import app


@pytest.fixture
def client(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client backed by an isolated database."""
    from acme_ap.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "database_path", temp_db)
    with TestClient(app) as c:
        yield c


def test_health_reports_the_provider(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["provider"] in {"xai", "stub"}
    assert body["high_value_threshold"] == 10_000.0


def test_invoices_are_listed(client: TestClient) -> None:
    files = client.get("/api/invoices").json()
    assert len(files) >= 17
    assert {f["format"] for f in files} >= {"txt", "json", "csv", "xml", "pdf"}


def test_inventory_matches_the_seed(client: TestClient) -> None:
    rows = {r["item"]: r["stock"] for r in client.get("/api/inventory").json()}
    assert rows == {"WidgetA": 15, "WidgetB": 10, "GadgetX": 5, "FakeItem": 0}


def test_dashboard_is_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "Acme AP" in page.text


# --------------------------------------------------------------------------- #
# Path handling
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../../../../etc/passwd", "/home/someone/.ssh/id_rsa"],
)
def test_paths_outside_the_invoice_directory_are_refused(client: TestClient, path: str) -> None:
    """The API takes a path from the network and must never read arbitrary files."""
    response = client.post("/api/runs", json={"invoice_path": path})
    assert response.status_code in {400, 404}


def test_unknown_invoice_is_a_404(client: TestClient) -> None:
    response = client.post("/api/runs", json={"invoice_path": "nope.txt"})
    assert response.status_code == 404


def test_unknown_run_is_a_404(client: TestClient) -> None:
    assert client.get("/api/runs/deadbeef").status_code == 404


# --------------------------------------------------------------------------- #
# A full run over HTTP
# --------------------------------------------------------------------------- #


def _await_run(client: TestClient, run_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/runs/{run_id}").json()
        if body["run"]["status"] != "RUNNING":
            return body
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} did not finish within {timeout}s")


def test_run_completes_and_is_queryable(client: TestClient) -> None:
    accepted = client.post("/api/runs", json={"invoice_path": "invoice_1001.txt"})
    run_id = accepted.json()["run_id"]
    body = _await_run(client, run_id)
    assert (
        accepted.status_code,
        body["run"]["status"],
        bool(body["decision"]["rationale"]),
        body["payment"]["status"],
        bool(body["events"]),
    ) == (202, "PAID", True, "success", True)


def test_rejected_run_records_its_reasoning(client: TestClient) -> None:
    run_id = client.post("/api/runs", json={"invoice_path": "invoice_1003.txt"}).json()["run_id"]
    body = _await_run(client, run_id)
    assert body["run"]["status"] == "REJECTED"
    assert {f["code"] for f in body["findings"]} >= {"ZERO_STOCK", "URGENCY_PRESSURE"}
    assert body["payment"] is None


def _stream_payloads(client: TestClient, run_id: str) -> list[dict]:
    with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
        payloads = []
        for line in stream.iter_lines():
            if line.startswith("data: "):
                payloads.append(json.loads(line[6:]))
            if line.startswith("event: done"):
                break
    return payloads


def test_event_stream_replays_a_finished_run(client: TestClient) -> None:
    """A client that connects late must still receive the whole trace."""
    run_id = client.post("/api/runs", json={"invoice_path": "invoice_1001.txt"}).json()["run_id"]
    _await_run(client, run_id)
    payloads = _stream_payloads(client, run_id)
    agents = {p.get("agent") for p in payloads if "agent" in p}
    assert (bool(payloads), {"loader", "ingestion", "validation"} <= agents) == (True, True)


def test_stats_reflect_completed_runs(client: TestClient) -> None:
    run_id = client.post("/api/runs", json={"invoice_path": "invoice_1001.txt"}).json()["run_id"]
    _await_run(client, run_id)
    stats = client.get("/api/stats").json()
    assert stats["runs"] >= 1
    assert stats["paid"] >= 1
    assert stats["paid_value"] >= 5000.0
