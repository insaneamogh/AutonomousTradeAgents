"""Position-sizing wire types.

Zero-dep dataclasses — same hygiene as ``engine.risk.types``. The sizer is
called from the council, the backtester, and any future paper-trade
harness. Keep this module Pydantic-free so it stays usable everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class SizingInputs:
    """What the sizer needs to compute a qty + stop + target."""

    symbol: str
    last_price: float
    atr_14: float | None
    """14-day ATR. ``None`` (or non-positive) triggers the fallback
    fixed-percent sizing path."""
    account_equity: float
    confidence: float = 0.5
    """0..1 — linearly scales the risk dollars per trade. confidence=1.0
    uses the full ``risk_per_trade_pct``; confidence=0.0 → qty=0.
    """


@dataclass(frozen=True)
class SizingDecision:
    """The sizer's output. ``qty`` may be 0 if confidence/clamping zeroed it out."""

    qty: int
    target_notional: float
    stop_price: float
    target_price: float
    method: Literal["atr", "fallback_pct"]
    notes: str = ""
