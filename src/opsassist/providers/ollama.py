"""Adapter for Ollama's native API (``/api/chat``, ``/api/embed``).

Ollama also exposes an OpenAI-compatible endpoint; the native one is used on purpose.
Its streaming format (NDJSON rather than SSE) and usage fields (``prompt_eval_count`` /
``eval_count``) differ from OpenAI's, which exercises the provider abstraction for real
instead of reusing one adapter under two names.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from opsassist.providers.base import (
    ChatMessage,
    ChatParams,
    ChatResult,
    EmbeddingResult,
    InputType,
    ProviderResponseError,
    ProviderTimeout,
    ProviderUnavailable,
    StreamDelta,
    StreamEnd,
    StreamItem,
    Usage,
    classify_http_status,
    estimate_tokens,
)

# Some embedding models are trained with task prefixes and retrieve noticeably worse without them.
_TASK_PREFIXES: dict[str, dict[InputType, str]] = {
    "nomic-embed-text": {"query": "search_query: ", "passage": "search_document: "},
}


class OllamaProvider:
    def __init__(
        self,
        name: str = "ollama",
        *,
        base_url: str = "http://localhost:11434",
        http_timeout_s: float = 120.0,
        keep_alive: str = "10m",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self._keep_alive = keep_alive
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(http_timeout_s, connect=5.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(self, model: str, messages: list[ChatMessage], params: ChatParams) -> ChatResult:
        data = await self._post_json(
            "/api/chat", self._chat_payload(model, messages, params, False)
        )
        try:
            content = data["message"]["content"] or ""
        except (KeyError, TypeError) as exc:
            raise ProviderResponseError(self.name, "malformed chat response") from exc
        return ChatResult(
            content=content,
            usage=self._usage(data, messages, content),
            finish_reason=data.get("done_reason"),
        )

    async def stream_chat(
        self, model: str, messages: list[ChatMessage], params: ChatParams
    ) -> AsyncGenerator[StreamItem, None]:
        payload = self._chat_payload(model, messages, params, True)
        produced: list[str] = []
        final: dict[str, Any] = {}
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise classify_http_status(self.name, resp.status_code, body)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ProviderResponseError(self.name, "malformed stream chunk") from exc
                    if "error" in chunk:
                        raise ProviderUnavailable(self.name, str(chunk["error"])[:200])
                    text = (chunk.get("message") or {}).get("content")
                    if text:
                        produced.append(text)
                        yield StreamDelta(text)
                    if chunk.get("done"):
                        final = chunk
                        break
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(self.name, "stream timed out") from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailable(self.name, f"transport error: {type(exc).__name__}") from exc
        if not final:
            raise ProviderResponseError(self.name, "stream ended without a final chunk")
        yield StreamEnd(
            usage=self._usage(final, messages, "".join(produced)),
            finish_reason=final.get("done_reason"),
        )

    async def embed(self, model: str, inputs: list[str], input_type: InputType) -> EmbeddingResult:
        prefix = _TASK_PREFIXES.get(model.split(":")[0], {}).get(input_type, "")
        data = await self._post_json(
            "/api/embed",
            {"model": model, "input": [prefix + t for t in inputs], "keep_alive": self._keep_alive},
        )
        try:
            vectors = [list(map(float, v)) for v in data["embeddings"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderResponseError(self.name, "malformed embedding response") from exc
        if len(vectors) != len(inputs):
            raise ProviderResponseError(self.name, "embedding count mismatch")
        tokens = data.get("prompt_eval_count")
        usage = (
            Usage(prompt_tokens=int(tokens))
            if tokens is not None
            else Usage(prompt_tokens=sum(estimate_tokens(t) for t in inputs), estimated=True)
        )
        return EmbeddingResult(vectors=vectors, usage=usage)

    def _chat_payload(
        self, model: str, messages: list[ChatMessage], params: ChatParams, stream: bool
    ) -> dict[str, Any]:
        return {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": stream,
            "keep_alive": self._keep_alive,
            "options": {"temperature": params.temperature, "num_predict": params.max_tokens},
        }

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post(path, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(self.name, "request timed out") from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailable(self.name, f"transport error: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise classify_http_status(self.name, resp.status_code, resp.text)
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderResponseError(self.name, "non-JSON response") from exc
        if not isinstance(data, dict):
            raise ProviderResponseError(self.name, "unexpected response type")
        return data

    @staticmethod
    def _usage(data: dict[str, Any], messages: list[ChatMessage], completion: str) -> Usage:
        if data.get("prompt_eval_count") is not None:
            return Usage(
                prompt_tokens=int(data["prompt_eval_count"]),
                completion_tokens=int(data.get("eval_count") or 0),
            )
        return Usage(
            prompt_tokens=sum(estimate_tokens(m.content) for m in messages),
            completion_tokens=estimate_tokens(completion),
            estimated=True,
        )
