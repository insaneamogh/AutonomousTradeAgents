"""Backtest portfolio — cash, positions, equity curve.

This is a simulation-only twin of what production reads from Postgres + the
broker. Phase 1 will share parts of it (position math, P&L) with the live
reconciler.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from broker.types import Side


@dataclass
class Position:
    qty: int = 0
    avg_entry_price: float = 0.0

    def apply_fill(self, side: Side, qty: int, price: float) -> float:
        """Updates running cost-basis. Returns realized P&L (non-zero on closes/reductions)."""
        if side is Side.BUY:
            new_qty = self.qty + qty
            if new_qty == 0:
                return 0.0
            # weighted average cost; only adjusts when adding to the same direction
            if self.qty >= 0:
                self.avg_entry_price = (
                    (self.qty * self.avg_entry_price + qty * price) / new_qty
                ) if new_qty > 0 else 0.0
            self.qty = new_qty
            return 0.0
        # SELL — realize P&L proportional to closed qty.
        realized = (price - self.avg_entry_price) * min(qty, max(self.qty, 0))
        self.qty -= qty
        if self.qty == 0:
            self.avg_entry_price = 0.0
        return realized


@dataclass
class Portfolio:
    starting_cash: float
    cash: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    realized_pnl: float = 0.0

    def __post_init__(self) -> None:
        self.cash = self.starting_cash

    def on_fill(
        self,
        symbol: str,
        side: Side,
        qty: int,
        fill_price: float,
        sec_fee: float,
        finra_taf: float,
    ) -> None:
        pos = self.positions.setdefault(symbol, Position())
        gross = qty * fill_price
        fees = sec_fee + finra_taf
        if side is Side.BUY:
            self.cash -= gross + fees
        else:
            self.cash += gross - fees
        self.realized_pnl += pos.apply_fill(side, qty, fill_price)

    def mark_to_market(self, prices: dict[str, float]) -> float:
        equity = self.cash
        for sym, pos in self.positions.items():
            if pos.qty == 0:
                continue
            mark = prices.get(sym)
            if mark is None:
                continue
            equity += pos.qty * mark
        return equity

    def record_equity(self, timestamp: datetime, prices: dict[str, float]) -> None:
        self.equity_curve.append((timestamp, self.mark_to_market(prices)))
