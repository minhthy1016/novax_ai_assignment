"""Audit access: your own records, and chain verification.

The audit log is append-only (the runtime database role has no UPDATE or DELETE) and
hash-chained, so `verify` detects any edit, deletion or reordering. Users see only their own
records; there is no permission in the sample data for reading everyone's.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from opsassist.api.schemas import AuditRecordOut, AuditVerifyOut
from opsassist.auth import CurrentPrincipal
from opsassist.db.models import AuditLog
from opsassist.policy.audit import verify_chain

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/verify", response_model=AuditVerifyOut)
async def verify(request: Request, _principal: CurrentPrincipal) -> AuditVerifyOut:
    async with request.app.state.session_factory() as session:
        status = await verify_chain(session)
    return AuditVerifyOut(
        records=status.records,
        intact=status.intact,
        broken_at=status.broken_at,
        detail=status.detail,
    )


@router.get("", response_model=list[AuditRecordOut])
async def my_audit(
    request: Request, principal: CurrentPrincipal, limit: int = Query(default=20, ge=1, le=200)
) -> Any:
    async with request.app.state.session_factory() as session:
        rows = (
            await session.scalars(
                select(AuditLog)
                .where(AuditLog.actor_id == principal.user_id)
                .order_by(AuditLog.id.desc())
                .limit(limit)
            )
        ).all()
    return [
        AuditRecordOut(
            id=r.id,
            created_at=r.created_at,
            request_id=r.request_id,
            actor_id=r.actor_id,
            actor_role=r.actor_role,
            event=r.event,
            tool=r.tool,
            decision=r.decision,
            reason=r.reason,
            arguments=r.arguments,
            result=r.result,
            pending_action_id=r.pending_action_id,
            action_hash=r.action_hash,
            hash=r.hash,
        )
        for r in rows
    ]
