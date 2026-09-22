"""LLM gateway: routing, per-attempt timeouts, retry with backoff, fallback, circuit
breaking, cancellation, and usage accounting for every attempt.

Decision table for a failed attempt:

======================  =======  ==========  ============  ==================================
Error                   Retry?   Fallback?   Trips breaker Rationale
======================  =======  ==========  ============  ==================================
Timeout / 5xx / conn    yes      yes         yes           transient provider trouble
429 rate limited        yes      yes         yes           honour Retry-After up to the cap
401/403 auth            no       yes         yes           our credential; another provider may work
404 model not found     no       yes         no            config gap, not provider health
400/422 bad request     no       NO          no            our request is wrong everywhere
Malformed response      no       yes         yes           provider misbehaving
======================  =======  ==========  ============  ==================================

Streaming adds one rule: fallback is only possible *before* the first token reaches the
client. After that, switching providers would splice two different answers together, so
a mid-stream failure ends the stream with an explicit error event instead.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol, TypeVar

import anyio

from opsassist.gateway.catalog import Catalog, ModelKind, ModelSpec
from opsassist.gateway.resilience import (
    BackoffPolicy,
    BreakerRegistry,
    BreakerState,
    CircuitBreaker,
)
from opsassist.logging_setup import get_logger
from opsassist.metrics import MODEL_CALLS, MODEL_LATENCY, MODEL_TOKENS
from opsassist.providers.base import (
    ChatMessage,
    ChatParams,
    ChatProvider,
    ChatResult,
    EmbeddingProvider,
    EmbeddingResult,
    InputType,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderModelNotFound,
    ProviderResponseError,
    ProviderTimeout,
    StreamDelta,
    StreamEnd,
    StreamItem,
    Usage,
    estimate_tokens,
)

log = get_logger("opsassist.gateway")
T = TypeVar("T")

AttemptOutcome = Literal["success", "error", "timeout", "cancelled", "skipped"]
GatewayErrorCode = Literal[
    "bad_request", "provider_unavailable", "no_available_provider", "deadline_exceeded"
]


@dataclass(frozen=True, slots=True)
class CallContext:
    request_id: str
    user_id: str | None = None
    conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    kind: ModelKind
    model_id: str
    provider: str
    attempt: int
    outcome: AttemptOutcome
    latency_ms: float = 0.0
    ttft_ms: float | None = None
    usage: Usage | None = None
    cost_usd: Decimal = Decimal(0)
    error_type: str | None = None
    detail: str | None = None


class UsageRecorder(Protocol):
    async def record(self, ctx: CallContext, attempt: AttemptRecord) -> None: ...


class NullRecorder:
    async def record(self, ctx: CallContext, attempt: AttemptRecord) -> None:
        return None


class GatewayError(Exception):
    def __init__(self, code: GatewayErrorCode, message: str, attempts: list[AttemptRecord]):
        super().__init__(message)
        self.code = code
        self.attempts = attempts


@dataclass(frozen=True, slots=True)
class ChatOutcome:
    result: ChatResult
    model: ModelSpec
    route: str
    attempts: list[AttemptRecord]
    latency_ms: float

    @property
    def fallback_used(self) -> bool:
        return any(a.model_id != self.model.id for a in self.attempts)

    @property
    def cost_usd(self) -> Decimal:
        return sum((a.cost_usd for a in self.attempts), Decimal(0))


@dataclass(frozen=True, slots=True)
class EmbedOutcome:
    result: EmbeddingResult
    model: ModelSpec
    route: str
    attempts: list[AttemptRecord]


# ------------------------------------------------------------------ stream events


@dataclass(frozen=True, slots=True)
class StreamStarted:
    model: ModelSpec
    route: str
    fallback_used: bool


@dataclass(frozen=True, slots=True)
class StreamCompleted:
    model: ModelSpec
    usage: Usage
    finish_reason: str | None
    latency_ms: float
    ttft_ms: float
    cost_usd: Decimal
    attempts: list[AttemptRecord]


@dataclass(frozen=True, slots=True)
class StreamFailed:
    code: GatewayErrorCode | Literal["provider_error_mid_stream"]
    message: str
    started: bool
    attempts: list[AttemptRecord] = field(default_factory=list)


GatewayStreamEvent = StreamStarted | StreamDelta | StreamCompleted | StreamFailed


# ------------------------------------------------------------------ gateway


@dataclass
class GatewayConfig:
    request_deadline_s: float = 120.0
    stream_deadline_s: float = 300.0
    stream_idle_timeout_s: float = 30.0


class LLMGateway:
    def __init__(
        self,
        catalog: Catalog,
        providers: dict[str, object],
        *,
        recorder: UsageRecorder | None = None,
        config: GatewayConfig | None = None,
        backoff: BackoffPolicy | None = None,
        breakers: BreakerRegistry | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.catalog = catalog
        self.providers = providers
        self._recorder = recorder or NullRecorder()
        self._config = config or GatewayConfig()
        self._backoff = backoff or BackoffPolicy()
        self.breakers = breakers or BreakerRegistry()
        self._sleep = sleep

    # ------------------------------------------------------------ availability

    def chat_provider(self, spec: ModelSpec) -> ChatProvider | None:
        p = self.providers.get(spec.provider)
        return p if isinstance(p, ChatProvider) else None

    def embedding_provider(self, spec: ModelSpec) -> EmbeddingProvider | None:
        p = self.providers.get(spec.provider)
        return p if isinstance(p, EmbeddingProvider) else None

    def is_available(self, spec: ModelSpec) -> bool:
        return spec.provider in self.providers

    # ------------------------------------------------------------ public API

    async def chat(
        self,
        route: str | None,
        messages: list[ChatMessage],
        params: ChatParams,
        ctx: CallContext,
    ) -> ChatOutcome:
        route_name, targets = self.catalog.resolve(route, "chat")
        started = time.perf_counter()

        async def invoke(spec: ModelSpec) -> ChatResult:
            provider = self.chat_provider(spec)
            assert provider is not None
            return await provider.chat(spec.provider_model, messages, _params_for(spec, params))

        result, spec, attempts = await self._run(
            "chat", targets, ctx, invoke, lambda r: r.usage, self._config.request_deadline_s
        )
        return ChatOutcome(
            result=result,
            model=spec,
            route=route_name,
            attempts=attempts,
            latency_ms=_ms_since(started),
        )

    async def embed(
        self, route: str | None, inputs: list[str], input_type: InputType, ctx: CallContext
    ) -> EmbedOutcome:
        route_name, targets = self.catalog.resolve(route, "embedding")

        async def invoke(spec: ModelSpec) -> EmbeddingResult:
            provider = self.embedding_provider(spec)
            assert provider is not None
            result = await provider.embed(spec.provider_model, inputs, input_type)
            if spec.dimensions and any(len(v) != spec.dimensions for v in result.vectors):
                raise ProviderResponseError(spec.provider, "embedding dimension mismatch")
            return result

        result, spec, attempts = await self._run(
            "embedding",
            targets,
            ctx,
            invoke,
            lambda r: r.usage,
            self._config.request_deadline_s,
        )
        return EmbedOutcome(result=result, model=spec, route=route_name, attempts=attempts)

    async def stream_chat(
        self,
        route: str | None,
        messages: list[ChatMessage],
        params: ChatParams,
        ctx: CallContext,
    ) -> AsyncGenerator[GatewayStreamEvent, None]:
        route_name, targets = self.catalog.resolve(route, "chat")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.stream_deadline_s
        request_started = time.perf_counter()
        attempts: list[AttemptRecord] = []

        for spec in targets:
            provider = self.chat_provider(spec)
            if not await self._admit(spec, provider, ctx, attempts):
                continue
            assert provider is not None
            cfg = self.catalog.providers[spec.provider]
            breaker = self.breakers.get(spec.id)

            for n in range(1, cfg.max_attempts + 1):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    yield StreamFailed(
                        "deadline_exceeded", "request deadline exceeded", False, attempts
                    )
                    return
                attempt_started = time.perf_counter()
                produced: list[str] = []
                emitted = False
                ttft_ms: float | None = None
                agen = provider.stream_chat(
                    spec.provider_model, messages, _params_for(spec, params)
                )
                try:
                    item = await _next_item(
                        agen, min(cfg.timeout_s, remaining), spec.provider, "first token"
                    )
                    ttft_ms = _ms_since(attempt_started)
                    emitted = True
                    yield StreamStarted(spec, route_name, fallback_used=bool(attempts))
                    while isinstance(item, StreamDelta):
                        produced.append(item.text)
                        yield item
                        idle = min(self._config.stream_idle_timeout_s, deadline - loop.time())
                        if idle <= 0:
                            raise ProviderTimeout(spec.provider, "stream exceeded request deadline")
                        item = await _next_item(agen, idle, spec.provider, "next chunk")
                    assert isinstance(item, StreamEnd)
                    record = self._attempt(
                        "chat",
                        spec,
                        n,
                        "success",
                        attempt_started,
                        usage=item.usage,
                        ttft_ms=ttft_ms,
                    )
                    attempts.append(record)
                    breaker.record_success()
                    await self._record(ctx, record)
                    yield StreamCompleted(
                        model=spec,
                        usage=item.usage,
                        finish_reason=item.finish_reason,
                        latency_ms=_ms_since(request_started),
                        ttft_ms=ttft_ms,
                        cost_usd=record.cost_usd,
                        attempts=attempts,
                    )
                    return
                except ProviderError as err:
                    record = self._attempt(
                        "chat",
                        spec,
                        n,
                        "timeout" if isinstance(err, ProviderTimeout) else "error",
                        attempt_started,
                        usage=_partial_usage(messages, produced) if emitted else None,
                        ttft_ms=ttft_ms,
                        error=err,
                    )
                    attempts.append(record)
                    _update_breaker(breaker, err)
                    await self._record(ctx, record)
                    if emitted:
                        yield StreamFailed(
                            "provider_error_mid_stream",
                            "the model provider failed while streaming; the answer is incomplete",
                            True,
                            attempts,
                        )
                        return
                    if isinstance(err, ProviderBadRequest):
                        yield StreamFailed(
                            "bad_request", "the model rejected the request", False, attempts
                        )
                        return
                    if not await self._should_retry(err, n, cfg.max_attempts, breaker, deadline):
                        break
                except (asyncio.CancelledError, GeneratorExit):
                    # Client went away. Record what was consumed, then let cancellation proceed.
                    record = self._attempt(
                        "chat",
                        spec,
                        n,
                        "cancelled",
                        attempt_started,
                        usage=_partial_usage(messages, produced),
                        ttft_ms=ttft_ms,
                    )
                    await self._record(ctx, record)
                    raise
                finally:
                    with anyio.CancelScope(shield=True):
                        await agen.aclose()

        code: GatewayErrorCode = (
            "no_available_provider"
            if all(a.outcome == "skipped" for a in attempts)
            else "provider_unavailable"
        )
        yield StreamFailed(code, "no model provider could serve the request", False, attempts)

    # ------------------------------------------------------------ core loop

    async def _run(
        self,
        kind: ModelKind,
        targets: list[ModelSpec],
        ctx: CallContext,
        invoke: Callable[[ModelSpec], Awaitable[T]],
        usage_of: Callable[[T], Usage],
        deadline_s: float,
    ) -> tuple[T, ModelSpec, list[AttemptRecord]]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + deadline_s
        attempts: list[AttemptRecord] = []

        for spec in targets:
            provider = self.providers.get(spec.provider)
            if not await self._admit(spec, provider, ctx, attempts, kind):
                continue
            cfg = self.catalog.providers[spec.provider]
            breaker = self.breakers.get(spec.id)

            for n in range(1, cfg.max_attempts + 1):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise GatewayError("deadline_exceeded", "request deadline exceeded", attempts)
                timeout = min(cfg.timeout_s, remaining)
                attempt_started = time.perf_counter()
                try:
                    try:
                        async with asyncio.timeout(timeout):
                            result = await invoke(spec)
                    except TimeoutError:
                        raise ProviderTimeout(
                            spec.provider, f"no response within {timeout:.1f}s"
                        ) from None
                except ProviderError as err:
                    record = self._attempt(
                        kind,
                        spec,
                        n,
                        "timeout" if isinstance(err, ProviderTimeout) else "error",
                        attempt_started,
                        error=err,
                    )
                    attempts.append(record)
                    _update_breaker(breaker, err)
                    await self._record(ctx, record)
                    if isinstance(err, ProviderBadRequest):
                        raise GatewayError(
                            "bad_request", "the model rejected the request", attempts
                        ) from err
                    if not await self._should_retry(err, n, cfg.max_attempts, breaker, deadline):
                        break
                    continue
                except asyncio.CancelledError:
                    await self._record(
                        ctx, self._attempt(kind, spec, n, "cancelled", attempt_started)
                    )
                    raise
                record = self._attempt(
                    kind, spec, n, "success", attempt_started, usage=usage_of(result)
                )
                attempts.append(record)
                breaker.record_success()
                await self._record(ctx, record)
                return result, spec, attempts

        if all(a.outcome == "skipped" for a in attempts):
            raise GatewayError(
                "no_available_provider", "no configured provider is available", attempts
            )
        raise GatewayError(
            "provider_unavailable", "no model provider could serve the request", attempts
        )

    # ------------------------------------------------------------ helpers

    async def _admit(
        self,
        spec: ModelSpec,
        provider: object | None,
        ctx: CallContext,
        attempts: list[AttemptRecord],
        kind: ModelKind = "chat",
    ) -> bool:
        if provider is None:
            reason = "provider_disabled"
        elif not self.breakers.get(spec.id).allow():
            reason = "circuit_open"
        else:
            return True
        record = AttemptRecord(
            kind=kind,
            model_id=spec.id,
            provider=spec.provider,
            attempt=0,
            outcome="skipped",
            detail=reason,
        )
        attempts.append(record)
        MODEL_CALLS.labels(provider=spec.provider, model=spec.id, outcome="skipped").inc()
        log.info("model_target_skipped", model=spec.id, reason=reason)
        return False

    async def _should_retry(
        self,
        err: ProviderError,
        attempt: int,
        max_attempts: int,
        breaker: CircuitBreaker,
        deadline: float,
    ) -> bool:
        if not err.retryable or attempt >= max_attempts:
            return False
        if breaker.state is not BreakerState.CLOSED:
            return False  # this failure opened the breaker: stop hammering, move to fallback
        delay = self._backoff.delay(attempt, err)
        if asyncio.get_running_loop().time() + delay >= deadline:
            return False
        log.info(
            "model_retry_scheduled", provider=err.provider, attempt=attempt, delay_s=round(delay, 3)
        )
        await self._sleep(delay)
        return True

    def _attempt(
        self,
        kind: ModelKind,
        spec: ModelSpec,
        n: int,
        outcome: AttemptOutcome,
        started: float,
        *,
        usage: Usage | None = None,
        ttft_ms: float | None = None,
        error: ProviderError | None = None,
    ) -> AttemptRecord:
        latency_ms = _ms_since(started)
        cost = (
            spec.estimate_cost_usd(usage.prompt_tokens, usage.completion_tokens)
            if usage
            else Decimal(0)
        )
        MODEL_CALLS.labels(provider=spec.provider, model=spec.id, outcome=outcome).inc()
        MODEL_LATENCY.labels(provider=spec.provider, model=spec.id).observe(latency_ms / 1000)
        if usage:
            MODEL_TOKENS.labels(spec.provider, spec.id, "prompt").inc(usage.prompt_tokens)
            MODEL_TOKENS.labels(spec.provider, spec.id, "completion").inc(usage.completion_tokens)
        log.info(
            "model_attempt",
            kind=kind,
            model=spec.id,
            provider=spec.provider,
            attempt=n,
            outcome=outcome,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            error_type=type(error).__name__ if error else None,
        )
        return AttemptRecord(
            kind=kind,
            model_id=spec.id,
            provider=spec.provider,
            attempt=n,
            outcome=outcome,
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
            usage=usage,
            cost_usd=cost,
            error_type=type(error).__name__ if error else None,
            detail=str(error)[:300] if error else None,
        )

    async def _record(self, ctx: CallContext, record: AttemptRecord) -> None:
        # Shielded: usage for a cancelled stream must still be written. A recorder failure
        # is logged and swallowed — accounting problems must not fail the user's request.
        with anyio.CancelScope(shield=True):
            try:
                await self._recorder.record(ctx, record)
            except Exception:
                log.exception("usage_record_failed", model=record.model_id)


def _params_for(spec: ModelSpec, params: ChatParams) -> ChatParams:
    if not spec.supports_temperature and params.temperature is not None:
        return dataclasses.replace(params, temperature=None)
    return params


def _update_breaker(breaker: CircuitBreaker, err: ProviderError) -> None:
    if isinstance(err, ProviderBadRequest | ProviderModelNotFound):
        return  # our request or our config, not the provider's health
    if err.retryable or isinstance(err, ProviderAuthError | ProviderResponseError):
        breaker.record_failure()


async def _next_item(
    agen: AsyncIterator[StreamItem], timeout_s: float, provider: str, phase: str
) -> StreamItem:
    try:
        async with asyncio.timeout(timeout_s):
            return await anext(agen)
    except TimeoutError:
        raise ProviderTimeout(provider, f"no data within {timeout_s:.1f}s ({phase})") from None
    except StopAsyncIteration:
        raise ProviderResponseError(provider, "stream ended without a completion marker") from None


def _partial_usage(messages: list[ChatMessage], produced: list[str]) -> Usage:
    return Usage(
        prompt_tokens=sum(estimate_tokens(m.content) for m in messages),
        completion_tokens=estimate_tokens("".join(produced)),
        estimated=True,
    )


def _ms_since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
