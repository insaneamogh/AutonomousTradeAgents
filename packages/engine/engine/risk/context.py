"""RiskContext providers.

The Protocol decouples the evaluator from where state lives. Phase 0/1 uses
``MockRiskContextProvider`` (synthetic; configurable for testing). Phase 2
swaps in a real one that reads from ``engine.db`` + the reconciler-cached
Alpaca state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from engine.risk.types import ClosedTrade, PortfolioPosition, RiskContext


class RiskContextProvider(Protocol):
    """Async because the real implementation hits Postgres + Redis."""

    async def fetch(
        self, *, user_id: str | None = None, source: str | None = None
    ) -> RiskContext:
        """``source``: the broker whose book is being risked."""
        ...


@dataclass
class MockRiskContextProvider:
    """In-memory provider — useful for unit tests, the CLI smoke, and any
    code path that doesn't need persisted state yet.

    Defaults to a healthy $100K account with no positions, no day trades,
    no drawdown halt. Override individual fields per scenario.
    """

    account_equity: float = 100_000.0
    cash: float = 100_000.0
    buying_power: float = 200_000.0  # 2× margin
    open_positions: tuple[PortfolioPosition, ...] = ()
    day_trades_last_5d: int = 0
    recent_losing_closes: tuple[ClosedTrade, ...] = ()
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    drawdown_halted: bool = False
    drawdown_halt_reason: str | None = None
    now_utc: datetime | None = None
    """The clock every time-dependent risk rule should read.

    `RiskContext.now_utc` has existed since the risk engine was written and
    NOTHING outside tests ever populated it, so `expiry_day_entry`,
    `min_dte`, `max_dte` and `mis_square_off_block` all silently fell
    through their `context.now_utc or datetime.now(UTC)` guard to the
    process wall clock.

    That left the system reading two different clocks: the market-open gate
    goes through the resolved `alpaca clock` -> REST -> local-calendar
    chain, while the DTE rules used whatever the container thought the time
    was. Usually the same answer, which is why it never surfaced as a
    production bug — but it also made time-dependent behaviour untestable,
    and that is how 14 tests came to hard-code an expiry date that
    eventually arrived (2026-09-18) and started tripping
    `expiry_day_entry`.

    `None` keeps the previous behaviour exactly (the rules fall back to the
    wall clock), so nothing changes for a caller that does not set it."""

    options_trading_level: int | None = 3
    """Defaults to 3 (Alpaca's own "spreads + long/short singles" tier —
    see docs/OPTIONS_PLAN.md's live-account check), not None, so
    mock-mode/CI can exercise the options path without extra wiring.
    Override to test ``options_level_insufficient`` explicitly."""

    async def fetch(
        self, *, user_id: str | None = None, source: str | None = None
    ) -> RiskContext:
        return RiskContext(
            account_equity=self.account_equity,
            cash=self.cash,
            buying_power=self.buying_power,
            open_positions=self.open_positions,
            day_trades_last_5d=self.day_trades_last_5d,
            recent_losing_closes=self.recent_losing_closes,
            daily_pnl=self.daily_pnl,
            daily_pnl_pct=self.daily_pnl_pct,
            drawdown_halted=self.drawdown_halted,
            drawdown_halt_reason=self.drawdown_halt_reason,
            options_trading_level=self.options_trading_level,
            now_utc=self.now_utc,
        )
