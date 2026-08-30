"""Bootstrap an Alpaca PAPER connection from environment API keys.

Why this exists: the app's broker plumbing is OAuth-shaped — a
``broker_connections`` row holds an encrypted access token, and
``broker_use`` builds the client from it. That's right for real users,
who click "Connect Alpaca" and complete Alpaca's OAuth flow.

But an operator running their own deployment already has API keys in the
environment (``ALPACA_API_KEY`` / ``ALPACA_SECRET_KEY``) — the same keys
``engine.features`` and ``engine.prices`` use for market data. Without a
connection row those keys bought market data but no ability to trade:
the UI reported "No broker linked" and the executor had nothing to build
a client from.

The design mirrors the existing Zerodha precedent, where the API key
comes from the environment and only the session token is stored: we
persist a **sentinel** instead of a token, and ``broker_use._build_alpaca``
maps that sentinel to ``AlpacaBroker.from_env()``. No plaintext secret is
written to the database, and no migration is needed.

PAPER ONLY, deliberately. A bootstrapped connection is created with
``is_paper=True`` and ``live_trading_consent`` left False. Real money
still requires the explicit two-key gate (per-connection consent AND
``LIVE_TRADING_ENABLED``), which no automatic process may grant.

🚨 OWNER ALLOWLIST — see ``docs/PLAN_MULTI_TENANT.md`` §1. The env keys
belong to the OPERATOR's own Alpaca account (the one being traded /
scored), not to "whoever happens to be logged in." Before this gate
existed, EVERY new signup — magic-link or Google, first login, no exotic
path required — was silently handed a live, write-capable connection to
that same account: they could approve a pending proposal, close a
position, revoke the connection, or arm auto-approve, on the operator's
own book. ``_env_connection_allowlist()`` is checked inside
``ensure_env_broker_connection`` itself (the one function BOTH the
per-login catch-up in ``routers/auth.py`` and the boot-time fan-out in
``bootstrap_env_broker_connections`` call) so there is exactly one place
to gate, not two to remember — the CLAUDE.md §4.4 "same check in two
places" trap does not apply here by construction.
"""

from __future__ import annotations

import logging
import os

from app.services.broker.broker_store import BrokerStore, get_broker_store
from app.services.broker.crypto import encrypt_for_storage

logger = logging.getLogger("api.services.broker.env_bootstrap")

ALPACA_ENV_SENTINEL = "env:alpaca"
"""Stored in place of an OAuth token. ``broker_use`` reads it as
'authenticate with the process environment's API key + secret'."""


def env_keys_present() -> bool:
    """True when both Alpaca API credentials are configured."""
    return bool(
        os.environ.get("ALPACA_API_KEY", "").strip()
        and os.environ.get("ALPACA_SECRET_KEY", "").strip()
    )


def _env_connection_allowlist() -> set[str]:
    """User ids allowed to receive the env-sentinel Alpaca connection.

    ``ALPACA_ENV_CONNECTION_USER_IDS`` (comma-separated) if set — this is
    the explicit, reviewable list an operator running multiple named
    accounts would use. If unset, falls back to ``AGENT_CRON_USER_ID``
    ALONE: the one identity that already has to keep trading for the
    agent to function at all, and (per ``.env.example``) the same id
    ``FIXTURE_USER_ID``/``DEV_AUTH_BYPASS`` resolve to by convention, so a
    single-tenant dev/demo deployment that has only ever set that one var
    keeps working unchanged.

    Neither var set → empty set → NOBODY gets the env connection. That is
    deliberate and safe: it means "auto-attach the operator's account" is
    opt-in, not a default a forgotten env var quietly grants to strangers.
    """
    raw = os.environ.get("ALPACA_ENV_CONNECTION_USER_IDS", "").strip()
    if raw:
        return {uid.strip() for uid in raw.split(",") if uid.strip()}
    fallback = os.environ.get("AGENT_CRON_USER_ID", "").strip()
    return {fallback} if fallback else set()


def _is_paper_env() -> bool:
    """Paper unless ALPACA_BASE_URL explicitly names a live endpoint.

    Defaults to paper: an ambiguous or unset base URL must never resolve
    to real money.
    """
    base = os.environ.get("ALPACA_BASE_URL", "").strip().lower()
    return "paper" in base if base else True


