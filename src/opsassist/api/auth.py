"""Development token endpoint.

Mounted only when OPSASSIST_ENV is dev or test. It stands in for the company identity
provider so reviewers can act as any seeded user; it does not exist in staging or prod.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from opsassist.api.schemas import DevTokenRequest, DevTokenResponse
from opsassist.auth import issue_token, load_principal
from opsassist.config import Settings

router = APIRouter(prefix="/api/auth", tags=["auth (dev only)"])


@router.post("/dev-token", response_model=DevTokenResponse)
async def dev_token(request: Request, body: DevTokenRequest) -> DevTokenResponse:
    settings: Settings = request.app.state.settings
    principal = await load_principal(request.app.state.session_factory, body.user_id)
    if principal is None:
        raise HTTPException(status_code=404, detail="unknown or inactive user")
    return DevTokenResponse(
        access_token=issue_token(settings, principal.user_id),
        expires_in=settings.jwt_ttl_seconds,
        user_id=principal.user_id,
    )
