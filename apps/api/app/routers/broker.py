"""/api/v1/broker — Alpaca OAuth + connection management.

Phase 3 continued. PLAN.md §3 calls for per-user broker OAuth with
encrypted token storage. Routes:

  POST   /api/v1/broker/connect/alpaca/start
         Generate PKCE authorize URL. Server stashes (state, user_id,
         code_verifier) in the pending-OAuth cache; mobile opens the URL
         in the system browser.

  POST   /api/v1/broker/connect/alpaca/callback
         Body { code, state }. Verifies state, exchanges code, encrypts
         tokens, upserts broker_connections row.

  GET    /api/v1/broker/connections
         List the caller's connections (encrypted tokens NOT included).

  DELETE /api/v1/broker/connections/{id}
         Revoke. Nulls encrypted tokens + flips status='revoked'.

All routes are gated by ``get_current_user`` (no bypass — broker
connections are inherently per-user).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse

from app.middleware.auth import AuthedUser, require_real_auth
from app.schemas.broker import (
    BrokerConnectionResponse,
    CallbackRequest,
    CallbackResponse,
    StartOAuthRequest,
    StartOAuthResponse,
    StartZerodhaResponse,
    ZerodhaCallbackRequest,
)
# Module import (not from-import) so tests can monkeypatch
# ``alpaca_oauth.exchange_code_for_tokens`` and have the router see it.
from app.services import alpaca_oauth
from app.services import zerodha_connect
from app.services.alpaca_oauth import TokenExchangeError
from app.services.broker_store import (
    BrokerConnectionRecord,
    BrokerStore,
    PendingOAuth,
    PendingOAuthCache,
    get_broker_store,
    get_pending_oauth_cache,
)
from app.services.crypto import (
    CryptoUnavailableError,
    encrypt_for_storage,
    is_available as crypto_available,
    is_dev_key_in_use,
)

logger = logging.getLogger("api.router.broker")

router = APIRouter(prefix="/broker", tags=["broker"])


def _to_response(rec: BrokerConnectionRecord) -> BrokerConnectionResponse:
    return BrokerConnectionResponse(
        id=rec.id,
        broker=rec.broker,
        is_paper=rec.is_paper,
        account_number=rec.account_number,
        status=rec.status,
        created_at=rec.created_at,
        last_used_at=rec.last_used_at,
    )


def _require_crypto() -> None:
    """Refuse OAuth routes early when ``cryptography`` isn't installed.

    The encryption helper would raise ``CryptoUnavailableError`` on the
    first ``encrypt_for_storage`` call anyway, but failing here gives a
    clean 503 + a message that points the operator at ``uv sync``.
    """
    if not crypto_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Broker OAuth requires the 'cryptography' Python package. "
                "Run `uv sync` to install it."
            ),
        )


# ─────────────────────────────────────────────────────────────────────
# Start OAuth
# ─────────────────────────────────────────────────────────────────────


@router.post(
    "/connect/alpaca/start",
    response_model=StartOAuthResponse,
    response_model_by_alias=True,
)
async def start_alpaca_oauth(
    body: StartOAuthRequest,
    user: AuthedUser = Depends(require_real_auth),
    pending: PendingOAuthCache = Depends(get_pending_oauth_cache),
) -> StartOAuthResponse:
    _require_crypto()

    built = alpaca_oauth.build_authorize_url()
    pending.put(
        PendingOAuth(
            state=built.state,
            user_id=user.id,
            code_verifier=built.code_verifier,
            is_paper=body.is_paper,
            redirect_uri="",  # default redirect; explicit override lands later
        )
    )

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    return StartOAuthResponse(
        authorize_url=built.authorize_url,
        state=built.state,
        expires_at=expires_at,
        dev_warning=(
            "BROKER_TOKEN_ENCRYPTION_KEY is the dev fallback. Set a real key via Doppler before prod."
            if is_dev_key_in_use()
            else None
        ),
    )


# ─────────────────────────────────────────────────────────────────────
# Callback
# ─────────────────────────────────────────────────────────────────────


@router.post(
    "/connect/alpaca/callback",
    response_model=CallbackResponse,
    response_model_by_alias=True,
)
async def alpaca_callback(
    body: CallbackRequest,
    user: AuthedUser = Depends(require_real_auth),
    store: BrokerStore = Depends(get_broker_store),
    pending: PendingOAuthCache = Depends(get_pending_oauth_cache),
) -> CallbackResponse:
    _require_crypto()

    # State must exist + match the caller (otherwise we'd let Alice complete
    # Bob's OAuth dance, attaching Bob's broker tokens to Alice).
    stash = pending.consume(body.state)
    if stash is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unknown or expired state — start the OAuth flow again",
        )
    if stash.user_id != user.id:
        # Don't tell the attacker WHY (no "wrong user" leak) — just refuse.
        logger.warning(
            "broker callback: state belonged to user=%s but caller=%s — refusing",
            stash.user_id, user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="state mismatch",
        )

    try:
        tokens = await alpaca_oauth.exchange_code_for_tokens(
            code=body.code,
            code_verifier=stash.code_verifier,
        )
    except TokenExchangeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"broker token exchange failed: {exc}",
        ) from exc

    if not tokens.access_token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="broker returned no access token",
        )

    try:
        enc_access = encrypt_for_storage(tokens.access_token)
        enc_refresh = (
            encrypt_for_storage(tokens.refresh_token) if tokens.refresh_token else None
        )
    except CryptoUnavailableError as exc:  # pragma: no cover — _require_crypto already gates
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc),
        ) from exc

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=tokens.expires_in_seconds)
        if tokens.expires_in_seconds > 0
        else None
    )

    rec = await store.upsert_connection(
        user_id=user.id,
        broker="alpaca",
        is_paper=stash.is_paper,
        account_number=tokens.account_number,
        encrypted_access_token=enc_access,
        encrypted_refresh_token=enc_refresh,
        access_token_expires_at=expires_at,
    )
    logger.info(
        "broker: connected alpaca (%s) for user=%s — account=%s",
        "paper" if stash.is_paper else "live",
        user.id, tokens.account_number,
    )
    return CallbackResponse(connection=_to_response(rec))


# ─────────────────────────────────────────────────────────────────────
# Zerodha (Kite Connect) — request-token flow
# ─────────────────────────────────────────────────────────────────────


def _require_zerodha_configured() -> None:
    try:
        zerodha_connect.require_configured()
    except zerodha_connect.ZerodhaNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc),
        ) from exc


@router.post(
    "/connect/zerodha/start",
    response_model=StartZerodhaResponse,
    response_model_by_alias=True,
)
async def start_zerodha_connect(
    user: AuthedUser = Depends(require_real_auth),
    pending: PendingOAuthCache = Depends(get_pending_oauth_cache),
) -> StartZerodhaResponse:
    """Return the Kite login URL. The user logs in at kite.zerodha.com;
    Zerodha redirects to the app's REGISTERED redirect URL (set it to this
    API's /connect/zerodha/redirect in the Kite developer console) with
    ``request_token`` + our ``state`` echoed via redirect_params.
    """
    _require_crypto()
    _require_zerodha_configured()

    built = zerodha_connect.build_login_url()
    pending.put(
        PendingOAuth(
            state=built.state,
            user_id=user.id,
            code_verifier="",  # Kite has no PKCE — field unused for zerodha
            is_paper=False,  # Kite has no paper environment
            redirect_uri="",
        )
    )
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    return StartZerodhaResponse(
        login_url=built.login_url,
        state=built.state,
        expires_at=expires_at,
        dev_warning=(
            "BROKER_TOKEN_ENCRYPTION_KEY is the dev fallback. Set a real key via Doppler before prod."
            if is_dev_key_in_use()
            else None
        ),
    )


async def _complete_zerodha_connect(
    *,
    state: str,
    request_token: str,
    store: BrokerStore,
    pending: PendingOAuthCache,
    expected_user_id: str | None,
) -> BrokerConnectionRecord:
    """Shared tail of the POST /callback and GET /redirect paths.

    ``expected_user_id`` is enforced when the caller is authenticated
    (mobile POST). The browser GET path has no bearer — there the
    single-use, 15-minute, high-entropy state IS the proof of initiation.
    """
    stash = pending.consume(state)
    if stash is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unknown or expired state — start the Zerodha connect flow again",
        )
    if expected_user_id is not None and stash.user_id != expected_user_id:
        logger.warning(
            "zerodha callback: state belonged to user=%s but caller=%s — refusing",
            stash.user_id, expected_user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="state mismatch",
        )

    try:
        session = await zerodha_connect.exchange_request_token(
            request_token=request_token,
        )
    except zerodha_connect.RequestTokenExchangeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"zerodha token exchange failed: {exc}",
        ) from exc

    try:
        enc_access = encrypt_for_storage(session.access_token)
    except CryptoUnavailableError as exc:  # pragma: no cover — gated earlier
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc),
        ) from exc

    rec = await store.upsert_connection(
        user_id=stash.user_id,
        broker="zerodha",
        is_paper=False,
        account_number=session.user_id or None,
        encrypted_access_token=enc_access,
        encrypted_refresh_token=None,  # Kite has no refresh tokens
        access_token_expires_at=session.expires_at,
    )
    logger.info(
        "broker: connected zerodha for user=%s — kite_user=%s (token expires %s)",
        stash.user_id, session.user_id, session.expires_at,
    )
    return rec


@router.post(
    "/connect/zerodha/callback",
    response_model=CallbackResponse,
    response_model_by_alias=True,
)
async def zerodha_callback(
    body: ZerodhaCallbackRequest,
    user: AuthedUser = Depends(require_real_auth),
    store: BrokerStore = Depends(get_broker_store),
    pending: PendingOAuthCache = Depends(get_pending_oauth_cache),
) -> CallbackResponse:
    """Authenticated completion path (mobile forwards the redirect params)."""
    _require_crypto()
    _require_zerodha_configured()
    rec = await _complete_zerodha_connect(
        state=body.state,
        request_token=body.request_token,
        store=store,
        pending=pending,
        expected_user_id=user.id,
    )
    return CallbackResponse(connection=_to_response(rec))


@router.get("/connect/zerodha/redirect", response_class=HTMLResponse)
async def zerodha_browser_redirect(
    request_token: str = "",
    state: str = "",
    store: BrokerStore = Depends(get_broker_store),
    pending: PendingOAuthCache = Depends(get_pending_oauth_cache),
) -> HTMLResponse:
    """Browser landing for the Kite redirect — register THIS URL in the
    Kite developer console. No bearer (it's a top-level browser
    navigation); the single-use state stashed by the authed /start call
    identifies the user. Renders a tiny human-readable result page.
    """
    _require_crypto()
    _require_zerodha_configured()
    if not request_token or not state:
        return HTMLResponse(
            "<h2>Zerodha connect failed</h2><p>Missing request_token or state "
            "on the redirect. Start the connect flow again from the app.</p>",
            status_code=400,
        )
    try:
        rec = await _complete_zerodha_connect(
            state=state,
            request_token=request_token,
            store=store,
            pending=pending,
            expected_user_id=None,
        )
    except HTTPException as exc:
        return HTMLResponse(
            f"<h2>Zerodha connect failed</h2><p>{exc.detail}</p>",
            status_code=exc.status_code,
        )
    return HTMLResponse(
        "<h2>Zerodha connected ✓</h2>"
        f"<p>Kite account <b>{rec.account_number or 'unknown'}</b> is now linked. "
        "You can close this tab and return to the app.</p>"
        "<p><small>Kite access tokens expire daily around 06:00 IST — "
        "you'll repeat this login each trading day.</small></p>"
    )


# ─────────────────────────────────────────────────────────────────────
# List / Revoke
# ─────────────────────────────────────────────────────────────────────


@router.get(
    "/connections",
    response_model=list[BrokerConnectionResponse],
    response_model_by_alias=True,
)
async def list_connections(
    user: AuthedUser = Depends(require_real_auth),
    store: BrokerStore = Depends(get_broker_store),
) -> list[BrokerConnectionResponse]:
    rows = await store.list_connections(user.id)
    return [_to_response(r) for r in rows]


@router.delete(
    "/connections/{connection_id}",
    response_model=BrokerConnectionResponse,
    response_model_by_alias=True,
)
async def revoke_connection(
    connection_id: str,
    user: AuthedUser = Depends(require_real_auth),
    store: BrokerStore = Depends(get_broker_store),
) -> BrokerConnectionResponse:
    rec = await store.get_connection(connection_id)
    if rec is None or rec.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="connection not found",
        )

    revoked = await store.revoke_connection(connection_id)
    if not revoked:
        # Already revoked. We could 200-idempotent or 410-gone; pick 200 +
        # let the client see status='revoked' on the body.
        pass
    fresh = await store.get_connection(connection_id)
    assert fresh is not None
    return _to_response(fresh)
