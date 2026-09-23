"""Sensitive actions: list, approve, reject.

Approval is an explicit, resumable confirmation of an exact action:

* the approver must hold the approve permission and be a **different person** than the
  requester (enforced in `tools/executor.py`, not here);
* the request body carries the **action hash**, so an approver can only confirm the
  arguments that were actually proposed;
* after execution the requester's conversation is **resumed** from the point where it paused
  (LangGraph thread checkpointed in Postgres), and the final answer is stored there.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from langgraph.types import Command
from sqlalchemy import or_, select

from opsassist.agent.service import conversation_thread, describe_tool_result
from opsassist.api.common import request_id_of
from opsassist.api.schemas import (
    ApproveRequest,
    ErrorResponse,
    PendingActionOut,
    RejectRequest,
    ToolOut,
)
from opsassist.auth import CurrentPrincipal
from opsassist.conversations import add_message
from opsassist.db.models import PendingAction
from opsassist.logging_setup import get_logger
from opsassist.tools import executor

router = APIRouter(prefix="/api/actions", tags=["actions"])
log = get_logger("opsassist.actions")


def _context(request: Request, principal_id: str) -> executor.ToolContext:
    return executor.ToolContext(
        request_id=request_id_of(request),
        factory=request.app.state.session_factory,
        gateway=request.app.state.gateway,
        settings=request.app.state.settings,
    )


def _out(row: PendingAction) -> PendingActionOut:
    return PendingActionOut(
        id=row.id,
        tool=row.tool,
        summary=row.summary,
        arguments=row.arguments,
        action_hash=row.action_hash,
        status=row.status,
        requester_id=row.requester_id,
        approver_id=row.approver_id,
        approve_permission=row.approve_permission,
        created_at=row.created_at,
        expires_at=row.expires_at,
        result=row.result,
    )


@router.get("", response_model=list[PendingActionOut])
async def list_actions(
    request: Request, principal: CurrentPrincipal, status: str = "pending"
) -> Any:
    """Actions you requested, plus those you are allowed to approve - nothing else."""
    approvable = [
        p for p in ("vpn:approve",) if principal.has(p)
    ]  # extend as sensitive tools are added
    async with request.app.state.session_factory() as session:
        rows = (
            await session.scalars(
                select(PendingAction)
                .where(
                    PendingAction.status == status,
                    or_(
                        PendingAction.requester_id == principal.user_id,
                        PendingAction.approve_permission.in_(approvable or [""]),
                    ),
                )
                .order_by(PendingAction.created_at.desc())
                .limit(50)
            )
        ).all()
    return [_out(row) for row in rows]


async def _resume_conversation(
    request: Request, pending_id: uuid.UUID, payload: dict[str, Any]
) -> None:
    """Continue the requester's paused conversation and store the final answer there."""
    agent = request.app.state.agent
    async with request.app.state.session_factory() as session:
        pending = await session.get(PendingAction, pending_id)
    if pending is None or pending.conversation_id is None or agent is None:
        return
    config = {"configurable": {"thread_id": conversation_thread(pending.conversation_id)}}
    try:
        await agent.ainvoke(Command(resume=payload), config=config)
    except Exception:  # the approval already happened; resuming is best-effort
        log.exception("conversation_resume_failed", pending_action_id=str(pending_id))
        return
    async with request.app.state.session_factory() as session, session.begin():
        await add_message(
            session,
            pending.conversation_id,
            "assistant",
            payload["message"],
            request_id=request_id_of(request),
            citations=[],
        )


@router.post(
    "/{action_id}/approve",
    response_model=ToolOut,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def approve(
    request: Request, action_id: uuid.UUID, body: ApproveRequest, principal: CurrentPrincipal
) -> Any:
    outcome = await executor.approve_pending(
        principal, action_id, body.action_hash, _context(request, principal.user_id)
    )
    message = (
        describe_tool_result(outcome.tool, outcome.data)
        if outcome.executed and outcome.data
        else outcome.message
    )
    if outcome.executed:
        await _resume_conversation(
            request,
            action_id,
            {"status": "ok", "message": message, "data": outcome.data},
        )
    return ToolOut(
        name=outcome.tool,
        status=outcome.status,
        message=message,
        data=outcome.data,
        pending_action_id=outcome.pending_action_id,
        action_hash=outcome.action_hash,
    )


@router.post("/{action_id}/reject", response_model=ToolOut)
async def reject(
    request: Request, action_id: uuid.UUID, body: RejectRequest, principal: CurrentPrincipal
) -> Any:
    outcome = await executor.reject_pending(
        principal, action_id, body.reason, _context(request, principal.user_id)
    )
    if outcome.status == "denied" and outcome.message == "Action rejected.":
        await _resume_conversation(
            request, action_id, {"status": "rejected", "message": "The request was rejected."}
        )
    return ToolOut(name=outcome.tool, status=outcome.status, message=outcome.message)
