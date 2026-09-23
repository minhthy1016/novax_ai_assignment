"""Chat, streaming chat and conversation retrieval."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select

from opsassist.agent.service import (
    answer_from_knowledge,
    conversation_thread,
    describe_tool_result,
)
from opsassist.api.common import (
    GATEWAY_STATUS,
    NOT_FOUND,
    error_response,
    request_id_of,
    usage_out,
)
from opsassist.api.schemas import (
    AttemptOut,
    ChatRequest,
    ChatResponse,
    CitationOut,
    ConversationResponse,
    ConversationUpdate,
    ConversationUsage,
    ErrorResponse,
    MessageOut,
    ModelRef,
    RetrievalOut,
    ToolOut,
)
from opsassist.auth import CurrentPrincipal, Principal
from opsassist.config import Settings
from opsassist.conversations import (
    ConversationNotFound,
    add_message,
    get_or_create,
    get_owned,
    history_window,
    usage_totals,
)
from opsassist.db.models import Conversation, Message
from opsassist.gateway.catalog import UnknownModelError
from opsassist.gateway.gateway import (
    CallContext,
    GatewayError,
    LLMGateway,
    StreamCompleted,
    StreamFailed,
    StreamStarted,
)
from opsassist.knowledge.retrieval import RetrievalResult, retrieve
from opsassist.logging_setup import get_logger
from opsassist.memory import update_summary
from opsassist.policy.access import scope_for
from opsassist.providers.base import ChatMessage, ChatParams, StreamDelta, Usage
from opsassist.rag import GroundedAnswer, abstention, build_messages, finalize

router = APIRouter(prefix="/api", tags=["chat"])
log = get_logger("opsassist.chat")


@dataclass(frozen=True)
class Prepared:
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    question: str
    history: list[ChatMessage]
    model_choice: str | None  # None -> catalog default route


def _stored_choice(gateway: LLMGateway, settings: Settings, stored: str | None) -> str | None:
    """A stored preference may have been removed from the catalog since; fall back to the
    default rather than failing the user's next message."""
    if stored is None:
        return None
    try:
        gateway.catalog.check_choice(stored, allow_any=settings.allow_any_model)
    except UnknownModelError:
        return None
    return stored


async def _prepare(request: Request, principal_id: str, body: ChatRequest) -> Prepared:
    """Resolve the conversation and model choice, load history, persist the user turn.

    Choosing a model (``body.model``) switches the conversation to it; omitting it keeps the
    conversation's current model. History is model-independent, so a switched-to model
    sees the whole conversation. The user message is committed before anything else runs,
    so it survives a provider failure.
    """
    settings: Settings = request.app.state.settings
    gateway: LLMGateway = request.app.state.gateway
    async with request.app.state.session_factory() as session, session.begin():
        conv = await get_or_create(session, body.conversation_id, principal_id, body.message)
        conv.updated_at = func.now()
        if body.model is not None:
            conv.model_id = body.model
        choice = body.model or _stored_choice(gateway, settings, conv.model_id)
        history = await history_window(
            session, conv.id, settings.history_max_messages, settings.history_token_budget
        )
        user_msg = await add_message(
            session, conv.id, "user", body.message, request_id=request_id_of(request)
        )
    return Prepared(conv.id, user_msg.id, body.message, history, choice)


async def _retrieve(
    request: Request, principal: Principal, question: str, ctx: CallContext
) -> RetrievalResult:
    """Retrieval for the caller's access scope. Scope comes from database permissions only."""
    return await retrieve(
        question,
        scope_for(principal),
        factory=request.app.state.session_factory,
        gateway=request.app.state.gateway,
        settings=request.app.state.settings,
        ctx=ctx,
    )


def _retrieval_out(r: RetrievalResult) -> RetrievalOut:
    return RetrievalOut(
        candidates=r.candidates,
        used=len(r.chunks),
        below_threshold=r.below_threshold,
        latency_ms=r.latency_ms,
        embedding_model=r.embedding_model,
    )


