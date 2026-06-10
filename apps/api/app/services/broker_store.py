"""BrokerStore — broker_connections backing + pending-OAuth cache.

Two collaborators bundled into one module because they're tightly coupled
in the OAuth flow:

  - ``BrokerStore`` — one row per ``(user_id, broker, is_paper)``. Encrypted
    tokens at rest (see ``app.services.crypto``).
  - ``PendingOAuthCache`` — short-lived (state → (user_id, code_verifier))
    map. Lives ONLY for the round-trip between /start and /callback.
    Phase 3 keeps it in-memory; Phase 3.2 migrates to Redis.

Same Protocol-+-Mock-+-Postgres-later pattern the rest of the app uses.
Postgres impl is wired against ``broker_connections`` from migration 0001;
deferred to a follow-on session.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

logger = logging.getLogger("api.broker_store")


# How long a pending-OAuth entry sits in the cache before it's auto-evicted.
# Long enough that a slow OAuth round-trip (broker login + 2FA + redirect)
# succeeds; short enough that a stale state token can't be redeemed days
# later. Mirrors apps/api/app/services/auth.py magic-link TTL.
PENDING_OAUTH_TTL = timedelta(minutes=15)


@dataclass
class BrokerConnectionRecord:
    id: str
    user_id: str
    broker: str
    """e.g. 'alpaca'. Lowercased."""
    is_paper: bool
    account_number: str | None
    encrypted_access_token: str
    encrypted_refresh_token: str | None
    access_token_expires_at: datetime | None
    refresh_token_expires_at: datetime | None
    status: str = "active"
    """active | revoked | expired."""
    last_used_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@runtime_checkable
class BrokerStore(Protocol):
    async def upsert_connection(
        self,
        *,
        user_id: str,
        broker: str,
        is_paper: bool,
        account_number: str | None,
        encrypted_access_token: str,
        encrypted_refresh_token: str | None,
        access_token_expires_at: datetime | None,
    ) -> BrokerConnectionRecord: ...

    async def list_connections(self, user_id: str) -> list[BrokerConnectionRecord]: ...

    async def list_active_connections_by_broker(
        self, broker: str
    ) -> list[BrokerConnectionRecord]: ...

    async def get_connection(self, connection_id: str) -> BrokerConnectionRecord | None: ...

    async def revoke_connection(self, connection_id: str) -> bool: ...


class InMemoryBrokerStore:
    """Default in-memory backing. Lives only while the API process is up."""

    def __init__(self) -> None:
        self._rows: dict[str, BrokerConnectionRecord] = {}

    async def upsert_connection(
        self,
        *,
        user_id: str,
        broker: str,
        is_paper: bool,
        account_number: str | None,
        encrypted_access_token: str,
        encrypted_refresh_token: str | None,
        access_token_expires_at: datetime | None,
    ) -> BrokerConnectionRecord:
        broker = broker.lower()
        # The (user_id, broker, is_paper) tuple is the unique key per
        # migration 0001's uq_broker_connections_user_broker_env. Find an
        # existing active row + overwrite it; otherwise create.
        existing = next(
            (
                r for r in self._rows.values()
                if r.user_id == user_id
                and r.broker == broker
                and r.is_paper == is_paper
                and r.status == "active"
            ),
            None,
        )
        now = datetime.now(timezone.utc)
        if existing is not None:
            existing.account_number = account_number
            existing.encrypted_access_token = encrypted_access_token
            existing.encrypted_refresh_token = encrypted_refresh_token
            existing.access_token_expires_at = access_token_expires_at
            existing.updated_at = now
            return existing

        rec = BrokerConnectionRecord(
            id=str(uuid.uuid4()),
            user_id=user_id,
            broker=broker,
            is_paper=is_paper,
            account_number=account_number,
            encrypted_access_token=encrypted_access_token,
            encrypted_refresh_token=encrypted_refresh_token,
            access_token_expires_at=access_token_expires_at,
            refresh_token_expires_at=None,
        )
        self._rows[rec.id] = rec
        return rec

    async def list_connections(self, user_id: str) -> list[BrokerConnectionRecord]:
        return [r for r in self._rows.values() if r.user_id == user_id]

    async def list_active_connections_by_broker(
        self, broker: str
    ) -> list[BrokerConnectionRecord]:
        """All ACTIVE connections for one broker, across users. Cron fan-out
        (e.g. the Zerodha daily-reconnect reminder) iterates this.
        """
        broker = broker.lower()
        return [
            r for r in self._rows.values()
            if r.broker == broker and r.status == "active"
        ]

    async def get_connection(self, connection_id: str) -> BrokerConnectionRecord | None:
        return self._rows.get(connection_id)

    async def revoke_connection(self, connection_id: str) -> bool:
        rec = self._rows.get(connection_id)
        if rec is None or rec.status == "revoked":
            return False
        rec.status = "revoked"
        rec.encrypted_access_token = ""
        rec.encrypted_refresh_token = None
        rec.updated_at = datetime.now(timezone.utc)
        return True


# ─────────────────────────────────────────────────────────────────────
# Pending-OAuth cache
# ─────────────────────────────────────────────────────────────────────


@dataclass
class PendingOAuth:
    state: str
    user_id: str
    code_verifier: str
    is_paper: bool
    redirect_uri: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PendingOAuthCache:
    """In-memory state → PendingOAuth, with TTL eviction on every read.

    NOT thread-safe across processes; FastAPI typically runs as a single
    worker in dev and behind a load-balancer in prod (the cache should
    move to Redis before multi-worker prod). Phase 3.2 follow-on.
    """

    def __init__(self, ttl: timedelta = PENDING_OAUTH_TTL) -> None:
        self._rows: dict[str, PendingOAuth] = {}
        self._ttl = ttl

    def put(self, entry: PendingOAuth) -> None:
        self._evict()
        self._rows[entry.state] = entry

    def consume(self, state: str) -> PendingOAuth | None:
        """Single-use lookup. Found → returns + removes. Missing → None.
        Match the magic-link single-use semantics from the auth service.
        """
        self._evict()
        return self._rows.pop(state, None)

    def _evict(self) -> None:
        cutoff = datetime.now(timezone.utc) - self._ttl
        stale = [s for s, e in self._rows.items() if e.created_at < cutoff]
        for s in stale:
            del self._rows[s]


# ─────────────────────────────────────────────────────────────────────
# Factory + reset for tests
# ─────────────────────────────────────────────────────────────────────


_broker_store: BrokerStore | None = None
_pending_oauth: PendingOAuthCache | None = None


def _is_truthy(v: str | None) -> bool:
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


def get_broker_store() -> BrokerStore:
    global _broker_store
    if _broker_store is None:
        if _is_truthy(os.environ.get("USE_POSTGRES")):
            from app.services.postgres_broker_store import PostgresBrokerStore

            _broker_store = PostgresBrokerStore()
        else:
            _broker_store = InMemoryBrokerStore()
    return _broker_store


def get_pending_oauth_cache() -> PendingOAuthCache:
    global _pending_oauth
    if _pending_oauth is None:
        _pending_oauth = PendingOAuthCache()
    return _pending_oauth


def reset_broker_store_for_tests() -> None:
    global _broker_store, _pending_oauth
    _broker_store = None
    _pending_oauth = None
