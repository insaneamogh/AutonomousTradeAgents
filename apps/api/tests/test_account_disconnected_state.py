"""``PostgresStore.get_account`` — docs/PLAN_MULTI_TENANT.md §3.

Before this fix, a user with NO broker connection at all got the exact
same hardcoded fixture as a genuine cold boot: a confident-looking
$100,000 portfolio (``equity=100_000, buying_power=200_000,
status="connected"``) that is a constant in this source file, not a real
account. The tell was ``buying_power``: a real Alpaca paper account
reports $400,000, this fixture reports $200,000. A judge who signed up
(now correctly given zero broker connections, per the allowlist fix in
``test_env_bootstrap.py``) would otherwise see a plausible fake instead
of an honest "not connected" state.

The real session/DB round-trip is exercised elsewhere
(``test_postgres_stores.py``, opt-in via ``RUN_POSTGRES_TESTS=1``) — this
file tests ``get_account``'s BRANCHING logic (no connection -> disconnected;
connection + no snapshot yet -> the legitimate cold-boot fixture;
connection + snapshot -> the real numbers) against a fake session, so the
three-way branch is verified without needing a live Postgres.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from app.services.broker.broker_store import InMemoryBrokerStore
from app.services.council.postgres_store import PostgresStore

# get_account resolves user_id through postgres_store._uid(), which parses
# it as a real UUID and falls back to DEFAULT_USER_ID on anything that
# isn't one (ValueError/TypeError). A plain string like "user-1" would
# silently be coerced to DEFAULT_USER_ID inside get_account while this
# file's InMemoryBrokerStore fixtures stay keyed on the literal string —
# a mismatch that would make every "connection exists" test look
# disconnected for the wrong reason. Real UUIDs sidestep that entirely.
_USER_WITH_NO_CONNECTION = str(uuid.uuid4())
_USER_WITH_FRESH_CONNECTION = str(uuid.uuid4())
_USER_WITH_REAL_DATA = str(uuid.uuid4())


@dataclass
class _FakeSnapshotRow:
    account_equity: float
    cash: float
    buying_power: float
    daily_pnl: float | None
    daily_pnl_pct: float | None
    open_positions: list[Any] | None
    source: str = "alpaca"


class _FakeResult:
    def __init__(self, row: _FakeSnapshotRow | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> _FakeSnapshotRow | None:
        return self._row


class _FakeSession:
    def __init__(self, row: _FakeSnapshotRow | None) -> None:
        self._row = row

    async def execute(self, _stmt: object) -> _FakeResult:
        return _FakeResult(self._row)

    async def commit(self) -> None:
        pass


class _FakeSessionCM:
    def __init__(self, row: _FakeSnapshotRow | None) -> None:
        self._row = row

    async def __aenter__(self) -> _FakeSession:
        return _FakeSession(self._row)

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _store_with_fake_session(row: _FakeSnapshotRow | None) -> PostgresStore:
    """A real ``PostgresStore`` with its session factory swapped for a
    fake one returning ``row`` (or nothing) — ``_ensure_seed`` is also
    short-circuited (``_seeded = True``) since it does its own real DB
    upsert that the fake session can't service."""
    store = PostgresStore()
    store._session_factory = lambda: _FakeSessionCM(row)  # type: ignore[assignment]
    store._seeded = True
    return store


async def _connected_broker_store(user_id: str) -> InMemoryBrokerStore:
    broker_store = InMemoryBrokerStore()
    await broker_store.upsert_connection(
        user_id=user_id,
        broker="alpaca",
        is_paper=True,
        account_number="PA-TEST-001",
        encrypted_access_token="irrelevant-for-this-test",
        encrypted_refresh_token=None,
        access_token_expires_at=None,
    )
    return broker_store


async def test_no_connection_reports_disconnected_not_a_fake_portfolio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression test for docs/PLAN_MULTI_TENANT.md §3. Break: return
    the cold-boot fixture unconditionally (drop the ``has_connection``
    check) and this starts asserting a fake $100,000/$200,000 portfolio
    again."""
    monkeypatch.setattr(
        "app.services.broker.broker_store.get_broker_store",
        lambda: InMemoryBrokerStore(),  # zero connections for anyone
    )
    store = _store_with_fake_session(row=None)

    account = await store.get_account(_USER_WITH_NO_CONNECTION)

    assert account.status == "disconnected"
    assert account.equity == 0.0
    assert account.cash == 0.0
    assert account.buying_power == 0.0
    assert account.open_positions == 0


async def test_connection_exists_but_no_snapshot_yet_uses_the_cold_boot_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards the LEGITIMATE case §3 must not regress: a real connection
    exists, the reconciler simply hasn't ticked yet. Break: make
    ``has_connection`` always False and this starts asserting
    "disconnected" even though a connection is present."""
    user_id = _USER_WITH_FRESH_CONNECTION
    broker_store = await _connected_broker_store(user_id)
    monkeypatch.setattr(
        "app.services.broker.broker_store.get_broker_store", lambda: broker_store
    )
    store = _store_with_fake_session(row=None)  # no snapshot row yet

    account = await store.get_account(user_id)

    assert account.status == "connected"
    assert account.equity == 100_000.00
    assert account.buying_power == 200_000.00


async def test_connection_and_snapshot_both_exist_uses_the_real_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normal, steady-state case: neither the disconnected branch nor
    the cold-boot fixture apply — the real reconciler-cached numbers pass
    through untouched."""
    user_id = _USER_WITH_REAL_DATA
    broker_store = await _connected_broker_store(user_id)
    monkeypatch.setattr(
        "app.services.broker.broker_store.get_broker_store", lambda: broker_store
    )
    row = _FakeSnapshotRow(
        account_equity=87_654.32,
        cash=12_000.0,
        buying_power=175_308.64,
        daily_pnl=321.5,
        daily_pnl_pct=0.37,
        open_positions=["AAPL", "NVDA"],
    )
    store = _store_with_fake_session(row=row)

    account = await store.get_account(user_id)

    assert account.status == "connected"
    assert account.equity == 87_654.32
    assert account.open_positions == 2
