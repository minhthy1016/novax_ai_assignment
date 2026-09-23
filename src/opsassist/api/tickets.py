"""Reading tickets: the ones you raised, and the ones your department would work.

A ticket is operational data, not knowledge: it is never indexed and never retrievable as a
document, so the only ways to see one are this endpoint and the `get_support_ticket` tool.
Both apply the same rule, and it is a record-level rule rather than a permission: holding
`ticket:create` says you may raise tickets, not that you may read everyone's.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import or_, select

from opsassist.api.schemas import TicketOut
from opsassist.auth import CurrentPrincipal
from opsassist.db.models import Ticket, User

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


@router.get("", response_model=list[TicketOut])
async def my_tickets(
    request: Request, principal: CurrentPrincipal, limit: int = Query(default=20, ge=1, le=100)
) -> Any:
    if not principal.has("ticket:create"):
        return []
    async with request.app.state.session_factory() as session:
        rows = (
            await session.execute(
                select(Ticket, User.name)
                .join(User, User.id == Ticket.created_by)
                .where(
                    or_(
                        Ticket.created_by == principal.user_id,
                        User.department == principal.department,
                    )
                )
                .order_by(Ticket.created_at.desc())
                .limit(limit)
            )
        ).all()
    return [
        TicketOut(
            ticket_id=t.id,
            title=t.title,
            severity=t.severity,
            status=t.status,
            details=t.details,
            raised_by=t.created_by,
            raised_by_name=name,
            raised_at=t.created_at,
        )
        for t, name in rows
    ]
