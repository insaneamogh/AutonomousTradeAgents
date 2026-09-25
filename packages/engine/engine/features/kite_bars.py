"""Daily bars and instrument facts for Indian symbols, from Kite Connect.

docs/PLAN_ZERODHA.md Z2. Every bar the council saw came from Alpaca, which
has no NSE data, so an NSE symbol could not be analysed at all.

Kite's market data rides on the user's daily access token (the paid
Kite Connect plan; the free "personal" API has no market data), so this
provider does not build its own client: ``client_factory`` opens one the
way the rest of the app does (app.services.broker.broker_use), which also
means an expired morning token surfaces as that error, not as empty bars.

Symbols are ``EXCHANGE:TRADINGSYMBOL`` (``NSE:RELIANCE``, the index
``NSE:NIFTY 50``). The instruments dump maps them to Kite's
instrument_token and carries the lot and tick sizes that sizing and the
lot-size rule must read, never hardcode: NSE revises lot sizes by
circular (NIFTY went to 65 in January 2026).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from engine.features.bars import DEFAULT_LOOKBACK_DAYS
from engine.features.technicals import DailyBar
from engine.risk.markets import exchange_of, tradingsymbol_of

logger = logging.getLogger("engine.features.kite_bars")

_IST = ZoneInfo("Asia/Kolkata")

ClientFactory = Callable[[], AbstractAsyncContextManager[Any]]


class KiteDailyBarsProvider:
    """``BarsProvider`` for Indian-exchange symbols."""

    name = "kite"

    def __init__(self, client_factory: ClientFactory) -> None:
        self._client_factory = client_factory
        self._instruments: dict[str, tuple[date, dict[str, dict[str, Any]]]] = {}
        self._bars: dict[tuple[str, date, int], list[DailyBar]] = {}

    async def instrument(self, symbol: str) -> dict[str, Any] | None:
        """The dump row for ``EXCHANGE:TRADINGSYMBOL`` (a bare symbol means
        NSE), or None when Kite lists no such instrument today."""
        exchange = (exchange_of(symbol) or "NSE").upper()
        today = datetime.now(_IST).date()
        cached = self._instruments.get(exchange)
        if cached is None or cached[0] != today:
            async with self._client_factory() as client:
                rows = await client.instruments(exchange)
            by_symbol = {str(r["tradingsymbol"]).upper(): r for r in rows}
            self._instruments[exchange] = (today, by_symbol)
            cached = self._instruments[exchange]
            logger.info("kite_bars: %d %s instruments cached for %s", len(by_symbol), exchange, today)
        return cached[1].get(tradingsymbol_of(symbol).upper())

    async def daily_bars(
        self, symbol: str, *, lookback_days: int = DEFAULT_LOOKBACK_DAYS
    ) -> list[DailyBar]:
        today = datetime.now(_IST).date()
        key = (symbol.upper(), today, lookback_days)
        if key in self._bars:
            return self._bars[key]
        inst = await self.instrument(symbol)
        if inst is None or not inst.get("instrument_token"):
            logger.warning("kite_bars: %s is not in today's instruments dump", symbol)
            return []
        end = datetime.now(UTC)
        start = end - timedelta(days=lookback_days)
        async with self._client_factory() as client:
            candles = await client.historical_daily(
                int(inst["instrument_token"]), start=start, end=end
            )
        bars = [
            DailyBar(
                day=ts.astimezone(_IST).date(), open=o, high=h, low=lo, close=c, volume=v
            )
            for ts, o, h, lo, c, v in candles
        ]
        self._bars[key] = bars
        return bars


class MarketRoutedBarsProvider:
    """One ``BarsProvider`` for both markets: an Indian-exchange symbol goes
    to Kite, everything else to the US provider. Without it, an NSE symbol
    asked Alpaca for bars and got none."""

    name = "market_routed"

    def __init__(self, us: Any, india: KiteDailyBarsProvider | None) -> None:
        self.us = us
        self.india = india

    async def daily_bars(
        self, symbol: str, *, lookback_days: int = DEFAULT_LOOKBACK_DAYS
    ) -> list[DailyBar]:
        from engine.risk.markets import market_of

        if market_of(symbol) == "IN":
            if self.india is None:
                logger.warning("bars: %s is an Indian symbol and no Kite provider is wired", symbol)
                return []
            return await self.india.daily_bars(symbol, lookback_days=lookback_days)
        return await self.us.daily_bars(symbol, lookback_days=lookback_days)

    async def prefetch_daily_bars(self, symbols: list[str], **kwargs: Any) -> Any:
        """Batch-prefetch the US symbols only (Kite has no batch endpoint;
        its per-symbol calls are cached for the day)."""
        from engine.risk.markets import market_of

        prefetch = getattr(self.us, "prefetch_daily_bars", None)
        us_symbols = [s for s in symbols if market_of(s) != "IN"]
        if prefetch is None or not us_symbols:
            return None
        return await prefetch(us_symbols, **kwargs)