def _citations_out(answer: GroundedAnswer) -> list[CitationOut]:
    return [
        CitationOut(
            number=c.number,
            doc_key=c.doc_key,
            version=c.version,
            title=c.title,
            locator=c.locator,
            ref=c.ref,
            label=c.label(),
            snippet=c.snippet,
        )
        for c in answer.citations
    ]


def _citations_json(answer: GroundedAnswer) -> list[dict[str, object]]:
    return [c.model_dump() for c in _citations_out(answer)]


async def _save_assistant(
    request: Request,
    conversation_id: uuid.UUID,
    content: str,
    *,
    status: str,
    model_id: str | None,
    usage: Usage | None,
    citations: list[dict[str, object]] | None = None,
) -> uuid.UUID:
    with anyio.CancelScope(shield=True):  # must persist even when the client disconnected
        async with request.app.state.session_factory() as session, session.begin():
            msg = await add_message(
                session,
                conversation_id,
                "assistant",
                content,
                request_id=request_id_of(request),
                status=status,
                model_id=model_id,
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
                citations=citations,
            )
    return msg.id


async def _start(
    request: Request, body: ChatRequest, principal: Principal
) -> tuple[Prepared, CallContext] | JSONResponse:
    gateway: LLMGateway = request.app.state.gateway
    settings: Settings = request.app.state.settings
    request_id = request_id_of(request)
    try:
        # Validate the choice before persisting anything.
        gateway.catalog.check_choice(body.model, allow_any=settings.allow_any_model)
        prep = await _prepare(request, principal.user_id, body)
    except UnknownModelError as exc:
        return error_response(400, "unknown_model", str(exc), request_id)
    except ConversationNotFound:
        return error_response(404, "not_found", NOT_FOUND, request_id)
    return prep, CallContext(request_id, principal.user_id, str(prep.conversation_id))


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses={
        400: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
        504: {"model": ErrorResponse},
    },
)
async def chat(request: Request, body: ChatRequest, principal: CurrentPrincipal) -> Any:
    """One turn through the agent: small talk, knowledge, a tool, or a refusal.

    The graph only chooses the route. Tools are authorized, validated and audited in the
    policy layer; the knowledge path answers strictly from what the caller may read, with a
    fixed abstention when nothing relevant is retrieved (no model call at all).
    """
    started = time.perf_counter()
    gateway: LLMGateway = request.app.state.gateway
    begun = await _start(request, body, principal)
    if isinstance(begun, JSONResponse):
        return begun
    prep, ctx = begun
    conv_id = prep.conversation_id
    params = ChatParams(temperature=body.temperature, max_tokens=body.max_tokens)

    decision = await _route(request, prep, principal, body)
    if decision["route"] in ("small_talk", "refuse"):
        text = str(decision["text"])
        message_id = await _save_assistant(
            request, conv_id, text, status="complete", model_id=None, usage=None, citations=[]
        )
        return _chat_response(conv_id, message_id, text, decision["route"], ctx.request_id, started)

    if decision["route"] == "tool":
        tool = ToolOut.model_validate(decision["tool"])
        message_id = await _save_assistant(
            request,
            conv_id,
            tool.message,
            status="complete",
            model_id=None,
            usage=None,
            citations=[],
        )
        return _chat_response(
            conv_id, message_id, tool.message, "tool", ctx.request_id, started, tool=tool
        )

    # ---------------------------------------------------------------- knowledge path
    try:
        retrieval = await _retrieve(request, principal, prep.question, ctx)
    except GatewayError as err:
        await _save_assistant(request, conv_id, "", status="error", model_id=None, usage=None)
        return error_response(
            503,
            "retrieval_unavailable",
            "knowledge search is unavailable",
            ctx.request_id,
            err.attempts,
        )

    result = await answer_from_knowledge(
        prep.question,
        prep.history,
        retrieval,
        gateway=gateway,
        ctx=ctx,
        model_choice=prep.model_choice,
        params=params,
        allow_egress=request.app.state.settings.allows_egress(retrieval.max_classification),
    )
    if result.error is not None:
        await _save_assistant(request, conv_id, "", status="error", model_id=None, usage=None)
        failure = result.error
        return error_response(
            GATEWAY_STATUS[failure.code],
            failure.code,
            str(failure),
            ctx.request_id,
            failure.attempts,
        )

    answer = result.answer
    message_id = await _save_assistant(
        request,
        conv_id,
        answer.text,
        status="complete",
        model_id=result.model_id,
        usage=result.usage if result.model_id else None,
        citations=_citations_json(answer),
    )
    await _maybe_summarize(request, prep, principal, ctx)
    return ChatResponse(
        conversation_id=conv_id,
        message_id=message_id,
        content=answer.text,
        route="knowledge",
        tool=None,
        citations=_citations_out(answer),
        grounded=answer.grounded,
        abstained=answer.abstained,
        repointed_citations=answer.repointed_citations,
        model=ModelRef(id=result.model_id, provider=result.provider or "")
        if result.model_id
        else None,
        model_route=result.route,
        fallback_used=result.fallback_used,
        finish_reason=result.finish_reason,
        usage=usage_out(result.usage, result.cost_usd),
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        retrieval=_retrieval_out(retrieval),
        attempts=[AttemptOut.of(a) for a in result.attempts],
        request_id=ctx.request_id,
    )


