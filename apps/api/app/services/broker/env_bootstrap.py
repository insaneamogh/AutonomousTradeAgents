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
    environment is live rather than paper, or when an active Alpaca
    connection already exists. Never raises into the caller — a failure
    here must not stop the API from booting.
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
