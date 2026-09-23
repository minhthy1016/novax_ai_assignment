"""HTTP client for the evaluation scripts.

An evaluation run is exactly the traffic the rate limiter exists to slow down: dozens of
model-backed calls from one identity, as fast as the machine allows. Rather than exempting
ourselves - which would mean the limiter is never exercised by anything but its own tests -
the evaluation client behaves like a well-mannered batch client and waits out `Retry-After`.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

MAX_WAITS = 10


def request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Send, honouring `Retry-After` on 429. Any other status is returned untouched."""
    for _ in range(MAX_WAITS):
        response = client.request(method, url, **kwargs)
        if response.status_code != 429:
            return response
        time.sleep(min(float(response.headers.get("retry-after", "1")), 30) + 0.1)
    raise RuntimeError(f"still rate limited after {MAX_WAITS} waits: {method} {url}")


def post(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    return request(client, "POST", url, **kwargs)


def get(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    return request(client, "GET", url, **kwargs)