async def ensure_env_broker_connection(
    user_id: str,
    *,
    store: BrokerStore | None = None,
) -> bool:
    """Create the env-key Alpaca paper connection for ``user_id`` if absent.

    Idempotent: returns False when the keys aren't set, when the
    environment is live rather than paper, when ``user_id`` is not on the
    owner allowlist (see the module docstring — this is the multi-tenant
    fix), or when an active Alpaca connection already exists. Never raises
    into the caller — a failure here must not stop the API from booting.
    """
    if not env_keys_present():
        return False

    if not _is_paper_env():
        # Live keys are never auto-connected. Connecting real money is an
        # explicit, human, per-connection decision.
        logger.warning(
            "ALPACA_BASE_URL points at a live endpoint — refusing to "
            "auto-create a broker connection. Connect live accounts "
            "explicitly via OAuth."
        )
        return False

    if user_id not in _env_connection_allowlist():
        # NOT a live-mode-style warning: this is the normal, expected
        # outcome for every real signup/judge login, so it stays at info
        # level rather than paging anyone.
        logger.info(
            "env broker bootstrap: user=%s is not on ALPACA_ENV_CONNECTION_USER_IDS "
            "(or the AGENT_CRON_USER_ID fallback) — refusing to auto-attach the "
            "operator's own Alpaca connection. They can connect their own via OAuth.",
            user_id,
        )
        return False

    st = store or get_broker_store()
    try:
        existing = await st.list_connections(user_id)
        if any(c.broker == "alpaca" and c.status == "active" for c in existing):
            return False

        await st.upsert_connection(
            user_id=user_id,
            broker="alpaca",
            is_paper=True,
            account_number=None,
            encrypted_access_token=encrypt_for_storage(ALPACA_ENV_SENTINEL),
            encrypted_refresh_token=None,
            # API keys don't expire the way OAuth tokens do, so there is
            # nothing for the expiry check to enforce.
            access_token_expires_at=None,
        )
    except Exception:  # noqa: BLE001 — bootstrap is best-effort, never fatal
        logger.exception("env broker bootstrap failed — continuing without it")
        return False

    logger.info("Alpaca PAPER connection bootstrapped from environment API keys")
    return True


async def bootstrap_env_broker_connections(
    *,
    use_pg: bool,
    store: BrokerStore | None = None,
) -> tuple[int, int]:
    """Run ``ensure_env_broker_connection`` for every user this process can
    reach, at boot, regardless of which store backs the deployment.

    This is the call site logic — deliberately gated ONLY by
    ``env_keys_present()`` (checked first, so a deployment with no Alpaca
    keys does zero extra work) and never by ``USE_POSTGRES`` or
    ``RECONCILER_ENABLED``. Those two used to gate the only call site that
    existed, which meant the bootstrap silently never ran under MockStore
    (``USE_POSTGRES=0``, the shipped default) or whenever an operator set
    ``RECONCILER_ENABLED=0`` on Postgres — exactly the runtimes where a
    restart leaving every user disconnected hurts most.

    ``use_pg=True`` enumerates every ``User`` row via Postgres — the same
    fan-out the old call site did. ``use_pg=False`` can't do that:
    ``MockAuthStore`` has no "list all users" accessor (``_users`` is a
    private dict), so instead this targets exactly ``FIXTURE_USER_ID`` —
    the one identity guaranteed to survive a MockStore restart
    (``MockAuthStore._seed_fixture()`` reseeds it on every construction),
    and the same id ``DEV_AUTH_BYPASS`` resolves to and ``.env.example``'s
    ``AGENT_CRON_USER_ID`` already targets by convention.

    Returns ``(created_count, considered_count)``. Best-effort: a failure
    for one user (surfaced as a log line by ``ensure_env_broker_connection``)
    never stops the rest, and this never raises into the caller — boot must
    not fail because of this.
    """
    if not env_keys_present():
        return (0, 0)

    if use_pg:
        # Import lazily so MockStore code paths never pull Postgres in.
        from sqlalchemy import select

        from engine.db.models import User
        from engine.db.session import async_session_factory

        try:
            session_factory = async_session_factory()
            async with session_factory() as session:
                rows = (await session.execute(select(User.id))).scalars().all()
        except Exception:  # noqa: BLE001 — best-effort; a DB hiccup here must
            # not fail the whole app boot over a feature that's allowed to
            # catch up on the next restart.
            logger.exception("env broker bootstrap: could not enumerate users — skipping")
            return (0, 0)
        user_ids = [str(uid) for uid in rows]
    else:
        from app.services.auth.auth_store import FIXTURE_USER_ID

        user_ids = [FIXTURE_USER_ID]

    created = 0
    for uid in user_ids:
        if await ensure_env_broker_connection(uid, store=store):
            created += 1
    return created, len(user_ids)
