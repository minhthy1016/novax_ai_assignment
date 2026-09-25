"""Conversation persistence. The history window sent to the model is in ``memory.py``.

Ownership is enforced here, not in the route: every lookup is scoped to the caller, and
a conversation owned by someone else is indistinguishable from one that does not exist
(both raise ``ConversationNotFound`` -> 404), so IDs cannot be probed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.db.models import Conversation, LLMUsage, Message


class ConversationNotFound(LookupError):
    pass


async def get_owned(
    session: AsyncSession, conversation_id: uuid.UUID, user_id: str
) -> Conversation:
    conv = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    if conv is None:
        raise ConversationNotFound(str(conversation_id))
    return conv


async def get_or_create(
    session: AsyncSession, conversation_id: uuid.UUID | None, user_id: str, first_message: str
) -> Conversation:
    if conversation_id is not None:
        return await get_owned(session, conversation_id, user_id)
    conv = Conversation(id=uuid.uuid4(), user_id=user_id, title=first_message[:80])
    session.add(conv)
    await session.flush()
    return conv


async def add_message(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    *,
    request_id: str,
    status: str = "complete",
    model_id: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    citations: list[dict[str, object]] | None = None,
) -> Message:
    msg = Message(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        role=role,
        content=content,
        status=status,
        model_id=model_id,
        request_id=request_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        citations=citations,
    )
    session.add(msg)
    await session.flush()
    return msg


@dataclass(frozen=True)
class UsageTotals:
    model_calls: int
    chat_calls: int
    embedding_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal


async def usage_totals(session: AsyncSession, conversation_id: uuid.UUID) -> UsageTotals:
    row = (
        await session.execute(
            select(
                func.count().filter(LLMUsage.outcome != "skipped"),
                func.count().filter(LLMUsage.outcome != "skipped", LLMUsage.kind == "chat"),
                func.count().filter(LLMUsage.outcome != "skipped", LLMUsage.kind == "embedding"),
                func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
                func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
                func.coalesce(func.sum(LLMUsage.cost_usd), 0),
            ).where(LLMUsage.conversation_id == conversation_id)
        )
    ).one()
    return UsageTotals(
        int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4]), Decimal(row[5])
    )
