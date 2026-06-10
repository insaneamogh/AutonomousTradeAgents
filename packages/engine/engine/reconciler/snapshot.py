"""write_snapshot — persist a RawAccountState as a ``positions_snapshot`` row.

Daily P&L is computed against the FIRST snapshot of the current UTC day. If
no prior snapshot exists for today (first tick of the day, or cold boot),
``daily_pnl`` and ``daily_pnl_pct`` are 0 — the breaker can't trip on the
zeroth tick.

Phase 1 swaps UTC days for NY business days. The function signature won't
change; the date-comparison logic will.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select

from engine.db.models import PositionsSnapshot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from engine.reconciler.poller import RawAccountState


async def write_snapshot(
    session: "AsyncSession",
    *,
    user_id: uuid.UUID,
    state: "RawAccountState",
    source: str,
) -> PositionsSnapshot:
    """Insert a new snapshot row and commit. Returns the row."""
    daily_pnl, daily_pnl_pct = await _daily_pnl(session, user_id=user_id, current_equity=state.equity)

    snapshot = PositionsSnapshot(
        id=uuid.uuid4(),
        user_id=user_id,
        source=source,
        account_equity=Decimal(str(round(state.equity, 2))),
        cash=Decimal(str(round(state.cash, 2))),
        buying_power=Decimal(str(round(state.buying_power, 2))),
        open_positions=[
            {
                "symbol": p.symbol,
                "qty": p.qty,
                "avg_entry_price": p.avg_entry_price,
                "market_value": p.market_value,
                "sector": p.sector,
            }
            for p in state.open_positions
        ],
        daily_pnl=Decimal(str(round(daily_pnl, 2))),
        daily_pnl_pct=Decimal(str(round(daily_pnl_pct, 3))),
        raw=dict(state.raw),
    )
    session.add(snapshot)
    await session.commit()
    await session.refresh(snapshot)
    return snapshot


async def _daily_pnl(
    session: "AsyncSession",
    *,
    user_id: uuid.UUID,
    current_equity: float,
) -> tuple[float, float]:
    """Look up today's earliest snapshot and compute (pnl, pnl_pct) against it.
    Returns (0, 0) when no prior snapshot exists for today.
    """
    today_start = datetime.combine(
        datetime.now(timezone.utc).date(), datetime.min.time(), tzinfo=timezone.utc
    )
    stmt = (
        select(PositionsSnapshot)
        .where(PositionsSnapshot.user_id == user_id)
        .where(PositionsSnapshot.captured_at >= today_start)
        .order_by(PositionsSnapshot.captured_at.asc())
        .limit(1)
    )
    first_today = (await session.execute(stmt)).scalar_one_or_none()
    if first_today is None:
        return 0.0, 0.0
    start_equity = float(first_today.account_equity)
    if start_equity <= 0:
        return 0.0, 0.0
    pnl = current_equity - start_equity
    pnl_pct = (pnl / start_equity) * 100.0
    return pnl, pnl_pct
