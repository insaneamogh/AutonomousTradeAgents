"""order_sync decision-lifecycle tests — pure logic, mocked sessions.

The SQL-touching paths (open-order scan, external-close detection) follow
the Postgres-marked integration pattern from engine's reconciler tests and
are exercised against a real DB in Phase 4 validation. What's pinned here
is the math + state transitions that must never drift:

  - a filled BUY heals the decision's entry columns
  - a filled SELL closes the decision with (exit - entry) * qty
  - an already-closed decision is never re-closed (idempotent)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.orders import order_sync as order_sync_mod
from app.services.orders.order_sync import _apply_decision_lifecycle, _last_snapshot_mark


def _decision(**overrides: Any) -> SimpleNamespace:
    base = SimpleNamespace(
        id=uuid.uuid4(),
        fill_qty=None,
        fill_avg_price=None,
        realized_pnl=None,
        closed_at=None,
        close_reason=None,
        # The decision's OWN entry proposal — None here reproduces the
        # pre-item-1 shape (no "direction"/"side" fields yet recorded),
        # which must still resolve to the "BUY" fallback everywhere below.
        proposal=None,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _order(side: str, *, filled_qty: int, avg: str, decision_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_decision_id=decision_id,
        side=side,
        filled_qty=filled_qty,
        avg_fill_price=Decimal(avg),
        filled_at=datetime(2026, 6, 12, 15, 30, tzinfo=timezone.utc),
        symbol="NVDA",
        user_id=uuid.uuid4(),
    )


def _session_for(decision: SimpleNamespace) -> MagicMock:
    """AsyncSession stand-in: ``get`` returns the decision; the PDT entry
    lookup returns no entry order so _maybe_record_pdt is a no-op."""
    session = MagicMock()
    session.get = AsyncMock(return_value=decision)
    empty = MagicMock()
    empty.scalar_one_or_none = MagicMock(return_value=None)
    session.execute = AsyncMock(return_value=empty)
    session.add = MagicMock()
    return session


async def test_buy_fill_heals_entry_columns() -> None:
    decision = _decision()
    order = _order("BUY", filled_qty=12, avg="101.50", decision_id=decision.id)
    await _apply_decision_lifecycle(_session_for(decision), order)

    assert decision.fill_qty == 12
    assert decision.fill_avg_price == Decimal("101.50")
    assert decision.closed_at is None  # entries never close a decision


async def test_sell_fill_closes_decision_with_realized_pnl() -> None:
    decision = _decision(fill_qty=12, fill_avg_price=Decimal("100.00"))
    order = _order("SELL", filled_qty=12, avg="104.25", decision_id=decision.id)
    await _apply_decision_lifecycle(_session_for(decision), order)

    # (104.25 - 100.00) * 12 = 51.00
    assert decision.realized_pnl == Decimal("51.00")
    assert decision.closed_at is not None
    assert decision.close_reason == "user_manual"  # default when no agent reason set


async def test_sell_fill_respects_existing_close_reason_and_idempotency() -> None:
    already_closed_at = datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc)
    decision = _decision(
        fill_qty=12,
        fill_avg_price=Decimal("100.00"),
        realized_pnl=Decimal("33.00"),
        closed_at=already_closed_at,
        close_reason="agent_target",
    )
    order = _order("SELL", filled_qty=12, avg="90.00", decision_id=decision.id)
    await _apply_decision_lifecycle(_session_for(decision), order)

    # Nothing about the already-closed decision changed.
    assert decision.realized_pnl == Decimal("33.00")
    assert decision.closed_at == already_closed_at
    assert decision.close_reason == "agent_target"


async def test_partial_sell_uses_min_qty_for_pnl() -> None:
    """Exit filled for fewer shares than the entry → P&L on the exited qty."""
    decision = _decision(fill_qty=12, fill_avg_price=Decimal("100.00"))
    order = _order("SELL", filled_qty=10, avg="102.00", decision_id=decision.id)
    await _apply_decision_lifecycle(_session_for(decision), order)

    # (102 - 100) * min(10, 12) = 20.00
    assert decision.realized_pnl == Decimal("20.00")


# ─────────────────────────────────────────────────────────────────────
# Options — the contract multiplier scales realized P&L
# ─────────────────────────────────────────────────────────────────────


async def test_option_fill_realizes_pnl_scaled_by_multiplier() -> None:
    """A $2.50 -> $3.00 move on 1 CONTRACT is $50.00 of realized P&L, not
    $0.50 — the multiplier converts a per-contract-unit price move into
    the actual dollar P&L. Read off the decision's own persisted proposal
    (the existing JSONB .get(..., default) idiom this file already uses
    for "side"), not a new DB column."""
    decision = _decision(
        proposal={"side": "BUY", "multiplier": 100},
        fill_qty=1,
        fill_avg_price=Decimal("2.50"),
    )
    order = _order("SELL", filled_qty=1, avg="3.00", decision_id=decision.id)
    await _apply_decision_lifecycle(_session_for(decision), order)

    assert decision.realized_pnl == Decimal("50.00")


async def test_equity_fill_pnl_unaffected_by_absent_multiplier_key() -> None:
    """Pre-options decisions have no "multiplier" key in their persisted
    proposal at all — must default to 1 (a no-op), never crash or scale
    equity P&L by anything else."""
    decision = _decision(
        proposal={"side": "BUY"}, fill_qty=10, fill_avg_price=Decimal("100.00")
    )
    order = _order("SELL", filled_qty=10, avg="104.25", decision_id=decision.id)
    await _apply_decision_lifecycle(_session_for(decision), order)

    # (104.25 - 100.00) * 10 = 42.50
    assert decision.realized_pnl == Decimal("42.50")


# ─────────────────────────────────────────────────────────────────────
# Short positions — the entry side is SELL, not BUY
# ─────────────────────────────────────────────────────────────────────


async def test_short_entry_sell_fill_lands_in_entry_branch_not_exit() -> None:
    """The decision's OWN entry side is SELL (it opened a short). Its entry
    fill must heal fill_qty/fill_avg_price, not self-close. Before the fix,
    _apply_decision_lifecycle keyed off a hardcoded 'BUY' literal, so a
    short's own opening fill fell into the exit branch and stamped
    closed_at before the position was ever visible as open."""
    decision = _decision(proposal={"side": "SELL"})
    order = _order("SELL", filled_qty=10, avg="100.00", decision_id=decision.id)
    await _apply_decision_lifecycle(_session_for(decision), order)

    assert decision.fill_qty == 10
    assert decision.fill_avg_price == Decimal("100.00")
    assert decision.closed_at is None


async def test_short_cover_buy_fill_closes_with_short_sign_pnl() -> None:
    """A BUY that covers a short realizes (entry - exit) * qty — the
    mirror of the long formula, keyed off the decision's own entry side."""
    decision = _decision(
        proposal={"side": "SELL"}, fill_qty=10, fill_avg_price=Decimal("100.00")
    )
    order = _order("BUY", filled_qty=10, avg="92.00", decision_id=decision.id)
    await _apply_decision_lifecycle(_session_for(decision), order)

    # (100.00 - 92.00) * 10 = 80.00 — profit on a short that fell.
    assert decision.realized_pnl == Decimal("80.00")
    assert decision.closed_at is not None
    assert decision.close_reason == "user_manual"


