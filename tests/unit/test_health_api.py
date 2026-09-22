"""Health endpoints against deliberately unreachable dependencies (no services needed)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from opsassist.config import Settings
from opsassist.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        env="test",
        database_url="postgresql+psycopg://nobody:nothing@127.0.0.1:1/none",
        redis_url="redis://127.0.0.1:1/0",
        dependency_check_timeout_s=1.0,
    )
    with TestClient(create_app(settings)) as c:
        yield c


def test_liveness_does_not_depend_on_backends(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


def test_readiness_reports_each_failed_dependency(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not_ready"
    assert set(body["checks"]) == {"postgres", "redis"}
    assert not body["checks"]["postgres"]["ok"]
    assert not body["checks"]["redis"]["ok"]
    # Failure detail must not leak connection strings or credentials.
    assert "nothing" not in resp.text


def test_request_id_is_echoed_or_minted(client: TestClient) -> None:
    echoed = client.get("/healthz", headers={"X-Request-ID": "trace-12345678"})
    assert echoed.headers["x-request-id"] == "trace-12345678"
    minted = client.get("/healthz", headers={"X-Request-ID": "bad id\n"})
    assert minted.headers["x-request-id"] != "bad id\n"


def test_metrics_use_route_templates(client: TestClient) -> None:
    client.get("/healthz")
    client.get("/does-not-exist/123")
    text = client.get("/metrics").text
    assert 'route="/healthz"' in text
    assert 'route="unmatched"' in text
    assert "/does-not-exist/123" not in text
