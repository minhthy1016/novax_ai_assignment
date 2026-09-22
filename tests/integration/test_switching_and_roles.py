"""Model switching within a conversation, and role-bound tokens."""

from __future__ import annotations

import os

import httpx
import psycopg
import pytest

from tests.integration.conftest import token_for

pytestmark = pytest.mark.integration

# Editing users is an administrative (owner) operation; the runtime role cannot do it.
DB = os.environ.get(
    "OPSASSIST_MIGRATION_DATABASE_URL",
    "postgresql+psycopg://opsassist:opsassist@localhost:5432/opsassist",
).replace("postgresql+psycopg://", "postgresql://")


def chat(api: httpx.Client, auth: dict[str, str], message: str, **extra: object) -> dict:
    resp = api.post("/api/chat", json={"message": message, **extra}, headers=auth)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_switching_models_keeps_the_conversation(api: httpx.Client, u001: dict[str, str]) -> None:
    first = chat(api, u001, "When may we deploy to production?", model="mock/echo")
    conv = first["conversation_id"]

    # Same question after the switch: same retrieved sources, so the only difference in the
    # prompt is the carried-over history - proof the new model received the conversation.
    second = chat(
        api,
        u001,
        "When may we deploy to production?",
        model="mock/echo-alt",
        conversation_id=conv,
    )
    assert second["model"]["id"] == "mock/echo-alt"
    assert second["usage"]["prompt_tokens"] > first["usage"]["prompt_tokens"]

    # No model given: the conversation keeps its current one.
    third = chat(api, u001, "What is the production deployment procedure?", conversation_id=conv)
    assert third["model"]["id"] == "mock/echo-alt"

    patched = api.patch(f"/api/conversations/{conv}", json={"model": "mock/echo"}, headers=u001)
    assert patched.status_code == 200 and patched.json()["model"] == "mock/echo"
    fourth = chat(api, u001, "What caused the payment incident?", conversation_id=conv)
    assert fourth["model"]["id"] == "mock/echo"

    history = api.get(f"/api/conversations/{conv}", headers=u001).json()
    assert [m["model"] for m in history["messages"] if m["role"] == "assistant"] == [
        "mock/echo",
        "mock/echo-alt",
        "mock/echo-alt",
        "mock/echo",
    ]


def test_switching_to_unknown_model_is_rejected(api: httpx.Client, u001: dict[str, str]) -> None:
    conv = chat(api, u001, "hello", model="mock/echo")["conversation_id"]
    resp = api.patch(f"/api/conversations/{conv}", json={"model": "gpt-9"}, headers=u001)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unknown_model"


def test_models_endpoint_exposes_the_three_model_picker(
    api: httpx.Client, u001: dict[str, str]
) -> None:
    body = api.get("/api/models", headers=u001).json()
    assert body["selectable"] == ["nim/gpt-oss-20b", "claude/sonnet-4.5", "ollama/llama3.2-3b"]
    flags = {m["id"]: m["selectable"] for m in body["models"]}
    assert sum(flags.values()) == 3


def test_token_is_rejected_after_role_change(api: httpx.Client) -> None:
    token = token_for(api, "U002")
    auth = {"Authorization": f"Bearer {token}"}
    assert api.get("/api/models", headers=auth).status_code == 200
    with psycopg.connect(DB, autocommit=True) as conn:
        original = conn.execute("SELECT role FROM users WHERE id = 'U002'").fetchone()[0]
        conn.execute("UPDATE users SET role = 'Senior Engineer' WHERE id = 'U002'")
        try:
            resp = api.get("/api/models", headers=auth)
            assert resp.status_code == 401
            assert "role changed" in resp.json()["error"]["message"]
            # A fresh sign-in picks up the new role and works again.
            fresh = {"Authorization": f"Bearer {token_for(api, 'U002')}"}
            assert api.get("/api/models", headers=fresh).status_code == 200
        finally:
            conn.execute("UPDATE users SET role = %s WHERE id = 'U002'", (original,))


def test_token_is_rejected_after_deactivation(api: httpx.Client) -> None:
    auth = {"Authorization": f"Bearer {token_for(api, 'U006')}"}
    with psycopg.connect(DB, autocommit=True) as conn:
        conn.execute("UPDATE users SET is_active = false WHERE id = 'U006'")
        try:
            assert api.get("/api/models", headers=auth).status_code == 401
        finally:
            conn.execute("UPDATE users SET is_active = true WHERE id = 'U006'")
