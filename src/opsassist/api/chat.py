"""Chat, streaming chat and conversation retrieval."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

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
    ConversationResponse,
    ConversationUpdate,
    ConversationUsage,
    ErrorResponse,
    MessageOut,
    ModelRef,
)
from opsassist.auth import CurrentPrincipal
from opsassist.config import Settings
from opsassist.conversations import (
    ConversationNotFound,
    add_message,
    build_prompt,
    get_or_create,
    get_owned,
    history_window,
    usage_totals,
)
from opsassist.db.models import Message
from opsassist.gateway.catalog import UnknownModelError
from opsassist.gateway.gateway import (
    CallContext,
    GatewayError,
    LLMGateway,
    StreamCompleted,
    StreamFailed,
    StreamStarted,
)
from opsassist.logging_setup import get_logger
from opsassist.providers.base import ChatMessage, ChatParams, StreamDelta, Usage

router = APIRouter(prefix="/api", tags=["chat"])
log = get_logger("opsassist.chat")


@dataclass(frozen=True)
class Prepared:
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    prompt: list[ChatMessage]
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
    """Resolve the conversation and model choice, build the prompt, persist the user turn.

    Choosing a model (``body.model``) switches the conversation to it; omitting it keeps the
    conversation's current model. History is model-independent, so a switched-to model
    sees the whole conversation. The user message is committed before the model is called,
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
    return Prepared(conv.id, user_msg.id, build_prompt(history, body.message), choice)


async def _save_assistant(
    request: Request,
    conversation_id: uuid.UUID,
    content: str,
    *,
    status: str,
    model_id: str | None,
    usage: Usage | None,
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
            )
    return msg.id


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
    gateway: LLMGateway = request.app.state.gateway
    request_id = request_id_of(request)
    settings: Settings = request.app.state.settings
    try:
        # Validate the choice before persisting anything.
        gateway.catalog.check_choice(body.model, allow_any=settings.allow_any_model)
        prep = await _prepare(request, principal.user_id, body)
    except UnknownModelError as exc:
        return error_response(400, "unknown_model", str(exc), request_id)
    except ConversationNotFound:
        return error_response(404, "not_found", NOT_FOUND, request_id)
    conv_id = prep.conversation_id

    ctx = CallContext(request_id, principal.user_id, str(conv_id))
    params = ChatParams(temperature=body.temperature, max_tokens=body.max_tokens)
    try:
        outcome = await gateway.chat(prep.model_choice, prep.prompt, params, ctx)
    except GatewayError as err:
        await _save_assistant(request, conv_id, "", status="error", model_id=None, usage=None)
        return error_response(
            GATEWAY_STATUS[err.code], err.code, str(err), request_id, err.attempts
        )

    usage = outcome.result.usage
    message_id = await _save_assistant(
        request,
        conv_id,
        outcome.result.content,
        status="complete",
        model_id=outcome.model.id,
        usage=usage,
    )
    return ChatResponse(
        conversation_id=conv_id,
        message_id=message_id,
        content=outcome.result.content,
        model=ModelRef(id=outcome.model.id, provider=outcome.model.provider),
        route=outcome.route,
        fallback_used=outcome.fallback_used,
        finish_reason=outcome.result.finish_reason,
        usage=usage_out(usage, outcome.cost_usd),
        latency_ms=outcome.latency_ms,
        attempts=[AttemptOut.of(a) for a in outcome.attempts],
        request_id=request_id,
    )


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post("/chat/stream", responses={200: {"content": {"text/event-stream": {}}}})
async def chat_stream(request: Request, body: ChatRequest, principal: CurrentPrincipal) -> Any:
    """Server-sent events: ``meta`` -> ``model`` -> ``delta``* -> ``done`` | ``error``.

    Closing the connection cancels the upstream provider request; whatever was generated
    so far is stored with status ``partial`` and its usage is recorded as ``cancelled``.
    """
    gateway: LLMGateway = request.app.state.gateway
    settings: Settings = request.app.state.settings
    request_id = request_id_of(request)
    try:
        gateway.catalog.check_choice(body.model, allow_any=settings.allow_any_model)
        prep = await _prepare(request, principal.user_id, body)
    except UnknownModelError as exc:
        return error_response(400, "unknown_model", str(exc), request_id)
    except ConversationNotFound:
        return error_response(404, "not_found", NOT_FOUND, request_id)
    conv_id, user_msg_id, prompt = prep.conversation_id, prep.user_message_id, prep.prompt

    ctx = CallContext(request_id, principal.user_id, str(conv_id))
    params = ChatParams(temperature=body.temperature, max_tokens=body.max_tokens)

    async def events() -> AsyncIterator[str]:
        produced: list[str] = []
        model_id: str | None = None
        finished = False
        yield _sse(
            "meta",
            {
                "conversation_id": str(conv_id),
                "user_message_id": str(user_msg_id),
                "request_id": request_id,
            },
        )
        try:
            async with aclosing(
                gateway.stream_chat(prep.model_choice, prompt, params, ctx)
            ) as stream:
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
                            msg_id = await _save_assistant(
                                request,
                                conv_id,
                                "".join(produced),
                                status="complete",
                                model_id=event.model.id,
                                usage=event.usage,
                            )
                            yield _sse(
                                "done",
                                {
                                    "message_id": str(msg_id),
                                    "finish_reason": event.finish_reason,
                                    "usage": usage_out(event.usage, event.cost_usd).model_dump(
                                        mode="json"
                                    ),
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
                                    "request_id": request_id,
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
                    "stream_cancelled", conversation_id=str(conv_id), chars=len("".join(produced))
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
                request_id=m.request_id,
                created_at=m.created_at,
            )
            for m in messages
        ],
        usage=ConversationUsage(
            model_calls=totals.model_calls,
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
