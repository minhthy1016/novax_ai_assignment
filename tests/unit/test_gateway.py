"""Gateway behaviour with deterministic providers: no network, no sleeps (fake clock sleep)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any

import pytest

from opsassist.gateway.catalog import Catalog
from opsassist.gateway.gateway import (
    AttemptRecord,
    CallContext,
    GatewayConfig,
    GatewayError,
    LLMGateway,
    StreamCompleted,
    StreamFailed,
    StreamStarted,
)
from opsassist.gateway.resilience import BreakerRegistry
from opsassist.providers.base import (
    ChatMessage,
    ChatParams,
    ChatResult,
    ProviderBadRequest,
    ProviderUnavailable,
    StreamDelta,
    StreamEnd,
    StreamItem,
    Usage,
)
from opsassist.providers.mock import MockProvider

MESSAGES = [ChatMessage("system", "be brief"), ChatMessage("user", "hello there")]
PARAMS = ChatParams()
CTX = CallContext(request_id="req-test-0001", user_id="U001")


class Recorder:
    def __init__(self) -> None:
        self.records: list[AttemptRecord] = []

    async def record(self, ctx: CallContext, attempt: AttemptRecord) -> None:
        self.records.append(attempt)


class ScriptedProvider:
    """Chat provider whose behaviour is scripted per call, for cases the mock can't express."""

    name = "scripted"

    def __init__(self, stream_script: list[Any]) -> None:
        self.stream_script = stream_script

    async def chat(self, model: str, messages: list[ChatMessage], params: ChatParams) -> ChatResult:
        raise ProviderBadRequest(self.name, "context too long", status_code=400)

    async def stream_chat(
        self, model: str, messages: list[ChatMessage], params: ChatParams
    ) -> AsyncGenerator[StreamItem, None]:
        for step in self.stream_script:
            if isinstance(step, Exception):
                raise step
            if isinstance(step, float):
                await asyncio.sleep(step)
                continue
            yield step


def make_catalog(timeout_s: float = 0.2, max_attempts: int = 3) -> Catalog:
    def chat(id_: str, provider: str, model: str) -> dict[str, Any]:
        return {"id": id_, "provider": provider, "provider_model": model, "kind": "chat"}

    return Catalog.model_validate(
        {
            "providers": {
                "mock": {"kind": "mock", "timeout_s": timeout_s, "max_attempts": max_attempts},
                "backup": {"kind": "mock", "timeout_s": timeout_s, "max_attempts": 1},
                "scripted": {"kind": "mock", "timeout_s": timeout_s, "max_attempts": 1},
            },
            "models": [
                chat("mock/echo", "mock", "mock-echo"),
                chat("mock/slow", "mock", "mock-slow"),
                chat("mock/flaky", "mock", "mock-flaky"),
                chat("mock/down", "mock", "mock-down"),
                chat("mock/ratelimit", "mock", "mock-ratelimit"),
                chat("backup/echo", "backup", "mock-echo"),
                chat("scripted/x", "scripted", "x"),
                {
                    "id": "mock/embed",
                    "provider": "mock",
                    "provider_model": "mock-embed",
                    "kind": "embedding",
                    "dimensions": 256,
                },
                {
                    "id": "mock/embed-wrongdim",
                    "provider": "mock",
                    "provider_model": "mock-embed",
                    "kind": "embedding",
                    "dimensions": 999,
                },
            ],
            "routes": {
                "failover": ["mock/down", "backup/echo"],
                "timeout": ["mock/slow", "backup/echo"],
                "scripted-then-backup": ["scripted/x", "backup/echo"],
                "embed": ["mock/embed"],
            },
            "defaults": {"chat": "failover", "embedding": "embed"},
        }
    )


