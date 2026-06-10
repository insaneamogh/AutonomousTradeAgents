"""``get_current_user`` Depends + DEV_AUTH_BYPASS.

Two facts shape this module:

1. Phase 3 is mid-rollout. Mobile auth screens land in a follow-on session,
   so until then the existing mobile build calls /account, /approvals, /agent
   *without* an Authorization header. Breaking those would block our own
   demo.

2. We can't ship a "no-auth-everywhere" default — that would let the auth
   middleware look correct in code but be effectively bypassed forever.

Compromise: ``DEV_AUTH_BYPASS=1`` (default-on in local dev) resolves
``get_current_user`` to the fixture user when no Bearer token is present.
A real Bearer token is ALWAYS validated; bypass only kicks in when the
header is missing AND the env switch is on. Once mobile auth ships, set
``DEV_AUTH_BYPASS=0`` and the older path 401s.

For routes that MUST never bypass (e.g. /auth/logout, /auth/me — they only
make sense with a real session), use ``require_real_auth`` instead.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from app.core.config import get_settings
from app.services.auth_store import (
    FIXTURE_USER_EMAIL,
    FIXTURE_USER_ID,
    AuthStore,
    UserRecord,
    get_auth_store,
)
from app.services.jwt_service import TokenError, verify_access

logger = logging.getLogger("api.auth.middleware")


def _is_truthy(v: str | None) -> bool:
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


def _dev_bypass_enabled() -> bool:
    """Default ON in local dev. Mobile-auth-ready deploys set this to 0."""
    return _is_truthy(os.environ.get("DEV_AUTH_BYPASS", "1"))


@dataclass(frozen=True)
class AuthedUser:
    """The identity injected into every protected route handler."""

    id: str
    email: str
    auth_method: str
    is_dev_bypass: bool = False
    """True when this user came in via DEV_AUTH_BYPASS (no Bearer header).
    Routes that must refuse the bypass path can check this flag (or use
    ``require_real_auth``).
    """


def _extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization") or request.headers.get("authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


async def get_current_user(
    request: Request,
    store: AuthStore = Depends(get_auth_store),
) -> AuthedUser:
    """Resolve the caller's identity.

    Priority:
      1. ``Authorization: Bearer <access_jwt>``  — validate + lookup. Always
         honored; never bypassed.
      2. No header + ``DEV_AUTH_BYPASS=1``         — fall through to the
         fixture user so existing mobile screens keep working during the
         Phase 3 transition.
      3. No header + bypass disabled               — 401.
    """
    token = _extract_bearer(request)
    settings = get_settings()

    if token:
        try:
            claims = verify_access(secret=settings.jwt_secret, token=token)
        except TokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"invalid access token: {exc}",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

        user = await store.get_user_by_id(claims.sub)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="user not found for token subject",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return AuthedUser(
            id=user.id,
            email=user.email,
            auth_method=user.auth_method,
            is_dev_bypass=False,
        )

    if _dev_bypass_enabled():
        logger.debug("DEV_AUTH_BYPASS=1 — resolving to fixture user")
        fixture = await store.get_user_by_id(FIXTURE_USER_ID)
        if fixture is None:
            # Should never happen — MockAuthStore seeds it; Postgres impl
            # seeds in migration 0001. Surface loudly if it does.
            return AuthedUser(
                id=FIXTURE_USER_ID,
                email=FIXTURE_USER_EMAIL,
                auth_method="dev_bypass",
                is_dev_bypass=True,
            )
        return AuthedUser(
            id=fixture.id,
            email=fixture.email,
            auth_method=fixture.auth_method,
            is_dev_bypass=True,
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing or malformed Authorization header",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_real_auth(
    user: AuthedUser = Depends(get_current_user),
) -> AuthedUser:
    """Like ``get_current_user`` but refuses the DEV_AUTH_BYPASS path.

    Use on routes where bypass would be nonsensical (e.g. /auth/logout —
    you can't log a fixture user out; they have no real session).
    """
    if user.is_dev_bypass:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="this route requires a real authenticated session",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def _silence_unused() -> None:
    """Keep ``UserRecord`` import alive for type-checkers — used in docstrings."""
    _ = UserRecord
