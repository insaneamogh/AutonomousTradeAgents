"""Daily closes for NSE/NFO symbols from Kite Connect, for ghost marking.

The Refusal Ledger prices each refusal from daily closes. Alpaca has no
Indian symbols (its bars call errors on ``NSE:RELIANCE``), so an NSE
refusal is priced here, through the same KiteDailyBarsProvider the
council's features use (instruments dump -> token -> historical candles).
An option contract is priced while it is still listed: an expired
contract is gone from the dump, so it returns no closes and the ghost
keeps the marks it already has.
"""

from __future__ import annotations

from datetime import date

from engine.features.kite_bars import KiteDailyBarsProvider
from engine.prices.base import DailyClose


class KitePriceProvider:
    name = "kite"

    def __init__(self, bars: KiteDailyBarsProvider) -> None:
        self._bars = bars

    async def daily_closes(self, symbol: str, start: date, end: date) -> list[DailyClose]:
        lookback = max(10, (date.today() - start).days + 10)
        bars = await self._bars.daily_bars(symbol, lookback_days=lookback)
        return [DailyClose(day=b.day, close=b.close) for b in bars if start <= b.day <= end]
