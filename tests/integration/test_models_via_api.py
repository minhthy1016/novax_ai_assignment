"""Every model in the catalog, called through the running API.

The contract: each model returns 200, or a *controlled* failure - 502/503/504 with the
standard error envelope and request ID. Never a 500, never an unhandled crash. Which one
you get depends on the environment (keys present, Ollama running), so both are accepted,
but the first attempt must always be the model that was asked for.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from opsassist.gateway.catalog import load_catalog

pytestmark = pytest.mark.integration

CATALOG = load_catalog(Path(__file__).resolve().parents[2] / "config" / "models.toml")
CHAT = [m.id for m in CATALOG.models if m.kind == "chat"]
EMBED = [m.id for m in CATALOG.models if m.kind == "embedding"]
CONTROLLED = {502, 503, 504}


def assert_controlled_or_ok(resp: httpx.Response, model_id: str) -> None:
    assert resp.status_code != 500, resp.text
    body = resp.json()
    if resp.status_code == 200:
        return
    assert resp.status_code in CONTROLLED, f"{model_id}: {resp.status_code} {resp.text}"
    assert body["error"]["request_id"] == resp.headers["x-request-id"]
    assert body["error"]["code"] in {
        "provider_unavailable",
        "no_available_provider",
        "deadline_exceeded",
        "bad_request",
    }
    assert body["attempts"] and body["attempts"][0]["model"] == model_id


# A question the engineering user's knowledge answers, so retrieval admits sources and the
# chosen model is actually called (small talk would abstain without any model call).
QUESTION = "When may we deploy to production?"


@pytest.mark.parametrize("model_id", CHAT)
def test_chat_model_via_api(api: httpx.Client, u001: dict[str, str], model_id: str) -> None:
    resp = api.post(
        "/api/chat",
        json={"message": QUESTION, "model": model_id, "max_tokens": 200},
        headers=u001,
        timeout=180,
    )
    assert_controlled_or_ok(resp, model_id)
    if resp.status_code == 200:
        body = resp.json()
        assert body["attempts"][0]["model"] == model_id
        assert body["fallback_used"] == (body["model"]["id"] != model_id)
        assert body["usage"]["total_tokens"] > 0


@pytest.mark.parametrize("model_id", EMBED)
def test_embedding_model_via_api(api: httpx.Client, u001: dict[str, str], model_id: str) -> None:
    resp = api.post(
        "/api/embeddings",
        json={"input": ["deployment window"], "model": model_id},
        headers=u001,
        timeout=120,
    )
    assert_controlled_or_ok(resp, model_id)
    if resp.status_code == 200:
        assert resp.json()["dimensions"] == CATALOG.model(model_id).dimensions
