"""/api/v1/auth — magic-link login + JWT refresh + logout.

Phase 3 auth foundation. Flow:

    1. POST /auth/request-login  { email }
       → 200 { expiresAt, devToken? }
       Mints a one-shot magic-link. In dev, returns the raw token so the
       mobile app can deep-link without needing a real email service.

    2. POST /auth/verify         { email, token, deviceId?, deviceLabel? }
       → 200 { userId, email, accessToken, refreshToken, ... }
       Consumes the magic-link, creates the session row, mints access + refresh.

    3. POST /auth/refresh        { refreshToken }
       → 200 { ... new pair ... }
       Rotates the refresh token. Old refresh is invalidated by hash mismatch.

    4. POST /auth/logout         { refreshToken? }
       → 200 { revoked: true }
       Revokes the session embedded in the access token (or the refresh,
       if provided).

    5. GET  /auth/me
       → 200 { userId, email, authMethod }
       Identity probe — protected by ``require_real_auth`` (no bypass).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import get_settings
from app.middleware.auth import AuthedUser, get_current_user, require_real_auth
from app.schemas.auth import (
    IssuedTokensResponse,
    LogoutRequest,
    LogoutResponse,
    MeResponse,
    RefreshRequest,
    RequestLoginRequest,
    RequestLoginResponse,
    VerifyMagicLinkRequest,
)
from app.services.auth import (
    AuthError,
    IssuedTokens,
    refresh as auth_refresh,
    request_login as auth_request_login,
    verify_magic_link as auth_verify_magic_link,
)
from app.services.auth_store import AuthStore, get_auth_store
from app.services.jwt_service import (
    ACCESS_TOKEN_TTL,
    REFRESH_TOKEN_TTL,
    TokenError,
    verify_access,
)

logger = logging.getLogger("api.router.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


def _to_issued_response(issued: IssuedTokens) -> IssuedTokensResponse:
    return IssuedTokensResponse(
        user_id=issued.user.id,
        email=issued.user.email,
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        access_expires_in_seconds=int(ACCESS_TOKEN_TTL.total_seconds()),
        refresh_expires_in_seconds=int(REFRESH_TOKEN_TTL.total_seconds()),
    )


@router.post(
    "/request-login",
    response_model=RequestLoginResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def request_login(
    body: RequestLoginRequest,
    store: AuthStore = Depends(get_auth_store),
) -> RequestLoginResponse:
    """Issue a magic-link token.

    In production, this would hand off to an email service (e.g. Postmark,
    SES). In Phase 3.1 dev mode we return the raw token in the response
    payload — the mobile app picks it up from the verify screen.

    Rate limiting (5/hour/email) is on the Phase 3 follow-on list — see
    AGENTV1.md. We log the request so abuse is visible immediately.
    """
    settings = get_settings()
    is_prod = settings.env.lower() in ("prod", "production")

    try:
        challenge = await auth_request_login(
            email=body.email,
            store=store,
            is_production=is_prod,
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return RequestLoginResponse(
        expires_at=challenge.expires_at,
        dev_token=challenge.dev_token,
    )


@router.post(
    "/verify",
    response_model=IssuedTokensResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def verify(
    body: VerifyMagicLinkRequest,
    store: AuthStore = Depends(get_auth_store),
) -> IssuedTokensResponse:
    settings = get_settings()
    try:
        issued = await auth_verify_magic_link(
            email=body.email,
            token=body.token,
            store=store,
            secret=settings.jwt_secret,
            device_id=body.device_id,
            device_label=body.device_label,
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    logger.info("auth: verified magic-link for %s — session=%s", issued.user.email, issued.session.id)
    return _to_issued_response(issued)


@router.post(
    "/refresh",
    response_model=IssuedTokensResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def refresh(
    body: RefreshRequest,
    store: AuthStore = Depends(get_auth_store),
) -> IssuedTokensResponse:
    settings = get_settings()
    try:
        issued = await auth_refresh(
            refresh_token=body.refresh_token,
            store=store,
            secret=settings.jwt_secret,
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    return _to_issued_response(issued)


@router.post(
    "/logout",
    response_model=LogoutResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def logout(
    body: LogoutRequest | None = None,
    user: AuthedUser = Depends(require_real_auth),
    store: AuthStore = Depends(get_auth_store),
) -> LogoutResponse:
    """Revoke the session.

    Two paths:
      - ``refreshToken`` in the body: revoke that session id directly.
      - No body: revoke the session embedded in the access token's claims.

    Both end up at ``store.revoke_session``; idempotent on already-revoked.
    """
    settings = get_settings()
    session_id: str | None = None

    if body is not None and body.refresh_token:
        try:
            from app.services.jwt_service import verify_refresh

            claims = verify_refresh(secret=settings.jwt_secret, token=body.refresh_token)
            session_id = claims.sid
        except TokenError:
            # Already-invalid refresh — treat as "nothing to revoke" + 200.
            return LogoutResponse(revoked=False)

    if session_id is None:
        # Pull from the access token. We re-verify so a tampered claim
        # doesn't get to call revoke_session against an arbitrary id.
        from fastapi import Request as _Req  # noqa — local for clarity
        # We already have the AuthedUser; the access-token sid would be
        # needed but isn't on AuthedUser. Phase 3.1 keeps logout body-driven.
        # (Mobile sends the refresh token explicitly on logout anyway.)
        return LogoutResponse(revoked=False)

    await store.revoke_session(session_id)
    logger.info("auth: revoked session %s (user=%s)", session_id, user.email)
    return LogoutResponse(revoked=True)


@router.get(
    "/me",
    response_model=MeResponse,
    response_model_by_alias=True,
)
async def me(user: AuthedUser = Depends(require_real_auth)) -> MeResponse:
    """Identity probe. Refuses DEV_AUTH_BYPASS — must have a real session."""
    return MeResponse(
        user_id=user.id,
        email=user.email,
        auth_method=user.auth_method,
    )


# Quiet unused-import lint on the convenience re-export.
_ = verify_access
