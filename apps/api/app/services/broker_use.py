"""Decrypt-on-use helper for the user's active broker connection.

The plaintext access token MUST NOT live in memory longer than necessary.
This module exposes:

  - ``with_broker_client(user_id, broker=None)``: async context manager.
    Looks up the user's active broker connection (Alpaca or Zerodha),
    decrypts the access token, yields a ``BrokerInterface`` ready to call.
    On exit, drops the broker reference (and the underlying SDK client) so
    plaintext gets GC'd ASAP.

  - ``with_alpaca_client(user_id)``: legacy alias — Alpaca-only filter.

  - ``get_active_broker_connection(user_id, broker=None)``: read-only
    helper. Returns the encrypted row + connection metadata WITHOUT
    decrypting. Useful for routes that need "is this user connected?"
    without unsealing the token.

Broker selection when the user has multiple active connections:
``BROKER_PREFERENCE`` env (comma-separated, default ``alpaca,zerodha``)
decides which one the executor uses unless the caller passes ``broker=``
explicitly.

Zerodha specifics:
  - Kite access tokens expire daily (~06:00 IST). We check the stored
    ``access_token_expires_at`` BEFORE decrypting and raise a clear
    "reconnect Zerodha" error instead of letting Kite 403 confusingly.
  - The app-level ``KITE_API_KEY`` env pairs with the per-user token.

Architectural notes:
  - Decryption uses ``app.services.crypto`` which lazy-imports
    ``cryptography``. When the lib isn't installed, this module raises
    ``BrokerUnavailableError`` and the router translates to 503.
  - We deliberately don't pool / cache decrypted tokens. Holding the
    plaintext between requests is a vector we'd rather not have.
  - Audit logging is masked. Tokens never appear in logs.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, AsyncIterator

from app.services.broker_store import (
    BrokerConnectionRecord,
    BrokerStore,
    get_broker_store,
)
from app.services.crypto import (
    CryptoUnavailableError,
    decrypt_from_storage,
    is_available as crypto_available,
)

# Lazy imports: ``broker.alpaca`` pulls in the ``alpaca-py`` SDK which is
# declared in ``packages/broker/pyproject.toml`` but may not be
# ``uv sync``'d yet. The API needs to boot without it — the executor route
# returns 503 if a needed dep is missing. ``TYPE_CHECKING`` keeps the
# annotations available for static checkers without the runtime cost.
if TYPE_CHECKING:
    from broker.base import BrokerInterface

logger = logging.getLogger("api.broker_use")

SUPPORTED_BROKERS = ("alpaca", "zerodha")
DEFAULT_BROKER_PREFERENCE = "alpaca,zerodha"


class BrokerUnavailableError(Exception):
    """Raised when we can't get a broker client.

    Scenarios:
      - User has no active connection (router → 412 / 409).
      - ``cryptography`` isn't installed (router → 503).
      - Broker SDK / config missing (router → 503).
      - Zerodha daily token expired (router → 412 with reconnect hint).
    """


def _mask(token: str) -> str:
    if len(token) < 8:
        return "***"
    return f"{token[:4]}…{token[-4:]}"


def _broker_preference() -> list[str]:
    raw = os.environ.get("BROKER_PREFERENCE", "").strip() or DEFAULT_BROKER_PREFERENCE
    return [b.strip().lower() for b in raw.split(",") if b.strip()]


async def get_active_broker_connection(
    user_id: str,
    *,
    broker: str | None = None,
    store: BrokerStore | None = None,
) -> BrokerConnectionRecord | None:
    """Return the user's active connection, if any.

    ``broker=None`` walks ``BROKER_PREFERENCE`` order; an explicit broker
    name filters to that broker only.
    """
    s = store or get_broker_store()
    rows = await s.list_connections(user_id)
    active = [r for r in rows if r.status == "active"]
    if broker is not None:
        wanted = broker.lower()
        return next((r for r in active if r.broker == wanted), None)
    for preferred in _broker_preference():
        match = next((r for r in active if r.broker == preferred), None)
        if match is not None:
            return match
    return None


def _check_not_expired(conn: BrokerConnectionRecord) -> None:
    """Zerodha tokens die daily — fail fast with a reconnect hint."""
    if conn.access_token_expires_at is None:
        return
    expires = conn.access_token_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        raise BrokerUnavailableError(
            f"Stored {conn.broker} access token has expired"
            + (
                " — Kite tokens are flushed daily around 06:00 IST; "
                "reconnect Zerodha to trade today."
                if conn.broker == "zerodha"
                else " — reconnect the broker."
            )
        )


def _build_alpaca(access_token: str, conn: BrokerConnectionRecord) -> "BrokerInterface":
    try:
        from broker.alpaca import AlpacaBroker
    except ImportError as exc:
        raise BrokerUnavailableError(
            "Broker integration requires the 'alpaca-py' Python package. "
            "Run `uv sync` to install."
        ) from exc
    return AlpacaBroker.from_oauth_token(access_token, paper=conn.is_paper)


def _build_zerodha(access_token: str, conn: BrokerConnectionRecord) -> "BrokerInterface":
    from broker.zerodha import ZerodhaBroker  # httpx-only; always importable

    kite_api_key = os.environ.get("KITE_API_KEY", "").strip()
    if not kite_api_key:
        raise BrokerUnavailableError(
            "Zerodha connection exists but KITE_API_KEY is not set on the API."
        )
    return ZerodhaBroker(api_key=kite_api_key, access_token=access_token)


@asynccontextmanager
async def with_broker_client(
    user_id: str,
    *,
    broker: str | None = None,
    store: BrokerStore | None = None,
) -> AsyncIterator[tuple["BrokerInterface", BrokerConnectionRecord]]:
    """Yield a broker client configured with the user's decrypted token.

    Raises ``BrokerUnavailableError`` on:
      - no active connection (for the requested broker, if given)
      - ``cryptography`` not installed
      - broker SDK / env config missing
      - expired daily token (Zerodha)
      - decryption failure (tampered ciphertext / key rotation gone wrong)

    On exit, drops the broker + token references so the plaintext is
    eligible for GC. Python doesn't give us deterministic wipe of the
    string slab, but minimizing lifetime + scope is the right move.
    """
    if not crypto_available():
        raise BrokerUnavailableError(
            "Broker token decryption requires the 'cryptography' Python package. "
            "Run `uv sync` to install."
        )

    conn = await get_active_broker_connection(user_id, broker=broker, store=store)
    if conn is None:
        wanted = broker or "a broker"
        raise BrokerUnavailableError(
            f"No active {wanted} connection for this user — connect a broker first."
        )

    _check_not_expired(conn)

    try:
        access_token = decrypt_from_storage(conn.encrypted_access_token)
    except CryptoUnavailableError as exc:
        raise BrokerUnavailableError(str(exc)) from exc
    except Exception as exc:  # cryptography.fernet.InvalidToken
        # Don't leak the underlying error message — it can encode key
        # rotation hints + ciphertext shape.
        raise BrokerUnavailableError(
            f"Stored broker token could not be decrypted — re-connect {conn.broker}."
        ) from exc

    if conn.broker == "alpaca":
        client = _build_alpaca(access_token, conn)
    elif conn.broker == "zerodha":
        client = _build_zerodha(access_token, conn)
    else:
        raise BrokerUnavailableError(f"Unsupported broker '{conn.broker}'.")

    logger.info(
        "broker_use: opened %s client for user=%s conn=%s paper=%s token=%s",
        conn.broker, user_id, conn.id, conn.is_paper, _mask(access_token),
    )

    try:
        yield client, conn
    finally:
        # Drop our references. The SDK client + the bearer token live in
        # the broker client's internals; we can't deterministically wipe
        # them, but we can stop holding pointers so they're GC-eligible.
        del client
        access_token = "_dropped_"  # noqa: F841 — intent: rebind away from the secret


@asynccontextmanager
async def with_alpaca_client(
    user_id: str,
    *,
    store: BrokerStore | None = None,
) -> AsyncIterator[tuple["BrokerInterface", BrokerConnectionRecord]]:
    """Legacy Alpaca-only wrapper. Prefer ``with_broker_client``."""
    async with with_broker_client(user_id, broker="alpaca", store=store) as pair:
        yield pair
