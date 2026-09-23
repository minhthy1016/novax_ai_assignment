"""Authorize, execute and audit tool calls.

Order of operations for every call (the model influences only the first step):

1. The proposed tool name must exist in the registry.
2. Arguments are validated against the tool's schema (unknown fields rejected).
3. The caller's permission is checked against the database, never against the token or
   anything the model said.
4. A **sensitive** tool is not executed: it becomes a pending action with a hash of the exact
   arguments, waiting for a *different* holder of the approve permission.
5. Everything else executes, and the result is written together with its audit record in the
   same transaction - a tool result can never exist without its audit row.

Every outcome, including denials and invalid arguments, is appended to the hash-chained
audit log.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsassist.auth import Principal
from opsassist.config import Settings
from opsassist.db.models import PendingAction, Server, Ticket, User, VpnProfile
from opsassist.gateway.gateway import CallContext, LLMGateway
from opsassist.knowledge.retrieval import retrieve
from opsassist.logging_setup import get_logger
from opsassist.policy import audit
from opsassist.policy.access import scope_for
from opsassist.tools.registry import TOOLS, ToolArgs, ToolSpec

log = get_logger("opsassist.tools")
Status = Literal["ok", "denied", "pending", "error"]
PENDING_TTL = timedelta(hours=24)
TICKET_IDEMPOTENCY_WINDOW = timedelta(minutes=10)


@dataclass(frozen=True)
class ToolContext:
    request_id: str
    factory: async_sessionmaker[AsyncSession]
    gateway: LLMGateway
    settings: Settings
    conversation_id: uuid.UUID | None = None
    # What the person actually typed. A tool that records free text (a ticket) stores it
    # verbatim next to the model's wording, so nobody has to trust that the summary is
    # faithful - the request is right there to compare against.
    user_message: str | None = None


@dataclass(frozen=True)
class ToolOutcome:
    status: Status
    tool: str
    message: str
    data: dict[str, Any] | None = None
    pending_action_id: uuid.UUID | None = None
    action_hash: str | None = None

    @property
    def executed(self) -> bool:
        return self.status == "ok"


def action_hash(tool: str, args: dict[str, Any], requester_id: str) -> str:
    """Pins the exact action an approver confirms."""
    payload = json.dumps(
        {"tool": tool, "arguments": args, "requester": requester_id},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ------------------------------------------------------------------ handlers


async def _search_internal_docs(
    session: AsyncSession, principal: Principal, args: Any, ctx: ToolContext
) -> dict[str, Any]:
    """Retrieval, scoped exactly like /api/chat: a tool cannot widen access."""
    result = await retrieve(
        args.query,
        scope_for(principal),
        factory=ctx.factory,
        gateway=ctx.gateway,
        settings=ctx.settings,
        ctx=CallContext(ctx.request_id, principal.user_id),
    )
    hits = [
        {"title": c.title, "ref": c.stable_ref, "department": c.department, "text": c.context}
        for c in result.chunks
        if args.department is None or c.department == args.department
    ]
    return {"hits": hits, "count": len(hits)}


def visible_server_fields(principal: Principal, server: Server) -> dict[str, Any]:
    """Field-level policy (D-30): `server:read` shows identity, environment, status and the
    last check for every server; **utilisation is only visible to the owning department and
    IT Operations**, because CPU/memory of another team's service is capacity information
    they own."""
    fields: dict[str, Any] = {
        "server_id": server.id,
        "environment": server.environment,
        "owner_department": server.owner_department,
        "status": server.status,
        "last_check": server.last_check.isoformat(),
    }
    if principal.department in (server.owner_department, "it_ops"):
        fields["cpu_pct"] = float(server.cpu_pct) if server.cpu_pct is not None else None
        fields["memory_pct"] = float(server.memory_pct) if server.memory_pct is not None else None
    else:
        fields["utilisation"] = "hidden: visible to the owning department and IT Operations"
    return fields


async def _get_server_status(
    session: AsyncSession, principal: Principal, args: Any, ctx: ToolContext
) -> dict[str, Any]:
    server = await session.get(Server, args.server_id)
    if server is None:
        raise ToolError(f"no server with id {args.server_id!r}")
    return visible_server_fields(principal, server)


def with_provenance(details: str, request: str | None) -> str:
    """Keep the requester's own words with the ticket.

    The model writes `details` from the conversation, and it will happily flesh out a
    one-line request into a paragraph that reads like fact. The person who works the ticket
    must be able to see what was actually asked, so the verbatim request is appended and
    never replaced by a summary.
    """
    request = (request or "").strip()
    if not request or request.lower() in details.lower():
        return details
    return f'{details}\n\nRaised from: "{request}"'


async def _create_support_ticket(
    session: AsyncSession, principal: Principal, args: Any, ctx: ToolContext
) -> dict[str, Any]:
    key_source = f"{principal.user_id}|{args.title}|{args.severity}|{args.details}"
    key = hashlib.sha256(key_source.encode()).hexdigest()
    existing = await session.scalar(select(Ticket).where(Ticket.idempotency_key == key))
    if existing is not None:
        if datetime.now(UTC) - existing.created_at < TICKET_IDEMPOTENCY_WINDOW:
            return {"ticket_id": existing.id, "status": existing.status, "duplicate": True}
        key = f"{key}:{uuid.uuid4().hex[:8]}"  # same request later is a new ticket
    number = await session.scalar(select(func.nextval("ticket_number_seq")))
    ticket = Ticket(
        id=f"INC-{number}",
        title=args.title,
        severity=args.severity,
        details=with_provenance(args.details, ctx.user_message),
        created_by=principal.user_id,
        idempotency_key=key,
    )
    session.add(ticket)
    await session.flush()
    # Echo back what was actually stored, not what was asked for: the caller (and the
    # console) should see the ticket as it exists, including the title it can be found by.
    return {
        "ticket_id": ticket.id,
        "title": ticket.title,
        "severity": ticket.severity,
        "status": ticket.status,
        "details": ticket.details,
    }


async def _get_support_ticket(
    session: AsyncSession, principal: Principal, args: Any, ctx: ToolContext
) -> dict[str, Any]:
    """Read one ticket.

    Visibility is a record-level rule, so it cannot live in the permission check: holding
    `ticket:create` says you may raise tickets, not that you may read everyone's. A ticket is
    visible to the person who raised it and to their department - the people who would work
    it - and to nobody else.
    """
    ticket = await session.get(Ticket, args.ticket_id)
    if ticket is None:
        raise ToolError(f"no ticket with id {args.ticket_id!r}")
    raiser = await session.get(User, ticket.created_by)
    own = ticket.created_by == principal.user_id
    same_team = raiser is not None and raiser.department == principal.department
    if not (own or same_team):
        raise ToolDenied(f"{args.ticket_id} belongs to another department")
    return {
        "ticket_id": ticket.id,
        "title": ticket.title,
        "severity": ticket.severity,
        "status": ticket.status,
        "details": ticket.details,
        "raised_by": ticket.created_by,
        "raised_at": ticket.created_at.isoformat(),
    }


async def resolve_employee(session: AsyncSession, args: Any) -> User:
    if args.employee_id:
        user = await session.get(User, args.employee_id)
        if user is None:
            raise ToolError(f"no employee with id {args.employee_id!r}")
        return user
    name = args.employee_name.strip().lower()
    matches = (
        await session.scalars(select(User).where(func.lower(User.name).like(f"%{name}%")))
    ).all()
    if not matches:
        raise ToolError(f"no employee named {args.employee_name!r}")
    if len(matches) > 1:
        raise ToolError(
            f"{args.employee_name!r} matches {len(matches)} employees; ask for the employee id"
        )
    return matches[0]


async def _create_vpn_profile(
    session: AsyncSession, principal: Principal, args: Any, ctx: ToolContext
) -> dict[str, Any]:
    employee = await resolve_employee(session, args)
    number = await session.scalar(select(func.nextval("vpn_profile_seq")))
    profile = VpnProfile(
        id=f"VPN-{number:04d}",
        employee_id=employee.id,
        duration_days=args.duration_days,
        requested_by=principal.user_id,
        approved_by=principal.user_id,  # replaced by the approver in approve_pending
        pending_action_id=uuid.uuid4(),
        expires_at=datetime.now(UTC) + timedelta(days=args.duration_days),
    )
    session.add(profile)
    await session.flush()
    return {
        "profile_id": profile.id,
        "employee_id": employee.id,
        "expires_at": profile.expires_at.isoformat(),
    }


class ToolError(RuntimeError):
    """The tool could not do its job (bad id, missing record). Never a policy failure."""


class ToolDenied(RuntimeError):
    """A record-level policy refusal from inside a handler: the caller holds the permission
    but not for *this* record. Audited as a denial, like any other refusal."""


HANDLERS = {
    "search_internal_docs": _search_internal_docs,
    "get_server_status": _get_server_status,
    "create_support_ticket": _create_support_ticket,
    "get_support_ticket": _get_support_ticket,
    "create_vpn_profile": _create_vpn_profile,
}


# ------------------------------------------------------------------ authorization


def authorize(principal: Principal, spec: ToolSpec) -> str | None:
    """Returns a denial reason, or None when the caller may use the tool."""
    if spec.requires_permission and not principal.has(spec.requires_permission):
        return f"requires the {spec.requires_permission} permission"
    return None


def summarize(spec: ToolSpec, args: ToolArgs) -> str:
    if spec.name == "create_vpn_profile":
        who = getattr(args, "employee_id", None) or getattr(args, "employee_name", None)
        return f"Create a VPN profile for {who} valid for {args.duration_days} days"  # type: ignore[attr-defined]
    return f"Run {spec.name}"


async def run_tool(
    principal: Principal, tool: str, raw_args: dict[str, Any], ctx: ToolContext
) -> ToolOutcome:
    async with ctx.factory() as session, session.begin():

        def entry(**kw: Any) -> audit.AuditEntry:
            return audit.entry_of(
                principal, request_id=ctx.request_id, event="tool_call", tool=tool, **kw
            )

        spec = TOOLS.get(tool)
        if spec is None:
            await audit.append(session, entry(decision="deny", reason="unknown tool"))
            return ToolOutcome("denied", tool, f"{tool!r} is not an available tool")

        try:
            args = spec.args_model.model_validate(raw_args)
        except ValidationError as exc:
            reason = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            await audit.append(
                session,
                entry(decision="deny", arguments=raw_args, reason=f"invalid arguments: {reason}"),
            )
            return ToolOutcome("error", tool, f"invalid arguments for {tool}: {reason}")

        validated = args.model_dump(mode="json")
        if denial := authorize(principal, spec):
            await audit.append(session, entry(decision="deny", arguments=validated, reason=denial))
            log.info("tool_denied", tool=tool, user=principal.user_id, reason=denial)
            return ToolOutcome("denied", tool, f"You are not authorized: {denial}.")

        if spec.sensitive:
            digest = action_hash(tool, validated, principal.user_id)
            pending = PendingAction(
                id=uuid.uuid4(),
                conversation_id=ctx.conversation_id,
                requester_id=principal.user_id,
                tool=tool,
                arguments=validated,
                summary=summarize(spec, args),
                action_hash=digest,
                requires_permission=spec.requires_permission or "",
                approve_permission=spec.approve_permission or "",
                request_id=ctx.request_id,
                expires_at=datetime.now(UTC) + PENDING_TTL,
            )
            session.add(pending)
            await session.flush()
            await audit.append(
                session,
                entry(
                    decision="pending",
                    arguments=validated,
                    reason=f"awaiting approval by a different holder of {spec.approve_permission}",
                    pending_action_id=pending.id,
                    action_hash=digest,
                ),
            )
            return ToolOutcome(
                "pending",
                tool,
                f"{pending.summary}. This needs approval from someone with "
                f"{spec.approve_permission}, who must be a different person.",
                pending_action_id=pending.id,
                action_hash=digest,
            )

        try:
            result = await HANDLERS[tool](session, principal, args, ctx)
        except ToolDenied as exc:
            await audit.append(
                session, entry(decision="deny", arguments=validated, reason=str(exc))
            )
            log.info("tool_denied", tool=tool, user=principal.user_id, reason=str(exc))
            return ToolOutcome("denied", tool, f"You are not authorized: {exc}.")
        except ToolError as exc:
            await audit.append(
                session, entry(decision="error", arguments=validated, reason=str(exc))
            )
            return ToolOutcome("error", tool, str(exc))
        await audit.append(session, entry(decision="executed", arguments=validated, result=result))
        log.info("tool_executed", tool=tool, user=principal.user_id)
        return ToolOutcome("ok", tool, f"{tool} completed", data=result)


# ------------------------------------------------------------------ approvals


async def principal_of(session: AsyncSession, user_id: str) -> Principal | None:
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return Principal(
        user_id=user.id,
        name=user.name,
        department=user.department,
        role=user.role,
        permissions=frozenset(user.permissions),
    )


async def _load_for_decision(session: AsyncSession, pending_id: uuid.UUID) -> PendingAction | None:
    row: PendingAction | None = await session.scalar(
        select(PendingAction).where(PendingAction.id == pending_id).with_for_update()
    )
    return row


async def approve_pending(
    approver: Principal, pending_id: uuid.UUID, confirm_hash: str, ctx: ToolContext
) -> ToolOutcome:
    """Execute a pending sensitive action after an explicit, matching confirmation.

    Separation of duties: the approver must hold the approve permission and must be a
    different person from the requester. The confirmation must carry the action hash, so an
    approver can only approve the exact arguments that were proposed. Execution is guarded by
    the pending row's status under a row lock, so a double approval cannot run it twice.
    """
    async with ctx.factory() as session, session.begin():

        def entry(tool: str | None = None, **kw: Any) -> audit.AuditEntry:
            return audit.entry_of(
                approver,
                request_id=ctx.request_id,
                event="approval",
                tool=tool,
                pending_action_id=pending_id,
                **kw,
            )

        pending = await _load_for_decision(session, pending_id)
        if pending is None:
            await audit.append(session, entry(decision="deny", reason="unknown pending action"))
            return ToolOutcome("denied", "", "That action does not exist.")
        if pending.status != "pending":
            await audit.append(
                session,
                entry(pending.tool, decision="deny", reason=f"already {pending.status}"),
            )
            return ToolOutcome("denied", pending.tool, f"That action is already {pending.status}.")
        if pending.expires_at < datetime.now(UTC):
            pending.status = "expired"
            pending.decided_at = func.now()
            await audit.append(session, entry(pending.tool, decision="deny", reason="expired"))
            return ToolOutcome("denied", pending.tool, "That action has expired.")
        if not approver.has(pending.approve_permission):
            await audit.append(
                session,
                entry(
                    pending.tool,
                    decision="deny",
                    reason=f"approver lacks {pending.approve_permission}",
                    action_hash=pending.action_hash,
                ),
            )
            return ToolOutcome(
                "denied", pending.tool, f"You need the {pending.approve_permission} permission."
            )
        if approver.user_id == pending.requester_id:
            await audit.append(
                session,
                entry(
                    pending.tool,
                    decision="deny",
                    reason="requester and approver must be different people",
                    action_hash=pending.action_hash,
                ),
            )
            return ToolOutcome(
                "denied", pending.tool, "The requester cannot approve their own request."
            )
        if confirm_hash != pending.action_hash:
            await audit.append(
                session,
                entry(pending.tool, decision="deny", reason="confirmation does not match"),
            )
            return ToolOutcome(
                "denied",
                pending.tool,
                "That confirmation does not match the pending action; review it again.",
            )

        requester = await principal_of(session, pending.requester_id)
        spec = TOOLS[pending.tool]
        if requester is None or authorize(requester, spec):
            pending.status = "rejected"
            pending.decided_at = func.now()
            await audit.append(
                session,
                entry(
                    pending.tool,
                    decision="deny",
                    reason="requester no longer holds the required permission",
                    action_hash=pending.action_hash,
                ),
            )
            return ToolOutcome(
                "denied", pending.tool, "The requester is no longer authorized for this action."
            )

        args = spec.args_model.model_validate(pending.arguments)
        try:
            result = await HANDLERS[pending.tool](session, requester, args, ctx)
        except ToolError as exc:
            pending.status = "failed"
            pending.decided_at = func.now()
            await audit.append(
                session,
                entry(
                    pending.tool, decision="error", reason=str(exc), action_hash=pending.action_hash
                ),
            )
            return ToolOutcome("error", pending.tool, str(exc))

        if pending.tool == "create_vpn_profile":  # record who approved it
            profile = await session.get(VpnProfile, result["profile_id"])
            if profile is not None:
                profile.approved_by = approver.user_id
                profile.pending_action_id = pending.id
        pending.status = "executed"
        pending.approver_id = approver.user_id
        pending.result = result
        pending.decided_at = func.now()
        pending.executed_at = func.now()
        await audit.append(
            session,
            entry(
                pending.tool,
                decision="executed",
                arguments=pending.arguments,
                result=result,
                action_hash=pending.action_hash,
                reason=f"approved by {approver.user_id} for requester {pending.requester_id}",
            ),
        )
        log.info(
            "sensitive_action_executed",
            tool=pending.tool,
            requester=pending.requester_id,
            approver=approver.user_id,
        )
        return ToolOutcome(
            "ok",
            pending.tool,
            f"{pending.summary} - approved and executed.",
            data=result,
            pending_action_id=pending.id,
            action_hash=pending.action_hash,
        )


async def reject_pending(
    approver: Principal, pending_id: uuid.UUID, reason: str | None, ctx: ToolContext
) -> ToolOutcome:
    async with ctx.factory() as session, session.begin():
        pending = await _load_for_decision(session, pending_id)
        if pending is None or pending.status != "pending":
            return ToolOutcome("denied", pending.tool if pending else "", "Nothing to reject.")
        may_decide = approver.has(pending.approve_permission) or (
            approver.user_id == pending.requester_id  # requesters may cancel their own request
        )
        if not may_decide:
            return ToolOutcome("denied", pending.tool, "You may not decide this action.")
        pending.status = "rejected"
        pending.approver_id = approver.user_id
        pending.decided_at = func.now()
        await audit.append(
            session,
            audit.entry_of(
                approver,
                request_id=ctx.request_id,
                event="approval",
                tool=pending.tool,
                decision="deny",
                reason=reason or "rejected",
                pending_action_id=pending.id,
                action_hash=pending.action_hash,
            ),
        )
        return ToolOutcome("denied", pending.tool, "Action rejected.")
