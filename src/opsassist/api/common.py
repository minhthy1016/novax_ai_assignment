"""Shared helpers for API routes: request IDs, error envelopes, usage rendering."""

from __future__ import annotations

from decimal import Decimal

from fastapi import Request
from fastapi.responses import JSONResponse

from opsassist.api.schemas import AttemptOut, ErrorBody, ErrorResponse, UsageOut
from opsassist.gateway.gateway import AttemptRecord
from opsassist.providers.base import Usage

NOT_FOUND = "conversation not found"

# Gateway failure -> HTTP status. The model rejecting our request is an upstream failure
# the client cannot fix, hence 502 rather than 400.
GATEWAY_STATUS = {
    "bad_request": 502,
    "provider_unavailable": 503,
    "no_available_provider": 503,
    "deadline_exceeded": 504,
}


def request_id_of(request: Request) -> str:
    return str(request.scope.get("state", {}).get("request_id", "unknown"))


def error_response(
    status: int,
    code: str,
    message: str,
    request_id: str,
    attempts: list[AttemptRecord] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, request_id=request_id),
        attempts=[AttemptOut.of(a) for a in attempts or []],
    )
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def usage_out(usage: Usage, cost: Decimal) -> UsageOut:
    return UsageOut(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        estimated=usage.estimated,
        cost_usd=cost,
    )
