"""Google ID-token verification — "Continue with Google" login.

Mirrors ``apps.api.app.services.broker.alpaca_oauth``'s shape: an injectable
``httpx.AsyncClient`` so tests can stub the network via ``httpx.MockTransport``
instead of hitting a real provider, and a small set of module-level functions
rather than a class.

This module answers exactly one question: "is this Google ID token genuinely
Google's, unexpired, meant for us, and for a verified email?" It does NOT
touch our own session/user state — ``app.services.auth.auth.login_with_google``
does that once it has a verified ``GoogleIdentity`` in hand.

We use ``python-jose`` (already a declared + installed dependency — see
``apps/api/pyproject.toml``) rather than Google's own ``google-auth`` package:
``google-auth``'s default HTTP transport is synchronous and would block the
event loop on every JWKS fetch, which is a non-starter in this async-first
codebase (CLAUDE.md).

Verification order matters and is deliberately front-loaded with the
cheapest, most attacker-hostile checks first:

  1. Lock ``alg`` to RS256 from the UNVERIFIED header, before touching the
     network or any key material. Refuses the classic RS256→HS256 (or
     ``alg: none``) confusion attack the same way ``jwt_service.verify``
     locks our own tokens to HS256.
  2. Resolve the signing key by ``kid`` against a small in-memory JWKS
     cache (Google rotates keys occasionally; refetching on every request
     would be both slow and rude to Google's servers). An unknown ``kid``
     forces exactly one refetch — covers the normal rotation window — and
     only fails after that.
  3. Signature + ``exp`` verification via jose against the resolved JWK.
  4. ``aud`` and ``iss`` checked BY US, not jose: ``aud`` must be one of
     the caller-supplied client ids (Google issues a separate client id per
     platform for one logical app — jose's ``audience=`` only accepts a
     single expected value, not "one of several"), and ``iss`` must be one
     of Google's two documented issuer strings (jose's built-in issuer
     check only takes one value; Google documents both
     ``accounts.google.com`` and ``https://accounts.google.com`` as valid).
  5. ``email_verified`` must be literally ``True`` — required, not
     optional. An unverified email claim must never create or link an
     account.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from jose import jwt as jose_jwt
from jose.exceptions import JWTError

# Google's published JWKS for ID-token verification. Not env-overridable —
# tests inject a mocked ``client`` instead of pointing at a different URL
# (see apps/api/tests/test_auth_google.py), so there's no operational need
# for an env var here the way ALPACA_TOKEN_URL exists for staging swaps.
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

# Google documents BOTH forms as valid ``iss`` values. jose's own issuer
# check only accepts a single expected string, so we verify this ourselves.
_VALID_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})

# How long a fetched JWKS is trusted before we'll refetch on a cache HIT.
# Independent of the unknown-``kid``-forces-a-refetch path below, which
# always bypasses this TTL.
_JWKS_CACHE_TTL_SECONDS = 3600.0


class GoogleAuthError(Exception):
    """The presented Google ID token itself is invalid: bad signature, wrong
    audience/issuer, expired, unverified email, malformed. Router translates
    this to 401 — it is the CALLER's problem, not ours."""


class GoogleJWKSFetchError(Exception):
    """We could not reach or parse Google's JWKS endpoint. NOT the caller's
    fault — router translates this to 503 so a client knows to retry rather
    than treating its token as bad."""


@dataclass(frozen=True)
class GoogleIdentity:
    """The claims we trust once verification has fully passed."""

    email: str
    email_verified: bool
    sub: str
    name: str | None


# ─────────────────────────────────────────────────────────────────────
# JWKS cache
#
# Module-level + a plain asyncio.Lock: this process runs one event loop,
# and the alternative (refetching Google's JWKS on every login) is both
# slow and needlessly hostile to Google's servers. ``reset_..._for_tests``
# exists so test files that mock the JWKS endpoint don't leak a cached key
# set into a later test expecting a different one.
# ─────────────────────────────────────────────────────────────────────

_jwks_cache: dict[str, dict[str, Any]] = {}
_jwks_fetched_at: float = 0.0
_jwks_lock = asyncio.Lock()


