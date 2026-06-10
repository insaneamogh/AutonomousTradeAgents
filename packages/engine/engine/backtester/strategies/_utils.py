"""Shared helpers for reference strategies.

Extracted after writing SmaCrossover + 4 more reference strategies — the
ATR buffer and sizer-call shape were repeating verbatim. This is the
"feel the pain first, then extract" pattern the AGENTV1 playbook called for.

Two helpers:
  - ``RollingAtr``  — true-range buffer with a 14-period (configurable) SMA-ATR.
  - ``size_for_entry`` — single entry point for "turn a bar into an OrderRequest qty",
                          fixed-qty fallback when no ``sizing_config`` is provided.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field

from engine.backtester.events import Bar
from engine.sizing import AtrSizingConfig, SizingInputs, atr_position_size


@dataclass
class RollingAtr:
    """Wilder-style ATR with a simple SMA over true ranges. Phase 0 simplification —
    real ATR uses EMA smoothing (alpha = 1/window). Good enough for vol-targeted sizing.
    """

    window: int = 14

    _tr_buf: deque[float] = field(default_factory=deque, init=False)
    _prev_close: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._tr_buf = deque(maxlen=self.window)

    def update(self, bar: Bar) -> None:
        """Push this bar's true range into the buffer."""
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._tr_buf.append(tr)
        self._prev_close = bar.close

    @property
    def value(self) -> float | None:
        """ATR or None if the buffer isn't full yet."""
        if len(self._tr_buf) < self.window:
            return None
        return sum(self._tr_buf) / self.window


def size_for_entry(
    bar: Bar,
    *,
    atr: float | None,
    sizing_config: AtrSizingConfig | None,
    starting_equity: float,
    confidence: float,
    fallback_qty: int,
) -> int:
    """Resolve the entry qty.

    - ``sizing_config=None`` → returns ``fallback_qty`` (legacy fixed sizing).
    - ``sizing_config`` set → calls ``engine.sizing.atr_position_size`` with
      the strategy's ATR estimate. Returns 0 when the sizer says skip.
    """
    if sizing_config is None:
        return fallback_qty
    sizing = atr_position_size(
        SizingInputs(
            symbol=bar.symbol,
            last_price=bar.close,
            atr_14=atr,
            account_equity=starting_equity,
            confidence=confidence,
        ),
        config=sizing_config,
    )
    return max(0, sizing.qty)


def make_coid(strategy_name: str, bar: Bar) -> str:
    """Idempotency key for the broker. ``strategy_name`` keeps audit logs
    distinguishable across multiple strategies running on the same symbol."""
    return f"{strategy_name}-{bar.symbol}-{bar.timestamp.isoformat()}-{uuid.uuid4().hex[:6]}"
