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
DEV_APP_DB_PASSWORD = "opsassist_app_dev"  # noqa: S105 - sentinel, rejected outside dev


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

    # Runtime connections (API, worker, ingestion) use a least-privilege role that is NOT a
    # superuser and cannot bypass row-level security. Superusers bypass RLS even with
    # FORCE ROW LEVEL SECURITY, which would silently disable the storage-layer isolation.
    database_url: SecretStr = SecretStr(
        f"postgresql+psycopg://opsassist_app:{DEV_APP_DB_PASSWORD}@localhost:5432/opsassist"
    )
    # Schema owner: migrations and seeding only.
    migration_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://opsassist:opsassist@localhost:5432/opsassist"
    )
    # Password the migration assigns to the runtime role.
    app_db_password: SecretStr = SecretStr(DEV_APP_DB_PASSWORD)
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

    # Highest classification that may be sent to a provider outside our boundary (NIM,
    # Claude, ...). Confirmed with the team lead: confidential documents must never reach an
    # external model. Set to "public" to keep internal documents on-box as well.
    egress_max_classification: Literal["public", "internal"] = "internal"

    # Knowledge index. The embedding model must match the index dimensions (768) and must
    # not send data off-box (confidential documents are embedded with it).
    index_embedding_model: str = "ollama/nomic-embed-text"
    knowledge_root: Path = Path("sample_data/knowledge")
    # Parent-child chunking (measured in evaluation/reports/chunking.md; see D-20).
    chunk_target_tokens: int = Field(default=64, ge=16, le=512)
    chunk_max_tokens: int = Field(default=256, ge=32, le=2048)
    retrieval_top_k: int = Field(default=4, ge=1, le=20)
    retrieval_candidates: int = Field(default=20, ge=1, le=200)
    # Admit only hits within this similarity margin of the best hit (drops the weak tail).
    retrieval_relative_margin: float = Field(default=0.10, ge=0.0, le=1.0)

    # Routing is a small, frequent classification: it runs on a fast local model by default,
    # independent of the model the user picked for answers (a slow hosted model would make
    # every turn wait, and on timeout the assistant would silently degrade to knowledge-only).
    router_model: str | None = "ollama/llama3.2-3b"

    # Extra preference keys the deployment allows in persistent memory, comma-separated.
    # The built-in allowlist stays: a model must not decide what is worth remembering.
    memory_extra_keys: str = ""

    # Rate limiting (D-61). Two buckets: everything, and the routes that cost a model
    # call or an ingestion job. Limits are per caller (token subject, else client address).
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = Field(default=300, ge=1, le=10_000)
    rate_limit_burst: int = Field(default=100, ge=1, le=10_000)
    rate_limit_expensive_per_minute: int = Field(default=60, ge=1, le=10_000)
    rate_limit_expensive_burst: int = Field(default=30, ge=1, le=10_000)

    # Conversation window sent to the model (Task 4 refines this with summaries).
    history_max_messages: int = Field(default=20, ge=0, le=200)
    history_token_budget: int = Field(default=3000, ge=0, le=100_000)

    def allows_egress(self, classification: str | None) -> bool:
        """May context of this classification leave our boundary?"""
        rank = {"public": 0, "internal": 1, "confidential": 2}
        if classification is None:
            return True
        return rank.get(classification, 2) <= rank[self.egress_max_classification]

    @property
    def allow_any_model(self) -> bool:
        """Outside dev/test, clients may only pick from the catalog's ``selectable`` list."""
        return self.env in ("dev", "test")

    @property
    def mock_provider_enabled(self) -> bool:
        if self.enable_mock_provider is None:
            return self.env in ("dev", "test")
        return self.enable_mock_provider

    @model_validator(mode="after")
    def validate_for_env(self) -> Settings:
        if self.env in ("staging", "prod") and self.jwt_secret.get_secret_value() == DEV_JWT_SECRET:
            raise ValueError("OPSASSIST_JWT_SECRET must be set outside dev/test environments")
        if (
            self.env in ("staging", "prod")
            and self.app_db_password.get_secret_value() == DEV_APP_DB_PASSWORD
        ):
            raise ValueError("OPSASSIST_APP_DB_PASSWORD must be set outside dev/test environments")
        if self.env == "prod" and self.enable_mock_provider:
            raise ValueError("the mock provider cannot be enabled in prod")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
