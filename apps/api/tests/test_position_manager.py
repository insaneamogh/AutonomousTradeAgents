"""Position-manager exit-condition tests — pure logic, mocked session.

The broker-touching close path follows the executor's already-tested
plumbing; what must be pinned here is WHEN the agent decides to close:

  - time stop fires at the proposal's disclosed horizon, not before
  - a newer council SELL on the same symbol fires the signal exit
  - manual-mode positions are never selected (query-level, asserted via
    the worker's filter in integration; here we pin the per-decision rule)
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.orders import position_manager as position_manager_mod
from app.services.orders.position_manager import (
    _close_position,
    _exit_reason,
    _has_in_flight_close,
)
from broker.types import Side

NOW = datetime(2026, 6, 12, 15, 0, tzinfo=UTC)


def _decision(*, days_held: int, time_stop_days: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        symbol="NVDA",
        horizon="short",
        proposal={"timeStopDays": time_stop_days},
        user_responded_at=NOW - timedelta(days=days_held),
        triggered_at=NOW - timedelta(days=days_held),
    )


def _session(newer_sell_exists: bool) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(
        return_value=uuid.uuid4() if newer_sell_exists else None
    )
    session.execute = AsyncMock(return_value=result)
    return session


async def test_time_stop_fires_at_horizon() -> None:
    reason = await _exit_reason(_session(False), _decision(days_held=5), NOW)
    assert reason == "agent_time"


async def test_no_exit_before_horizon_without_signal() -> None:
    reason = await _exit_reason(_session(False), _decision(days_held=2), NOW)
    assert reason is None


async def test_newer_council_sell_fires_signal_exit() -> None:
    reason = await _exit_reason(_session(True), _decision(days_held=2), NOW)
    assert reason == "agent_signal"


async def test_time_stop_wins_over_signal_check() -> None:
    """At horizon, the time stop is reported even if a signal also exists —
    the labels matter for the audit trail."""
    reason = await _exit_reason(_session(True), _decision(days_held=9), NOW)
    assert reason == "agent_time"


async def test_old_proposals_without_time_stop_use_horizon_fallback() -> None:
    decision = _decision(days_held=5)
    decision.proposal = {}  # pre-0009 proposal shape
    reason = await _exit_reason(_session(False), decision, NOW)
    assert reason == "agent_time"  # 'short' horizon → 5d fallback


async def test_in_flight_close_guard_detects_pending_sell() -> None:
    """Re-entrance guard: a pending/accepted SELL for the decision means a
    close is already live → the manager must not re-submit."""
    assert await _has_in_flight_close(_session(newer_sell_exists=True), uuid.uuid4()) is True


async def test_in_flight_close_guard_clear_when_no_open_sell() -> None:
    assert await _has_in_flight_close(_session(newer_sell_exists=False), uuid.uuid4()) is False


# ─────────────────────────────────────────────────────────────────────
# _close_position — a short must be covered with a BUY, not another SELL
# ─────────────────────────────────────────────────────────────────────


@dataclass
class _FakePosition:
    symbol: str
    qty: int
    avg_entry_price: float
    market_value: float
    unrealized_pl: float = 0.0
    unrealized_pl_pct: float = 0.0


@dataclass
class _FakeCloseOrder:
    broker_order_id: str
    client_order_id: str | None
    symbol: str
    side: Any
    qty: int
    filled_qty: int = 0
    avg_fill_price: float | None = None
    status: Any = "accepted"
    submitted_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    filled_at: datetime | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class _FakeCloseBroker:
    positions: list[Any] = field(default_factory=list)
    placed: list[Any] = field(default_factory=list)
    canceled: list[str] = field(default_factory=list)

    async def get_account_equity(self) -> float:
        return 100_000.0

    async def get_buying_power(self) -> float:
        return 100_000.0

    async def list_positions(self) -> list[Any]:
        return list(self.positions)

    async def cancel_open_orders(self, symbol: str) -> int:
        self.canceled.append(symbol)
        return 0

    async def place_order(self, request: Any) -> _FakeCloseOrder:
        order = _FakeCloseOrder(
            broker_order_id="alp-close-0001",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
        )
        self.placed.append(order)
        return order


class _FakeSessionCM:
    """Async-context-manager stand-in for ``session_factory()``."""

    def __init__(self) -> None:
        self.session = MagicMock()
        self.session.execute = AsyncMock()
        self.session.commit = AsyncMock()

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _short_decision() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        symbol="NVDA",
        fill_qty=10,
        fill_avg_price=100.0,
    )


async def test_close_position_covers_a_short_with_a_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short is held (qty=-10) — closing it must place a BUY for 10
    shares, not another SELL. Before the fix, ``_close_position`` hardcoded
    SELL for every close, which for a short doesn't increase the position
    (the risk engine vetoes it outright — see the docstring) so the
    observable bug was that a short could never be closed through this
    path at all, agent or manual.
    """
    broker = _FakeCloseBroker(
        positions=[
            _FakePosition(
                symbol="NVDA", qty=-10, avg_entry_price=100.0, market_value=-1000.0
            )
        ]
    )
    conn = SimpleNamespace(id="conn-1", is_paper=True)

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, conn

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    session_cm = _FakeSessionCM()
    initiated = await _close_position(
        lambda: session_cm,
        user_id="00000000-0000-0000-0000-000000000001",
        decision=_short_decision(),
        reason="agent_time",
    )

    assert initiated is True
    assert len(broker.placed) == 1
    placed = broker.placed[0]
    assert placed.side == Side.BUY
    assert placed.qty == 10


