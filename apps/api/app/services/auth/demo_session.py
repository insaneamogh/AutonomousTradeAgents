"""Demo-session issuance — a read-only link that resolves to the cron
user's REAL data for judges/reviewers, without a single new authorization
check.

The whole design is one reused field: ``AuthedUser.is_dev_bypass=True``.
Every money-moving route already calls ``require_real_auth`` (see
``app.middleware.auth``), which refuses any ``AuthedUser`` with that flag
set. A demo session sets it and nothing else has to change — see
``docs/IMPL_DEMO_SESSION.md``.

Two tokens, two different ``typ``s, two different lifetimes:

  1. The **demo** token (``typ="demo"``, ``DEMO_TOKEN_TTL_DAYS`` — long
     enough to outlast the submission window). This is what's embedded in
     the judge's link (``?demo=<token>``), minted OFFLINE by
     ``scripts/mint_demo_link.py`` — never by a request handler. It is
     never accepted anywhere an access token is expected: ``verify_access``
     requires ``typ == "access"``, and this token's ``typ`` is ``"demo"``.

  2. A normal short-lived **access** token (``typ="access"``,
     ``ACCESS_TOKEN_TTL`` = 15 minutes, same as every other access token),
     minted by ``exchange_demo_token`` below when the client redeems the
     demo token via ``POST /api/v1/auth/demo``. It carries an
     ``extra={"demo": True}`` marker so ``get_current_user`` resolves it to
     the demo identity instead of a normal Bearer-token user lookup.

No session row, no refresh token: a demo session simply expires and the
judge re-opens the link.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.services.auth.jwt_service import (
    ACCESS_TOKEN_TTL,
    Claims,
    TokenError,
    mint_access,
    mint_demo,
    verify_demo,
)
from engine.env import env_flag

DEMO_CLAIM_KEY = "demo"
"""``extra`` dict key on a demo-marked access token's claims."""

DEMO_USER_EMAIL = "demo@judges.invalid"
"""Fixed, display-only placeholder — deliberately NOT a lookup against the
auth store, so a demo session never surfaces the cron user's real email."""

_DEFAULT_TTL_DAYS = 14.0


class DemoSessionError(Exception):
    """Base for a failed demo-token exchange. Router maps to 4xx/5xx."""


class DemoSessionDisabled(DemoSessionError):
    """``DEMO_SESSION_ENABLED`` is off, or no demo user id is configured."""


class DemoTokenInvalid(DemoSessionError):
    """Bad signature, wrong ``typ``, or expired."""


def demo_session_enabled() -> bool:
    return env_flag("DEMO_SESSION_ENABLED")


def demo_user_id() -> str:
    """The account a demo session resolves to.

    ``DEMO_USER_ID`` if set, else ``AGENT_CRON_USER_ID`` — the same
    fallback convention ``env_bootstrap._env_connection_allowlist`` already
    uses, since the cron user is the one identity that has to exist for
    the agent to trade at all. Empty when neither is set; callers must
    treat that as "not configured", never as a real (empty-string) id.
    """
    explicit = os.environ.get("DEMO_USER_ID", "").strip()
    if explicit:
        return explicit
    return os.environ.get("AGENT_CRON_USER_ID", "").strip()


def demo_token_ttl() -> timedelta:
    """``DEMO_TOKEN_TTL_DAYS`` (default 14) as a ``timedelta``. Falls back
    to the default on anything unset or unparseable, rather than raising —
    this only sizes a link's validity window, not a security boundary."""
    raw = os.environ.get("DEMO_TOKEN_TTL_DAYS", "").strip()
    try:
        days = float(raw) if raw else _DEFAULT_TTL_DAYS
    except ValueError:
        days = _DEFAULT_TTL_DAYS
    return timedelta(days=days)


def mint_demo_link_token(*, secret: str) -> str:
    """Mint the long-lived token embedded in the judge's demo link.

    Used by ``scripts/mint_demo_link.py`` only — never by a request
    handler (there is no route that mints a demo token; only one that
    exchanges an already-minted one).
    """
    uid = demo_user_id()
    if not uid:
        raise DemoSessionDisabled(
            "DEMO_USER_ID (or AGENT_CRON_USER_ID) is not set — cannot mint a demo link"
        )
    expires_at = datetime.now(UTC) + demo_token_ttl()
    return mint_demo(secret=secret, user_id=uid, expires_at=expires_at)


@dataclass(frozen=True)
class DemoAccess:
    user_id: str
    access_token: str
    access_expires_in_seconds: int


def exchange_demo_token(*, token: str, secret: str) -> DemoAccess:
    """Verify a demo-link token and mint a short-lived, demo-marked access
    token in its place.

    Raises ``DemoSessionDisabled`` when the feature is off/unconfigured,
    ``DemoTokenInvalid`` on any bad/expired/wrong-typ token — the router
    maps the two to 503 and 401 respectively.
    """
    if not demo_session_enabled():
        raise DemoSessionDisabled("demo sessions are disabled on this server")
    if not demo_user_id():
        raise DemoSessionDisabled(
            "DEMO_USER_ID (or AGENT_CRON_USER_ID) is not configured on this server"
        )

    try:
        claims: Claims = verify_demo(secret=secret, token=token)
    except TokenError as exc:
        raise DemoTokenInvalid(f"invalid demo token: {exc}") from exc

    access_token = mint_access(
        secret=secret,
        user_id=claims.sub,
        session_id=None,
        extra={DEMO_CLAIM_KEY: True},
    )
    return DemoAccess(
        user_id=claims.sub,
        access_token=access_token,
        access_expires_in_seconds=int(ACCESS_TOKEN_TTL.total_seconds()),
    )


def is_demo_claims(claims: Claims) -> bool:
    """True when an ALREADY-VERIFIED access token's claims carry the demo
    marker. Only meaningful post-``verify_access`` — a demo-``typ`` token
    never reaches this path at all (rejected earlier by the ``typ`` check)."""
    return bool(claims.extra and claims.extra.get(DEMO_CLAIM_KEY) is True)
