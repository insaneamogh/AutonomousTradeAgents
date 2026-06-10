"""SMA-crossover reference strategy.

Fast SMA crosses above slow SMA → flat or short flips to long (BUY).
Fast SMA crosses below slow SMA → long flips to flat (SELL full position).

This is a baseline — it loses money in chop and prints in trends. The point
is end-to-end plumbing, not alpha. Use it as the smoke test for new engine work.

Sizing modes:
  - Fixed qty:          ``SmaCrossover(qty=10)`` — legacy, easy regression baseline.
  - Vol-targeted:       ``SmaCrossover(sizing_config=AtrSizingConfig(...), starting_equity=100_000)``
                        — strategy buffers recent bars, computes a 14-day ATR via
                        ``RollingAtr``, delegates qty to ``engine.sizing.atr_position_size``.
                        Tracks held qty so SELLs flat exactly what was opened.

The two modes are mutually exclusive: passing ``sizing_config`` makes ``qty``
the *fallback* (used only when ATR isn't ready yet — first 14 bars).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from broker.types import OrderRequest, OrderType, Side
from engine.backtester.events import Bar
from engine.backtester.strategies._utils import RollingAtr, make_coid, size_for_entry
from engine.sizing import AtrSizingConfig


@dataclass
class SmaCrossover:
    fast: int = 20
    slow: int = 50
    qty: int = 10
    name: str = "sma_crossover"

    sizing_config: AtrSizingConfig | None = None
    starting_equity: float = 100_000.0
    atr_window: int = 14
    confidence: float = 0.7

    _fast_buf: deque[float] = field(default_factory=deque, init=False)
    _slow_buf: deque[float] = field(default_factory=deque, init=False)
    _atr: RollingAtr = field(default_factory=RollingAtr, init=False)
    _prev_signal: int = field(default=0, init=False)
    _long: bool = field(default=False, init=False)
    _held_qty: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._fast_buf = deque(maxlen=self.fast)
        self._slow_buf = deque(maxlen=self.slow)
        self._atr = RollingAtr(window=self.atr_window)

    def on_bar(self, bar: Bar) -> list[OrderRequest]:
        self._atr.update(bar)
        self._fast_buf.append(bar.close)
        self._slow_buf.append(bar.close)

        if len(self._slow_buf) < self.slow:
            return []

        fast_avg = sum(self._fast_buf) / self.fast
        slow_avg = sum(self._slow_buf) / self.slow
        signal = 1 if fast_avg > slow_avg else -1

        orders: list[OrderRequest] = []
        if signal == 1 and not self._long and self._prev_signal != 0:
            buy_qty = size_for_entry(
                bar,
                atr=self._atr.value,
                sizing_config=self.sizing_config,
                starting_equity=self.starting_equity,
                confidence=self.confidence,
                fallback_qty=self.qty,
            )
            if buy_qty >= 1:
                orders.append(_buy(bar, buy_qty, self.name))
                self._long = True
                self._held_qty = buy_qty
        elif signal == -1 and self._long:
            # SELL exactly what we hold — same lesson as everywhere else in this engine.
            sell_qty = self._held_qty
            if sell_qty >= 1:
                orders.append(_sell(bar, sell_qty, self.name))
            self._long = False
            self._held_qty = 0

        self._prev_signal = signal
        return orders


def _buy(bar: Bar, qty: int, strategy_name: str) -> OrderRequest:
    return OrderRequest(
        symbol=bar.symbol,
        side=Side.BUY,
        qty=qty,
        order_type=OrderType.MARKET,
        client_order_id=make_coid(strategy_name, bar),
    )


def _sell(bar: Bar, qty: int, strategy_name: str) -> OrderRequest:
    return OrderRequest(
        symbol=bar.symbol,
        side=Side.SELL,
        qty=qty,
        order_type=OrderType.MARKET,
        client_order_id=make_coid(strategy_name, bar),
    )
