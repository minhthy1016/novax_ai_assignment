from __future__ import annotations

import os
from collections.abc import Iterator

import httpx
import pytest

API = os.environ.get("OPSASSIST_API_URL", "http://127.0.0.1:8000")


@pytest.fixture(scope="session")
def api() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=API, timeout=30) as client:
        yield client


def token_for(client: httpx.Client, user_id: str) -> str:
    resp = client.post("/api/auth/dev-token", json={"user_id": user_id})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["access_token"])


@pytest.fixture(scope="session")
def u001(api: httpx.Client) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(api, 'U001')}"}


@pytest.fixture(scope="session")
def u003(api: httpx.Client) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(api, 'U003')}"}
