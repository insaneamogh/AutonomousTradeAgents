"""Vol-regime-switch strategy — conditional momentum.

Same momentum trigger as ``Momentum``, but gated by a realized-vol regime:
  - Compute ``vol_20d`` = stdev of daily returns over the last ``vol_window`` days.
  - Track those vol_20d values in a rolling history of ``regime_lookback`` days.
  - Current regime = "high" if vol_20d sits in the top ``high_vol_percentile``
    of that history; otherwise "normal".
  - BUY when momentum_score > 0 AND regime is "normal" AND flat.
  - SELL (close long) when momentum_score < 0 OR regime flips to "high".

This is the closest of the four to what a real shop runs — the high-vol veto
saves you from holding through August 2024–style spikes. ATR-sized positions
also automatically shrink when ATR widens, so the two safety nets compound.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field

from broker.types import OrderRequest, OrderType, Side
from engine.backtester.events import Bar
from engine.backtester.strategies._utils import RollingAtr, make_coid, size_for_entry
from engine.sizing import AtrSizingConfig


@dataclass
class VolRegimeSwitch:
    vol_window: int = 20
    regime_lookback: int = 252
    high_vol_percentile: float = 0.80
    lookback_days: int = 252
    skip_days: int = 21
    qty: int = 10
    name: str = "vol_regime_switch"

    sizing_config: AtrSizingConfig | None = None
    starting_equity: float = 100_000.0
    atr_window: int = 14
    confidence: float = 0.7

    _closes: deque[float] = field(default_factory=deque, init=False)
    _returns: deque[float] = field(default_factory=deque, init=False)
    _vol_history: deque[float] = field(default_factory=deque, init=False)
    _atr: RollingAtr = field(default_factory=RollingAtr, init=False)
    _prev_close: float | None = field(default=None, init=False)
    _long: bool = field(default=False, init=False)
    _held_qty: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._closes = deque(maxlen=self.lookback_days + 1)
        self._returns = deque(maxlen=self.vol_window)
        self._vol_history = deque(maxlen=self.regime_lookback)
        self._atr = RollingAtr(window=self.atr_window)

    def on_bar(self, bar: Bar) -> list[OrderRequest]:
        self._atr.update(bar)
        self._closes.append(bar.close)

        # Daily return for vol calc.
        if self._prev_close is not None and self._prev_close > 0:
            self._returns.append((bar.close - self._prev_close) / self._prev_close)
        self._prev_close = bar.close

        # Rolling 20d vol → push into vol-history once full.
        if len(self._returns) >= self.vol_window:
            vol_20d = statistics.pstdev(self._returns)
            self._vol_history.append(vol_20d)
        else:
            vol_20d = None

        # Need history to score the regime.
        if vol_20d is None or len(self._vol_history) < max(20, self.vol_window):
            return []

        regime = self._classify_regime(vol_20d)

        # Momentum trigger.
        if len(self._closes) <= self.lookback_days or self.skip_days <= 0:
            return []
        anchor_old = self._closes[0]
        if anchor_old <= 0:
            return []
        anchor_skip = self._closes[-(self.skip_days + 1)]
        momentum_score = (anchor_skip - anchor_old) / anchor_old

        orders: list[OrderRequest] = []
        if not self._long and momentum_score > 0 and regime == "normal":
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
        elif self._long and (momentum_score < 0 or regime == "high"):
            if self._held_qty >= 1:
                orders.append(_order(bar, Side.SELL, self._held_qty, self.name))
            self._long = False
            self._held_qty = 0

        return orders

    def _classify_regime(self, current_vol: float) -> str:
        """Bucket current vol against its rolling history. 'normal' or 'high'."""
        history = sorted(self._vol_history)
        if not history:
            return "normal"
        # Percentile rank — fraction of history values ≤ current_vol.
        count_le = sum(1 for v in history if v <= current_vol)
        percentile = count_le / len(history)
        return "high" if percentile >= self.high_vol_percentile else "normal"


def _order(bar: Bar, side: Side, qty: int, strategy_name: str) -> OrderRequest:
    return OrderRequest(
        symbol=bar.symbol,
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        client_order_id=make_coid(strategy_name, bar),
    )
