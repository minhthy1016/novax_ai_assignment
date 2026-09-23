"""Inspect and delete persistent memory (Task 4)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from opsassist.api.common import error_response, request_id_of
from opsassist.api.schemas import ErrorResponse, MemoryOut, MemoryPut
from opsassist.auth import CurrentPrincipal
from opsassist.memory import (
    MemoryRejected,
    clear_memories,
    delete_memory,
    list_memories,
    put_memory,
)

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _out(row: Any) -> MemoryOut:
    return MemoryOut(
        key=row.key,
        value=row.value,
        category=row.category,
        source=row.source,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[MemoryOut])
async def list_(request: Request, principal: CurrentPrincipal) -> Any:
    async with request.app.state.session_factory() as session:
        return [_out(r) for r in await list_memories(session, principal.user_id)]


@router.put("", response_model=MemoryOut, responses={400: {"model": ErrorResponse}})
async def put(request: Request, body: MemoryPut, principal: CurrentPrincipal) -> Any:
    try:
        async with request.app.state.session_factory() as session, session.begin():
            row = await put_memory(session, principal.user_id, body.key, body.value)
            out = _out(row)
    except MemoryRejected as exc:
        return error_response(400, "memory_rejected", str(exc), request_id_of(request))
    return out


@router.delete("/{key}", responses={404: {"model": ErrorResponse}})
async def delete_one(request: Request, key: str, principal: CurrentPrincipal) -> Any:
    async with request.app.state.session_factory() as session, session.begin():
        removed = await delete_memory(session, principal.user_id, key)
    if not removed:
        return error_response(
            404, "not_found", f"no stored value for {key!r}", request_id_of(request)
        )
    return {"deleted": key}


@router.delete("")
async def delete_all(request: Request, principal: CurrentPrincipal) -> dict[str, int]:
    async with request.app.state.session_factory() as session, session.begin():
        count = await clear_memories(session, principal.user_id)
    return {"deleted": count}
