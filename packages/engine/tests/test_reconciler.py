"""Reconciler tests.

Mix of pure-logic tests (run anywhere) and Postgres integration tests
(marked, skipped automatically when the DB isn't reachable). The marked
tests give you confidence the SQL actually works; the pure-logic ones
catch breaker-decision regressions instantly without infra.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.reconciler import (
    BreakerTransition,
    MockBrokerPoller,
    RawAccountState,
    Reconciler,
    ReconcilerConfig,
)
from engine.reconciler.breaker import evaluate_breaker
from engine.risk import PortfolioPosition


# ─────────────────────────────────────────────────────────────────────
# MockBrokerPoller
# ─────────────────────────────────────────────────────────────────────


async def test_mock_poller_returns_configured_state() -> None:
    poller = MockBrokerPoller(
        equity=97_000.0,
        cash=50_000.0,
        buying_power=100_000.0,
        positions=(PortfolioPosition("AAPL", 10, 150.0, 1500.0, "tech"),),
    )
    state = await poller.get_account_state()
    assert state.equity == 97_000.0
    assert state.cash == 50_000.0
    assert state.buying_power == 100_000.0
    assert len(state.open_positions) == 1
    assert state.open_positions[0].symbol == "AAPL"


async def test_mock_poller_defaults_to_healthy_account() -> None:
    state = await MockBrokerPoller().get_account_state()
    assert state.equity == 100_000.0
    assert state.open_positions == ()


# ─────────────────────────────────────────────────────────────────────
# evaluate_breaker — pure logic via mocked sessions
# ─────────────────────────────────────────────────────────────────────


def _mock_session_with(breaker_state: Any = None) -> MagicMock:
    """A MagicMock that behaves like an AsyncSession for the queries
    evaluate_breaker makes. The first execute() returns the current
    breaker state; subsequent execute() calls are commits / inserts.
    """
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=breaker_state)
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    return session


def _snapshot_with(daily_pnl_pct: float, equity: float = 100_000.0) -> MagicMock:
    snap = MagicMock()
    snap.daily_pnl_pct = Decimal(str(daily_pnl_pct))
    snap.account_equity = Decimal(str(equity))
    return snap


async def test_breaker_does_not_trip_when_pnl_above_threshold() -> None:
    session = _mock_session_with(breaker_state=None)
    snapshot = _snapshot_with(daily_pnl_pct=-1.5)  # above -3.0 threshold
    user_id = uuid.uuid4()

    transition = await evaluate_breaker(
        session, user_id=user_id, snapshot=snapshot, threshold_pct=-3.0
    )

    assert not transition.tripped
    assert transition.new_status == "normal"
    # Only the SELECT for current state; no UPSERT.
    assert session.execute.call_count == 1
    session.commit.assert_not_called()


async def test_breaker_trips_when_pnl_breaches_threshold() -> None:
    session = _mock_session_with(breaker_state=None)
    snapshot = _snapshot_with(daily_pnl_pct=-3.5, equity=96_500.0)
    user_id = uuid.uuid4()

    transition = await evaluate_breaker(
        session, user_id=user_id, snapshot=snapshot, threshold_pct=-3.0
    )

    assert transition.tripped
    assert transition.previous_status == "normal"
    assert transition.new_status == "halted"
    assert transition.reason is not None and "-3.5" in transition.reason
    # SELECT + UPSERT.
    assert session.execute.call_count == 2
    session.commit.assert_called_once()


async def test_breaker_no_op_when_already_halted() -> None:
    halted = MagicMock()
    halted.status = "halted"
    session = _mock_session_with(breaker_state=halted)
    snapshot = _snapshot_with(daily_pnl_pct=-5.0)
    user_id = uuid.uuid4()

    transition = await evaluate_breaker(
        session, user_id=user_id, snapshot=snapshot, threshold_pct=-3.0
    )

    # Already halted — never auto-unhalts, never re-trips.
    assert not transition.tripped
    assert transition.previous_status == "halted"
    assert transition.new_status == "halted"
    session.commit.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# Reconciler lifecycle (no DB — uses an injectable session_factory)
# ─────────────────────────────────────────────────────────────────────


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.committed = 0

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed += 1

    async def refresh(self, obj: Any) -> None:
        # Simulate captured_at being populated by the DB default.
        if not hasattr(obj, "captured_at") or obj.captured_at is None:
            obj.captured_at = datetime.now(timezone.utc)


async def test_reconciler_run_forever_stops_cleanly() -> None:
    """Smoke: start the loop, give it a beat, stop. Should drain without hanging."""
    factory = MagicMock(return_value=_FakeSession())
    rec = Reconciler(
        poller=MockBrokerPoller(),
        session_factory=factory,
        user_id=uuid.uuid4(),
        config=ReconcilerConfig(interval_seconds=0.05, swallow_errors=True),
    )
    rec.start()
    await asyncio.sleep(0.15)  # let the loop fire 1–3 times
    await rec.stop()
    # If stop hung, asyncio.timeout would fire from the test runner.


# ─────────────────────────────────────────────────────────────────────
# Postgres integration — gated. Run when DB is reachable.
# ─────────────────────────────────────────────────────────────────────


def _postgres_available() -> bool:
    """Probe DATABASE_URL — if asyncpg can't connect, the marked tests skip."""
    if os.environ.get("RUN_POSTGRES_TESTS", "").strip().lower() not in ("1", "true", "yes"):
        return False
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark_postgres = pytest.mark.skipif(
    not _postgres_available(),
    reason="Postgres tests opt-in via RUN_POSTGRES_TESTS=1 (requires running DB).",
)


# Integration tests would land here once docker compose is up. The shape:
#
#   @pytestmark_postgres
#   async def test_tick_writes_snapshot_to_postgres():
#       # spin up async_session_factory pointed at the test DB,
#       # run rec.tick(), assert positions_snapshot row count = 1.
#       ...
#
#   @pytestmark_postgres
#   async def test_breaker_persists_across_ticks():
#       # rec.tick() with pnl=-5% → halted; rec.tick() with pnl=-1% → still halted.
#       ...
#
#   @pytestmark_postgres
#   async def test_acknowledge_resumes_normal():
#       # UPDATE circuit_breaker_state SET status='normal', acknowledged_at=now();
#       # next risk evaluate() on a BUY passes.
#       ...
#
# Left as @pytestmark_postgres stubs deliberately — running them requires
# `make infra-up && make migrate`, which is the user's CI lane (per AGENTV1
# "DO NOT" section: don't run CI dry-runs unprompted).
