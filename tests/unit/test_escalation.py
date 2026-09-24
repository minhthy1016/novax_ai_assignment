"""Answer escalation (D-33): runtime warning signs, which answer is shown, what the user is
told, and that the second call obeys the same egress rule as the first."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from opsassist.agent.service import (
    answer_from_knowledge,
    clarify_hint,
    escalation_reason,
    pick_answer,
)
from opsassist.auth import Principal
from opsassist.gateway.gateway import CallContext
from opsassist.knowledge.retrieval import RetrievalResult, RetrievedChunk
from opsassist.providers.base import ChatParams, Usage
from opsassist.rag import ABSTAIN, finalize

SOURCE = "API servers are patched one server at a time."


def chunk(content: str = SOURCE) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=1,
        table="chunks",
        doc_key="KB-ENG-004",
        version=1,
        title="Production Server Operations Handbook",
        department="engineering",
        classification="internal",
        locator="¶1",
        parent_id=("doc-4", 0),
        content=content,
        context=content,
        doc_updated_at="2026-08-30",
        similarity=0.8,
        fts_rank=None,
        score=0.1,
    )


def retrieval(chunks: list[RetrievedChunk]) -> RetrievalResult:
    return RetrievalResult(chunks, len(chunks), 0, 1.0, "mock/embed-768")


def principal(*permissions: str) -> Principal:
    return Principal(
        user_id="U001",
        name="Aisha Rahman",
        department="engineering",
        role="Engineer",
        permissions=frozenset(permissions),
    )


class FakeGateway:
    """Answers per model id, and records every call with its egress flag."""

    def __init__(self, answers: dict[str, str], down: set[str] | None = None) -> None:
        self.answers = answers
        self.down = down or set()
        self.calls: list[tuple[str | None, bool]] = []

    async def chat(self, model: str | None, messages: Any, params: Any, ctx: Any, *,
                   allow_egress: bool = True) -> Any:  # fmt: skip
        from opsassist.gateway.gateway import GatewayError

        self.calls.append((model, allow_egress))
        if model in self.down:
            raise GatewayError("all_providers_failed", "down", [])
        return SimpleNamespace(
            result=SimpleNamespace(
                content=self.answers[str(model)],
                usage=Usage(prompt_tokens=100, completion_tokens=10),
                finish_reason="stop",
            ),
            model=SimpleNamespace(id=model, provider="ollama"),
            cost_usd=Decimal(0),
            route=model,
            fallback_used=False,
            attempts=[f"attempt:{model}"],
        )


def run(gateway: FakeGateway, chunks: list[RetrievedChunk], **kwargs: Any) -> Any:
    return asyncio.run(
        answer_from_knowledge(
            "How many API servers may we patch at once?",
            [],
            retrieval(chunks),
            gateway=gateway,  # type: ignore[arg-type]
            ctx=CallContext("req-1"),
            model_choice="small",
            params=ChatParams(),
            escalation_model="large",
            **kwargs,
        )
    )


# ------------------------------------------------------------------ signals


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("API servers are patched one server at a time [1].", None),
        (ABSTAIN, None),  # measured: a larger model rescued 0 of 11 abstentions
        ("API servers are patched one server at a time.", "uncited"),  # E12's shape
        (
            "The number is not specified. API servers are patched one at a time [1].",  # K08
            "not_covered",
        ),
        ("The source does not mention Fridays [1].", "not_covered"),  # M02
    ],
)
def test_escalation_signals_are_read_from_the_answer_alone(text: str, reason: str | None) -> None:
    assert escalation_reason(finalize(text, [chunk()]), retrieved=True) == reason


def test_nothing_retrieved_is_never_escalated() -> None:
    # A larger model cannot answer from no sources; only the user can help.
    assert escalation_reason(finalize(ABSTAIN, []), retrieved=False) is None


def test_second_answer_must_clear_the_bar_the_first_missed() -> None:
    cited = finalize("One server at a time [1].", [chunk()])
    uncited = finalize("One server at a time.", [chunk()])
    declined = finalize(ABSTAIN, [chunk()])
    assert pick_answer(uncited, cited) == "second"
    assert pick_answer(cited, declined) == "first"
    assert pick_answer(uncited, uncited) == "first"
    assert pick_answer(uncited, declined) == "first"


@pytest.mark.security
def test_clarify_hint_names_only_departments_the_caller_may_write() -> None:
    hint = clarify_hint(principal("docs:engineering", "kb:write:engineering"))
    assert "upload" in hint and "(engineering)" in hint
    reader = clarify_hint(principal("docs:engineering"))
    assert "upload" not in reader and "document owner" in reader
    assert "hr" not in hint.lower().split() and "finance" not in hint.lower()


# ------------------------------------------------------------------ the flow


def test_a_good_first_answer_costs_one_call() -> None:
    gateway = FakeGateway({"small": "One server at a time [1]."})
    result = run(gateway, [chunk()])
    assert gateway.calls == [("small", True)] and result.escalation is None


def test_an_uncited_first_answer_is_replaced_by_a_cited_second_one() -> None:
    gateway = FakeGateway({"small": "One server at a time.", "large": "One at a time [1]."})
    result = run(gateway, [chunk()])
    assert result.text == "One at a time [1]." and result.model_id == "large"
    assert result.escalation == {
        "reason": "uncited",
        "first_model": "small",
        "model": "large",
        "outcome": "used",
    }
    # Both calls are paid for and both are in the attempt trail.
    assert result.usage.prompt_tokens == 200
    assert result.attempts == ["attempt:small", "attempt:large"]


def test_an_abstention_tells_the_user_what_to_do_without_a_second_call() -> None:
    gateway = FakeGateway({"small": ABSTAIN, "large": "Answer [1]."})
    result = run(gateway, [chunk()], principal=principal("kb:write:engineering"))
    assert gateway.calls == [("small", True)] and result.escalation is None
    assert result.answer.abstained and result.text.startswith(ABSTAIN)
    assert "upload" in result.text and "(engineering)" in result.text


def test_nothing_retrieved_asks_the_user_without_calling_any_model() -> None:
    gateway = FakeGateway({})
    result = run(gateway, [], principal=principal("docs:engineering"))
    assert gateway.calls == [] and result.answer.abstained
    assert "document owner" in result.text


def test_an_unavailable_larger_model_keeps_the_first_answer() -> None:
    gateway = FakeGateway({"small": "One server at a time."}, down={"large"})
    result = run(gateway, [chunk()])
    assert result.text == "One server at a time." and result.escalation["outcome"] == "unavailable"


@pytest.mark.security
def test_the_second_call_obeys_the_same_egress_rule() -> None:
    # Confidential context: the first call was barred from hosted providers, so is the second.
    gateway = FakeGateway({"small": "Answer.", "large": "Answer [1]."})
    run(gateway, [chunk()], allow_egress=False)
    assert gateway.calls == [("small", False), ("large", False)]
