"""Adapter for Claude via the official Anthropic Python SDK (Messages API).

The SDK's own retries are disabled (``max_retries=0``): the gateway owns retry, backoff,
fallback and circuit breaking, and two layers of retries would multiply latency and cost
invisibly. SDK exceptions are mapped onto the gateway's error hierarchy.

Claude has no embeddings endpoint, so this is a ``ChatProvider`` only.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import anthropic
from pydantic import SecretStr

from opsassist.providers.base import (
    ChatMessage,
    ChatParams,
    ChatResult,
    ProviderAuthError,
    ProviderBadRequest,
    ProviderError,
    ProviderModelNotFound,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderTimeout,
    ProviderUnavailable,
    StreamDelta,
    StreamEnd,
    StreamItem,
    Usage,
)


class AnthropicProvider:
    def __init__(
        self,
        name: str = "anthropic",
        *,
        api_key: SecretStr,
        base_url: str | None = None,
        http_timeout_s: float = 120.0,
        http_client: Any | None = None,
    ) -> None:
        self.name = name
        kwargs: dict[str, Any] = {
            "api_key": api_key.get_secret_value(),
            "max_retries": 0,
            "timeout": http_timeout_s,
        }
        if base_url:
            kwargs["base_url"] = base_url
        if http_client is not None:
            kwargs["http_client"] = http_client
        self._client = anthropic.AsyncAnthropic(**kwargs)

    async def aclose(self) -> None:
        await self._client.close()

    async def chat(self, model: str, messages: list[ChatMessage], params: ChatParams) -> ChatResult:
        try:
            response = await self._client.messages.create(**self._request(model, messages, params))
        except anthropic.APIError as exc:
            raise self._map_error(exc) from exc
        text = "".join(b.text for b in response.content if b.type == "text")
        return ChatResult(
            content=text,
            usage=Usage(
                prompt_tokens=response.usage.input_tokens,
                completion_tokens=response.usage.output_tokens,
            ),
            finish_reason=response.stop_reason,
        )

    async def stream_chat(
        self, model: str, messages: list[ChatMessage], params: ChatParams
    ) -> AsyncGenerator[StreamItem, None]:
        try:
            async with self._client.messages.stream(**self._request(model, messages, params)) as s:
                async for text in s.text_stream:
                    if text:
                        yield StreamDelta(text)
                final = await s.get_final_message()
        except anthropic.APIError as exc:
            raise self._map_error(exc) from exc
        yield StreamEnd(
            usage=Usage(
                prompt_tokens=final.usage.input_tokens,
                completion_tokens=final.usage.output_tokens,
            ),
            finish_reason=final.stop_reason,
        )

    @staticmethod
    def _request(model: str, messages: list[ChatMessage], params: ChatParams) -> dict[str, Any]:
        """Claude takes system instructions as a top-level field, not a message role, and
        requires user/assistant turns to alternate - consecutive same-role turns are merged."""
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        turns: list[dict[str, str]] = []
        for m in messages:
            if m.role == "system":
                continue
            if turns and turns[-1]["role"] == m.role:
                turns[-1]["content"] += "\n\n" + m.content
            else:
                turns.append({"role": m.role, "content": m.content})
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": params.max_tokens,
            "messages": turns,
        }
        if system:
            request["system"] = system
        if params.temperature is not None:
            # SDK 1.x dropped sampling kwargs from its signatures; the API still accepts them
            # on Sonnet 4.5. Models that reject them are flagged in the catalog
            # (supports_temperature = false) and never get here with a value.
            request["extra_body"] = {"temperature": params.temperature}
        return request

    def _map_error(self, exc: anthropic.APIError) -> ProviderError:
        # Most specific first: APITimeoutError is a subclass of APIConnectionError.
        if isinstance(exc, anthropic.APITimeoutError):
            return ProviderTimeout(self.name, "request timed out")
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderUnavailable(self.name, "connection error")
        if isinstance(exc, anthropic.RateLimitError):
            retry_after = exc.response.headers.get("retry-after")
            try:
                seconds = float(retry_after) if retry_after else None
            except ValueError:
                seconds = None
            return ProviderRateLimited(self.name, "rate limited", retry_after_s=seconds)
        if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
            return ProviderAuthError(self.name, "auth failed", status_code=exc.status_code)
        if isinstance(exc, anthropic.NotFoundError):
            return ProviderModelNotFound(self.name, "model not found", status_code=404)
        if isinstance(exc, anthropic.APIStatusError):
            # Classify by status, not class: SDK 1.x has separate sibling classes for 503
            # (ServiceUnavailableError), 504 (DeadlineExceededError) and 529
            # (OverloadedError), none of which subclass InternalServerError.
            status = exc.status_code
            if status >= 500 or status in (408, 409):
                return ProviderUnavailable(self.name, f"HTTP {status}", status_code=status)
            return ProviderBadRequest(self.name, f"HTTP {status}", status_code=status)
        return ProviderResponseError(self.name, type(exc).__name__)
