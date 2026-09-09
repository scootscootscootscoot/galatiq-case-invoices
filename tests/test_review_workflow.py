"""HTTP upload → confidence hold → durable alert → correction → revalidation."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from acme_ap.api.app import app
from acme_ap.config import get_settings
from acme_ap.db.repository import Repository
from tests.test_reliability import invoice_data


@pytest.fixture
def client(temp_db, tmp_path, monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "database_path", temp_db)
    monkeypatch.setattr(cfg, "invoice_dir", tmp_path / "invoices")
    with TestClient(app) as client:
        yield client


def upload(client, data=None, *, filename="invoice.json", content=None):
    data = data or invoice_data()
    if content is None:
        content = json.dumps(data).encode()
    response = client.post("/api/uploads", files={"file": (filename, content)})
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    for _ in range(100):
        result = client.get(f"/api/runs/{run_id}").json()
        if result["run"]["status"] != "RUNNING":
            return result
        time.sleep(0.05)
    raise AssertionError("Upload did not finish")


def held_invoice(client):
    data = invoice_data()
    del data["currency"]
    result = upload(client, data)
    assert result["run"]["status"] == "REVIEW_REQUIRED"
    return result


def correction(result):
    return {
        "action": "correct",
        "reviewer": "Demo Reviewer",
        "note": "Verified currency and all values against the original source.",
        "source_verified": True,
        "invoice": result["extraction"]["invoice"],
    }


def test_uploaded_uncertainty_is_durable_and_correctable(client, temp_db):
    original = held_invoice(client)
    run_id = original["run"]["id"]
    assert client.get("/api/stats").json()["open_alerts"] == 1
    with Repository(temp_db) as reopened:
        assert reopened.list_reviews()[0]["run_id"] == run_id
    response = client.post(f"/api/reviews/{run_id}/resolve", json=correction(original))
    assert response.status_code == 200, response.text
    child_id = response.json()["resolution_run_id"]
    child = client.get(f"/api/runs/{child_id}").json()
    assert child["run"]["status"] == "PAID"
    assert child["extraction"]["quality"]["human_verified"] is True
    assert child["extraction"]["quality"]["score"] == 0.85  # Never inflate the machine score.
    assert any(event["kind"] == "human_correction" for event in child["events"])
    after = client.get(f"/api/runs/{run_id}").json()
    assert after["run"]["status"] == "REVIEW_REQUIRED"
    assert after["extraction"] == original["extraction"]
    assert after["review"]["resolution_run_id"] == child_id
    assert client.get("/api/stats").json()["open_alerts"] == 0
    assert (
        client.post(f"/api/reviews/{run_id}/resolve", json=correction(original)).status_code == 409
    )


def test_correction_does_not_bypass_business_rules(client):
    original = held_invoice(client)
    body = correction(original)
    body["invoice"]["line_items"][0].update(quantity=99, amount=24750)
    body["invoice"].update(subtotal=24750, total=24750)
    response = client.post(f"/api/reviews/{original['run']['id']}/resolve", json=body)
    child = client.get(f"/api/runs/{response.json()['resolution_run_id']}").json()
    assert child["run"]["status"] == "REJECTED"
    assert child["payment"] is None
    assert any(finding["code"] == "STOCK_EXCEEDED" for finding in child["findings"])


def test_review_requires_explicit_verification_and_audit_fields(client):
    original = held_invoice(client)
    body = correction(original)
    body["source_verified"] = False
    path = f"/api/reviews/{original['run']['id']}/resolve"
    assert client.post(path, json=body).status_code == 422
    body["source_verified"] = True
    body["reviewer"] = "  "
    assert client.post(path, json=body).status_code == 422
    assert client.get("/api/stats").json()["paid"] == 0


def test_two_reviewers_cannot_resolve_the_same_alert(client):
    original = held_invoice(client)
    path = f"/api/reviews/{original['run']['id']}/resolve"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(path, json=correction(original)), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert client.get("/api/stats").json()["paid"] == 1


def test_alert_dismissal_never_pays(client):
    original = held_invoice(client)
    response = client.post(
        f"/api/reviews/{original['run']['id']}/resolve",
        json={
            "action": "dismiss",
            "reviewer": "Reviewer",
            "note": "Vendor will send a replacement invoice.",
        },
    )
    assert response.json()["status"] == "DISMISSED"
    assert client.get("/api/stats").json()["paid"] == 0


def test_source_snapshot_is_immutable_if_file_changes(client):
    from pathlib import Path

    result = upload(client)
    source_url = f"/api/runs/{result['run']['id']}/source"
    assert client.get(source_url).status_code == 200
    Path(result["run"]["source_path"]).write_text("changed")
    assert client.get(source_url).status_code == 409
    stored = client.get(f"/api/runs/{result['run']['id']}").json()
    assert "Evidence Supply" in stored["extraction"]["document"]["text"]


def test_upload_limits_and_filename_isolation(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 10)
    assert client.post("/api/uploads", files={"file": ("a.pdf", b"x" * 11)}).status_code == 413
    assert client.post("/api/uploads", files={"file": ("a.exe", b"x")}).status_code == 415
    assert client.post("/api/uploads", files={"file": ("a.txt", b"")}).status_code == 400
    monkeypatch.setattr(get_settings(), "max_upload_bytes", 10000)
    result = upload(client, filename="../../escape.json")
    assert "/uploads/" in result["run"]["source_path"]
    assert "escape" not in result["run"]["source_path"]


def test_missing_event_stream_returns_404(client):
    assert client.get("/api/runs/no-such-run/events").status_code == 404


def test_json_duplicate_keys_are_visible_in_review(client):
    data = json.dumps(invoice_data())
    data = data[:-1] + ', "total": 500}'
    result = upload(client, content=data.encode())
    assert result["run"]["status"] == "REVIEW_REQUIRED"
    assert "Repeated JSON key: total" in result["review"]["reasons"]
