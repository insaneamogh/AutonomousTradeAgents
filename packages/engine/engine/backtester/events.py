"""Backtester event types.

The simulated broker accepts the same ``broker.types.OrderRequest`` the live
broker accepts — that's the whole point of the abstraction. Fill events are
backtester-internal (live broker returns its own ``Order`` shape).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from broker.types import Order, Side


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar. ``timestamp`` is the close time of the bar."""

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class FillEvent:
    """Internal: a simulated fill produced by ``SimulatedBroker``."""

    order: Order
    symbol: str
    side: Side
    qty: int
    fill_price: float
    fill_time: datetime
    sec_fee: float
    finra_taf: float
    slippage_bps: float
