"""Provider-neutral contracts.

Business logic only ever sees these types. Each adapter translates its vendor's wire
format into them and its vendor's failures into the ``ProviderError`` hierarchy, which is
what the gateway's retry and fallback decisions are based on.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant"]
InputType = Literal["query", "passage"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # True when the provider did not report usage and we estimated it locally.
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class ChatParams:
    # None = do not send a sampling setting (some models reject any temperature value).
    temperature: float | None = 0.2
    max_tokens: int = 1024


@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str
    usage: Usage
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class StreamDelta:
    """One streamed text fragment."""

    text: str


@dataclass(frozen=True, slots=True)
class StreamEnd:
    """Always the last item of a successful stream."""

    usage: Usage
    finish_reason: str | None = None


StreamItem = StreamDelta | StreamEnd


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: list[list[float]]
    usage: Usage = field(default_factory=Usage)


# --------------------------------------------------------------------------- errors


class ProviderError(Exception):
    """Base for all provider failures. ``retryable`` drives the gateway's retry loop."""

    retryable: bool = False

    def __init__(self, provider: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.status_code = status_code


class ProviderTimeout(ProviderError):
    retryable = True


class ProviderUnavailable(ProviderError):
    """Connection failure or 5xx."""

    retryable = True


class ProviderRateLimited(ProviderError):
    retryable = True

    def __init__(self, provider: str, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(provider, message, status_code=429)
        self.retry_after_s = retry_after_s


class ProviderAuthError(ProviderError):
    """401/403: retrying cannot help, but a different provider might."""


class ProviderModelNotFound(ProviderError):
    """404: this provider does not serve the model. Retrying cannot help; fallback can."""


class ProviderBadRequest(ProviderError):
    """4xx caused by our request. Neither retry nor fallback is expected to help."""


class ProviderResponseError(ProviderError):
    """The provider answered, but not in a shape we can use."""


# --------------------------------------------------------------------------- interface


@runtime_checkable
class ChatProvider(Protocol):
    name: str

    async def chat(
        self, model: str, messages: list[ChatMessage], params: ChatParams
    ) -> ChatResult: ...

    def stream_chat(
        self, model: str, messages: list[ChatMessage], params: ChatParams
    ) -> AsyncGenerator[StreamItem, None]: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str

    async def embed(
        self, model: str, inputs: list[str], input_type: InputType
    ) -> EmbeddingResult: ...


def estimate_tokens(text: str) -> int:
    """Rough fallback when a provider omits usage (~4 characters per token for English)."""
    return max(1, (len(text) + 3) // 4) if text else 0


def classify_http_status(
    provider: str, status: int, body: str, retry_after: str | None = None
) -> ProviderError:
    """Map an HTTP error status to the error hierarchy. Body is truncated: it can be large
    and occasionally echoes request content."""
    snippet = body[:200]
    if status == 429:
        retry_s: float | None = None
        if retry_after:
            try:
                retry_s = float(retry_after)
            except ValueError:
                retry_s = None
        return ProviderRateLimited(provider, f"rate limited: {snippet}", retry_after_s=retry_s)
    if status in (401, 403):
        return ProviderAuthError(provider, f"auth failed ({status})", status_code=status)
    if status == 404:
        return ProviderModelNotFound(provider, f"not found: {snippet}", status_code=status)
    if status == 408 or status >= 500:
        return ProviderUnavailable(provider, f"HTTP {status}: {snippet}", status_code=status)
    return ProviderBadRequest(provider, f"HTTP {status}: {snippet}", status_code=status)
