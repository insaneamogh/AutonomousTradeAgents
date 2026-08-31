"""``get_current_user`` Depends + DEV_AUTH_BYPASS.

Two facts shape this module:

1. Phase 3 is mid-rollout. Mobile auth screens land in a follow-on session,
   so until then the existing mobile build calls /account, /approvals, /agent
   *without* an Authorization header. Breaking those would block our own
   demo.

2. We can't ship a "no-auth-everywhere" default — that would let the auth
   middleware look correct in code but be effectively bypassed forever.

Compromise: ``DEV_AUTH_BYPASS=1`` — **explicit opt-in, off by default in
every environment** — resolves ``get_current_user`` to the fixture user
when no Bearer token is present. A real Bearer token is ALWAYS validated;
bypass only kicks in when the header is missing AND the env switch is
explicitly on. Set it in a local ``.env`` only; never on a deployed box.

For routes that MUST never bypass (e.g. /auth/logout, /auth/me — they only
make sense with a real session), use ``require_real_auth`` instead.

A THIRD identity resolves through this same ``get_current_user``: a demo
session (``docs/IMPL_DEMO_SESSION.md``, minted via ``POST /auth/demo``). It
carries a real, signed ``Bearer`` access token — never the no-header
DEV_AUTH_BYPASS path above — but ``get_current_user`` still returns it with
``is_dev_bypass=True``, so it is refused by ``require_real_auth`` exactly
like the dev bypass is. See ``app.services.auth.demo_session``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from app.core.config import get_settings
from app.core.time import utc_now
from app.services.auth.auth_store import (
    FIXTURE_USER_EMAIL,
    FIXTURE_USER_ID,
    AuthStore,
    UserRecord,
    get_auth_store,
)
from app.services.auth.demo_session import DEMO_USER_EMAIL, is_demo_claims
from app.services.auth.jwt_service import TokenError, verify_access
from engine.env import env_flag

logger = logging.getLogger("api.auth.middleware")


def _dev_bypass_enabled() -> bool:
    """Whether the no-Bearer fixture-user fallback is allowed.

    Default OFF everywhere — opt in explicitly with ``DEV_AUTH_BYPASS=1``.
    It used to default ON outside production, and ``_PRODUCTION_ENVS``
    doesn't include "staging", so a staging box resolved every
    unauthenticated request to the fixture user. Any environment that is
    not a developer's laptop must never depend on a default.

    FORCE-OFF in production regardless of the env var — a prod deploy that
    forgets to set DEV_AUTH_BYPASS=0 must NEVER silently accept
    unauthenticated requests.
    """
    requested = env_flag("DEV_AUTH_BYPASS")
    if get_settings().is_production:
        if requested:
            logger.warning(
                "DEV_AUTH_BYPASS is set truthy but ENV is production — "
                "IGNORING it. Unauthenticated access is never allowed in prod."
            )
        return False
    return requested


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
    session_id: str | None = None
    """``sid`` from the access token's claims — already verified above
    (signature + session-not-revoked). Logout revokes by this id, so a
    caller can end their session without handing back a refresh token.
    None for dev-bypass callers and for pre-``sid`` legacy tokens.
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

        if is_demo_claims(claims):
            # A demo session (docs/IMPL_DEMO_SESSION.md): resolves to the
            # cron user's real id so every READ route shows real data, but
            # is_dev_bypass=True is the entire enforcement — every route
            # that already calls require_real_auth refuses it with zero
            # new authorization code. Deliberately skips the store lookup
            # + session-binding check below: a demo token carries no sid
            # (nothing to revoke by id — it simply expires) and the demo
            # identity's email/auth_method are fixed display values, never
            # a lookup that could leak the real cron user's email.
            return AuthedUser(
                id=claims.sub,
                email=DEMO_USER_EMAIL,
                auth_method="demo",
                is_dev_bypass=True,
                session_id=claims.sid,
            )

        user = await store.get_user_by_id(claims.sub)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="user not found for token subject",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Session binding: access tokens carry the session id. If that
        # session was revoked (logout / admin) or expired, refuse — so a
        # logged-out access token can't keep executing trades for up to
        # its 15-min TTL. Tokens minted before this (no sid) skip the check
        # and age out within the TTL.
        if claims.sid is not None:
            session = await store.get_session(claims.sid)
            if (
                session is None
                or session.revoked_at is not None
                or session.expires_at < utc_now()
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="session revoked or expired",
                    headers={"WWW-Authenticate": "Bearer"},
                )

        return AuthedUser(
            id=user.id,
            email=user.email,
            auth_method=user.auth_method,
            is_dev_bypass=False,
            session_id=claims.sid,
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
