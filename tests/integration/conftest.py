from __future__ import annotations

import os
import time
from collections.abc import Iterator

import httpx
import psycopg
import pytest

API = os.environ.get("OPSASSIST_API_URL", "http://127.0.0.1:8000")
APP_DB = os.environ.get(
    "OPSASSIST_DATABASE_URL",
    "postgresql+psycopg://opsassist_app:opsassist_app_dev@localhost:5432/opsassist",
).replace("postgresql+psycopg://", "postgresql://")
FINISHED = ("succeeded", "unchanged", "failed", "dead")


def wait_for_job(job_id: str, timeout: float = 180) -> tuple[str, int, str | None]:
    """Wait for one ingestion job to reach a final state and return (status, attempts,
    detail).

    Tests used to poll search results for a fixed 45 s. On a busy CI runner the upload's job
    can sit behind other queued jobs, or a transient retry with backoff, for longer than
    that, and the test then failed without saying why. Waiting on the job itself separates
    "not finished yet" from "failed", and a failure reports the worker's own detail.
    """
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        with psycopg.connect(APP_DB) as conn:
            row = conn.execute(
                "SELECT status, attempts, detail FROM ingestion_jobs WHERE id = %s", (job_id,)
            ).fetchone()
        if row and row[0] in FINISHED:
            return str(row[0]), int(row[1]), row[2]
        time.sleep(0.5)
    raise AssertionError(f"ingestion job {job_id} not finished after {timeout:.0f}s: {row}")


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
