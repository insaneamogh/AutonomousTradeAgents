"""Execution-time risk context tests.

The executor's risk re-check is the LAST deterministic gate before a broker
order. These tests pin its three load-bearing properties:

  1. FAIL CLOSED — if the DB-owned halt/PDT state can't be read while
     Postgres is active, the order is refused (ExecutorError), never placed.
  2. Halt state reaches execution — a tripped circuit breaker in the DB
     blocks a BUY at the execute moment even though the council approved it
     earlier.
  3. Broker positions reach the rules — portfolio-shape rules
     (max_open_positions here) actually see the broker's positions instead
     of the empty tuple they got before this change.

All service-level (no HTTP) — the routers are covered by test_orders_route.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.schemas.approvals import ApprovalProposalDto
from app.services import executor as executor_mod
from app.services.broker_store import BrokerConnectionRecord
from app.services.executor import ExecutorError, execute_proposal
from app.services.mock_store import MockStore
from engine.risk import DbRiskState

USER_ID = "00000000-0000-0000-0000-000000000001"


# ─────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _FakePosition:
    symbol: str
    qty: int
    avg_entry_price: float
    market_value: float
    unrealized_pl: float = 0.0
    unrealized_pl_pct: float = 0.0


@dataclass
class _FakeOrder:
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
class _FakeBroker:
    equity: float = 100_000.0
    buying_power: float = 200_000.0
    positions: list[_FakePosition] = field(default_factory=list)
    placed: list[_FakeOrder] = field(default_factory=list)

    async def get_account_equity(self) -> float:
        return self.equity

    async def get_buying_power(self) -> float:
        return self.buying_power

    async def list_positions(self) -> list[_FakePosition]:
        return list(self.positions)

    async def place_order(self, request: Any) -> _FakeOrder:
        order = _FakeOrder(
            broker_order_id=f"alp-{len(self.placed) + 1:04d}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
        )
        self.placed.append(order)
        return order


def _paper_conn() -> BrokerConnectionRecord:
    return BrokerConnectionRecord(
        id="conn-1",
        user_id=USER_ID,
        broker="alpaca",
        is_paper=True,
        account_number="PA-TEST",
        encrypted_access_token="enc",
        encrypted_refresh_token=None,
        access_token_expires_at=None,
        refresh_token_expires_at=None,
        status="active",
    )


def _proposal(symbol: str = "NVDA", qty: int = 10, price: float = 100.0) -> ApprovalProposalDto:
    return ApprovalProposalDto(
        id=f"agent-test-{symbol.lower()}",
        symbol=symbol,
        side="BUY",
        qty=qty,
        order_type="MARKET",
        estimated_notional=qty * price,
        rationale="test",
        bull_case="test bull",
        bear_case="test bear",
        risk_level=2,
        conviction_level=4,  # → confidence 0.8, clears the 0.50 floor
        proposed_at=datetime.now(UTC),
    )


def _patch_broker(monkeypatch: pytest.MonkeyPatch, broker: _FakeBroker) -> None:
    @asynccontextmanager
    async def fake_cm(_user_id, *, broker_=None, store=None, **_kw):
        yield broker, _paper_conn()

    monkeypatch.setattr(executor_mod, "with_broker_client", fake_cm)


async def _seed(store: MockStore, dto: ApprovalProposalDto) -> None:
    await store.append_pending(dto)


# ─────────────────────────────────────────────────────────────────────
# 1. Fail closed
# ─────────────────────────────────────────────────────────────────────


async def test_execution_fails_closed_when_db_state_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postgres active + risk-state read fails → ExecutorError, no order."""
    monkeypatch.setenv("TRADING_MODE", "live")
    broker = _FakeBroker()
    _patch_broker(monkeypatch, broker)
    monkeypatch.setattr(executor_mod, "_postgres_active", lambda: True)

    async def _boom(*_a: Any, **_kw: Any) -> DbRiskState:
        raise RuntimeError("db is down")

    monkeypatch.setattr(executor_mod, "load_db_risk_state", _boom)

    store = MockStore()
    dto = _proposal()
    await _seed(store, dto)

    with pytest.raises(ExecutorError, match="failing closed"):
        await execute_proposal(user_id=USER_ID, proposal_id=dto.id, store=store)

    assert broker.placed == []


# ─────────────────────────────────────────────────────────────────────
# 2. Halt state reaches the execution gate
# ─────────────────────────────────────────────────────────────────────


async def test_tripped_breaker_blocks_buy_at_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    broker = _FakeBroker()
    _patch_broker(monkeypatch, broker)

    halted = DbRiskState(
        drawdown_halted=True,
        drawdown_halt_reason="daily drawdown -3.2%",
    )

    async def _halted_state(_user_id: str, _equity: float | None) -> DbRiskState:
        return halted

    monkeypatch.setattr(executor_mod, "_load_db_state_or_fail", _halted_state)

    store = MockStore()
    dto = _proposal()
    await _seed(store, dto)

    result = await execute_proposal(user_id=USER_ID, proposal_id=dto.id, store=store)

    assert result.risk_blocked is True
    assert result.risk_veto_rule is not None
    assert "drawdown" in result.risk_veto_rule
    assert broker.placed == []


# ─────────────────────────────────────────────────────────────────────
# 3. Broker positions reach the portfolio-shape rules
# ─────────────────────────────────────────────────────────────────────


async def test_broker_positions_feed_max_open_positions_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """15 names held at the broker (the default cap) → a NEW-name BUY is
    vetoed by max_open_positions. Before this change the executor saw an
    empty portfolio and the rule could never fire."""
    monkeypatch.setenv("TRADING_MODE", "live")
    held = [
        _FakePosition(symbol=f"SYM{i:02d}", qty=5, avg_entry_price=50.0, market_value=250.0)
        for i in range(15)
    ]
    broker = _FakeBroker(positions=held)
    _patch_broker(monkeypatch, broker)
    # USE_POSTGRES is off in tests → dev-default DB state (no halt, no PDT).

    store = MockStore()
    dto = _proposal(symbol="NVDA")
    await _seed(store, dto)

    result = await execute_proposal(user_id=USER_ID, proposal_id=dto.id, store=store)

    assert result.risk_blocked is True
    assert result.risk_veto_rule == "max_open_positions"
    assert broker.placed == []


async def test_open_slot_allows_order_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same setup with 14 held names → the order passes and reaches the broker."""
    monkeypatch.setenv("TRADING_MODE", "live")
    held = [
        _FakePosition(symbol=f"SYM{i:02d}", qty=5, avg_entry_price=50.0, market_value=250.0)
        for i in range(14)
    ]
    broker = _FakeBroker(positions=held)
    _patch_broker(monkeypatch, broker)

    store = MockStore()
    dto = _proposal(symbol="NVDA")
    await _seed(store, dto)

    result = await execute_proposal(user_id=USER_ID, proposal_id=dto.id, store=store)

    assert result.risk_blocked is False
    assert len(broker.placed) == 1
    assert broker.placed[0].symbol == "NVDA"
