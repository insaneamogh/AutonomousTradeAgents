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
       A 401 failure carries a machine-readable ``code`` alongside ``detail``
       (see ``app.services.auth.auth.RefreshError``) so the client can tell
       a truly-dead credential apart from a session the backend merely
       doesn't currently recognize.

    4. POST /auth/logout         { refreshToken? }
       → 200 { revoked: true }
       Revokes the session embedded in the access token (or the refresh,
       if provided).

    5. GET  /auth/me
       → 200 { userId, email, authMethod }
       Identity probe — protected by ``require_real_auth`` (no bypass).

    6. POST /auth/google         { idToken, deviceId?, deviceLabel? }
       → 200 { userId, email, accessToken, refreshToken, ... }
       "Continue with Google" — a second way to reach the exact same
       session shape ``/auth/verify`` produces. Verifies the Google ID
       token (``app.services.auth.google_oauth``) before ever touching
       user/session state.

    7. POST /auth/google/exchange { code, codeVerifier, redirectUri, ... }
       → 200 { ... same shape as /auth/google ... }
       Fallback leg for a Google OAuth client type whose platform policy
       is confidential (typically "Web application") — exchanges the PKCE
       authorization code server-side (we hold the client secret; the
       mobile app never does), then runs the SAME verify+issue path as
       #6. Not needed for a public/native (iOS/Android) client type, which
       exchanges directly against Google with no secret at all.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.core.config import Settings, get_settings
