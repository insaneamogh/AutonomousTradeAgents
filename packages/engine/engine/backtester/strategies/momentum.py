"""12-1 momentum reference strategy.

The classic Jegadeesh–Titman / Asness 12-1 momentum: rank by the return over
the past 12 months EXCLUDING the most recent 1 month (the "12-1 minus 1"
formulation). Excluding the recent month removes short-term reversal noise.

Daily-bar implementation:
  momentum_score = (close[t - skip_days] − close[t - lookback_days]) / close[t - lookback_days]

  - BUY  when momentum_score > 0 and we're flat.
  - SELL (close long) when momentum_score < 0 and we're long.

Defaults (daily bars): lookback_days=252, skip_days=21 — i.e., the 12-1
formulation. Tests override to short windows (e.g., 30/5) so they finish.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from broker.types import OrderRequest, OrderType, Side
from engine.backtester.events import Bar
from engine.backtester.strategies._utils import RollingAtr, make_coid, size_for_entry
from engine.sizing import AtrSizingConfig


@dataclass
class Momentum:
    lookback_days: int = 252
    skip_days: int = 21
    qty: int = 10
    name: str = "momentum"

    sizing_config: AtrSizingConfig | None = None
    starting_equity: float = 100_000.0
    atr_window: int = 14
    confidence: float = 0.7

    # We only need the most-distant + the skip-anchor close. Keep a single
    # deque of closes sized to the lookback window.
    _closes: deque[float] = field(default_factory=deque, init=False)
    _atr: RollingAtr = field(default_factory=RollingAtr, init=False)
    _long: bool = field(default=False, init=False)
    _held_qty: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        # +1 so closes[-lookback] is exactly lookback_days ago.
        self._closes = deque(maxlen=self.lookback_days + 1)
        self._atr = RollingAtr(window=self.atr_window)

    def on_bar(self, bar: Bar) -> list[OrderRequest]:
        self._atr.update(bar)
        self._closes.append(bar.close)

        if len(self._closes) <= self.lookback_days or self.skip_days <= 0:
            return []
        if self.skip_days >= self.lookback_days:
            return []  # nonsense config — bail rather than crash.

        anchor_old = self._closes[0]                          # ~lookback_days ago
        anchor_skip = self._closes[-(self.skip_days + 1)]     # skip_days ago
        if anchor_old <= 0:
            return []
        momentum_score = (anchor_skip - anchor_old) / anchor_old

        orders: list[OrderRequest] = []
        if not self._long and momentum_score > 0:
            buy_qty = size_for_entry(
                bar,
                atr=self._atr.value,
                sizing_config=self.sizing_config,
                starting_equity=self.starting_equity,
                confidence=self.confidence,
                fallback_qty=self.qty,
            )
            if buy_qty >= 1:
                orders.append(_order(bar, Side.BUY, buy_qty, self.name))
                self._long = True
                self._held_qty = buy_qty
        elif self._long and momentum_score < 0:
            if self._held_qty >= 1:
                orders.append(_order(bar, Side.SELL, self._held_qty, self.name))
            self._long = False
            self._held_qty = 0

        return orders


def _order(bar: Bar, side: Side, qty: int, strategy_name: str) -> OrderRequest:
    return OrderRequest(
        symbol=bar.symbol,
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        client_order_id=make_coid(strategy_name, bar),
    )
