"""Store Protocol — the contract every backend implementation satisfies.

Phase 0:
  - ``MockStore``      in-memory, lock-guarded. The default.
  - ``PostgresStore``  SQLAlchemy against the ``engine.db`` schema.

Switched via env: when ``USE_POSTGRES=1`` (or any truthy value) the factory
returns the Postgres-backed store, reading ``DATABASE_URL`` for the
connection. Otherwise the in-memory store is used — keeps the demo
runnable without infrastructure.
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

from app.schemas.account import AccountResponse
from app.schemas.activity import ActivityEntryDto
from app.schemas.approvals import (
    ApprovalProposalDto,
    DecisionOutcome,
    DecisionResponse,
)


@runtime_checkable
class Store(Protocol):
    """Backend contract. MockStore + PostgresStore both satisfy this."""

    async def get_account(self) -> AccountResponse: ...
    async def list_activity(self, limit: int = 50) -> list[ActivityEntryDto]: ...
    async def append_activity(self, entry: ActivityEntryDto) -> None: ...
    async def list_pending(self) -> list[ApprovalProposalDto]: ...
    async def append_pending(self, proposal: ApprovalProposalDto) -> ApprovalProposalDto: ...
    async def decide(self, proposal_id: str, outcome: DecisionOutcome) -> DecisionResponse | None: ...


# Process-wide singleton — picked once at first call.
_store: Store | None = None


def _is_truthy(v: str | None) -> bool:
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


def get_store() -> Store:
    """Return the active store. Env-driven, idempotent across the process."""
    global _store
    if _store is not None:
        return _store

    if _is_truthy(os.environ.get("USE_POSTGRES")):
        from app.services.postgres_store import PostgresStore
        _store = PostgresStore()
    else:
        from app.services.mock_store import MockStore
        _store = MockStore()

    return _store


def reset_store_for_tests() -> None:
    """Drop the singleton. Tests use this to re-pick after monkeypatching env."""
    global _store
    _store = None
