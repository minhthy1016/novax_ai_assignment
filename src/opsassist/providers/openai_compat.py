"""Adapter for OpenAI-compatible HTTP APIs (NVIDIA NIM, vLLM, OpenAI, ...).

Only the fields we rely on are read from responses, so small vendor differences (e.g. the
separate ``reasoning_content`` field that gpt-oss models return) do not break parsing.
Reasoning text is deliberately dropped: it is not part of the answer and must not be
shown to users or stored as the assistant's reply.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from pydantic import SecretStr

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


class OpenAICompatibleProvider:
    def __init__(
        self,
        name: str,
        *,
        base_url: str,
        api_key: SecretStr | None,
        send_input_type: bool = False,
        http_timeout_s: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self._send_input_type = send_input_type
        headers = {"Accept": "application/json"}
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key.get_secret_value()}"
        # The gateway enforces the real per-attempt deadline; this is only a backstop.
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(http_timeout_s, connect=10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ chat

    async def chat(self, model: str, messages: list[ChatMessage], params: ChatParams) -> ChatResult:
        payload = self._chat_payload(model, messages, params, stream=False)
        data = await self._post_json("/chat/completions", payload)
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(self.name, "malformed chat response") from exc
        usage = self._usage(data.get("usage"), messages, content)
        return ChatResult(content=content, usage=usage, finish_reason=choice.get("finish_reason"))

    async def stream_chat(
        self, model: str, messages: list[ChatMessage], params: ChatParams
    ) -> AsyncGenerator[StreamItem, None]:
        payload = self._chat_payload(model, messages, params, stream=True)
        produced: list[str] = []
        usage_raw: dict[str, Any] | None = None
        finish_reason: str | None = None
        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise classify_http_status(
                        self.name, resp.status_code, body, resp.headers.get("retry-after")
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ProviderResponseError(self.name, "malformed stream chunk") from exc
                    if chunk.get("usage"):
                        usage_raw = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            produced.append(text)
                            yield StreamDelta(text)
                        finish_reason = choice.get("finish_reason") or finish_reason
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(self.name, "stream timed out") from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailable(self.name, f"transport error: {type(exc).__name__}") from exc
        yield StreamEnd(
            usage=self._usage(usage_raw, messages, "".join(produced)), finish_reason=finish_reason
        )

    # ------------------------------------------------------------------ embeddings

    async def embed(self, model: str, inputs: list[str], input_type: InputType) -> EmbeddingResult:
        payload: dict[str, Any] = {"model": model, "input": inputs, "encoding_format": "float"}
        if self._send_input_type:
            payload["input_type"] = input_type
            payload["truncate"] = "END"
        data = await self._post_json("/embeddings", payload)
        try:
            rows = sorted(data["data"], key=lambda r: r["index"])
            vectors = [list(map(float, r["embedding"])) for r in rows]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderResponseError(self.name, "malformed embedding response") from exc
        if len(vectors) != len(inputs):
            raise ProviderResponseError(self.name, "embedding count mismatch")
        raw = data.get("usage") or {}
        tokens = raw.get("prompt_tokens")
        usage = (
            Usage(prompt_tokens=int(tokens))
            if tokens is not None
            else Usage(prompt_tokens=sum(estimate_tokens(t) for t in inputs), estimated=True)
        )
        return EmbeddingResult(vectors=vectors, usage=usage)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _chat_payload(
        model: str, messages: list[ChatMessage], params: ChatParams, *, stream: bool
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": params.max_tokens,
            "stream": stream,
        }
        if params.temperature is not None:
            payload["temperature"] = params.temperature
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post(path, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(self.name, "request timed out") from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailable(self.name, f"transport error: {type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise classify_http_status(
                self.name, resp.status_code, resp.text, resp.headers.get("retry-after")
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderResponseError(self.name, "non-JSON response") from exc
        if not isinstance(data, dict):
            raise ProviderResponseError(self.name, "unexpected response type")
        return data

    @staticmethod
    def _usage(raw: dict[str, Any] | None, messages: list[ChatMessage], completion: str) -> Usage:
        if raw and raw.get("prompt_tokens") is not None:
            return Usage(
                prompt_tokens=int(raw["prompt_tokens"]),
                completion_tokens=int(raw.get("completion_tokens") or 0),
            )
        return Usage(
            prompt_tokens=sum(estimate_tokens(m.content) for m in messages),
            completion_tokens=estimate_tokens(completion),
            estimated=True,
        )
