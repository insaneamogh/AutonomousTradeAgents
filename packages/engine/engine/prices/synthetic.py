"""Synthetic daily closes — deterministic seeded walk.

Same idiom as ``trading_agents.features.synthetic``: the (symbol, day)
pair fully determines the price, so re-running the ghost evaluator is
idempotent and tests are reproducible. Anchored at ``anchor_price`` when
provided (the proposal's entry price) so ghost P&L magnitudes are sane.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

from engine.prices.base import DailyClose

_DAILY_VOL = 0.018  # ~1.8% daily move cap, equities-ish


def _unit_noise(symbol: str, day: date) -> float:
    """Deterministic noise in [-1, 1] from (symbol, day)."""
    digest = hashlib.sha256(f"{symbol.upper()}:{day.isoformat()}".encode()).digest()
    return (int.from_bytes(digest[:8], "big") / 2**64) * 2.0 - 1.0


class SyntheticPriceProvider:
    """Seeded random walk. ``anchor_price`` pins the first day's close."""

    name = "synthetic"

    def __init__(self, anchor_price: float = 100.0, anchor_day: date | None = None) -> None:
        self._anchor_price = max(anchor_price, 0.01)
        self._anchor_day = anchor_day

    async def daily_closes(self, symbol: str, start: date, end: date) -> list[DailyClose]:
        if end < start:
            return []
        anchor_day = self._anchor_day or start
        out: list[DailyClose] = []
        price = self._anchor_price
        # Walk forward from the anchor; weekends excluded (no bar).
        day = anchor_day
        while day <= end:
            if day.weekday() < 5:
                if day != anchor_day:
                    price = price * (1.0 + _unit_noise(symbol, day) * _DAILY_VOL)
                if day >= start:
                    out.append(DailyClose(day=day, close=round(price, 4)))
            day += timedelta(days=1)
        return out