def _chat_response(
    conv_id: uuid.UUID,
    message_id: uuid.UUID,
    text: str,
    route: str,
    request_id: str,
    started: float,
    tool: ToolOut | None = None,
) -> ChatResponse:
    """Response for the paths that produce no citations and no grounded answer."""
    return ChatResponse(
        conversation_id=conv_id,
        message_id=message_id,
        content=text,
        route=route,
        tool=tool,
        citations=[],
        grounded=True,
        abstained=False,
        model=None,
        model_route=None,
        fallback_used=False,
        finish_reason=None,
        usage=usage_out(Usage(), Decimal(0)),
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
        retrieval=RetrievalOut(
            candidates=0, used=0, below_threshold=0, latency_ms=0.0, embedding_model="-"
        ),
        attempts=[],
        request_id=request_id,
    )


async def _route(
    request: Request, prep: Prepared, principal: Principal, body: ChatRequest
) -> dict[str, Any]:
    """Ask the agent graph which path this turn takes (and run the tool if that is it)."""
    agent = request.app.state.agent
    if agent is None:  # graph unavailable: behave like the day-3 knowledge-only assistant
        return {"route": "knowledge"}
    state = {
        "question": prep.question,
        "user_id": principal.user_id,
        "request_id": request_id_of(request),
        "conversation_id": str(prep.conversation_id),
        "model_choice": prep.model_choice,
    }
    config = {"configurable": {"thread_id": conversation_thread(prep.conversation_id)}}
    output = await agent.ainvoke(state, config=config)
    interrupts = output.get("__interrupt__")
    if interrupts:  # a sensitive action is waiting for an approver
        payload = interrupts[0].value
        return {
            "route": "tool",
            "tool": {
                "name": payload.get("tool", "create_vpn_profile"),
                "status": "pending",
                "message": payload["summary"],
                "data": None,
                "pending_action_id": payload["pending_action_id"],
                "action_hash": payload["action_hash"],
            },
        }
    result = output.get("result") or {}
    kind = result.get("kind", "knowledge")
    if kind in ("small_talk", "refuse"):
        return {"route": kind, "text": result["text"]}
    if kind == "tool":
        data = result.get("data")
        message = (
            describe_tool_result(result["tool"], data)
            if result.get("status") == "ok" and data
            else result.get("message", "")
        )
        return {
            "route": "tool",
            "tool": {
                "name": result.get("tool", ""),
                "status": result.get("status", "error"),
                "message": message,
                "data": data,
                "pending_action_id": result.get("pending_action_id"),
                "action_hash": result.get("action_hash"),
            },
        }
    return {"route": "knowledge"}


