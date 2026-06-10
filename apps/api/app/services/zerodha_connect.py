"""Zerodha (Kite Connect) connect flow — login URL + request-token exchange.

Kite's flow is NOT OAuth (no PKCE, no refresh tokens):

    1. /broker/connect/zerodha/start
       Server picks a ``state`` (CSRF guard), stashes (user_id, state) in
       the pending-OAuth cache, and returns the Kite login URL with
       ``redirect_params=state%3D<state>`` so Zerodha echoes the state back
       on the redirect.

    2. User logs in at kite.zerodha.com → Zerodha redirects to the app's
       REGISTERED redirect URL with ``request_token=…&state=<state>``.
       (The redirect URL is configured in the Kite developer console and
       must point at this API's /connect/zerodha/redirect for the browser
       flow, or the mobile app forwards the params to /callback.)

    3. /broker/connect/zerodha/callback (or the GET /redirect variant)
       Server verifies the state, exchanges the request_token
       (sha256 checksum with the app secret), encrypts the access token,
       upserts the broker_connections row with broker='zerodha'.

Credentials model: ``KITE_API_KEY`` + ``KITE_API_SECRET`` are app-level env
(the operator's personal Kite Connect app), unlike Alpaca where each user
OAuths against Alpaca's own client registry. The per-user secret is only
the daily ``access_token``.

Token lifetime: Kite flushes access tokens ~06:00 IST daily. We store the
computed expiry so ``broker_use`` can say "reconnect Zerodha" instead of
surfacing a raw Kite 403.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from broker.zerodha import (
    ZerodhaError,
    exchange_request_token as _kite_exchange,
    login_url as _kite_login_url,
    next_token_expiry,
)

from app.services.alpaca_oauth import new_state

logger = logging.getLogger("api.zerodha_connect")


class ZerodhaNotConfiguredError(Exception):
    """KITE_API_KEY / KITE_API_SECRET missing — routers translate to 503."""


class RequestTokenExchangeError(Exception):
    """Kite session-token exchange failed — routers translate to 502."""


def api_key() -> str:
    return os.environ.get("KITE_API_KEY", "").strip()


def api_secret() -> str:
    return os.environ.get("KITE_API_SECRET", "").strip()


def is_configured() -> bool:
    return bool(api_key() and api_secret())


def require_configured() -> None:
    if not is_configured():
        raise ZerodhaNotConfiguredError(
            "Zerodha is not configured — set KITE_API_KEY and KITE_API_SECRET "
            "(from your Kite Connect app at developers.kite.trade)."
        )


@dataclass(frozen=True)
class LoginBuild:
    login_url: str
    state: str


def build_login_url() -> LoginBuild:
    """Kite login URL with our CSRF state riding on ``redirect_params``."""
    require_configured()
    state = new_state()
    return LoginBuild(
        login_url=_kite_login_url(api_key(), redirect_params=f"state={state}"),
        state=state,
    )


@dataclass(frozen=True)
class ZerodhaSession:
    access_token: str
    user_id: str
    """Zerodha client id, e.g. 'AB1234' — stored as account_number."""
    user_name: str
    expires_at: Any
    """UTC datetime of the next ~06:00 IST token flush."""
    raw: dict[str, Any]


async def exchange_request_token(
    *,
    request_token: str,
    client: httpx.AsyncClient | None = None,
) -> ZerodhaSession:
    """Exchange the single-use request_token for a daily access token."""
    require_configured()
    try:
        data = await _kite_exchange(
            api_key=api_key(),
            api_secret=api_secret(),
            request_token=request_token,
            client=client,
        )
    except ZerodhaError as exc:
        logger.warning("zerodha request-token exchange failed: %s", exc)
        raise RequestTokenExchangeError(str(exc)) from exc

    return ZerodhaSession(
        access_token=str(data["access_token"]),
        user_id=str(data.get("user_id", "")),
        user_name=str(data.get("user_name", "")),
        expires_at=next_token_expiry(),
        raw=data,
    )
