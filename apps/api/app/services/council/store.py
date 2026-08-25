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

from typing import Protocol, runtime_checkable

from app.core.singleton import LazyEnvSingleton
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

    async def get_account(self, user_id: str | None = None) -> AccountResponse: ...
    async def list_activity(
        self, user_id: str | None = None, limit: int = 50
    ) -> list[ActivityEntryDto]: ...
    async def append_activity(self, entry: ActivityEntryDto) -> None: ...
    async def list_pending(self, user_id: str | None = None) -> list[ApprovalProposalDto]: ...
    async def append_pending(self, proposal: ApprovalProposalDto) -> ApprovalProposalDto: ...
    async def decide(
        self,
        proposal_id: str,
        outcome: DecisionOutcome,
        *,
        user_id: str | None = None,
        exit_mode: str | None = None,
    ) -> DecisionResponse | None: ...


def _build_mock_store() -> Store:
    from app.services.council.mock_store import MockStore

    return MockStore()


def _build_postgres_store() -> Store:
    from app.services.council.postgres_store import PostgresStore

    return PostgresStore()


# Process-wide singleton — picked once at first call.
_store: LazyEnvSingleton[Store] = LazyEnvSingleton(_build_mock_store, _build_postgres_store)


def get_store() -> Store:
    """Return the active store. Env-driven, idempotent across the process."""
    return _store.get()


def reset_store_for_tests() -> None:
    """Drop the singleton. Tests use this to re-pick after monkeypatching env."""
    _store.reset()
