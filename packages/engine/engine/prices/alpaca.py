"""Alpaca market-data provider — free IEX daily bars via alpaca-py.

Requires ``ALPACA_API_KEY`` + ``ALPACA_SECRET_KEY`` env (data-only keys
work). Import of alpaca-py is deferred to first use so environments
without the dependency (or keys) never pay for it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time

from engine.prices.base import DailyClose

logger = logging.getLogger("engine.prices.alpaca")


class AlpacaPriceProvider:
    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._client = StockHistoricalDataClient(self._api_key, self._secret_key)
        return self._client

    async def daily_closes(self, symbol: str, start: date, end: date) -> list[DailyClose]:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        req = StockBarsRequest(
            symbol_or_symbols=symbol.upper(),
            timeframe=TimeFrame.Day,
            start=datetime.combine(start, time.min, tzinfo=UTC),
            end=datetime.combine(end, time.max, tzinfo=UTC),
        )
        # alpaca-py is sync — run in a thread so the evaluator stays async.
        bars = await asyncio.to_thread(self._get_client().get_stock_bars, req)
        data = bars.data.get(symbol.upper(), [])
        return [DailyClose(day=b.timestamp.date(), close=float(b.close)) for b in data]
