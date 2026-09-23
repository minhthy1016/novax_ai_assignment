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
``mock-embed-N``    Same, with N dimensions (``mock-embed-768`` matches the index).
==================  =====================================================================
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
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


_SOURCE_1 = re.compile(r'<source id="1"[^>]*>\n(.*?)\n</source>', re.DOTALL)
_SERVER = re.compile(r"\b([a-z]+-[a-z]+-\d+)\b")


def _route_decision(question: str) -> str:
    """Rule-based stand-in for the router model, so CI exercises the agent's tool path
    without any real model. Mirrors the routes a real classifier would choose."""
    q = question.lower()
    if any(w in q for w in ("skip approval", "deploy now", "bypass", "without approval")):
        return json.dumps({"route": "refuse", "tool": None, "arguments": None})
    if "vpn" in q:
        name = re.search(r"for ([A-Z][a-z]+ [A-Z][a-z]+)", question)
        user_id = re.search(r"\b(U\d{3})\b", question)
        args: dict[str, object] = {"employee_id": user_id.group(1)} if user_id else {}
        if not args and name:
            args = {"employee_name": name.group(1)}
        return json.dumps({"route": "tool", "tool": "create_vpn_profile", "arguments": args})
    if "ticket" in q:
        severity = next((s for s in ("critical", "high", "medium", "low") if s in q), "medium")
        return json.dumps(
            {
                "route": "tool",
                "tool": "create_support_ticket",
                "arguments": {
                    "title": question[:80],
                    "severity": severity,
                    "details": question[:500],
                },
            }
        )
    if server := _SERVER.search(q):
        return json.dumps(
            {
                "route": "tool",
                "tool": "get_server_status",
                "arguments": {"server_id": server.group(1)},
            }
        )
    return json.dumps({"route": "knowledge", "tool": None, "arguments": None})


def default_responder(messages: list[ChatMessage]) -> str:
    """Echo for plain chat; for grounded prompts, answer with the first sentence of
    source 1 and cite it - deterministic, so the citation pipeline is testable offline."""
    last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
    system = next((m.content for m in messages if m.role == "system"), "")
    if '"route"' in system:  # the router prompt
        return _route_decision(last_user)
    if m := _SOURCE_1.search(last_user):
        first_sentence = re.split(r"(?<=[.!?])\s", m.group(1).strip(), maxsplit=1)[0]
        return f"{html.unescape(first_sentence)} [1]"
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
        dim = self._embedding_dim
        if model.startswith("mock-embed-") and model.rsplit("-", 1)[1].isdigit():
            dim = int(model.rsplit("-", 1)[1])
        elif model != "mock-embed":
            await self._behave(model)
        vectors = [hashed_embedding(t, dim) for t in inputs]
        tokens = sum(estimate_tokens(t) for t in inputs)
        return EmbeddingResult(vectors=vectors, usage=Usage(prompt_tokens=tokens))

    @staticmethod
    def _usage(messages: list[ChatMessage], completion: str) -> Usage:
        prompt = sum(estimate_tokens(m.content) for m in messages)
        return Usage(prompt_tokens=prompt, completion_tokens=estimate_tokens(completion))


# Function words carry no topic signal; without this, every text "matches" every other.
_STOPWORDS = frozenset(
    (  # noqa: SIM905 - a word list reads better as one string
        "a an the and or but if of to in on at by for with from as is are was "
        "were be been being it its this that these those we you they he she i me "
        "my our your their what when where which who whom how why do does did "
        "done can could may might must shall should will would not no yes all any "
        "each some so than then there here about into over after before up down "
        "out also only just more most "
    ).split()
)


def hashed_embedding(text: str, dim: int) -> list[float]:
    """Signed feature hashing of lowercase content words (stopwords dropped, crude plural
    stripping), L2-normalised. Deterministic stand-in for a real embedding model."""
    vec = [0.0] * dim
    for raw in _WORD.findall(text.lower()):
        if raw in _STOPWORDS:
            continue
        word = raw[:-1] if len(raw) > 4 and raw.endswith("s") else raw
        h = int.from_bytes(hashlib.blake2b(word.encode(), digest_size=8).digest(), "big")
        vec[h % dim] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec
