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
from engine.risk.types import PortfolioPosition, RiskContext

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

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

            # Breaker state
            breaker_stmt = select(CircuitBreakerState).where(
                CircuitBreakerState.user_id == uid
            )
            breaker = (await session.execute(breaker_stmt)).scalar_one_or_none()

            # PDT count — Phase 0: 5 calendar days back. Phase 1: business days.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=5)).date()
            pdt_stmt = (
                select(func.count())
                .select_from(PdtLedger)
                .where(PdtLedger.user_id == uid)
                .where(PdtLedger.trade_date >= cutoff)
            )
            pdt_count_raw = (await session.execute(pdt_stmt)).scalar_one()
            pdt_count = int(pdt_count_raw or 0)

            # TODO(Phase 1.5): populate recent_losing_closes by joining
            # orders + their open counterparts to compute realized P&L per
            # close, filtered to losses inside `wash_sale_lookback_days`.
            # For now we pass () so the wash_sale rule is silent on the
            # Postgres path — Mock provider proves the rule logic in tests.
            recent_losing_closes: tuple = ()

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