class Harness:
    def __init__(self, scripted: ScriptedProvider | None = None, **gateway_kwargs: Any) -> None:
        self.recorder = Recorder()
        self.sleeps: list[float] = []
        self.mock = MockProvider(slow_seconds=5.0, flaky_failures=2)

        async def fake_sleep(s: float) -> None:
            self.sleeps.append(s)

        providers: dict[str, object] = {"mock": self.mock, "backup": MockProvider()}
        if scripted:
            providers["scripted"] = scripted
        self.gateway = LLMGateway(
            gateway_kwargs.pop("catalog", make_catalog()),
            providers,
            recorder=self.recorder,
            sleep=fake_sleep,
            **gateway_kwargs,
        )


# ------------------------------------------------------------------ non-streaming


async def test_success_records_usage_and_cost() -> None:
    h = Harness()
    out = await h.gateway.chat("mock/echo", MESSAGES, PARAMS, CTX)
    assert out.result.content.startswith("[mock:")
    assert not out.fallback_used
    assert [r.outcome for r in h.recorder.records] == ["success"]
    assert h.recorder.records[0].usage and h.recorder.records[0].usage.total_tokens > 0


async def test_transient_errors_are_retried_with_backoff() -> None:
    h = Harness()
    out = await h.gateway.chat("mock/flaky", MESSAGES, PARAMS, CTX)
    assert out.model.id == "mock/flaky"
    assert [r.outcome for r in h.recorder.records] == ["error", "error", "success"]
    assert len(h.sleeps) == 2


async def test_exhausted_retries_fall_back_to_next_provider() -> None:
    h = Harness()
    out = await h.gateway.chat("failover", MESSAGES, PARAMS, CTX)
    assert out.model.id == "backup/echo"
    assert out.fallback_used
    outcomes = [(r.model_id, r.outcome) for r in h.recorder.records]
    assert outcomes[-1] == ("backup/echo", "success")
    assert all(o == "error" for m, o in outcomes[:-1])


async def test_timeout_triggers_fallback() -> None:
    h = Harness()
    out = await h.gateway.chat("timeout", MESSAGES, PARAMS, CTX)
    assert out.model.id == "backup/echo"
    assert h.recorder.records[0].outcome == "timeout"
    assert h.recorder.records[0].error_type == "ProviderTimeout"


async def test_bad_request_neither_retries_nor_falls_back() -> None:
    h = Harness(scripted=ScriptedProvider([]))
    with pytest.raises(GatewayError) as exc:
        await h.gateway.chat("scripted-then-backup", MESSAGES, PARAMS, CTX)
    assert exc.value.code == "bad_request"
    assert [r.model_id for r in h.recorder.records] == ["scripted/x"]


async def test_rate_limit_honours_retry_after() -> None:
    h = Harness()
    with pytest.raises(GatewayError) as exc:
        await h.gateway.chat("mock/ratelimit", MESSAGES, PARAMS, CTX)
    assert exc.value.code == "provider_unavailable"
    assert h.sleeps and all(s == pytest.approx(0.05) for s in h.sleeps)


async def test_open_circuit_skips_provider_without_calling_it() -> None:
    h = Harness(breakers=BreakerRegistry(failure_threshold=3, cooldown_s=60))
    await h.gateway.chat("failover", MESSAGES, PARAMS, CTX)  # 3 failures open the breaker
    calls_before = h.mock.calls["mock-down"]
    out = await h.gateway.chat("failover", MESSAGES, PARAMS, CTX)
    assert h.mock.calls["mock-down"] == calls_before
    assert out.attempts[0].outcome == "skipped" and out.attempts[0].detail == "circuit_open"
    assert out.model.id == "backup/echo"


async def test_all_targets_disabled_is_reported_distinctly() -> None:
    gateway = LLMGateway(make_catalog(), providers={})
    with pytest.raises(GatewayError) as exc:
        await gateway.chat("failover", MESSAGES, PARAMS, CTX)
    assert exc.value.code == "no_available_provider"


async def test_request_deadline_bounds_total_time() -> None:
    h = Harness(config=GatewayConfig(request_deadline_s=0.05))
    with pytest.raises(GatewayError) as exc:
        await h.gateway.chat("timeout", MESSAGES, PARAMS, CTX)
    assert exc.value.code in ("deadline_exceeded", "provider_unavailable")
    assert h.recorder.records[0].outcome == "timeout"