async def _fetch_jwks(*, client: httpx.AsyncClient | None) -> dict[str, dict[str, Any]]:
    """One network round-trip to Google's JWKS endpoint. ``client`` is
    injectable so tests can pass a transport-mocked ``httpx.AsyncClient``
    (same convention as ``alpaca_oauth.exchange_code_for_tokens``)."""
    owned = False
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)
        owned = True

    try:
        resp = await client.get(GOOGLE_JWKS_URL)
    except httpx.HTTPError as exc:
        raise GoogleJWKSFetchError(f"network error reaching Google JWKS: {exc}") from exc
    finally:
        if owned:
            await client.aclose()

    if resp.status_code >= 400:
        raise GoogleJWKSFetchError(f"JWKS endpoint returned {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise GoogleJWKSFetchError("JWKS endpoint returned invalid JSON") from exc

    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        raise GoogleJWKSFetchError("JWKS endpoint response had no 'keys' array")

    return {k["kid"]: k for k in keys if isinstance(k, dict) and "kid" in k}


async def _get_jwks(
    *, client: httpx.AsyncClient | None, force: bool = False
) -> dict[str, dict[str, Any]]:
    """Return the cached JWKS, refetching when stale or ``force=True``.

    ``force`` is used exactly once per verification when a token's ``kid``
    isn't in the current cache — a real Google key rotation resolves on
    that single refetch; anything still missing afterward is a genuinely
    bad token, not a caching problem.
    """
    global _jwks_cache, _jwks_fetched_at

    now = time.monotonic()
    fresh = bool(_jwks_cache) and (now - _jwks_fetched_at) < _JWKS_CACHE_TTL_SECONDS
    if fresh and not force:
        return _jwks_cache

    async with _jwks_lock:
        now = time.monotonic()
        fresh = bool(_jwks_cache) and (now - _jwks_fetched_at) < _JWKS_CACHE_TTL_SECONDS
        if fresh and not force:
            return _jwks_cache
        _jwks_cache = await _fetch_jwks(client=client)
        _jwks_fetched_at = time.monotonic()
        return _jwks_cache


def reset_google_jwks_cache_for_tests() -> None:
    """Drop the cached JWKS. Tests call this so a mocked key set from one
    test can't leak into the next."""
    global _jwks_cache, _jwks_fetched_at
    _jwks_cache = {}
    _jwks_fetched_at = 0.0


# ─────────────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────────────


async def verify_google_id_token(
    id_token: str,
    *,
    audience: Sequence[str],
    client: httpx.AsyncClient | None = None,
) -> GoogleIdentity:
    """Verify a Google-issued OIDC ID token end to end.

    ``audience`` is the caller-supplied list of acceptable client ids
    (``Settings.google_oauth_client_ids``) — passed explicitly rather than
    read from settings here so this function stays a pure, independently
    testable unit with no hidden global reads.

    Raises ``GoogleAuthError`` for anything about the TOKEN being wrong,
    and ``GoogleJWKSFetchError`` if we simply couldn't reach/parse Google's
    key endpoint — the router maps these to 401 and 503 respectively.
    """
    if not id_token or not isinstance(id_token, str):
        raise GoogleAuthError("empty id_token")

    try:
        header = jose_jwt.get_unverified_header(id_token)
    except JWTError as exc:
        raise GoogleAuthError(f"malformed token header: {exc}") from exc

    # Alg-confusion guard, checked BEFORE any network call or key lookup —
    # never let an attacker-chosen alg (e.g. "none", or HS256 reusing a
    # public RSA key as an HMAC secret) drive which verification path runs.
    alg = header.get("alg")
    if alg != "RS256":
        raise GoogleAuthError(f"unexpected alg {alg!r} — only RS256 accepted")

    kid = header.get("kid")
    if not kid or not isinstance(kid, str):
        raise GoogleAuthError("token header missing kid")

    keys = await _get_jwks(client=client)
    jwk_key = keys.get(kid)
    if jwk_key is None:
        # Possible key rotation — force exactly one refetch before failing.
        keys = await _get_jwks(client=client, force=True)
        jwk_key = keys.get(kid)
    if jwk_key is None:
        raise GoogleAuthError(f"no matching Google signing key for kid={kid!r}")

    try:
        claims = jose_jwt.decode(
            id_token,
            jwk_key,
            algorithms=["RS256"],
            options={
                # We check aud/iss ourselves below — aud because Google's
                # multi-platform client ids mean "one of several", which
                # jose's single-value ``audience=`` can't express; iss
                # because Google documents TWO valid values and jose's
                # built-in check only accepts one.
                "verify_aud": False,
                "verify_iss": False,
                "require_exp": True,
            },
        )
    except JWTError as exc:
        raise GoogleAuthError(f"token verification failed: {exc}") from exc
    except (KeyError, ValueError, TypeError) as exc:
        # Malformed JWK / claims shape jose didn't turn into a JWTError.
        raise GoogleAuthError(f"token verification failed: {exc}") from exc

    aud = claims.get("aud")
    if aud not in audience:
        raise GoogleAuthError(f"unexpected audience {aud!r}")

    iss = claims.get("iss")
    if iss not in _VALID_ISSUERS:
        raise GoogleAuthError(f"unexpected issuer {iss!r}")

    # Required, not optional: an unverified email must never create/link
    # an account. Strict identity check — no truthy-string leniency.
    if claims.get("email_verified") is not True:
        raise GoogleAuthError("Google account email is not verified")

    email = claims.get("email")
    sub = claims.get("sub")
    if not isinstance(email, str) or not email:
        raise GoogleAuthError("token missing email claim")
    if not isinstance(sub, str) or not sub:
        raise GoogleAuthError("token missing sub claim")

    name = claims.get("name")
    return GoogleIdentity(
        email=email.strip().lower(),
        email_verified=True,
        sub=sub,
        name=name if isinstance(name, str) else None,
    )
