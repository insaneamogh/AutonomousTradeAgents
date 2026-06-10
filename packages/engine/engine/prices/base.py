"""PriceProvider protocol + the daily-close record."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class DailyClose:
    day: date
    close: float


@runtime_checkable
class PriceProvider(Protocol):
    """Daily close prices for one symbol over a date window (inclusive)."""

    name: str

    async def daily_closes(self, symbol: str, start: date, end: date) -> list[DailyClose]: ...
