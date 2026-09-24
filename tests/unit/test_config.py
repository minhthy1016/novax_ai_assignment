import pytest
from pydantic import ValidationError

from opsassist.config import DEV_JWT_SECRET, MIN_JWT_SECRET_BYTES, Settings


def test_dev_defaults_are_accepted() -> None:
    assert Settings(env="dev").jwt_secret.get_secret_value() == DEV_JWT_SECRET


@pytest.mark.parametrize("env", ["staging", "prod"])
@pytest.mark.security
def test_non_dev_env_rejects_dev_jwt_secret(env: str) -> None:
    with pytest.raises(ValidationError, match="OPSASSIST_JWT_SECRET"):
        Settings(env=env)


@pytest.mark.parametrize("env", ["staging", "prod"])
@pytest.mark.security
def test_non_dev_env_rejects_a_short_jwt_secret(env: str) -> None:
    ok = {"app_db_password": "x" * 40}
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(env=env, jwt_secret="s" * (MIN_JWT_SECRET_BYTES - 1), **ok)
    Settings(env=env, jwt_secret="s" * MIN_JWT_SECRET_BYTES, **ok)  # the minimum is accepted


@pytest.mark.security
def test_secrets_are_not_rendered_in_repr() -> None:
    settings = Settings(database_url="postgresql+psycopg://u:hunter2@db/x")
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings.model_dump())
