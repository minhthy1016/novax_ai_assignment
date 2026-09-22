"""Adapters against canned HTTP responses (httpx.MockTransport): wire-format parsing and
error classification, with no network."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from opsassist.providers.base import (
    ChatMessage,
    ChatParams,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderModelNotFound,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    StreamDelta,
    StreamEnd,
)
from opsassist.providers.ollama import OllamaProvider
from opsassist.providers.openai_compat import OpenAICompatibleProvider

MSGS = [ChatMessage("user", "hi")]
Handler = Callable[[httpx.Request], httpx.Response]


def nim(handler: Handler) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "nim",
        base_url="https://nim.test/v1",
        api_key=SecretStr("nvapi-test"),
        send_input_type=True,
        transport=httpx.MockTransport(handler),
    )


def ollama(handler: Handler) -> OllamaProvider:
    return OllamaProvider(base_url="http://ollama.test", transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ OpenAI-compatible


async def test_chat_parses_content_and_drops_reasoning() -> None:
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "ok", "reasoning_content": "hidden chain"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 70, "completion_tokens": 30},
            },
        )

    result = await nim(handler).chat("openai/gpt-oss-20b", MSGS, ChatParams(max_tokens=64))
    assert result.content == "ok"
    assert "hidden" not in result.content
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (70, 30)
    assert seen["auth"] == "Bearer nvapi-test"
    assert seen["body"]["max_tokens"] == 64  # type: ignore[index]


async def test_sse_stream_yields_deltas_then_usage() -> None:
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(req: httpx.Request) -> httpx.Response:
        assert json.loads(req.content)["stream_options"] == {"include_usage": True}
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    items = [i async for i in nim(handler).stream_chat("m", MSGS, ChatParams())]
    assert [i.text for i in items if isinstance(i, StreamDelta)] == ["Hel", "lo"]
    end = items[-1]
    assert (
        isinstance(end, StreamEnd) and end.usage.completion_tokens == 2 and not end.usage.estimated
    )


@pytest.mark.parametrize(
    ("status", "headers", "error"),
    [
        (429, {"retry-after": "3"}, ProviderRateLimited),
        (401, {}, ProviderAuthError),
        (404, {}, ProviderModelNotFound),
        (400, {}, ProviderBadRequest),
        (503, {}, ProviderUnavailable),
    ],
)
async def test_http_errors_are_classified(
    status: int, headers: dict[str, str], error: type[Exception]
) -> None:
    provider = nim(lambda req: httpx.Response(status, text="nope", headers=headers))
    with pytest.raises(error) as exc:
        await provider.chat("m", MSGS, ChatParams())
    if isinstance(exc.value, ProviderRateLimited):
        assert exc.value.retry_after_s == 3.0


async def test_transport_timeout_maps_to_provider_timeout() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(ProviderTimeout):
        await nim(handler).chat("m", MSGS, ChatParams())


async def test_nim_embeddings_send_input_type_and_keep_order() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["input_type"] == "passage"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ],
                "usage": {"prompt_tokens": 4},
            },
        )

    result = await nim(handler).embed("e", ["a", "b"], "passage")
    assert result.vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert result.usage.prompt_tokens == 4


# ------------------------------------------------------------------ Ollama native


async def test_ollama_ndjson_stream_and_usage() -> None:
    lines = [
        {"message": {"content": "Hi"}, "done": False},
        {"message": {"content": " there"}, "done": False},
        {
            "message": {"content": ""},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 12,
            "eval_count": 3,
        },
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/chat"
        return httpx.Response(200, text="\n".join(json.dumps(line) for line in lines))

    items = [i async for i in ollama(handler).stream_chat("llama3.2:3b", MSGS, ChatParams())]
    assert "".join(i.text for i in items if isinstance(i, StreamDelta)) == "Hi there"
    end = items[-1]
    assert isinstance(end, StreamEnd)
    assert (end.usage.prompt_tokens, end.usage.completion_tokens) == (12, 3)
    assert end.finish_reason == "stop"


async def test_ollama_stream_error_chunk_is_a_provider_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"error": "model crashed"}))

    with pytest.raises(ProviderUnavailable):
        _ = [i async for i in ollama(handler).stream_chat("m", MSGS, ChatParams())]


async def test_ollama_embed_applies_nomic_task_prefix() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["input"] == ["search_query: deploy window"]
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]], "prompt_eval_count": 4})

    result = await ollama(handler).embed("nomic-embed-text", ["deploy window"], "query")
    assert result.vectors == [[0.1, 0.2]]


async def test_ollama_missing_model_is_model_not_found() -> None:
    provider = ollama(lambda req: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(ProviderModelNotFound):
        await provider.chat("nope", MSGS, ChatParams())
