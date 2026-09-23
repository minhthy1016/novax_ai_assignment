"""Tamper-evident audit log.

Every policy decision and tool execution is appended with the hash of the previous record:

    hash(n) = sha256(prev_hash || canonical_json(record))

Removing, reordering or editing any row breaks the chain from that point on, which
``verify_chain`` reports with the first broken id. The runtime database role may INSERT and
SELECT but not UPDATE or DELETE (migration 0007), so the application cannot rewrite history
even if it wanted to.

Arguments and results are redacted before they are written: an audit trail must be safe to
read. Appends are serialized with an advisory lock so concurrent writers cannot interleave
and produce two rows claiming the same predecessor.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.auth import Principal
from opsassist.db.models import AuditLog
from opsassist.logging_setup import is_sensitive_key

GENESIS = "0" * 64
AUDIT_LOCK = 8_421_337  # advisory lock key for chain appends
Decision = Literal["allow", "deny", "pending", "executed", "error"]


@dataclass(frozen=True)
class AuditEntry:
    request_id: str
    actor_id: str
    actor_role: str
    event: str
    decision: Decision
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    reason: str | None = None
    result: dict[str, Any] | None = None
    pending_action_id: uuid.UUID | None = None
    action_hash: str | None = None


def redact(value: Any) -> Any:
    """Drop secret-looking values anywhere in the structure, and cap long strings."""
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if is_sensitive_key(str(k)) else redact(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "…"
    return value


def record_hash(prev_hash: str, entry: AuditEntry, created_at: str) -> str:
    payload = json.dumps(
        {
            "created_at": created_at,
            "request_id": entry.request_id,
            "actor_id": entry.actor_id,
            "actor_role": entry.actor_role,
            "event": entry.event,
            "tool": entry.tool,
            "arguments": redact(entry.arguments),
            "decision": entry.decision,
            "reason": entry.reason,
            "result": redact(entry.result),
            "pending_action_id": str(entry.pending_action_id) if entry.pending_action_id else None,
            "action_hash": entry.action_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256((prev_hash + payload).encode()).hexdigest()


async def append(session: AsyncSession, entry: AuditEntry) -> AuditLog:
    """Append one record inside the caller's transaction (so an action and its audit record
    commit together - a tool result can never exist without its audit row)."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": AUDIT_LOCK})
    last = await session.scalar(select(AuditLog).order_by(AuditLog.id.desc()).limit(1))
    prev_hash = last.hash if last else GENESIS
    created_at = await session.scalar(select(text("now()")))
    row = AuditLog(
        created_at=created_at,
        request_id=entry.request_id,
        actor_id=entry.actor_id,
        actor_role=entry.actor_role,
        event=entry.event,
        tool=entry.tool,
        arguments=redact(entry.arguments),
        decision=entry.decision,
        reason=entry.reason,
        result=redact(entry.result),
        pending_action_id=entry.pending_action_id,
        action_hash=entry.action_hash,
        prev_hash=prev_hash,
        hash=record_hash(prev_hash, entry, created_at.isoformat()),
    )
    session.add(row)
    await session.flush()
    return row


def entry_of(principal: Principal, **kwargs: Any) -> AuditEntry:
    return AuditEntry(actor_id=principal.user_id, actor_role=principal.role, **kwargs)


@dataclass(frozen=True)
class ChainStatus:
    records: int
    intact: bool
    broken_at: int | None = None
    detail: str | None = None


async def verify_chain(session: AsyncSession) -> ChainStatus:
    rows = (await session.scalars(select(AuditLog).order_by(AuditLog.id))).all()
    prev = GENESIS
    for row in rows:
        entry = AuditEntry(
            request_id=row.request_id,
            actor_id=row.actor_id,
            actor_role=row.actor_role,
            event=row.event,
            decision=row.decision,  # type: ignore[arg-type]
            tool=row.tool,
            arguments=row.arguments,
            reason=row.reason,
            result=row.result,
            pending_action_id=row.pending_action_id,
            action_hash=row.action_hash,
        )
        if row.prev_hash != prev:
            return ChainStatus(len(rows), False, row.id, "previous hash does not match")
        if row.hash != record_hash(prev, entry, row.created_at.isoformat()):
            return ChainStatus(len(rows), False, row.id, "record content does not match its hash")
        prev = row.hash
    return ChainStatus(len(rows), True)