# ─────────────────────────────────────────────────────────────────────
# _last_snapshot_mark — mark reconstruction, options-aware
# ─────────────────────────────────────────────────────────────────────


def _snapshot(*, symbol: str, qty: int, market_value: float) -> SimpleNamespace:
    return SimpleNamespace(
        open_positions=[{"symbol": symbol, "qty": qty, "market_value": market_value}]
    )


async def _session_with_snapshots(snapshots: list[SimpleNamespace]) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = snapshots
    session.execute = AsyncMock(return_value=result)
    return session


async def test_last_snapshot_mark_divides_by_multiplier_for_an_option() -> None:
    """1 contract marked at $300 total market_value is a $3.00 per-contract
    mark, not $300 — dividing by qty alone (the equity formula) would
    overstate an option's per-contract price by the multiplier."""
    session = await _session_with_snapshots(
        [_snapshot(symbol="AAPL260828C00250000", qty=1, market_value=300.0)]
    )
    mark = await _last_snapshot_mark(
        session, uuid.uuid4(), "AAPL260828C00250000", multiplier=100
    )
    assert mark == Decimal("3.00")


async def test_last_snapshot_mark_unaffected_for_equity_default_multiplier() -> None:
    session = await _session_with_snapshots(
        [_snapshot(symbol="AAPL", qty=10, market_value=1_500.0)]
    )
    mark = await _last_snapshot_mark(session, uuid.uuid4(), "AAPL")
    assert mark == Decimal("150.00")


