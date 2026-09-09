"""HTTP provider behavior without making network calls."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

import acme_ap.llm.xai as xai
from acme_ap.config import Settings
from acme_ap.llm.base import LLMError
from acme_ap.models import ApprovalProposal


class _FakeHTTP:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.closed = False

    def post(self, _path: str, **_kwargs: object) -> object:
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


def _response(status: int, body: object | None = None, text: str = "detail") -> object:
    return SimpleNamespace(
        status_code=status,
        request=httpx.Request("POST", "https://api.x.ai/v1/chat/completions"),
        text=text,
        json=lambda: body if body is not None else {},
    )


def _client(monkeypatch: pytest.MonkeyPatch, http: _FakeHTTP, retries: int = 3) -> xai.XAIClient:
    settings = Settings(xai_api_key="test", llm_max_retries=retries)
    monkeypatch.setattr(xai.httpx, "Client", lambda **_kwargs: http)
    return xai.XAIClient(settings)


def test_client_requires_key_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(LLMError, match="not set"):
        xai.XAIClient(Settings(xai_api_key=None))
    http = _FakeHTTP([])
    client = _client(monkeypatch, http)
    client.close()
    assert http.closed


def test_post_retries_then_returns_json(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHTTP([_response(500), _response(200, {"ok": True})])
    client = _client(monkeypatch, http)
    monkeypatch.setattr(xai.random, "uniform", lambda _low, _high: 0.0)
    monkeypatch.setattr(xai.time, "sleep", lambda _seconds: None)
    assert client._post({"model": "m"}, "test") == {"ok": True}


def test_post_exhausts_retryable_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _FakeHTTP([_response(503), _response(502)])
    client = _client(monkeypatch, http, retries=2)
    monkeypatch.setattr(xai.random, "uniform", lambda _low, _high: 0.0)
    monkeypatch.setattr(xai.time, "sleep", lambda _seconds: None)
    with pytest.raises(LLMError, match="after 2 attempts"):
        client._post({"model": "m"}, "test")


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("timed out"), httpx.ConnectError("offline")],
)
def test_post_retries_transport_errors(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    http = _FakeHTTP([error, _response(200, {"ok": True})])
    client = _client(monkeypatch, http)
    monkeypatch.setattr(xai.random, "uniform", lambda _low, _high: 0.0)
    monkeypatch.setattr(xai.time, "sleep", lambda _seconds: None)
    assert client._post({"model": "m"}, "transport") == {"ok": True}


@pytest.mark.parametrize("status", [400, 404])
def test_post_rejects_non_retryable_statuses(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    client = _client(monkeypatch, _FakeHTTP([_response(status)]), retries=1)
    with pytest.raises(LLMError) as raised:
        client._post({"model": "m"}, "status")
    if status == 404:
        assert "model 'grok-4' was rejected" in str(raised.value)
    else:
        assert "returned 400" in str(raised.value)


@pytest.mark.parametrize(
    "response",
    [
        {"choices": [{"message": {"content": "ok"}}]},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {"content": 3}}]},
    ],
)
def test_content_extracts_or_rejects_response_shapes(response: dict[str, object]) -> None:
    if response.get("choices") == [{"message": {"content": "ok"}}]:
        assert xai.XAIClient._content(response) == "ok"
    else:
        with pytest.raises(LLMError, match="unexpected response shape"):
            xai.XAIClient._content(response)


def test_complete_structured_accepts_plain_and_fenced_json(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _FakeHTTP([]))
    payloads = [
        {"choices": [{"message": {"content": '{"approve": true, "rationale": "ok"}'}}]},
        {
            "choices": [
                {"message": {"content": '```json\n{"approve": false, "rationale": "no"}\n```'}}
            ]
        },
    ]
    monkeypatch.setattr(client, "_post", lambda _payload, _purpose: payloads.pop(0))
    first = client.complete_structured(system="s", user="u", schema=ApprovalProposal, purpose="p")
    second = client.complete_structured(system="s", user="u", schema=ApprovalProposal, purpose="p")
    assert first.approve and not second.approve


def test_complete_structured_reports_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, _FakeHTTP([]))
    monkeypatch.setattr(
        client,
        "_post",
        lambda _payload, _purpose: {
            "choices": [{"message": {"content": json.dumps({"approve": "yes"})}}]
        },
    )
    with pytest.raises(LLMError, match="response did not match ApprovalProposal"):
        client.complete_structured(system="s", user="u", schema=ApprovalProposal, purpose="p")
