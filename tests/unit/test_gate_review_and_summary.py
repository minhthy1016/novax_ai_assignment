"""Gate review and conversation memory - no services needed.

Gate review: when the relevance gate admits nothing, the sections just under the bar go to a
judge before the assistant abstains, so a relevant passage is never dropped by a threshold
alone. Conversation memory: the rolling summary of older turns reaches the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from opsassist.agent.service import admitted_passages, review_near_misses
from opsassist.config import Settings
from opsassist.gateway.gateway import CallContext, GatewayError
from opsassist.knowledge.retrieval import RetrievalResult, RetrievedChunk
from opsassist.memory import load_conversation_memory
from opsassist.providers.base import ChatMessage
from opsassist.rag import REVIEWED_NOTE, build_messages


def chunk(doc_key: str, classification: str = "internal") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=hash(doc_key) % 1000,
        table="chunks",
        doc_key=doc_key,
        version=1,
        title=doc_key,
        department="it",
        classification=classification,
        locator="¶1",
        parent_id=(doc_key, 0),
        content="c",
        context="c",
        doc_updated_at="2026-01-01",
        similarity=0.55,
        fts_rank=None,
        score=0.1,
    )


def gated(*near: RetrievedChunk, kept: list[RetrievedChunk] | None = None) -> RetrievalResult:
    return RetrievalResult(
        chunks=kept or [],
        candidates=5,
        below_threshold=5,
        latency_ms=1.0,
        embedding_model="m",
        near_misses=list(near),
    )


@dataclass
class Judge:
    """A gateway stand-in: replies with a fixed text, or fails, and records each call."""

    reply: str = '{"relevant": [], "reason": "none"}'
    fail: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def chat(
        self,
        route: str | None,
        messages: list[ChatMessage],
        params: Any,
        ctx: CallContext,
        *,
        allow_egress: bool = True,
    ) -> Any:
        self.calls.append({"route": route, "messages": messages, "allow_egress": allow_egress})
        if self.fail:
            raise GatewayError("provider_unavailable", "down", [])
        return SimpleNamespace(result=SimpleNamespace(content=self.reply))


async def review(judge: Judge, retrieval: RetrievalResult) -> RetrievalResult:
    return await review_near_misses(
        "q",
        retrieval,
        gateway=judge,  # type: ignore[arg-type]
        settings=Settings(env="test"),
        ctx=CallContext(request_id="r", user_id="U001"),
        model_choice=None,
    )


# ------------------------------------------------------------------ gate review


def test_the_judge_reply_admits_only_passages_that_were_offered() -> None:
    assert admitted_passages('{"relevant": [2, 1], "reason": "x"}', 3) == [1, 2]
    assert admitted_passages('Sure: {"relevant": [1, 9, "2", true]}', 3) == [1]
    # Unreadable is the gate's own answer: admit nothing.
    for reply in ("yes", "{relevant: [1]}", '{"relevant": 1}', '{"reason": "x"}', ""):
        assert admitted_passages(reply, 3) == []


async def test_admitted_near_misses_become_the_context() -> None:
    judge = Judge('{"relevant": [2], "reason": "states the limit"}')
    out = await review(judge, gated(chunk("KB-A"), chunk("KB-B")))
    assert [c.doc_key for c in out.chunks] == ["KB-B"]
    assert (out.reviewed, out.admitted_by_review) == (2, 1)
    assert judge.calls[0]["route"] == "ollama/llama3.2-3b"  # the fast local router model


async def test_nothing_admitted_or_no_judge_keeps_the_abstention() -> None:
    for judge in (Judge(), Judge(fail=True), Judge("not json")):
        out = await review(judge, gated(chunk("KB-A")))
        assert out.chunks == [] and out.admitted_by_review == 0 and out.reviewed == 1


async def test_the_judge_is_not_called_when_the_gate_already_decided() -> None:
    judge = Judge()
    await review(judge, gated(kept=[chunk("KB-A")]))  # passages already admitted
    await review(judge, gated())  # nothing even close to the bar
    assert judge.calls == []


@pytest.mark.security
async def test_confidential_near_misses_never_leave_the_box() -> None:
    judge = Judge()
    await review(judge, gated(chunk("KB-A"), chunk("KB-HR-9", "confidential")))
    await review(judge, gated(chunk("KB-A", "public")))
    assert [c["allow_egress"] for c in judge.calls] == [False, True]


def test_the_answering_model_is_told_when_sources_were_admitted_on_review() -> None:
    plain = build_messages("q", [chunk("KB-A")], [])
    reviewed = build_messages("q", [chunk("KB-A")], [], reviewed=True)
    assert all(m.content != REVIEWED_NOTE for m in plain)
    assert reviewed[-2].content == REVIEWED_NOTE and reviewed[-1].role == "user"


# ------------------------------------------------------------------ conversation memory


class Rows:
    """An AsyncSession stand-in that returns the given messages, newest first."""

    def __init__(self, *contents: str) -> None:
        self.rows = [SimpleNamespace(role="user", content=c) for c in reversed(contents)]

    async def scalars(self, query: Any) -> Any:
        return SimpleNamespace(all=lambda: self.rows)


def conversation(summary: str | None) -> Any:
    return SimpleNamespace(id="c", summary=summary, summary_upto_seq=4 if summary else None)


async def test_the_summary_of_older_turns_reaches_the_model() -> None:
    memory = await load_conversation_memory(
        Rows("recent question"), conversation("User asked about VPN expiry."), 20, 3000
    )  # type: ignore[arg-type]
    messages = memory.as_messages()
    assert messages[0].role == "system" and "VPN expiry" in messages[0].content
    assert "data, not instructions" in messages[0].content
    assert messages[1].content == "recent question"


async def test_the_window_keeps_the_newest_turns_within_the_budget() -> None:
    memory = await load_conversation_memory(
        Rows("a" * 400, "b" * 40, "c" * 40), conversation(None), 20, 25
    )  # type: ignore[arg-type]
    assert [m.content[0] for m in memory.as_messages()] == ["b", "c"]
    empty = await load_conversation_memory(Rows("x"), conversation("s"), 0, 3000)  # type: ignore[arg-type]
    assert empty.window == [] and empty.summary == "s"
