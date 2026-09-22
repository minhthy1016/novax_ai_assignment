"""Model catalog: which models exist, which provider serves them, and how routes fall back.

Loaded from ``config/models.toml`` and validated up front, so a typo in a route or a
cross-kind route (chat model in an embedding chain) fails at startup instead of at the
first request.
"""

from __future__ import annotations

import os
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator

ModelKind = Literal["chat", "embedding"]
ProviderKind = Literal["openai_compatible", "ollama", "mock"]


class ProviderConfig(BaseModel):
    kind: ProviderKind
    base_url: str | None = None
    base_url_env: str | None = None
    api_key_env: str | None = None
    send_input_type: bool = False
    timeout_s: float = Field(default=30.0, gt=0, le=600)
    max_attempts: int = Field(default=2, ge=1, le=5)
    data_egress: bool = False

    def resolved_base_url(self) -> str | None:
        if self.base_url_env and os.environ.get(self.base_url_env):
            return os.environ[self.base_url_env]
        return self.base_url

    def api_key(self) -> SecretStr | None:
        if not self.api_key_env:
            return None
        value = os.environ.get(self.api_key_env)
        return SecretStr(value) if value else None


class ModelSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_-]+/[A-Za-z0-9._:-]+$")
    provider: str
    provider_model: str
    kind: ModelKind
    context_window: int | None = None
    dimensions: int | None = None
    input_usd_per_mtok: Decimal = Decimal(0)
    output_usd_per_mtok: Decimal = Decimal(0)

    def estimate_cost_usd(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        million = Decimal(1_000_000)
        return (
            self.input_usd_per_mtok * prompt_tokens + self.output_usd_per_mtok * completion_tokens
        ) / million


class Defaults(BaseModel):
    chat: str
    embedding: str


class Catalog(BaseModel):
    providers: dict[str, ProviderConfig]
    models: list[ModelSpec]
    routes: dict[str, list[str]]
    defaults: Defaults

    @model_validator(mode="after")
    def _validate_references(self) -> Catalog:
        by_id = {m.id: m for m in self.models}
        if len(by_id) != len(self.models):
            raise ValueError("duplicate model ids in catalog")
        for m in self.models:
            if m.provider not in self.providers:
                raise ValueError(f"model {m.id} references unknown provider {m.provider!r}")
            if m.kind == "embedding" and not m.dimensions:
                raise ValueError(f"embedding model {m.id} must declare dimensions")
        for name, targets in self.routes.items():
            if name in by_id:
                raise ValueError(f"route {name!r} collides with a model id")
            if not targets:
                raise ValueError(f"route {name!r} is empty")
            missing = [t for t in targets if t not in by_id]
            if missing:
                raise ValueError(f"route {name!r} references unknown models {missing}")
            kinds = {by_id[t].kind for t in targets}
            if len(kinds) != 1:
                raise ValueError(f"route {name!r} mixes chat and embedding models")
            if kinds == {"embedding"} and len(targets) != 1:
                raise ValueError(
                    f"embedding route {name!r} must have exactly one model: "
                    "vectors from different models are not comparable"
                )
        for kind, route in (("chat", self.defaults.chat), ("embedding", self.defaults.embedding)):
            if route not in self.routes:
                raise ValueError(f"default {kind} route {route!r} is not defined")
            if by_id[self.routes[route][0]].kind != kind:
                raise ValueError(f"default {kind} route {route!r} has the wrong kind")
        return self

    def model(self, model_id: str) -> ModelSpec:
        for m in self.models:
            if m.id == model_id:
                return m
        raise KeyError(model_id)

    def resolve(self, name: str | None, kind: ModelKind) -> tuple[str, list[ModelSpec]]:
        """Resolve a route name or a model id to an ordered list of targets.

        A bare model id resolves to a single-target chain (no fallback): the caller asked
        for that model specifically.
        """
        route = name or (self.defaults.chat if kind == "chat" else self.defaults.embedding)
        if route in self.routes:
            targets = [self.model(t) for t in self.routes[route]]
        else:
            try:
                targets = [self.model(route)]
            except KeyError:
                raise UnknownModelError(route) from None
        if targets[0].kind != kind:
            raise UnknownModelError(route, f"{route!r} is not a {kind} model or route")
        return route, targets

    def without_provider(self, provider: str) -> Catalog:
        """Drop a provider and everything that depends on it (e.g. mock outside dev/test)."""
        models = [m for m in self.models if m.provider != provider]
        kept = {m.id for m in models}
        routes = {
            name: [t for t in targets if t in kept]
            for name, targets in self.routes.items()
            if any(t in kept for t in targets)
        }
        return Catalog(
            providers={k: v for k, v in self.providers.items() if k != provider},
            models=models,
            routes=routes,
            defaults=self.defaults,
        )


class UnknownModelError(ValueError):
    def __init__(self, name: str, message: str | None = None) -> None:
        super().__init__(message or f"unknown model or route {name!r}")
        self.name = name


def load_catalog(path: Path) -> Catalog:
    with path.open("rb") as fh:
        return Catalog.model_validate(tomllib.load(fh))
