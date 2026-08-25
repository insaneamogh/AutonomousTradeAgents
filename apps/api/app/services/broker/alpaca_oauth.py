"""Alpaca OAuth — PKCE authorize URL + token exchange.

PLAN.md §3 wants per-user OAuth so each broker connection holds its own
credentials (and so revoking a user invalidates their broker access). The
env-driven ``ALPACA_API_KEY`` path stays for the paper smoke / dev runs but
real users go through here.

OAuth flow (PKCE, RFC 7636):
    1. /broker/connect/alpaca/start
       Server picks ``state`` (CSRF guard) + ``code_verifier`` (PKCE).
       Stashes (user_id, state, code_verifier) in pending-OAuth cache.
       Returns ``authorize_url`` (with ``code_challenge`` derived from
       the verifier) + the ``state`` token for the mobile to round-trip.

    2. User opens ``authorize_url`` in the system browser → grants access
       → Alpaca redirects to ``redirect_uri?code=...&state=...``.

    3. /broker/connect/alpaca/callback
       Server verifies ``state`` matches the stash, exchanges ``code`` for
       tokens (POSTs to ``/oauth/token`` with the original ``code_verifier``),
       encrypts the returned tokens, upserts the ``broker_connections`` row.

Endpoints (configurable):
    Authorize: https://app.alpaca.markets/oauth/authorize
    Token:     https://api.alpaca.markets/oauth/token

The token endpoint base is configurable so tests can stub it with a mock
URL and so paper vs live can use different bases when Alpaca splits them.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("api.alpaca_oauth")


# Alpaca's published OAuth endpoints. Override via env for tests / staging.
DEFAULT_AUTHORIZE_URL = "https://app.alpaca.markets/oauth/authorize"
DEFAULT_TOKEN_URL = "https://api.alpaca.markets/oauth/token"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


def authorize_endpoint() -> str:
    return _env("ALPACA_AUTHORIZE_URL", DEFAULT_AUTHORIZE_URL)


def token_endpoint() -> str:
    return _env("ALPACA_TOKEN_URL", DEFAULT_TOKEN_URL)


def client_id() -> str:
    """OAuth client id. The user is expected to set ``ALPACA_OAUTH_CLIENT_ID``
    via Doppler in prod. Local dev uses a clearly-fake value.
    """
    return _env("ALPACA_OAUTH_CLIENT_ID", "DEV-ALPACA-CLIENT-ID")


def client_secret() -> str:
    return _env("ALPACA_OAUTH_CLIENT_SECRET", "DEV-ALPACA-CLIENT-SECRET")


def default_redirect_uri() -> str:
    """The deep-link the mobile app registers. Hardcoded since the scheme
    is part of ``app.json`` and won't vary across environments.
    """
    return _env("ALPACA_OAUTH_REDIRECT_URI", "autotrader://broker/callback")


# ─────────────────────────────────────────────────────────────────────
# PKCE primitives
# ─────────────────────────────────────────────────────────────────────


def new_state() -> str:
    """CSRF token. High-entropy URL-safe. Matched in the callback."""
    return secrets.token_urlsafe(24)


def new_code_verifier() -> str:
    """RFC 7636 §4.1 — 43..128 chars URL-safe-alphabet. token_urlsafe(64)
    gives ~86 chars which is comfortably inside the range.
    """
    return secrets.token_urlsafe(64)


def code_challenge_from_verifier(verifier: str) -> str:
    """RFC 7636 §4.2 — base64url(sha256(verifier)) with no padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ─────────────────────────────────────────────────────────────────────
# Authorize URL
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AuthorizeBuild:
    authorize_url: str
    state: str
    code_verifier: str
    """The plain verifier — server stashes this; mobile NEVER sees it."""


def build_authorize_url(
    *,
    scopes: tuple[str, ...] = ("account:write", "trading"),
    redirect_uri: str | None = None,
) -> AuthorizeBuild:
    """Construct an /oauth/authorize URL with PKCE.

    The caller (start route) is responsible for stashing ``state`` +
    ``code_verifier`` in the pending-OAuth cache before returning the URL
    to the client. Mobile only ever sees ``authorize_url`` + ``state``.
    """
    state = new_state()
    verifier = new_code_verifier()
    challenge = code_challenge_from_verifier(verifier)

    params = {
        "response_type": "code",
        "client_id": client_id(),
        "redirect_uri": redirect_uri or default_redirect_uri(),
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return AuthorizeBuild(
        authorize_url=f"{authorize_endpoint()}?{urlencode(params)}",
        state=state,
        code_verifier=verifier,
    )


# ─────────────────────────────────────────────────────────────────────
# Token exchange
# ─────────────────────────────────────────────────────────────────────


class TokenExchangeError(Exception):
    """Network / OAuth failure on the token endpoint. Routers translate to 502."""


@dataclass(frozen=True)
class IssuedBrokerTokens:
    access_token: str
    refresh_token: str
    """May be empty if Alpaca returns no refresh — e.g. very short scopes."""
    expires_in_seconds: int
    token_type: str
    scope: str
    account_number: str | None
    raw: dict[str, object]
    """Full JSON for audit / unknown-field discovery."""


async def exchange_code_for_tokens(
    *,
    code: str,
    code_verifier: str,
    redirect_uri: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> IssuedBrokerTokens:
    """Exchange the auth code for an access + refresh pair.

    ``client`` is injectable so tests can pass a transport-mocked
    ``httpx.AsyncClient``. Production uses a fresh client per call —
    OAuth exchange isn't latency-critical + a long-lived pool would
    outlive the request that owns it.
    """
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri or default_redirect_uri(),
        "client_id": client_id(),
        "client_secret": client_secret(),
        "code_verifier": code_verifier,
    }
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "accept": "application/json",
    }

    owned = False
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)
        owned = True

    try:
        resp = await client.post(token_endpoint(), data=body, headers=headers)
    except httpx.HTTPError as exc:
        raise TokenExchangeError(f"network error reaching Alpaca: {exc}") from exc
    finally:
        if owned:
            await client.aclose()

    if resp.status_code >= 400:
        # Don't log the body — it may include client_secret echo / sensitive
        # error context. Status + truncated message is enough for triage.
        snippet = resp.text[:200]
        logger.warning("alpaca token exchange failed: %s — %s", resp.status_code, snippet)
        raise TokenExchangeError(f"token endpoint returned {resp.status_code}")

    data = resp.json()
    return IssuedBrokerTokens(
        access_token=str(data.get("access_token", "")),
        refresh_token=str(data.get("refresh_token", "")),
        expires_in_seconds=int(data.get("expires_in", 0) or 0),
        token_type=str(data.get("token_type", "Bearer")),
        scope=str(data.get("scope", "")),
        account_number=(
            str(data["account_number"]) if data.get("account_number") else None
        ),
        raw=data if isinstance(data, dict) else {},
    )