# ------------------------------------------------------------------ embeddings


async def test_embeddings_are_deterministic_and_dimension_checked() -> None:
    h = Harness()
    a = await h.gateway.embed("embed", ["deploy window"], "query", CTX)
    b = await h.gateway.embed("mock/embed", ["deploy window"], "query", CTX)
    assert a.result.vectors == b.result.vectors
    assert len(a.result.vectors[0]) == 256
    with pytest.raises(GatewayError):
        await h.gateway.embed("mock/embed-wrongdim", ["x"], "query", CTX)


# ------------------------------------------------------------------ streaming


async def collect(gateway: LLMGateway, route: str) -> list[object]:
    async with aclosing(gateway.stream_chat(route, MESSAGES, PARAMS, CTX)) as stream:
        return [e async for e in stream]


async def test_stream_emits_started_deltas_completed() -> None:
    h = Harness()
    events = await collect(h.gateway, "mock/echo")
    assert isinstance(events[0], StreamStarted)
    assert isinstance(events[-1], StreamCompleted)
    text = "".join(e.text for e in events if isinstance(e, StreamDelta))
    assert text.startswith("[mock:")
    assert events[-1].usage.completion_tokens > 0


async def test_stream_falls_back_before_first_token() -> None:
    h = Harness()
    events = await collect(h.gateway, "failover")
    started = events[0]
    assert isinstance(started, StreamStarted)
    assert started.model.id == "backup/echo" and started.fallback_used


async def test_stream_failure_after_first_token_does_not_fall_back() -> None:
    scripted = ScriptedProvider(
        [StreamDelta("partial "), ProviderUnavailable("scripted", "connection reset")]
    )
    h = Harness(scripted=scripted)
    events = await collect(h.gateway, "scripted-then-backup")
    assert isinstance(events[0], StreamStarted) and events[0].model.id == "scripted/x"
    assert isinstance(events[1], StreamDelta)
    failed = events[-1]
    assert isinstance(failed, StreamFailed) and failed.started
    assert failed.code == "provider_error_mid_stream"
    assert not any(isinstance(e, StreamStarted) and e.model.id == "backup/echo" for e in events)


async def test_stream_idle_timeout_is_enforced() -> None:
    scripted = ScriptedProvider(
        [StreamDelta("a"), 5.0, StreamEnd(Usage(1, 1))]  # stalls 5s after the first chunk
    )
    h = Harness(scripted=scripted, config=GatewayConfig(stream_idle_timeout_s=0.05))
    events = await collect(h.gateway, "scripted/x")
    assert isinstance(events[-1], StreamFailed)
    assert h.recorder.records[-1].outcome == "timeout"


async def test_stream_cancellation_records_cancelled_usage() -> None:
    scripted = ScriptedProvider(
        [StreamDelta("one "), StreamDelta("two "), 5.0, StreamEnd(Usage(1, 1))]
    )
    h = Harness(scripted=scripted, config=GatewayConfig(stream_idle_timeout_s=10))

    async def consume() -> None:
        async with aclosing(h.gateway.stream_chat("scripted/x", MESSAGES, PARAMS, CTX)) as s:
            async for _ in s:
                pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # let it receive two deltas and block on the stall
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    last = h.recorder.records[-1]
    assert last.outcome == "cancelled"
    assert last.usage is not None and last.usage.completion_tokens > 0 and last.usage.estimated


async def test_failing_model_does_not_open_breaker_for_sibling_models() -> None:
    h = Harness(breakers=BreakerRegistry(failure_threshold=1, cooldown_s=60))
    with pytest.raises(GatewayError):
        await h.gateway.chat("mock/down", MESSAGES, PARAMS, CTX)
    out = await h.gateway.chat("mock/echo", MESSAGES, PARAMS, CTX)  # same provider, other model
    assert out.model.id == "mock/echo" and out.attempts[0].outcome == "success"