from app.middleware.auth import AuthedUser, require_real_auth
from app.schemas.auth import (
    GoogleExchangeRequest,
    GoogleLoginRequest,
    IssuedTokensResponse,
    LogoutRequest,
    LogoutResponse,
    MeResponse,
    RefreshRequest,
    RequestLoginRequest,
    RequestLoginResponse,
    VerifyMagicLinkRequest,
)
from app.services.auth.auth import (
    AuthError,
    IssuedTokens,
    RefreshError,
)
from app.services.auth.auth import (
    login_with_google as auth_login_with_google,
)
from app.services.auth.auth import (
    refresh as auth_refresh,
)
from app.services.auth.auth import (
    request_login as auth_request_login,
)
from app.services.auth.auth import (
    verify_magic_link as auth_verify_magic_link,
)
from app.services.auth.auth_store import AuthStore, get_auth_store
from app.services.auth.jwt_service import (
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
    request: Request,
    store: AuthStore = Depends(get_auth_store),
) -> RequestLoginResponse:
    """Issue a magic-link token.

    In production, this would hand off to an email service (e.g. Postmark,
    SES). In Phase 3.1 dev mode we return the raw token in the response
    payload — the mobile app picks it up from the verify screen.

    Rate-limited: 5/hour/email + 30/hour/IP (in-process sliding window).
    Over-limit → 429. A Redis-backed window is the multi-worker upgrade.
    """
    settings = get_settings()
    # Single source of truth — ``_PRODUCTION_ENVS`` also includes "live". An
    # inline tuple here previously omitted it, leaking the dev magic-link
    # token in the response body under ENV=live (account takeover).
    is_prod = settings.is_production

    from app.services.auth.rate_limit import check_login_rate

    client_ip = request.client.host if request.client else None
    if not check_login_rate(body.email, client_ip):
        logger.warning("auth: rate limit hit for request-login (email/ip window)")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login requests — wait a bit and try again.",
        )

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
    request: Request,
    store: AuthStore = Depends(get_auth_store),
) -> IssuedTokensResponse:
    """Consume a magic-link token and issue the access + refresh pair.

    Rate-limited: 10/hour/email + 40/hour/IP. Verification costs one
    scrypt per outstanding link for that email, so an unthrottled endpoint
    is an unauthenticated CPU amplifier — the hashing itself runs off the
    event loop in ``verify_magic_link``.
    """
    settings = get_settings()

    from app.services.auth.rate_limit import check_verify_rate

    client_ip = request.client.host if request.client else None
    if not check_verify_rate(body.email, client_ip):
        logger.warning("auth: rate limit hit for verify (email/ip window)")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many verification attempts — wait a bit and try again.",
        )

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
    request: Request,
    store: AuthStore = Depends(get_auth_store),
) -> IssuedTokensResponse | JSONResponse:
    settings = get_settings()

    from app.services.auth.rate_limit import check_refresh_rate

    client_ip = request.client.host if request.client else None
    if not check_refresh_rate(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many token refreshes — slow down.",
        )

    try:
        issued = await auth_refresh(
            refresh_token=body.refresh_token,
            store=store,
            secret=settings.jwt_secret,
        )
    except RefreshError as exc:
        # Sibling ``code`` alongside ``detail`` (not nested inside it) so
        # ``detail`` stays the plain string every other auth error already
        # returns — the mobile authStore reads ``code`` to decide whether
        # this credential is truly dead or just unrecognized right now.
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": str(exc), "code": exc.code},
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
      - No body: revoke the session embedded in the access token's claims
        (``AuthedUser.session_id``, verified by the auth middleware).

    Both end up at ``store.revoke_session``; idempotent on already-revoked.
    The body is optional — a client that only holds an access token (or
    that already dropped its refresh token) still gets a real logout.
    """
    settings = get_settings()
    session_id: str | None = None

    if body is not None and body.refresh_token:
        try:
            from app.services.auth.jwt_service import verify_refresh

            claims = verify_refresh(secret=settings.jwt_secret, token=body.refresh_token)
            # Ownership: a caller may only revoke their OWN session. Without
            # this, user A holding user B's refresh token could log B out.
            if claims.sub != user.id:
                logger.warning(
                    "auth: logout refresh-token subject %s != caller %s — refusing",
                    claims.sub, user.id,
                )
                return LogoutResponse(revoked=False)
            session_id = claims.sid
        except TokenError:
            # Already-invalid refresh — treat as "nothing to revoke" + 200.
            return LogoutResponse(revoked=False)

    if session_id is None:
        # Fall back to the access token's own session. ``session_id`` comes
        # from claims the middleware already verified (signature + session
        # not revoked/expired), so no tampered claim can reach
        # revoke_session with an id that isn't the caller's.
        session_id = user.session_id

    if session_id is None:
        # Legacy access token minted before tokens carried ``sid`` — nothing
        # to revoke by id; it ages out within its 15-minute TTL.
        logger.info("auth: logout with no resolvable session id (user=%s)", user.email)
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


async def _verify_and_login_with_google(
    id_token: str,
    *,
    settings: Settings,
    store: AuthStore,
    device_id: str | None,
    device_label: str | None,
) -> IssuedTokensResponse:
    """Shared tail for both Google routes: verify the ID token, then issue
    the exact same session shape ``/auth/verify`` does. Kept in one place
    so #6 (direct) and #7 (exchange fallback) can never drift apart on
    what counts as "verified".
    """
    if not settings.google_oauth_client_ids:
        # Deliberately NOT part of production_config_problems()'s hard-fail
        # boot check — magic-link alone must remain a valid production
        # config. This surfaces as a per-request 503 instead.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google sign-in is not configured on this server.",
        )

    # Imported as a module (not `from ... import verify_google_id_token`) so
    # tests can monkeypatch `google_oauth.verify_google_id_token` the same
    # way test_broker.py patches `alpaca_oauth.exchange_code_for_tokens` —
    # this router calls it as a module attribute, resolved at call time.
    from app.services.auth import google_oauth

    try:
        identity = await google_oauth.verify_google_id_token(
            id_token,
            audience=settings.google_oauth_client_ids,
        )
    except google_oauth.GoogleJWKSFetchError as exc:
        # Not the caller's fault — we simply couldn't reach/parse Google's
        # key endpoint. 503, not 401, so the client knows to retry.
        logger.warning("auth: google JWKS fetch failed — %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not verify Google sign-in right now — try again shortly.",
        ) from exc
    except google_oauth.GoogleAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    issued = await auth_login_with_google(
        email=identity.email,
        store=store,
        secret=settings.jwt_secret,
        device_id=device_id,
        device_label=device_label,
    )
    logger.info(
        "auth: google sign-in for %s — session=%s", issued.user.email, issued.session.id
    )
    return _to_issued_response(issued)


@router.post(
    "/google",
    response_model=IssuedTokensResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def google_login(
    body: GoogleLoginRequest,
    request: Request,
    store: AuthStore = Depends(get_auth_store),
) -> IssuedTokensResponse:
    """"Continue with Google" — the primary path: the mobile app already
    exchanged its PKCE authorization code directly with Google (no client
    secret needed for a public/native client type) and hands us the
    resulting ID token. Magic-link stays untouched and remains a fully
    valid login path on its own.

    Rate-limited: 30/hour/IP. Unlike ``/verify`` there's no caller-supplied
    email to key on before the token verifies, so this is IP-only (see
    ``check_google_rate``).
    """
    settings = get_settings()

    from app.services.auth.rate_limit import check_google_rate

    client_ip = request.client.host if request.client else None
    if not check_google_rate(client_ip):
        logger.warning("auth: rate limit hit for google login (ip window)")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many Google sign-in attempts — wait a bit and try again.",
        )

    return await _verify_and_login_with_google(
        body.id_token,
        settings=settings,
        store=store,
        device_id=body.device_id,
        device_label=body.device_label,
    )


@router.post(
    "/google/exchange",
    response_model=IssuedTokensResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_200_OK,
)
async def google_exchange(
    body: GoogleExchangeRequest,
    request: Request,
    store: AuthStore = Depends(get_auth_store),
) -> IssuedTokensResponse:
    """Fallback leg: exchange the authorization code server-side (holding
    the client secret the mobile app never sees), then run the exact same
    verify+issue path as ``/google``. Only needed when the configured
    Google OAuth client type is confidential (typically "Web application")
    and refuses a client-side PKCE exchange with no secret.

    Rate-limited the same as ``/google`` — same abuse shape, same bucket.
    """
    settings = get_settings()

    from app.services.auth.rate_limit import check_google_rate

    client_ip = request.client.host if request.client else None
    if not check_google_rate(client_ip):
        logger.warning("auth: rate limit hit for google exchange (ip window)")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many Google sign-in attempts — wait a bit and try again.",
        )

    from app.services.auth import google_oauth

    try:
        id_token = await google_oauth.exchange_code_for_id_token(
            code=body.code,
            code_verifier=body.code_verifier,
            redirect_uri=body.redirect_uri,
        )
    except google_oauth.GoogleExchangeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Google code exchange failed: {exc}",
        ) from exc

    return await _verify_and_login_with_google(
        id_token,
        settings=settings,
        store=store,
        device_id=body.device_id,
        device_label=body.device_label,
    )


# Quiet unused-import lint on the convenience re-export.
_ = verify_access