async def _maybe_summarize(
    request: Request, prep: Prepared, principal: Principal, ctx: CallContext
) -> None:
    """Fold older turns into the conversation summary once the window overflows."""
    gateway: LLMGateway = request.app.state.gateway
    async with request.app.state.session_factory() as session, session.begin():
        conv = await session.get(Conversation, prep.conversation_id)
        if conv is not None:
            await update_summary(session, conv, gateway, ctx, prep.model_choice)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/chat/stream", responses={200: {"content": {"text/event-stream": {}}}})
async def chat_stream(request: Request, body: ChatRequest, principal: CurrentPrincipal) -> Any:
    """Server-sent events: ``meta`` -> ``model`` -> ``delta``* -> ``done`` | ``error``.

    Same grounding as ``/api/chat``. Deltas are the raw model text; the ``done`` event
    carries the validated citations and the stored (cleaned) answer. Closing the connection
    cancels the upstream provider request; the partial answer is stored as ``partial``.
    """
    gateway: LLMGateway = request.app.state.gateway
    begun = await _start(request, body, principal)
    if isinstance(begun, JSONResponse):
        return begun
    prep, ctx = begun
    conv_id = prep.conversation_id
    params = ChatParams(temperature=body.temperature, max_tokens=body.max_tokens)

    async def events() -> AsyncIterator[str]:
        produced: list[str] = []
        model_id: str | None = None
        finished = False
        yield _sse(
            "meta",
            {
                "conversation_id": str(conv_id),
                "user_message_id": str(prep.user_message_id),
                "request_id": ctx.request_id,
            },
        )
        try:
            try:
                retrieval = await _retrieve(request, principal, prep.question, ctx)
            except GatewayError:
                finished = True
                await _save_assistant(
                    request, conv_id, "", status="error", model_id=None, usage=None
                )
                yield _sse(
                    "error",
                    {
                        "code": "retrieval_unavailable",
                        "message": "knowledge search is unavailable",
                        "partial": False,
                        "request_id": ctx.request_id,
                    },
                )
                return
            if not retrieval.chunks:
                finished = True
                answer = abstention()
                msg_id = await _save_assistant(
                    request,
                    conv_id,
                    answer.text,
                    status="complete",
                    model_id=None,
                    usage=None,
                    citations=[],
                )
                yield _sse("delta", {"text": answer.text})
                yield _sse(
                    "done",
                    {
                        "message_id": str(msg_id),
                        "content": answer.text,
                        "citations": [],
                        "grounded": True,
                        "abstained": True,
                        "retrieval": _retrieval_out(retrieval).model_dump(),
                    },
                )
                return
            messages = build_messages(prep.question, retrieval.chunks, prep.history)
            stream_events = gateway.stream_chat(
                prep.model_choice,
                messages,
                params,
                ctx,
                allow_egress=request.app.state.settings.allows_egress(retrieval.max_classification),
            )
            async with aclosing(stream_events) as stream:
                async for event in stream:
                    match event:
                        case StreamStarted():
                            model_id = event.model.id
                            yield _sse(
                                "model",
                                {
                                    "model": event.model.id,
                                    "provider": event.model.provider,
                                    "route": event.route,
                                    "fallback_used": event.fallback_used,
                                },
                            )
                        case StreamDelta():
                            produced.append(event.text)
                            yield _sse("delta", {"text": event.text})
                        case StreamCompleted():
                            finished = True
                            answer = finalize("".join(produced), retrieval.chunks)
                            msg_id = await _save_assistant(
                                request,
                                conv_id,
                                answer.text,
                                status="complete",
                                model_id=event.model.id,
                                usage=event.usage,
                                citations=_citations_json(answer),
                            )
                            yield _sse(
                                "done",
                                {
                                    "message_id": str(msg_id),
                                    "content": answer.text,
                                    "citations": _citations_json(answer),
                                    "grounded": answer.grounded,
                                    "abstained": answer.abstained,
                                    "invalid_citations": answer.invalid_citations,
                                    "repointed_citations": answer.repointed_citations,
                                    "finish_reason": event.finish_reason,
                                    "usage": usage_out(event.usage, event.cost_usd).model_dump(
                                        mode="json"
                                    ),
                                    "retrieval": _retrieval_out(retrieval).model_dump(),
                                    "latency_ms": event.latency_ms,
                                    "ttft_ms": event.ttft_ms,
                                    "attempts": [
                                        AttemptOut.of(a).model_dump() for a in event.attempts
                                    ],
                                },
                            )
                        case StreamFailed():
                            finished = True
                            await _save_assistant(
                                request,
                                conv_id,
                                "".join(produced),
                                status="partial" if event.started else "error",
                                model_id=model_id,
                                usage=None,
                            )
                            yield _sse(
                                "error",
                                {
                                    "code": event.code,
                                    "message": event.message,
                                    "partial": event.started,
                                    "request_id": ctx.request_id,
                                    "attempts": [
                                        AttemptOut.of(a).model_dump() for a in event.attempts
                                    ],
                                },
                            )
        finally:
            if not finished:
                # Client disconnected (or the server is shutting down) mid-stream.
                await _save_assistant(
                    request,
                    conv_id,
                    "".join(produced),
                    status="partial",
                    model_id=model_id,
                    usage=None,
                )
                log.info(
                    "stream_cancelled",
                    conversation_id=str(conv_id),
                    chars=len("".join(produced)),
                )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _conversation_view(
    request: Request, conversation_id: uuid.UUID, user_id: str
) -> ConversationResponse:
    async with request.app.state.session_factory() as session:
        conv = await get_owned(session, conversation_id, user_id)
        messages = (
            await session.scalars(
                select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq)
            )
        ).all()
        totals = await usage_totals(session, conv.id)
    return ConversationResponse(
        id=conv.id,
        title=conv.title,
        model=conv.model_id,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        messages=[
            MessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                status=m.status,
                model=m.model_id,
                prompt_tokens=m.prompt_tokens,
                completion_tokens=m.completion_tokens,
                citations=m.citations,
                request_id=m.request_id,
                created_at=m.created_at,
            )
            for m in messages
        ],
        usage=ConversationUsage(
            model_calls=totals.model_calls,
            chat_calls=totals.chat_calls,
            embedding_calls=totals.embedding_calls,
            prompt_tokens=totals.prompt_tokens,
            completion_tokens=totals.completion_tokens,
            cost_usd=totals.cost_usd,
        ),
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
    responses={404: {"model": ErrorResponse}},
)
async def get_conversation(
    request: Request, conversation_id: uuid.UUID, principal: CurrentPrincipal
) -> Any:
    try:
        return await _conversation_view(request, conversation_id, principal.user_id)
    except ConversationNotFound:
        return error_response(404, "not_found", NOT_FOUND, request_id_of(request))


@router.patch(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def switch_conversation_model(
    request: Request,
    conversation_id: uuid.UUID,
    body: ConversationUpdate,
    principal: CurrentPrincipal,
) -> Any:
    """Switch the conversation's model; the next message uses it with the full history."""
    gateway: LLMGateway = request.app.state.gateway
    settings: Settings = request.app.state.settings
    request_id = request_id_of(request)
    try:
        gateway.catalog.check_choice(body.model, allow_any=settings.allow_any_model)
    except UnknownModelError as exc:
        return error_response(400, "unknown_model", str(exc), request_id)
    try:
        async with request.app.state.session_factory() as session, session.begin():
            conv = await get_owned(session, conversation_id, principal.user_id)
            conv.model_id = body.model
            conv.updated_at = func.now()
        return await _conversation_view(request, conversation_id, principal.user_id)
    except ConversationNotFound:
        return error_response(404, "not_found", NOT_FOUND, request_id)
