import time
from collections.abc import Iterator

import jwt
import pytest
from fastapi.testclient import TestClient

from opsassist.auth import ALGORITHM, AUDIENCE, ISSUER, decode_token, issue_token
from opsassist.config import Settings
from opsassist.main import create_app

SETTINGS = Settings(env="test", jwt_secret="unit-test-secret-0123456789abcdef")

# Authorization, tool policy, audit integrity and data-egress rules.
pytestmark = pytest.mark.security


def test_round_trip_binds_user_and_role() -> None:
    claims = decode_token(SETTINGS, issue_token(SETTINGS, "U001", "Senior Engineer"))
    assert (claims.user_id, claims.role) == ("U001", "Senior Engineer")


def test_token_without_role_claim_is_rejected() -> None:
    now = int(time.time())
    token = jwt.encode(
        {"sub": "U001", "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 60},
        SETTINGS.jwt_secret.get_secret_value(),
        algorithm=ALGORITHM,
    )
    with pytest.raises(jwt.MissingRequiredClaimError):
        decode_token(SETTINGS, token)


def test_expired_token_is_rejected() -> None:
    old = issue_token(SETTINGS, "U001", "r", now=time.time() - SETTINGS.jwt_ttl_seconds - 10)
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(SETTINGS, old)


def test_token_signed_with_another_secret_is_rejected() -> None:
    other = Settings(env="test", jwt_secret="another-secret-0123456789abcdef-0123")
    with pytest.raises(jwt.InvalidSignatureError):
        decode_token(SETTINGS, issue_token(other, "U001", "r"))


def test_unsigned_alg_none_token_is_rejected() -> None:
    now = int(time.time())
    forged = jwt.encode(
        {
            "sub": "U004",
            "role": "HR Manager",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + 60,
        },
        key=None,
        algorithm="none",
    )
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(SETTINGS, forged)


def test_wrong_audience_is_rejected() -> None:
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "U001",
            "role": "r",
            "iss": ISSUER,
            "aud": "other-service",
            "iat": now,
            "exp": now + 60,
        },
        SETTINGS.jwt_secret.get_secret_value(),
        algorithm=ALGORITHM,
    )
    with pytest.raises(jwt.InvalidAudienceError):
        decode_token(SETTINGS, token)


# ------------------------------------------------------------------ route coverage

# Only these may be reached without a token: probes, metrics scrape, and the dev-only issuer.
PUBLIC = {"/healthz", "/readyz", "/metrics", "/api/auth/dev-token"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        env="test",
        database_url="postgresql+psycopg://nobody:nothing@127.0.0.1:1/none",
        redis_url="redis://127.0.0.1:1/0",
    )
    with TestClient(create_app(settings)) as c:
        yield c


def test_every_api_route_requires_a_token(client: TestClient) -> None:
    """Nothing under /api can start without passing the JWT check - enumerated from the app
    itself so a newly added route cannot silently skip authentication."""
    checked = 0
    schema = client.app.openapi()  # type: ignore[attr-defined]
    for path, operations in schema["paths"].items():
        if not path.startswith("/api") or path in PUBLIC:
            continue
        url = path.replace("{conversation_id}", "00000000-0000-0000-0000-000000000000")
        for method in operations:
            resp = client.request(method.upper(), url, json={})
            assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"
            checked += 1
    assert checked >= 6


def test_garbage_token_is_rejected(client: TestClient) -> None:
    resp = client.get("/api/models", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
