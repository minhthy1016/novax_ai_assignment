"""Claude adapter through the official SDK against canned HTTP (httpx2.MockTransport)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx2
import pytest
from pydantic import SecretStr

from opsassist.providers.anthropic_provider import AnthropicProvider
from opsassist.providers.base import (
    ChatMessage,
    ChatParams,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderModelNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
    StreamDelta,
    StreamEnd,
)

MSGS = [
    ChatMessage("system", "Be brief."),
    ChatMessage("user", "hi"),
    ChatMessage("user", "are you there?"),  # consecutive user turns must be merged
]
Handler = Callable[[httpx2.Request], httpx2.Response]


def claude(handler: Handler) -> AnthropicProvider:
    return AnthropicProvider(
        api_key=SecretStr("sk-ant-test"),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


def message(text: str) -> dict[str, object]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 21, "output_tokens": 4},
    }


async def test_chat_request_shape_and_usage() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen["path"] = req.url.path
        seen["key"] = req.headers.get("x-api-key")
        seen["body"] = json.loads(req.content)
        return httpx2.Response(200, json=message("Yes."))

    result = await claude(handler).chat("claude-sonnet-4-5", MSGS, ChatParams(max_tokens=50))
    body = seen["body"]
    assert isinstance(body, dict)
    assert seen["path"] == "/v1/messages" and seen["key"] == "sk-ant-test"
    assert body["system"] == "Be brief."  # top-level, not a message role
    assert body["messages"] == [{"role": "user", "content": "hi\n\nare you there?"}]
    assert (result.content, result.finish_reason) == ("Yes.", "end_turn")
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (21, 4)


def sse(events: list[dict[str, object]]) -> str:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


async def test_stream_yields_text_then_final_usage() -> None:
    start = message("")
    start["content"] = []
    start["stop_reason"] = None
    start["usage"] = {"input_tokens": 12, "output_tokens": 1}
    events = [
        {"type": "message_start", "message": start},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]

    def handler(req: httpx2.Request) -> httpx2.Response:
        assert json.loads(req.content)["stream"] is True
        return httpx2.Response(200, text=sse(events), headers={"content-type": "text/event-stream"})

    items = [i async for i in claude(handler).stream_chat("claude-sonnet-4-5", MSGS, ChatParams())]
    assert [i.text for i in items if isinstance(i, StreamDelta)] == ["Hel", "lo"]
    end = items[-1]
    assert isinstance(end, StreamEnd)
    assert (end.usage.prompt_tokens, end.usage.completion_tokens) == (12, 5)
    assert end.finish_reason == "end_turn"


def api_error(status: int, err_type: str, headers: dict[str, str] | None = None) -> Handler:
    body = {"type": "error", "error": {"type": err_type, "message": "nope"}}
    return lambda req: httpx2.Response(status, json=body, headers=headers or {})


@pytest.mark.parametrize(
    ("status", "err_type", "expected"),
    [
        (401, "authentication_error", ProviderAuthError),
        (403, "permission_error", ProviderAuthError),
        (404, "not_found_error", ProviderModelNotFound),
        (400, "invalid_request_error", ProviderBadRequest),
        (500, "api_error", ProviderUnavailable),
        (503, "api_error", ProviderUnavailable),
        (504, "timeout_error", ProviderUnavailable),
        (529, "overloaded_error", ProviderUnavailable),  # must fall back, not fail as 4xx
    ],
)
async def test_sdk_errors_map_to_gateway_errors(
    status: int, err_type: str, expected: type[Exception]
) -> None:
    with pytest.raises(expected):
        await claude(api_error(status, err_type)).chat("claude-sonnet-4-5", MSGS, ChatParams())


async def test_rate_limit_carries_retry_after() -> None:
    provider = claude(api_error(429, "rate_limit_error", {"retry-after": "7"}))
    with pytest.raises(ProviderRateLimited) as exc:
        await provider.chat("claude-sonnet-4-5", MSGS, ChatParams())
    assert exc.value.retry_after_s == 7.0


async def test_sdk_does_not_retry_on_its_own() -> None:
    calls = 0

    def handler(req: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(
            503, json={"type": "error", "error": {"type": "api_error", "message": "x"}}
        )

    with pytest.raises(ProviderUnavailable):
        await claude(handler).chat("claude-sonnet-4-5", MSGS, ChatParams())
    assert calls == 1  # the gateway owns retries
