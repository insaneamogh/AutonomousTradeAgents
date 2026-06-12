"""Daily OHLCV bar providers — Alpaca (IEX feed) + protocol.

Mirrors ``engine.prices``'s provider pattern: lazy alpaca-py import, sync
SDK wrapped in ``asyncio.to_thread``. Bars come back oldest → newest, ready
for ``compute_technicals``.

An in-process per-day cache keeps the daily council from re-fetching the
same symbol's history (and SPY's, used for relative strength) on every
council pass within a run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol, runtime_checkable

from engine.features.technicals import DailyBar

logger = logging.getLogger("engine.features.bars")


@runtime_checkable
class BarsProvider(Protocol):
    name: str

    async def daily_bars(self, symbol: str, *, lookback_days: int = 320) -> list[DailyBar]: ...


class AlpacaDailyBarsProvider:
    """Free-tier IEX daily bars. Data-only API keys work."""

    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._client = None
        # (symbol, utc_date, lookback) → bars. Cleared naturally by process
        # lifecycle; the daily cron is a fresh process per run.
        self._cache: dict[tuple[str, date, int], list[DailyBar]] = {}

    def _get_client(self):
        if self._client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._client = StockHistoricalDataClient(self._api_key, self._secret_key)
        return self._client

    async def daily_bars(self, symbol: str, *, lookback_days: int = 320) -> list[DailyBar]:
        sym = symbol.upper()
        today = datetime.now(UTC).date()
        cache_key = (sym, today, lookback_days)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        start = today - timedelta(days=lookback_days)
        req = StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame.Day,
            start=datetime.combine(start, time.min, tzinfo=UTC),
            end=datetime.combine(today, time.max, tzinfo=UTC),
        )
        raw = await asyncio.to_thread(self._get_client().get_stock_bars, req)
        data = raw.data.get(sym, [])
        bars = [
            DailyBar(
                day=b.timestamp.date(),
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(b.volume),
            )
            for b in data
        ]
        bars.sort(key=lambda b: b.day)
        if not bars:
            logger.warning("bars: Alpaca returned no daily bars for %s", sym)
        self._cache[cache_key] = bars
        return bars
