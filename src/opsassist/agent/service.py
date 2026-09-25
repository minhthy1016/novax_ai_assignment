"""Answering services shared by the API and the agent graph.

Keeping these as plain functions (not graph nodes) means the security-relevant parts -
retrieval scope, egress control, citation validation - are unit-testable without a graph,
and the graph only decides *which* of them to call.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsassist.auth import Principal
from opsassist.config import Settings
from opsassist.gateway.gateway import CallContext, ChatOutcome, GatewayError, LLMGateway
from opsassist.knowledge.retrieval import RetrievalResult, retrieve
from opsassist.logging_setup import get_logger
from opsassist.policy.access import scope_for
from opsassist.providers.base import ChatMessage, ChatParams, Usage
from opsassist.rag import (
    GroundedAnswer,
    abstention,
    build_messages,
    finalize,
    gate_review_messages,
)
from opsassist.tools.registry import TOOLS

log = get_logger("opsassist.agent")

# Only unmistakable social openers take the small-talk path; anything that looks like a
# question goes to the knowledge path, where an unsupported answer is impossible.
_SMALL_TALK = re.compile(
    r"^\s*(hi|hello|hey|yo|good (morning|afternoon|evening)|thanks|thank you|thx|ok|okay|"
    r"bye|goodbye|cheers|chào|xin chào|cảm ơn)[\s!.,:;)-]*$",
    re.IGNORECASE,
)

CAPABILITIES = (
    "I answer from the company documents you are allowed to read, always with a citation, "
    "and I can check server status or open a support ticket. A VPN profile needs a separate "
    "approval before it is created."
)


@dataclass
class AnswerResult:
    """What the caller needs to persist and return, whichever path produced it."""

    text: str
    answer: GroundedAnswer
    retrieval: RetrievalResult | None = None
    outcome: ChatOutcome | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: Decimal = Decimal(0)
    route: str | None = None
    model_id: str | None = None
    provider: str | None = None
    fallback_used: bool = False
    finish_reason: str | None = None
    attempts: list[Any] = field(default_factory=list)
    error: GatewayError | None = None
    tool: dict[str, Any] | None = None


def small_talk(message: str) -> bool:
    return bool(_SMALL_TALK.match(message))


def greeting_reply(principal: Principal) -> AnswerResult:
    """Deterministic, model-free reply: a greeting must not cost a model call, and must not
    be able to state anything about company knowledge."""
    text = f"Hello {principal.name.split()[0]}. {CAPABILITIES}"
    return AnswerResult(text=text, answer=GroundedAnswer(text, [], False, 0), route="small_talk")


REFUSAL = (
    "I can't do that. I have no tool for deployments or for bypassing an approval, and I "
    "only act through tools that check your permissions first."
)


def refusal_reply() -> AnswerResult:
    return AnswerResult(text=REFUSAL, answer=GroundedAnswer(REFUSAL, [], False, 0), route="refuse")


async def run_retrieval(
    principal: Principal,
    question: str,
    *,
    factory: async_sessionmaker[AsyncSession],
    gateway: LLMGateway,
    settings: Settings,
    ctx: CallContext,
) -> RetrievalResult:
    return await retrieve(
        question, scope_for(principal), factory=factory, gateway=gateway, settings=settings, ctx=ctx
    )


def admitted_passages(reply: str, offered: int) -> list[int]:
    """1-based passage numbers the judge admitted. Anything unreadable admits nothing: the
    safe failure is the abstention the gate would have given anyway."""
    match = re.search(r"\{.*\}", reply, re.S)
    try:
        found = json.loads(match.group(0))["relevant"] if match else []
    except (ValueError, KeyError, TypeError):
        return []
    if not isinstance(found, list):
        return []
    numbers = {n for n in found if isinstance(n, int) and not isinstance(n, bool)}
    return sorted(n for n in numbers if 1 <= n <= offered)


async def review_near_misses(
    question: str,
    retrieval: RetrievalResult,
    *,
    gateway: LLMGateway,
    settings: Settings,
    ctx: CallContext,
    model_choice: str | None,
) -> RetrievalResult:
    """When the gate admitted nothing, a judge reads the sections just under the bar and
    decides which, if any, answer the question. Those become the context; the answering
    model is told they were admitted on review (``build_messages(reviewed=True)``).

    The near misses come from the same scoped query, so nothing outside the caller's access
    is ever shown to the judge, and they stay on the box when they are confidential.
    """
    if retrieval.chunks or not retrieval.near_misses:
        return retrieval
    offered = retrieval.near_misses
    rank = {"public": 0, "internal": 1, "confidential": 2}
    most = max((c.classification for c in offered), key=lambda c: rank.get(c, 2))
    try:
        outcome = await gateway.chat(
            settings.router_model or model_choice,
            gate_review_messages(question, offered),
            ChatParams(temperature=0.0, max_tokens=150),
            ctx,
            allow_egress=settings.allows_egress(most),
        )
        admitted = admitted_passages(outcome.result.content, len(offered))
    except GatewayError:
        admitted = []
    log.info(
        "gate_review",
        offered=[c.doc_key for c in offered],
        admitted=[offered[n - 1].doc_key for n in admitted],
        user=ctx.user_id,
    )
    return replace(
        retrieval,
        chunks=[offered[n - 1] for n in admitted],
        reviewed=len(offered),
        admitted_by_review=len(admitted),
    )


async def answer_from_knowledge(
    question: str,
    history: list[ChatMessage],
    retrieval: RetrievalResult,
    *,
    gateway: LLMGateway,
    ctx: CallContext,
    model_choice: str | None,
    params: ChatParams,
    allow_egress: bool = True,
) -> AnswerResult:
    """Grounded answer, or a fixed abstention when nothing relevant was retrieved."""
    if not retrieval.chunks:
        answer = abstention()
        return AnswerResult(text=answer.text, answer=answer, retrieval=retrieval, route="knowledge")
    messages = build_messages(
        question, retrieval.chunks, history, reviewed=retrieval.admitted_by_review > 0
    )
    try:
        outcome = await gateway.chat(model_choice, messages, params, ctx, allow_egress=allow_egress)
    except GatewayError as err:
        return AnswerResult(text="", answer=abstention(), retrieval=retrieval, error=err)
    answer = finalize(outcome.result.content, retrieval.chunks)
    return AnswerResult(
        text=answer.text,
        answer=answer,
        retrieval=retrieval,
        outcome=outcome,
        usage=outcome.result.usage,
        cost_usd=outcome.cost_usd,
        route=outcome.route,
        model_id=outcome.model.id,
        provider=outcome.model.provider,
        fallback_used=outcome.fallback_used,
        finish_reason=outcome.result.finish_reason,
        attempts=list(outcome.attempts),
    )


def describe_tool_result(tool: str, data: dict[str, Any]) -> str:
    """Tool results are rendered from the real data, never summarized by a model: an
    assistant must not be able to describe a success that did not happen.

    Every field is read defensively: a handler may legitimately return a smaller payload
    (a duplicate ticket has no severity), and a missing field must not fail the request.
    """
    match tool:
        case "get_server_status":
            parts = [
                f"{data.get('server_id')} ({data.get('environment')}, owned by "
                f"{data.get('owner_department')}) is {data.get('status')}.",
                f"Last checked {data.get('last_check')}.",
            ]
            if "cpu_pct" in data:
                cpu, mem = data.get("cpu_pct"), data.get("memory_pct")
                parts.append(
                    "CPU and memory are not reported for this server."
                    if cpu is None
                    else f"CPU {cpu}%, memory {mem}%."
                )
            else:
                parts.append(str(data.get("utilisation", "")))
            return " ".join(p for p in parts if p)
        case "create_support_ticket":
            title = data.get("title")
            named = f' "{title}"' if title else ""
            if data.get("duplicate"):
                return (
                    f"Ticket {data.get('ticket_id')}{named} already covers this; "
                    "it was not created twice."
                )
            severity = data.get("severity")
            suffix = f" with severity {severity}" if severity else ""
            lines = [f"Ticket {data.get('ticket_id')}{named} is open{suffix}."]
            if details := data.get("details"):
                lines.append(f"Details as recorded: {details}")
            return " ".join(lines)
        case "get_support_ticket":
            return (
                f"{data.get('ticket_id')} \u2014 {data.get('title')} "
                f"({data.get('severity')}, {data.get('status')}), raised by "
                f"{data.get('raised_by')} on {str(data.get('raised_at'))[:10]}.\n"
                f"Details as recorded: {data.get('details')}"
            )
        case "create_vpn_profile":
            return (
                f"VPN profile {data.get('profile_id')} created for {data.get('employee_id')}, "
                f"valid until {data.get('expires_at')}."
            )
        case "search_internal_docs":
            if not data.get("count"):
                return "I found nothing on that in the documents you may read."
            refs = ", ".join(str(h.get("ref")) for h in data.get("hits", []))
            return f"I found {data['count']} passage(s): {refs}."
    return "Done."


def tool_names() -> list[str]:
    return list(TOOLS)


def conversation_thread(conversation_id: uuid.UUID) -> str:
    """LangGraph thread id: one conversation is one durable thread."""
    return f"conversation:{conversation_id}"
