"""Application settings.

All configuration comes from environment variables (prefix ``OPSASSIST_``) or a local
``.env`` file. Secrets are typed as ``SecretStr`` so they never appear in ``repr()``,
logs, or error pages. Defaults are safe for local development only; ``validate_for_env``
refuses to start a non-dev environment that still uses a development default.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me"  # noqa: S105 - sentinel, rejected outside dev


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPSASSIST_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["dev", "test", "staging", "prod"] = "dev"
    service_name: str = "opsassist-api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://opsassist:opsassist@localhost:5432/opsassist"
    )
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")

    jwt_secret: SecretStr = SecretStr(DEV_JWT_SECRET)
    jwt_ttl_seconds: int = Field(default=3600, ge=60, le=86_400)

    # Readiness probes must answer quickly even when a dependency hangs.
    dependency_check_timeout_s: float = Field(default=2.0, gt=0, le=10)

    # Model gateway. Provider endpoints, models and fallback routes live in the catalog file.
    models_config: Path = Path("config/models.toml")
    # None = enabled only in dev/test. The mock provider must never serve production traffic.
    enable_mock_provider: bool | None = None
    request_deadline_s: float = Field(default=120.0, gt=0, le=600)
    stream_deadline_s: float = Field(default=300.0, gt=0, le=1800)
    stream_idle_timeout_s: float = Field(default=30.0, gt=0, le=300)

    # Conversation window sent to the model (Task 4 refines this with summaries).
    history_max_messages: int = Field(default=20, ge=0, le=200)
    history_token_budget: int = Field(default=3000, ge=0, le=100_000)

    @property
    def mock_provider_enabled(self) -> bool:
        if self.enable_mock_provider is None:
            return self.env in ("dev", "test")
        return self.enable_mock_provider

    @model_validator(mode="after")
    def validate_for_env(self) -> Settings:
        if self.env in ("staging", "prod") and self.jwt_secret.get_secret_value() == DEV_JWT_SECRET:
            raise ValueError("OPSASSIST_JWT_SECRET must be set outside dev/test environments")
        if self.env == "prod" and self.enable_mock_provider:
            raise ValueError("the mock provider cannot be enabled in prod")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
