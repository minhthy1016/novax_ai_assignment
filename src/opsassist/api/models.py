"""Model catalog and embeddings endpoints."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request

from opsassist.api.common import GATEWAY_STATUS, error_response, request_id_of, usage_out
from opsassist.api.schemas import (
    EmbeddingItem,
    EmbeddingRequest,
    EmbeddingResponse,
    ErrorResponse,
    ModelInfo,
    ModelRef,
    ModelsResponse,
)
from opsassist.auth import CurrentPrincipal
from opsassist.gateway.catalog import UnknownModelError
from opsassist.gateway.factory import ProviderStatus
from opsassist.gateway.gateway import CallContext, GatewayError, LLMGateway

router = APIRouter(prefix="/api", tags=["models"])


@router.get("/models", response_model=ModelsResponse)
async def list_models(request: Request, _principal: CurrentPrincipal) -> ModelsResponse:
    gateway: LLMGateway = request.app.state.gateway
    statuses: dict[str, ProviderStatus] = request.app.state.provider_statuses
    circuits = gateway.breakers.states()
    catalog = gateway.catalog
    models = []
    for m in catalog.models:
        status = statuses.get(m.provider, ProviderStatus(False, "not configured"))
        models.append(
            ModelInfo(
                id=m.id,
                kind=m.kind,
                provider=m.provider,
                available=gateway.is_available(m),
                unavailable_reason=None if status.enabled else status.reason,
                circuit=str(circuits.get(m.id, "closed")),
                data_egress=catalog.providers[m.provider].data_egress,
                context_window=m.context_window,
                dimensions=m.dimensions,
                input_usd_per_mtok=m.input_usd_per_mtok,
                output_usd_per_mtok=m.output_usd_per_mtok,
            )
        )
    return ModelsResponse(
        models=models,
        routes=catalog.routes,
        defaults={"chat": catalog.defaults.chat, "embedding": catalog.defaults.embedding},
    )


@router.post(
    "/embeddings",
    response_model=EmbeddingResponse,
    responses={400: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def embeddings(request: Request, body: EmbeddingRequest, principal: CurrentPrincipal) -> Any:
    gateway: LLMGateway = request.app.state.gateway
    request_id = request_id_of(request)
    ctx = CallContext(request_id, principal.user_id)
    try:
        outcome = await gateway.embed(body.model, body.input, body.input_type, ctx)
    except UnknownModelError as exc:
        return error_response(400, "unknown_model", str(exc), request_id)
    except GatewayError as err:
        return error_response(
            GATEWAY_STATUS[err.code], err.code, str(err), request_id, err.attempts
        )
    vectors = outcome.result.vectors
    cost = sum((a.cost_usd for a in outcome.attempts), Decimal(0))
    return EmbeddingResponse(
        model=ModelRef(id=outcome.model.id, provider=outcome.model.provider),
        dimensions=len(vectors[0]) if vectors else 0,
        data=[EmbeddingItem(index=i, embedding=v) for i, v in enumerate(vectors)],
        usage=usage_out(outcome.result.usage, cost),
        request_id=request_id,
    )
