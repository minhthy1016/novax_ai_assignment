"""Retrieval-only search (debugging, evaluation of retrieval quality)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from opsassist.api.common import error_response, request_id_of
from opsassist.api.schemas import (
    ErrorResponse,
    RetrievalOut,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from opsassist.auth import CurrentPrincipal
from opsassist.gateway.gateway import CallContext, GatewayError
from opsassist.knowledge.retrieval import retrieve
from opsassist.policy.access import scope_for

router = APIRouter(prefix="/api", tags=["knowledge"])


@router.post("/search", response_model=SearchResponse, responses={503: {"model": ErrorResponse}})
async def search(request: Request, body: SearchRequest, principal: CurrentPrincipal) -> Any:
    """The same retrieval the assistant uses, scoped to the caller - never wider."""
    request_id = request_id_of(request)
    try:
        result = await retrieve(
            body.query,
            scope_for(principal),
            factory=request.app.state.session_factory,
            gateway=request.app.state.gateway,
            settings=request.app.state.settings,
            ctx=CallContext(request_id, principal.user_id),
            top_k=body.top_k,
        )
    except GatewayError as err:
        return error_response(
            503,
            "retrieval_unavailable",
            "knowledge search is unavailable",
            request_id,
            err.attempts,
        )
    return SearchResponse(
        hits=[
            SearchHit(
                rank=i,
                doc_key=c.doc_key,
                version=c.version,
                title=c.title,
                locator=c.locator,
                ref=c.stable_ref,
                content=c.content,
                similarity=c.similarity,
                fts_rank=c.fts_rank,
                score=c.score,
            )
            for i, c in enumerate(result.chunks, start=1)
        ],
        retrieval=RetrievalOut(
            candidates=result.candidates,
            used=len(result.chunks),
            below_threshold=result.below_threshold,
            latency_ms=result.latency_ms,
            embedding_model=result.embedding_model,
        ),
        request_id=request_id,
    )