async def test_close_position_closes_a_long_with_a_sell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged behavior pin: a long (positive qty) still closes with a
    SELL, exactly as before this fix."""
    broker = _FakeCloseBroker(
        positions=[
            _FakePosition(
                symbol="NVDA", qty=10, avg_entry_price=100.0, market_value=1000.0
            )
        ]
    )
    conn = SimpleNamespace(id="conn-1", is_paper=True)

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, conn

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    session_cm = _FakeSessionCM()
    initiated = await _close_position(
        lambda: session_cm,
        user_id="00000000-0000-0000-0000-000000000001",
        decision=_short_decision(),
        reason="agent_time",
    )

    assert initiated is True
    assert len(broker.placed) == 1
    placed = broker.placed[0]
    assert placed.side == Side.SELL
    assert placed.qty == 10


# ─────────────────────────────────────────────────────────────────────
# cancel_pending_order_now — stopping an order that hasn't filled
#
# An approved proposal with no fill yet used to have NO way to be
# stopped: "no_open_position" was accurate but unhelpful, since there was
# never anything TO close — only an order still working at the broker.
# ─────────────────────────────────────────────────────────────────────


class _FakeCancelBroker:
    def __init__(self, *, final_status: str = "canceled") -> None:
        self.cancelled_ids: list[str] = []
        self._final_status = final_status

    async def cancel_order(self, broker_order_id: str) -> SimpleNamespace:
        self.cancelled_ids.append(broker_order_id)
        return SimpleNamespace(status=self._final_status)


def _fake_read_session(order_row: object | None) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=order_row)
    session.execute = AsyncMock(return_value=result)
    return session


async def test_cancel_pending_order_cancels_at_the_broker_and_updates_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.orders.position_manager import cancel_pending_order_now

    order_row = SimpleNamespace(
        id=uuid.uuid4(), broker_order_id="brk-order-1", status="accepted"
    )
    broker = _FakeCancelBroker(final_status="canceled")
    conn = SimpleNamespace(id="conn-1", is_paper=True)

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, conn

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    write_session_cm = _FakeSessionCM()
    result = await cancel_pending_order_now(
        _fake_read_session(order_row),
        lambda: write_session_cm,
        user_id="00000000-0000-0000-0000-000000000001",
        decision=SimpleNamespace(id=uuid.uuid4(), symbol="KO"),
    )

    assert result == {"closed": True, "error": None}
    assert broker.cancelled_ids == ["brk-order-1"]
    write_session_cm.session.execute.assert_awaited_once()
    write_session_cm.session.commit.assert_awaited_once()


async def test_cancel_pending_order_with_no_working_order_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The order already filled (or was already cancelled) between the
    list and the tap — nothing left to cancel. Must not touch the broker."""
    from app.services.orders.position_manager import cancel_pending_order_now

    broker = _FakeCancelBroker()

    @asynccontextmanager
    async def fake_broker_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, SimpleNamespace(id="conn-1", is_paper=True)

    monkeypatch.setattr(position_manager_mod, "with_broker_client", fake_broker_cm)

    result = await cancel_pending_order_now(
        _fake_read_session(None),
        lambda: _FakeSessionCM(),
        user_id="00000000-0000-0000-0000-000000000001",
        decision=SimpleNamespace(id=uuid.uuid4(), symbol="KO"),
    )

    assert result == {"closed": False, "error": "no_pending_order"}
    assert broker.cancelled_ids == []
