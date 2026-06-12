"""PostgresRiskContextProvider — the real ``RiskContextProvider`` for Phase 1.

Reads:
  - Newest ``positions_snapshot`` row → equity / cash / buying_power /
    open_positions / daily_pnl_pct.
  - ``circuit_breaker_state`` row → drawdown_halted + halt_reason.
  - ``pdt_ledger`` count over the rolling 5-business-day window.

Phase 0 simplification: PDT lookback uses 5 calendar days. Phase 1 swaps
to NY business days via ``pandas_market_calendars``.

Cold-boot fallback: if no snapshot exists yet (first deploy, first tick
not landed), returns a healthy synthetic context so the API doesn't 500
on the very first request. The MockRiskContextProvider's defaults.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import desc, func, select

from engine.db.models import CircuitBreakerState, PdtLedger, PositionsSnapshot
from engine.risk.types import ClosedTrade, PortfolioPosition, RiskContext

if TYPE_CHECKING:
    from datetime import date as date_type

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("engine.risk.postgres_context")


def _to_uuid(user_id: str | uuid.UUID | None) -> uuid.UUID | None:
    if user_id is None:
        return None
    if isinstance(user_id, uuid.UUID):
        return user_id
    try:
        return uuid.UUID(user_id)
    except (ValueError, AttributeError):
        return None


def _parse_positions(rows: list[dict]) -> list[PortfolioPosition]:
    out: list[PortfolioPosition] = []
    for r in rows or []:
        try:
            out.append(
                PortfolioPosition(
                    symbol=str(r.get("symbol", "")),
                    qty=int(r.get("qty", 0) or 0),
                    avg_entry_price=float(r.get("avg_entry_price", 0) or 0),
                    market_value=float(r.get("market_value", 0) or 0),
                    sector=r.get("sector"),
                )
            )
        except (TypeError, ValueError):
            continue
    return out


# ─────────────────────────────────────────────────────────────────────
# Shared row readers — used by the context provider AND the execution
# path's ``load_db_risk_state``. One query definition each, no drift.
# ─────────────────────────────────────────────────────────────────────


async def _breaker_state(session: "AsyncSession", uid: uuid.UUID) -> CircuitBreakerState | None:
    stmt = select(CircuitBreakerState).where(CircuitBreakerState.user_id == uid)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _pdt_count_5d(session: "AsyncSession", uid: uuid.UUID) -> int:
    # Phase 0: 5 calendar days back. Phase 1.5: NY business days.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).date()
    stmt = (
        select(func.count())
        .select_from(PdtLedger)
        .where(PdtLedger.user_id == uid)
        .where(PdtLedger.trade_date >= cutoff)
    )
    raw = (await session.execute(stmt)).scalar_one()
    return int(raw or 0)


async def _recent_losing_closes(
    session: "AsyncSession", uid: uuid.UUID
) -> tuple[ClosedTrade, ...]:
    # TODO(Phase 1.5): populate by joining orders + their open counterparts
    # to compute realized P&L per close, filtered to losses inside
    # `wash_sale_lookback_days`. For now () — the wash_sale rule is silent
    # on the Postgres path; the Mock provider proves the rule logic in tests.
    _ = session, uid
    return ()


async def _first_snapshot_equity_today(
    session: "AsyncSession", uid: uuid.UUID
) -> float | None:
    today_start = datetime.combine(
        datetime.now(timezone.utc).date(), datetime.min.time(), tzinfo=timezone.utc
    )
    stmt = (
        select(PositionsSnapshot)
        .where(PositionsSnapshot.user_id == uid)
        .where(PositionsSnapshot.captured_at >= today_start)
        .order_by(PositionsSnapshot.captured_at.asc())
        .limit(1)
    )
    first = (await session.execute(stmt)).scalar_one_or_none()
    if first is None:
        return None
    equity = float(first.account_equity)
    return equity if equity > 0 else None


# ─────────────────────────────────────────────────────────────────────
# Execution-time DB risk state
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DbRiskState:
    """Halt + PDT + wash-sale + daily-P&L state read from Postgres.

    The execution path overlays this onto the BROKER's fresh equity /
    positions so the last-line risk re-check sees both the freshest
    portfolio AND the persisted halt/PDT state. Callers on the execution
    path must treat a raise from ``load_db_risk_state`` as FAIL CLOSED:
    no risk state, no order.
    """

    drawdown_halted: bool = False
    drawdown_halt_reason: str | None = None
    drawdown_halted_at: "date_type | None" = None
    day_trades_last_5d: int = 0
    recent_losing_closes: tuple[ClosedTrade, ...] = ()
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0


async def load_db_risk_state(
    session_factory: "async_sessionmaker",
    *,
    user_id: str | uuid.UUID,
    current_equity: float | None = None,
) -> DbRiskState:
    """Read the DB-owned slice of ``RiskContext`` for a user.

    ``current_equity`` (the broker's fresh read) lets us compute today's
    drawdown against the first reconciler snapshot of the day, so the
    ``drawdown_halt`` rule can trip AT EXECUTION TIME even if the
    reconciler hasn't flipped the breaker row yet.

    Raises ``ValueError`` on an unusable user id and propagates DB errors —
    deliberately. The executor fails closed on any raise.
    """
    uid = _to_uuid(user_id)
    if uid is None:
        raise ValueError(f"load_db_risk_state: invalid user_id {user_id!r}")

    async with session_factory() as session:
        breaker = await _breaker_state(session, uid)
        pdt_count = await _pdt_count_5d(session, uid)
        losing_closes = await _recent_losing_closes(session, uid)

        daily_pnl = 0.0
        daily_pnl_pct = 0.0
        if current_equity is not None:
            start_equity = await _first_snapshot_equity_today(session, uid)
            if start_equity is not None:
                daily_pnl = current_equity - start_equity
                daily_pnl_pct = (daily_pnl / start_equity) * 100.0

    return DbRiskState(
        drawdown_halted=bool(breaker and breaker.status == "halted"),
        drawdown_halt_reason=(breaker.halt_reason if breaker else None),
        drawdown_halted_at=(
            breaker.halted_at.date() if breaker and breaker.halted_at else None
        ),
        day_trades_last_5d=pdt_count,
        recent_losing_closes=losing_closes,
        daily_pnl=daily_pnl,
        daily_pnl_pct=daily_pnl_pct,
    )


@dataclass
class PostgresRiskContextProvider:
    """Async ``RiskContextProvider`` reading from the engine.db schema."""

    session_factory: "async_sessionmaker"

    async def fetch(self, *, user_id: str | uuid.UUID | None = None) -> RiskContext:
        uid = _to_uuid(user_id)
        if uid is None:
            return self._cold_boot_fallback()

        async with self.session_factory() as session:
            # Newest snapshot
            snap_stmt = (
                select(PositionsSnapshot)
                .where(PositionsSnapshot.user_id == uid)
                .order_by(desc(PositionsSnapshot.captured_at))
                .limit(1)
            )
            snapshot = (await session.execute(snap_stmt)).scalar_one_or_none()

            breaker = await _breaker_state(session, uid)
            pdt_count = await _pdt_count_5d(session, uid)
            recent_losing_closes = await _recent_losing_closes(session, uid)

        if snapshot is None:
            logger.info("no snapshot for user %s — returning cold-boot context", uid)
            return self._cold_boot_fallback(drawdown_halted=bool(breaker and breaker.status == "halted"))

        return RiskContext(
            account_equity=float(snapshot.account_equity),
            cash=float(snapshot.cash),
            buying_power=float(snapshot.buying_power),
            open_positions=tuple(_parse_positions(snapshot.open_positions or [])),
            day_trades_last_5d=pdt_count,
            recent_losing_closes=recent_losing_closes,
            daily_pnl=float(snapshot.daily_pnl or 0),
            daily_pnl_pct=float(snapshot.daily_pnl_pct or 0),
            drawdown_halted=bool(breaker and breaker.status == "halted"),
            drawdown_halt_reason=(breaker.halt_reason if breaker else None),
            drawdown_halted_at=(breaker.halted_at.date() if breaker and breaker.halted_at else None),
        )

    @staticmethod
    def _cold_boot_fallback(drawdown_halted: bool = False) -> RiskContext:
        return RiskContext(
            account_equity=100_000.0,
            cash=100_000.0,
            buying_power=200_000.0,
            day_trades_last_5d=0,
            daily_pnl=0.0,
            daily_pnl_pct=0.0,
            drawdown_halted=drawdown_halted,
        )