# ─────────────────────────────────────────────────────────────────────
# _detect_external_closes — end to end, options-aware realized P&L
# ─────────────────────────────────────────────────────────────────────


def _values_of(stmt: object) -> dict[str, object]:
    """Pull the {column_name: bound_value} map out of a SQLAlchemy
    ``update(...).values(...)`` construct, keyed by column name whether
    SQLAlchemy resolved the key to the mapped Column or a bare string."""
    raw = dict(stmt._values)  # type: ignore[attr-defined]
    return {
        getattr(k, "key", k): (v.value if hasattr(v, "value") else v)
        for k, v in raw.items()
    }


async def test_external_close_option_realizes_pnl_scaled_by_multiplier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A $2.50 -> $3.00 external close on 1 contract realizes $50.00, not
    $0.50 — same multiplier fix as the ordinary close path, applied to the
    externally-detected-close branch. ``_last_snapshot_mark`` is faked
    directly here (it has its own dedicated tests above) so this test is
    only exercising _detect_external_closes's OWN arithmetic."""
    decision = SimpleNamespace(
        id=uuid.uuid4(),
        symbol="AAPL260828C00250000",
        proposal={"side": "BUY", "multiplier": 100},
        fill_qty=1,
        fill_avg_price=Decimal("2.50"),
    )

    decisions_result = MagicMock()
    decisions_result.scalars.return_value.all.return_value = [decision]
    in_flight_result = MagicMock()
    in_flight_result.scalar_one_or_none.return_value = None
    update_result = MagicMock()

    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[decisions_result, in_flight_result, update_result]
    )

    seen_multipliers: list[int] = []

    async def _fake_mark(_session: object, _uid: object, _symbol: str, *, multiplier: int = 1):
        seen_multipliers.append(multiplier)
        return Decimal("3.00")

    monkeypatch.setattr(order_sync_mod, "_last_snapshot_mark", _fake_mark)

    broker = SimpleNamespace(list_positions=AsyncMock(return_value=[]))

    await order_sync_mod._detect_external_closes(
        session, uuid.uuid4(), broker, user_id="00000000-0000-0000-0000-000000000001"
    )

    assert seen_multipliers == [100]
    update_stmt = session.execute.call_args_list[-1].args[0]
    assert _values_of(update_stmt)["realized_pnl"] == Decimal("50.00")


async def test_external_close_equity_unaffected_by_absent_multiplier_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = SimpleNamespace(
        id=uuid.uuid4(),
        symbol="NVDA",
        proposal={"side": "BUY"},
        fill_qty=10,
        fill_avg_price=Decimal("100.00"),
    )

    decisions_result = MagicMock()
    decisions_result.scalars.return_value.all.return_value = [decision]
    in_flight_result = MagicMock()
    in_flight_result.scalar_one_or_none.return_value = None
    update_result = MagicMock()

    session = MagicMock()
    session.execute = AsyncMock(
        side_effect=[decisions_result, in_flight_result, update_result]
    )

    async def _fake_mark(_session: object, _uid: object, _symbol: str, *, multiplier: int = 1):
        assert multiplier == 1
        return Decimal("104.25")

    monkeypatch.setattr(order_sync_mod, "_last_snapshot_mark", _fake_mark)

    broker = SimpleNamespace(list_positions=AsyncMock(return_value=[]))

    await order_sync_mod._detect_external_closes(
        session, uuid.uuid4(), broker, user_id="00000000-0000-0000-0000-000000000001"
    )

    update_stmt = session.execute.call_args_list[-1].args[0]
    # (104.25 - 100.00) * 10 = 42.50
    assert _values_of(update_stmt)["realized_pnl"] == Decimal("42.50")
