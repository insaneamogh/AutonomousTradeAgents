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
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select

from engine.db.models import PositionsSnapshot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from engine.reconciler.poller import RawAccountState


async def write_snapshot(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    state: RawAccountState,
    source: str,
) -> PositionsSnapshot:
    """Insert a new snapshot row and commit. Returns the row."""
    daily_pnl, daily_pnl_pct = await _daily_pnl(
        session,
        user_id=user_id,
        current_equity=state.equity,
        prior_close_equity=state.prior_close_equity,
    )

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
                "is_option": p.is_option,
                "multiplier": p.multiplier,
            }
            for p in state.open_positions
        ],
        daily_pnl=Decimal(str(round(daily_pnl, 2))),
        daily_pnl_pct=Decimal(str(round(daily_pnl_pct, 3))),
        options_trading_level=state.options_trading_level,
        raw=dict(state.raw),
    )
    session.add(snapshot)
    await session.commit()
    await session.refresh(snapshot)
    return snapshot


async def _daily_pnl(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    current_equity: float,
    prior_close_equity: float | None = None,
) -> tuple[float, float]:
    """(pnl, pnl_pct) for the session, against the previous CLOSE.

    ``prior_close_equity`` — the broker's own prior-session-close equity
    (Alpaca's ``last_equity``) — is preferred whenever it is available,
    and the reason is not tidiness. The fallback below baselines off the
    earliest snapshot bearing today's **UTC** date, and the US session
    runs 13:30-20:00 UTC. Every snapshot the 30-second fleet writes
    between 00:00 and 13:30 UTC therefore carries YESTERDAY's closing
    equity while already stamped with today's UTC date, so the fallback
    silently baselines the day against yesterday's close-of-day value and
    the entire overnight gap vanishes from the number.

    Measured live 2026-09-01 08:16 ET, which is what prompted this:
    Alpaca reported equity 100297.33 against last_equity 100871.17 — a
    real -573.84 (-0.57%) session — while the UI, reading the snapshot
    this function wrote, showed +53 (+0.05%).

    This is a risk-correctness bug, not a display one:
    ``reconciler.breaker`` trips ``daily_drawdown_halt_pct`` (-3.0) off
    ``snapshot.daily_pnl_pct``, so a baseline that understates the day's
    loss understates it for the halt too.

    Returns (0, 0) when neither a broker baseline nor a prior snapshot
    exists — a brand-new account has no previous session, and inventing a
    number there would be worse than admitting we cannot compute one.
    """
    start_equity: float | None = None
    if prior_close_equity is not None and prior_close_equity > 0:
        start_equity = float(prior_close_equity)
    else:
        # No broker baseline. Fall back to the earliest snapshot of the
        # current UTC day — wrong across the overnight boundary, as above,
        # but strictly better than reporting a flat zero all session.
        today_start = datetime.combine(
            datetime.now(UTC).date(), datetime.min.time(), tzinfo=UTC
        )
        stmt = (
            select(PositionsSnapshot)
            .where(PositionsSnapshot.user_id == user_id)
            .where(PositionsSnapshot.captured_at >= today_start)
            .order_by(PositionsSnapshot.captured_at.asc())
            .limit(1)
        )
        first_today = (await session.execute(stmt)).scalar_one_or_none()
        if first_today is not None:
            start_equity = float(first_today.account_equity)

    if start_equity is None or start_equity <= 0:
        return 0.0, 0.0
    pnl = current_equity - start_equity
    pnl_pct = (pnl / start_equity) * 100.0
    return pnl, pnl_pct
