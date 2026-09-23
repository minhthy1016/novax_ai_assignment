"""Task 1 end to end through the running API, using the deterministic mock provider."""

from __future__ import annotations

import json

import httpx
import pytest

pytestmark = pytest.mark.integration

# Since D3 every message is grounded: small talk ("hi") retrieves nothing and abstains
# without calling a model. Gateway behaviour is therefore exercised with a question the
# engineering user's knowledge answers, so a model call actually happens.
Q = "When may we deploy to production?"


def parse_sse(text: str) -> list[tuple[str, dict[str, object]]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_api_requires_authentication(api: httpx.Client) -> None:
    resp = api.post("/api/chat", json={"message": "hi", "model": "mock/echo"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
    assert resp.headers["www-authenticate"] == "Bearer"


def test_chat_returns_answer_usage_and_persists_conversation(
    api: httpx.Client, u001: dict[str, str]
) -> None:
    resp = api.post(
        "/api/chat",
        json={"message": Q, "model": "chat-mock"},
        headers={**u001, "X-Request-ID": "it-chat-00000001"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["request_id"] == "it-chat-00000001"
    assert body["model"] == {"id": "mock/echo", "provider": "mock"}
    assert body["usage"]["total_tokens"] > 0
    assert body["fallback_used"] is False

    conv = api.get(f"/api/conversations/{body['conversation_id']}", headers=u001).json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"]
    assert conv["messages"][1]["content"] == body["content"]
    # Every model call is accounted for: since D4 a turn costs one routing call plus the
    # answer, and retrieval costs one query embedding.
    assert (conv["usage"]["chat_calls"], conv["usage"]["embedding_calls"]) == (2, 1)

    follow_up = api.post(
        "/api/chat",
        json={
            "message": "and on Thursday?",
            "model": "chat-mock",
            "conversation_id": body["conversation_id"],
        },
        headers=u001,
    )
    assert follow_up.status_code == 200
    conv = api.get(f"/api/conversations/{body['conversation_id']}", headers=u001).json()
    assert len(conv["messages"]) == 4


def test_conversation_of_another_user_is_not_found(
    api: httpx.Client, u001: dict[str, str], u003: dict[str, str]
) -> None:
    conv_id = api.post(
        "/api/chat", json={"message": "private", "model": "chat-mock"}, headers=u001
    ).json()["conversation_id"]
    assert api.get(f"/api/conversations/{conv_id}", headers=u003).status_code == 404
    # ...and U003 cannot append to it either.
    resp = api.post(
        "/api/chat",
        json={"message": "sneak", "model": "chat-mock", "conversation_id": conv_id},
        headers=u003,
    )
    assert resp.status_code == 404


def test_provider_failure_falls_back_and_reports_attempts(
    api: httpx.Client, u001: dict[str, str]
) -> None:
    resp = api.post("/api/chat", json={"message": Q, "model": "demo-failover"}, headers=u001)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fallback_used"] is True
    assert body["model"]["id"] == "mock/echo"
    first = body["attempts"][0]
    assert first["model"] == "mock/down"
    assert first["outcome"] in ("error", "skipped:circuit_open")


def test_total_provider_failure_is_a_controlled_503(
    api: httpx.Client, u001: dict[str, str]
) -> None:
    resp = api.post("/api/chat", json={"message": Q, "model": "mock/down"}, headers=u001)
    assert resp.status_code == 503
    err = resp.json()
    # First call exhausts retries; later calls find the breaker open and skip the model.
    assert err["error"]["code"] in ("provider_unavailable", "no_available_provider")
    assert err["error"]["request_id"] == resp.headers["x-request-id"]
    assert all("detail" not in a for a in err["attempts"])  # no raw provider error text


def test_unknown_model_is_rejected_before_anything_is_stored(
    api: httpx.Client, u001: dict[str, str]
) -> None:
    resp = api.post("/api/chat", json={"message": "hi", "model": "gpt-9"}, headers=u001)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_model"


def test_validation_errors_do_not_echo_input(api: httpx.Client, u001: dict[str, str]) -> None:
    resp = api.post(
        "/api/chat",
        json={"message": "hi", "model": "chat-mock", "api_key": "sk-should-not-echo"},
        headers=u001,
    )
    assert resp.status_code == 422
    assert "sk-should-not-echo" not in resp.text


def test_stream_event_sequence_and_persistence(api: httpx.Client, u001: dict[str, str]) -> None:
    resp = api.post("/api/chat/stream", json={"message": Q, "model": "chat-mock"}, headers=u001)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    names = [e for e, _ in events]
    assert names[0] == "meta" and names[1] == "model" and names[-1] == "done"
    assert set(names[2:-1]) == {"delta"}
    streamed = "".join(str(d["text"]) for e, d in events if e == "delta")
    conv_id = events[0][1]["conversation_id"]
    conv = api.get(f"/api/conversations/{conv_id}", headers=u001).json()
    assert conv["messages"][-1]["content"] == streamed
    assert conv["messages"][-1]["status"] == "complete"


def test_stream_total_failure_emits_error_event(api: httpx.Client, u001: dict[str, str]) -> None:
    resp = api.post("/api/chat/stream", json={"message": Q, "model": "mock/down"}, headers=u001)
    events = parse_sse(resp.text)
    assert events[-1][0] == "error"
    assert events[-1][1]["partial"] is False


def test_embeddings(api: httpx.Client, u001: dict[str, str]) -> None:
    resp = api.post(
        "/api/embeddings",
        json={"input": ["deploy window", "leave policy"], "model": "embed-mock"},
        headers=u001,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dimensions"] == 256
    assert [d["index"] for d in body["data"]] == [0, 1]


def test_models_lists_catalog_with_availability(api: httpx.Client, u001: dict[str, str]) -> None:
    body = api.get("/api/models", headers=u001).json()
    ids = {m["id"]: m for m in body["models"]}
    assert ids["mock/echo"]["available"] is True
    assert ids["nim/gpt-oss-20b"]["data_egress"] is True
    assert body["defaults"]["chat"] == "chat-default"
