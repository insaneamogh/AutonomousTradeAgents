"""RSI mean-reversion reference strategy.

Classic 14-period RSI:
  - BUY  when RSI dips below ``oversold`` and we're flat.
  - SELL (close long) when RSI recovers above ``exit_threshold``.
  - No short side — Phase 0/1 is long-only.

Phase 0 simplification: SMA-of-gains / SMA-of-losses over the window instead
of Wilder's EMA-smoothed RSI. Close enough for a baseline; the difference is
visible only on long horizons.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from broker.types import OrderRequest, OrderType, Side
from engine.backtester.events import Bar
from engine.backtester.strategies._utils import RollingAtr, make_coid, size_for_entry
from engine.sizing import AtrSizingConfig


@dataclass
class RsiMeanReversion:
    rsi_period: int = 14
    oversold: float = 30.0
    exit_threshold: float = 50.0
    qty: int = 10
    name: str = "rsi_mean_reversion"

    sizing_config: AtrSizingConfig | None = None
    starting_equity: float = 100_000.0
    atr_window: int = 14
    confidence: float = 0.7

    _gains: deque[float] = field(default_factory=deque, init=False)
    _losses: deque[float] = field(default_factory=deque, init=False)
    _prev_close: float | None = field(default=None, init=False)
    _atr: RollingAtr = field(default_factory=RollingAtr, init=False)
    _long: bool = field(default=False, init=False)
    _held_qty: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._gains = deque(maxlen=self.rsi_period)
        self._losses = deque(maxlen=self.rsi_period)
        self._atr = RollingAtr(window=self.atr_window)

    def on_bar(self, bar: Bar) -> list[OrderRequest]:
        self._atr.update(bar)

        if self._prev_close is not None:
            change = bar.close - self._prev_close
            self._gains.append(max(change, 0.0))
            self._losses.append(max(-change, 0.0))
        self._prev_close = bar.close

        if len(self._gains) < self.rsi_period:
            return []

        avg_gain = sum(self._gains) / self.rsi_period
        avg_loss = sum(self._losses) / self.rsi_period
        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        orders: list[OrderRequest] = []
        if not self._long and rsi < self.oversold:
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
        elif self._long and rsi > self.exit_threshold:
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
