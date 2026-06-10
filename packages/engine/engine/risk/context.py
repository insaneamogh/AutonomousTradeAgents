"""RiskContext providers.

The Protocol decouples the evaluator from where state lives. Phase 0/1 uses
``MockRiskContextProvider`` (synthetic; configurable for testing). Phase 2
swaps in a real one that reads from ``engine.db`` + the reconciler-cached
Alpaca state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from engine.risk.types import ClosedTrade, PortfolioPosition, RiskContext


class RiskContextProvider(Protocol):
    """Async because the real implementation hits Postgres + Redis."""

    async def fetch(self, *, user_id: str | None = None) -> RiskContext: ...


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

    async def fetch(self, *, user_id: str | None = None) -> RiskContext:
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
        )
