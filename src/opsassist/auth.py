"""Authentication: bearer JWT -> Principal.

The token binds identity and role: ``sub`` (user ID) and ``role`` (the role assigned when
the token was issued). Every request re-checks both against the database: an unknown or
deactivated user, or a role that no longer matches, is rejected with 401 and must sign in
again. Permissions are never taken from the token - they are loaded from the database on
every request, so revoking one takes effect immediately.

Token issuance here is a local stand-in for an identity provider (see architecture.md
D-08). In production the API would validate tokens from the company IdP (OIDC) and this
module's ``issue_token`` would not exist.
"""

from __future__ import annotations

import time
from typing import Annotated

import jwt
import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsassist.config import Settings
from opsassist.db.models import User

ISSUER = "opsassist"
AUDIENCE = "opsassist-api"
ALGORITHM = "HS256"

_bearer = HTTPBearer(auto_error=False)


class Principal(BaseModel):
    user_id: str
    name: str
    department: str
    role: str
    permissions: frozenset[str]

    def has(self, permission: str) -> bool:
        return permission in self.permissions


class TokenClaims(BaseModel):
    user_id: str
    role: str


def issue_token(settings: Settings, user_id: str, role: str, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    claims = {
        "sub": user_id,
        "role": role,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": issued,
        "exp": issued + settings.jwt_ttl_seconds,
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM)


def decode_token(settings: Settings, token: str) -> TokenClaims:
    """Return the verified claims, or raise ``jwt.InvalidTokenError``."""
    claims = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=[ALGORITHM],  # pinned: never accept 'none' or a caller-chosen algorithm
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"require": ["sub", "role", "exp", "iat", "iss", "aud"]},
    )
    return TokenClaims(user_id=str(claims["sub"]), role=str(claims["role"]))


def _unauthorized(detail: str = "invalid or missing credentials") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def load_principal(
    factory: async_sessionmaker[AsyncSession], user_id: str
) -> Principal | None:
    async with factory() as session:
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


async def get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    settings: Settings = request.app.state.settings
    try:
        claims = decode_token(settings, credentials.credentials)
    except jwt.InvalidTokenError:
        raise _unauthorized() from None
    principal = await load_principal(request.app.state.session_factory, claims.user_id)
    if principal is None:
        raise _unauthorized()
    if principal.role != claims.role:
        # Role changed since the token was issued: the caller must re-authenticate.
        raise _unauthorized("role changed since sign-in; request a new token")
    structlog.contextvars.bind_contextvars(user_id=principal.user_id)
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
