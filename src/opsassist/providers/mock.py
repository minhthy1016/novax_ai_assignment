"""Deterministic mock provider.

Used for CI, reproducible evaluation runs and failure demos. Behaviour is selected by the
model name so the same API call can exercise every failure path without code changes:

==================  =====================================================================
``mock-echo``       Deterministic answer derived from the last user message.
``mock-slow``       Sleeps ``slow_seconds`` before answering (drives timeout handling).
``mock-flaky``      Fails with 503 for the first ``flaky_failures`` calls, then succeeds.
``mock-down``       Always fails with 503.
``mock-ratelimit``  Always fails with 429 and a ``Retry-After`` hint.
``mock-embed``      Feature-hashed bag-of-words embeddings: deterministic, and texts that
                    share words are close, so retrieval tests behave sensibly.
==================  =====================================================================
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections import Counter
from collections.abc import AsyncGenerator, Callable

from opsassist.providers.base import (
    ChatMessage,
    ChatParams,
    ChatResult,
    EmbeddingResult,
    InputType,
    ProviderModelNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
    StreamDelta,
    StreamEnd,
    StreamItem,
    Usage,
    estimate_tokens,
)

Responder = Callable[[list[ChatMessage]], str]
_WORD = re.compile(r"[a-z0-9]+")


def default_responder(messages: list[ChatMessage]) -> str:
    last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
    digest = hashlib.sha256(last_user.encode()).hexdigest()[:8]
    return f"[mock:{digest}] Received: {last_user[:200]}"


class MockProvider:
    name = "mock"

    def __init__(
        self,
        *,
        responder: Responder = default_responder,
        slow_seconds: float = 30.0,
        flaky_failures: int = 2,
        stream_delay_s: float = 0.0,
        embedding_dim: int = 256,
    ) -> None:
        self._responder = responder
        self._slow_seconds = slow_seconds
        self._flaky_failures = flaky_failures
        self._stream_delay_s = stream_delay_s
        self._embedding_dim = embedding_dim
        self.calls: Counter[str] = Counter()

    async def _behave(self, model: str) -> None:
        self.calls[model] += 1
        match model:
            case "mock-echo":
                return
            case "mock-slow":
                await asyncio.sleep(self._slow_seconds)
            case "mock-flaky":
                if self.calls[model] <= self._flaky_failures:
                    raise ProviderUnavailable(self.name, "injected 503", status_code=503)
            case "mock-down":
                raise ProviderUnavailable(self.name, "injected 503", status_code=503)
            case "mock-ratelimit":
                raise ProviderRateLimited(self.name, "injected 429", retry_after_s=0.05)
            case _:
                raise ProviderModelNotFound(self.name, f"unknown mock model {model!r}")

    async def chat(self, model: str, messages: list[ChatMessage], params: ChatParams) -> ChatResult:
        await self._behave(model)
        text = self._responder(messages)
        return ChatResult(content=text, usage=self._usage(messages, text), finish_reason="stop")

    async def stream_chat(
        self, model: str, messages: list[ChatMessage], params: ChatParams
    ) -> AsyncGenerator[StreamItem, None]:
        await self._behave(model)
        text = self._responder(messages)
        for i, word in enumerate(text.split(" ")):
            if self._stream_delay_s:
                await asyncio.sleep(self._stream_delay_s)
            yield StreamDelta(word if i == 0 else f" {word}")
        yield StreamEnd(usage=self._usage(messages, text), finish_reason="stop")

    async def embed(self, model: str, inputs: list[str], input_type: InputType) -> EmbeddingResult:
        if model != "mock-embed":
            await self._behave(model)
        vectors = [hashed_embedding(t, self._embedding_dim) for t in inputs]
        tokens = sum(estimate_tokens(t) for t in inputs)
        return EmbeddingResult(vectors=vectors, usage=Usage(prompt_tokens=tokens))

    @staticmethod
    def _usage(messages: list[ChatMessage], completion: str) -> Usage:
        prompt = sum(estimate_tokens(m.content) for m in messages)
        return Usage(prompt_tokens=prompt, completion_tokens=estimate_tokens(completion))


def hashed_embedding(text: str, dim: int) -> list[float]:
    """Signed feature hashing of lowercase word tokens, L2-normalised."""
    vec = [0.0] * dim
    for word in _WORD.findall(text.lower()):
        h = int.from_bytes(hashlib.blake2b(word.encode(), digest_size=8).digest(), "big")
        vec[h % dim] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec
