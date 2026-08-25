"""OHLCV bar providers — Alpaca (IEX feed) + protocols.

Mirrors ``engine.prices``'s provider pattern: lazy alpaca-py import, sync
SDK wrapped in ``asyncio.to_thread``. Bars come back oldest → newest, ready
for ``compute_technicals``.

An in-process per-day cache keeps the daily council from re-fetching the
same symbol's history (and SPY's, used for relative strength) on every
council pass within a run.

Two providers live here because they share the feed constraints and the
lazy-import pattern, but they answer different questions:

  - ``AlpacaDailyBarsProvider``    — settled daily bars for indicators.
    Ends YESTERDAY: a half-formed daily candle would skew every moving
    average the analysts read.
  - ``AlpacaIntradayBarsProvider`` — today's live-ish tape for the
    continuous scanner, batched across the whole watchlist in one
    request. This is the one that must NOT end yesterday.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic
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

        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        start = today - timedelta(days=lookback_days)
        # ``feed=IEX`` is required, not cosmetic: without it alpaca-py asks
        # for the SIP consolidated tape, which a free/paper data plan
        # rejects outright ("subscription does not permit querying recent
        # SIP data") — the whole council then 500s. IEX is the free tier's
        # entitled feed and is more than adequate for daily bars.
        #
        # ``end`` is yesterday for the same reason: SIP-vs-IEX aside,
        # free plans embargo the most recent 15 minutes, and asking for
        # today's partial (still-forming) daily bar is both restricted and
        # wrong for swing signals — a half-day candle would skew every
        # moving average and ATR the analysts read.
        req = StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame.Day,
            start=datetime.combine(start, time.min, tzinfo=UTC),
            end=datetime.combine(today - timedelta(days=1), time.max, tzinfo=UTC),
            feed=DataFeed.IEX,
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


# ─────────────────────────────────────────────────────────────────────
# Intraday bars — the continuous scanner's live-price feed
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IntradayBar:
    """One intraday OHLCV bar. ``ts`` is the bar's opening instant (UTC)."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@runtime_checkable
class IntradayBarsProvider(Protocol):
    name: str

    async def intraday_bars(
        self, symbols: list[str], *, bar_minutes: int = 15, session_start: datetime | None = None
    ) -> dict[str, list[IntradayBar]]: ...


class AlpacaIntradayBarsProvider:
    """Free-tier IEX intraday bars for a whole watchlist in ONE request.

    Why batched: the scanner polls every few minutes across the full
    watchlist. Alpaca's ``symbol_or_symbols`` accepts a list, so 15 symbols
    cost one HTTP round-trip, not fifteen. At a 5-minute cadence over a
    6.5-hour session that is ~78 requests/day total — trivially inside the
    free tier's 200 req/min, and it does not scale with watchlist size.

    Two feed constraints that are load-bearing, not cosmetic:

      - ``feed=DataFeed.IEX``. The alpaca-py default asks for the SIP
        consolidated tape, which a free/paper data plan rejects outright,
        500-ing the entire call. IEX is what this plan is entitled to.
      - The free plan embargoes the most recent ~15 minutes, so ``end`` is
        pinned to ``now − DATA_DELAY_MINUTES``. Asking for fresher data
        returns an error, not an empty list. The practical consequence:
        the scanner's "live" price is up to ~20 minutes stale. For a swing
        product on 1-10 day holds that is immaterial; for anything
        intraday it would not be, and this is the line where a paid feed
        becomes necessary.

    IEX is also a single venue (~2-3% of consolidated volume), so intraday
    VOLUME here is a fraction of true tape volume. Volume triggers compare
    IEX-to-IEX (today's IEX volume vs a 20-day IEX average from the same
    feed), which keeps the ratio meaningful even though the absolute
    number is not.
    """

    name = "alpaca-intraday"

    #: Free-plan embargo on recent data, plus a minute of slack.
    DATA_DELAY_MINUTES = 16

    def __init__(self, api_key: str, secret_key: str, *, cache_ttl_seconds: float = 60.0) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._client: object | None = None
        self._cache_ttl = cache_ttl_seconds
        # (symbols, bar_minutes, session_start) → (fetched_at_monotonic, bars).
        # Short-lived: two scans inside the TTL must not double-charge the
        # data API, but the whole point is fresh prices, so the TTL stays
        # well under the scan interval.
        self._cache: dict[tuple[str, int, str], tuple[float, dict[str, list[IntradayBar]]]] = {}

    def _get_client(self) -> object:
        if self._client is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._client = StockHistoricalDataClient(self._api_key, self._secret_key)
        return self._client

    async def intraday_bars(
        self,
        symbols: list[str],
        *,
        bar_minutes: int = 15,
        session_start: datetime | None = None,
    ) -> dict[str, list[IntradayBar]]:
        """Bars for every symbol since ``session_start``, oldest → newest.

        Symbols with no prints in the window map to an empty list rather
        than being dropped — the caller must be able to tell "quiet" from
        "not requested".
        """
        syms = sorted({s.upper() for s in symbols})
        if not syms:
            return {}

        now = datetime.now(UTC)
        start = session_start or datetime.combine(now.date(), time.min, tzinfo=UTC)
        cache_key = (",".join(syms), bar_minutes, start.isoformat())
        cached = self._cache.get(cache_key)
        if cached is not None and (monotonic() - cached[0]) < self._cache_ttl:
            return cached[1]

        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        req = StockBarsRequest(
            symbol_or_symbols=syms,
            timeframe=TimeFrame(bar_minutes, TimeFrameUnit.Minute),
            start=start,
            end=now - timedelta(minutes=self.DATA_DELAY_MINUTES),
            feed=DataFeed.IEX,
        )
        raw = await asyncio.to_thread(self._get_client().get_stock_bars, req)  # type: ignore[attr-defined]

        out: dict[str, list[IntradayBar]] = {}
        for sym in syms:
            bars = [
                IntradayBar(
                    ts=b.timestamp,
                    open=float(b.open),
                    high=float(b.high),
                    low=float(b.low),
                    close=float(b.close),
                    volume=float(b.volume),
                )
                for b in raw.data.get(sym, [])
            ]
            bars.sort(key=lambda b: b.ts)
            out[sym] = bars

        empty = [s for s, b in out.items() if not b]
        if empty:
            logger.info("bars: no intraday IEX prints yet for %s", ",".join(empty))
        self._cache[cache_key] = (monotonic(), out)
        return out


def intraday_provider_from_env() -> AlpacaIntradayBarsProvider | None:
    """Intraday provider when Alpaca data keys are set; otherwise None."""
    api_key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret:
        return None
    return AlpacaIntradayBarsProvider(api_key, secret)
