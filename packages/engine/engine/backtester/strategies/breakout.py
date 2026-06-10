"""Donchian-channel breakout strategy.

  - BUY  when close exceeds the rolling max of the last ``entry_window`` highs
         and we're flat.
  - SELL (close long) when close drops below the rolling min of the last
         ``exit_window`` lows.

Classic 20-up / 10-down channel by default. Doesn't trade in chop because the
channel is wide; cuts losses faster than entries because exit_window < entry_window.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from broker.types import OrderRequest, OrderType, Side
from engine.backtester.events import Bar
from engine.backtester.strategies._utils import RollingAtr, make_coid, size_for_entry
from engine.sizing import AtrSizingConfig


@dataclass
class Breakout:
    entry_window: int = 20
    exit_window: int = 10
    qty: int = 10
    name: str = "breakout"

    sizing_config: AtrSizingConfig | None = None
    starting_equity: float = 100_000.0
    atr_window: int = 14
    confidence: float = 0.7

    _highs: deque[float] = field(default_factory=deque, init=False)
    _lows: deque[float] = field(default_factory=deque, init=False)
    _atr: RollingAtr = field(default_factory=RollingAtr, init=False)
    _long: bool = field(default=False, init=False)
    _held_qty: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._highs = deque(maxlen=self.entry_window)
        self._lows = deque(maxlen=self.exit_window)
        self._atr = RollingAtr(window=self.atr_window)

    def on_bar(self, bar: Bar) -> list[OrderRequest]:
        self._atr.update(bar)

        orders: list[OrderRequest] = []

        # Check breakout BEFORE pushing this bar's high — we want to compare
        # against the previous N bars, not against bars including the current one.
        if not self._long and len(self._highs) >= self.entry_window:
            if bar.close > max(self._highs):
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
        elif self._long and len(self._lows) >= self.exit_window:
            if bar.close < min(self._lows):
                if self._held_qty >= 1:
                    orders.append(_order(bar, Side.SELL, self._held_qty, self.name))
                self._long = False
                self._held_qty = 0

        # Now buffer this bar.
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        return orders


def _order(bar: Bar, side: Side, qty: int, strategy_name: str) -> OrderRequest:
    return OrderRequest(
        symbol=bar.symbol,
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        client_order_id=make_coid(strategy_name, bar),
    )
