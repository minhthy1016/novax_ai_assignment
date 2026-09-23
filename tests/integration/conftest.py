from __future__ import annotations

import os
import time
from collections.abc import Iterator

import httpx
import pytest

API = os.environ.get("OPSASSIST_API_URL", "http://127.0.0.1:8000")


class WaitsOutRateLimits(httpx.HTTPTransport):
    """The suite is a burst from a handful of identities - exactly what the rate limiter
    (D-61) is there to slow down. Rather than exempting tests, the shared client waits out
    `Retry-After` like any well-behaved batch client. The limiter's own test builds a raw
    client so it can still observe the 429.
    """

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        for _ in range(10):
            response = super().handle_request(request)
            if response.status_code != 429:
                return response
            response.read()
            response.close()
            time.sleep(min(float(response.headers.get("retry-after", "1")), 30) + 0.1)
        return response


@pytest.fixture(scope="session")
def api() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=API, timeout=30, transport=WaitsOutRateLimits()) as client:
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
