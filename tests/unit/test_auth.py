import time

import jwt
import pytest

from opsassist.auth import ALGORITHM, AUDIENCE, ISSUER, decode_token, issue_token
from opsassist.config import Settings

SETTINGS = Settings(env="test", jwt_secret="unit-test-secret-0123456789abcdef")


def test_round_trip() -> None:
    assert decode_token(SETTINGS, issue_token(SETTINGS, "U001")) == "U001"


def test_expired_token_is_rejected() -> None:
    old = issue_token(SETTINGS, "U001", now=time.time() - SETTINGS.jwt_ttl_seconds - 10)
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(SETTINGS, old)


def test_token_signed_with_another_secret_is_rejected() -> None:
    other = Settings(env="test", jwt_secret="another-secret-0123456789abcdef")
    with pytest.raises(jwt.InvalidSignatureError):
        decode_token(SETTINGS, issue_token(other, "U001"))


def test_unsigned_alg_none_token_is_rejected() -> None:
    now = int(time.time())
    forged = jwt.encode(
        {"sub": "U004", "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 60},
        key=None,
        algorithm="none",
    )
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(SETTINGS, forged)


def test_wrong_audience_is_rejected() -> None:
    now = int(time.time())
    token = jwt.encode(
        {"sub": "U001", "iss": ISSUER, "aud": "other-service", "iat": now, "exp": now + 60},
        SETTINGS.jwt_secret.get_secret_value(),
        algorithm=ALGORITHM,
    )
    with pytest.raises(jwt.InvalidAudienceError):
        decode_token(SETTINGS, token)
