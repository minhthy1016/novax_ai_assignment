"""Build the gateway from settings + catalog: the only place that knows adapter classes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsassist.config import Settings
from opsassist.db.models import LLMUsage
from opsassist.gateway.catalog import Catalog, ProviderConfig, load_catalog
from opsassist.gateway.gateway import AttemptRecord, CallContext, GatewayConfig, LLMGateway
from opsassist.logging_setup import get_logger
from opsassist.providers.anthropic_provider import AnthropicProvider
from opsassist.providers.mock import MockProvider
from opsassist.providers.ollama import OllamaProvider
from opsassist.providers.openai_compat import OpenAICompatibleProvider

log = get_logger("opsassist.gateway")


@dataclass
class ProviderStatus:
    enabled: bool
    reason: str | None = None


def build_provider(name: str, cfg: ProviderConfig) -> tuple[object | None, ProviderStatus]:
    match cfg.kind:
        case "mock":
            return MockProvider(), ProviderStatus(True)
        case "ollama":
            ollama_url = cfg.resolved_base_url() or "http://localhost:11434"
            return OllamaProvider(name, base_url=ollama_url), ProviderStatus(True)
        case "anthropic":
            anthropic_key = cfg.api_key()
            if anthropic_key is None:
                return None, ProviderStatus(False, f"{cfg.api_key_env} not set")
            return (
                AnthropicProvider(name, api_key=anthropic_key, base_url=cfg.resolved_base_url()),
                ProviderStatus(True),
            )
        case "openai_compatible":
            key = cfg.api_key()
            if cfg.api_key_env and key is None:
                return None, ProviderStatus(False, f"{cfg.api_key_env} not set")
            base_url = cfg.resolved_base_url()
            if not base_url:
                return None, ProviderStatus(False, "base_url not configured")
            provider = OpenAICompatibleProvider(
                name, base_url=base_url, api_key=key, send_input_type=cfg.send_input_type
            )
            return provider, ProviderStatus(True)


class DbUsageRecorder:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def record(self, ctx: CallContext, attempt: AttemptRecord) -> None:
        usage = attempt.usage
        async with self._factory() as session, session.begin():
            session.add(
                LLMUsage(
                    request_id=ctx.request_id,
                    user_id=ctx.user_id,
                    conversation_id=uuid.UUID(ctx.conversation_id) if ctx.conversation_id else None,
                    kind=attempt.kind,
                    model_id=attempt.model_id,
                    provider=attempt.provider,
                    attempt=attempt.attempt,
                    outcome=attempt.outcome,
                    error_type=attempt.error_type,
                    latency_ms=attempt.latency_ms,
                    ttft_ms=attempt.ttft_ms,
                    prompt_tokens=usage.prompt_tokens if usage else 0,
                    completion_tokens=usage.completion_tokens if usage else 0,
                    tokens_estimated=usage.estimated if usage else False,
                    cost_usd=attempt.cost_usd,
                )
            )


def build_gateway(
    settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> tuple[LLMGateway, dict[str, ProviderStatus]]:
    catalog: Catalog = load_catalog(settings.models_config)
    if not settings.mock_provider_enabled and "mock" in catalog.providers:
        catalog = catalog.without_provider("mock")
    providers: dict[str, object] = {}
    statuses: dict[str, ProviderStatus] = {}
    for name, cfg in catalog.providers.items():
        provider, status = build_provider(name, cfg)
        statuses[name] = status
        if provider is not None:
            providers[name] = provider
        log.info("provider_configured", provider=name, enabled=status.enabled, reason=status.reason)
    gateway = LLMGateway(
        catalog,
        providers,
        recorder=DbUsageRecorder(factory),
        config=GatewayConfig(
            request_deadline_s=settings.request_deadline_s,
            stream_deadline_s=settings.stream_deadline_s,
            stream_idle_timeout_s=settings.stream_idle_timeout_s,
        ),
    )
    return gateway, statuses


async def close_providers(gateway: LLMGateway) -> None:
    for provider in gateway.providers.values():
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()
