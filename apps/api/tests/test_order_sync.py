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

from app.services.order_sync import _apply_decision_lifecycle


def _decision(**overrides: Any) -> SimpleNamespace:
    base = SimpleNamespace(
        id=uuid.uuid4(),
        fill_qty=None,
        fill_avg_price=None,
        realized_pnl=None,
        closed_at=None,
        close_reason=None,
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
