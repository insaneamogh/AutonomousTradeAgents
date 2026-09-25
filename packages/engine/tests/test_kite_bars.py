"""Indian market data (docs/PLAN_ZERODHA.md Z2): Kite bars, routing by
market, and what the feature provider does differently for an NSE symbol."""

from __future__ import annotations

import contextlib
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from engine.features.kite_bars import KiteDailyBarsProvider, MarketRoutedBarsProvider
from engine.features.provider import RealFeatureProvider
from engine.features.technicals import DailyBar


class _FakeKite:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def instruments(self, exchange: str) -> list[dict[str, Any]]:
        self.calls.append(("instruments", exchange))
        return [
            {"tradingsymbol": "RELIANCE", "instrument_token": 738561, "lot_size": 1},
            {"tradingsymbol": "NIFTY 50", "instrument_token": 256265, "lot_size": 0},
        ]

    async def historical_daily(self, token: int, *, start: datetime, end: datetime):
        self.calls.append(("history", token))
        base = datetime(2026, 1, 1, 18, 30, tzinfo=UTC)  # 00:00 IST the next day
        return [
            (base + timedelta(days=i), 100 + i, 101 + i, 99 + i,
             100 + i + math.sin(i), 1e6)
            for i in range(300)
        ]


def _factory(fake: _FakeKite):
    @contextlib.asynccontextmanager
    async def _open():
        yield fake

    return _open


async def test_kite_bars_resolve_the_token_and_date_candles_in_ist() -> None:
    fake = _FakeKite()
    kite = KiteDailyBarsProvider(_factory(fake))
    bars = await kite.daily_bars("NSE:RELIANCE")
    assert bars[0].day.isoformat() == "2026-01-02"  # 18:30 UTC is midnight IST
    await kite.daily_bars("NSE:RELIANCE")
    await kite.daily_bars("NSE:NIFTY 50")
    # One dump per exchange per day, one history call per symbol per day.
    assert fake.calls == [("instruments", "NSE"), ("history", 738561), ("history", 256265)]
    assert await kite.daily_bars("NSE:NOT_LISTED") == []


class _FakeUS:
    name = "fake_us"

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def daily_bars(self, symbol: str, *, lookback_days: int = 400) -> list[DailyBar]:
        self.asked.append(symbol)
        return []


async def test_bars_are_routed_by_the_symbols_market() -> None:
    us, fake = _FakeUS(), _FakeKite()
    routed = MarketRoutedBarsProvider(us, KiteDailyBarsProvider(_factory(fake)))
    await routed.daily_bars("AAPL")
    assert await routed.daily_bars("NSE:RELIANCE")
    assert us.asked == ["AAPL"], "an NSE symbol must never be asked of Alpaca"
    assert await MarketRoutedBarsProvider(us, None).daily_bars("NSE:RELIANCE") == []


async def test_an_nse_symbol_gets_nifty_its_own_equity_and_no_us_blocks() -> None:
    fake = _FakeKite()
    sources: list[str] = []

    async def equity(source: str | None = None) -> float:
        sources.append(source)
        return 1_000_000.0

    asked: list[str] = []

    class _Recorder:
        """Records instead of raising: _optional_blocks swallows a block's
        exception, so a raise could never fail this test."""

        async def fetch(self, symbol: str, *_a: Any) -> Any:
            asked.append(symbol)
            return None

        liquidity = fetch

    provider = RealFeatureProvider(
        bars=MarketRoutedBarsProvider(_FakeUS(), KiteDailyBarsProvider(_factory(fake))),
        equity_resolver=equity, news=_Recorder(), quotes=_Recorder(),
        corporate_actions=_Recorder(), asset_info=_Recorder(), options_context=_Recorder(),
    )
    features = await provider("NSE:RELIANCE")
    assert asked == [], "an Alpaca-only block was asked about an NSE symbol"

    assert ("history", 256265) in fake.calls, "the benchmark must be NIFTY 50, not SPY"
    assert features["feature_source"] == "kite" and features["universe"] == "IN"
    assert features["macro"]["available"] is False
    assert features["portfolio_equity"] == 1_000_000.0 and sources == ["zerodha"]
    for key in ("news", "liquidity", "events", "asset", "options_context"):
        assert key not in features


@pytest.mark.parametrize("resolver_takes_source", [True, False])
async def test_an_older_equity_resolver_without_source_still_works(
    resolver_takes_source: bool,
) -> None:
    from engine.features.provider import _resolve_equity

    async def with_source(source: str | None = None) -> float:
        return 1.0 if source == "zerodha" else 0.0

    async def without() -> float:
        return 2.0

    got = await _resolve_equity(with_source if resolver_takes_source else without, "zerodha")
    assert got == (1.0 if resolver_takes_source else 2.0)
