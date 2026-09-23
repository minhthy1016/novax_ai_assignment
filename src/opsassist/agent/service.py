"""Answering services shared by the API and the agent graph.

Keeping these as plain functions (not graph nodes) means the security-relevant parts -
retrieval scope, egress control, citation validation - are unit-testable without a graph,
and the graph only decides *which* of them to call.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsassist.auth import Principal
from opsassist.config import Settings
from opsassist.gateway.gateway import CallContext, ChatOutcome, GatewayError, LLMGateway
from opsassist.knowledge.retrieval import RetrievalResult, retrieve
from opsassist.policy.access import scope_for
from opsassist.providers.base import ChatMessage, ChatParams, Usage
from opsassist.rag import GroundedAnswer, abstention, build_messages, finalize
from opsassist.tools.registry import TOOLS

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
    messages = build_messages(question, retrieval.chunks, history)
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
            if data.get("duplicate"):
                return (
                    f"Ticket {data.get('ticket_id')} already covers this; it was not created twice."
                )
            severity = data.get("severity")
            suffix = f" with severity {severity}" if severity else ""
            return f"Ticket {data.get('ticket_id')} is open{suffix}."
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
