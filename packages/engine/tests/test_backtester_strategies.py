"""Reference strategies — round-trip BUY → SELL on a synthetic feed.

Each test confirms:
  - The strategy fires at least one BUY on a feed designed to trigger entries.
  - The matching SELL fires when conditions reverse.
  - SELL qty == BUY qty (the lesson from 9160408b — over-selling burns trust).
  - Strategy honors `_held_qty` tracking (no shorts via accidental qty drift).

`risk_gate=None` is used everywhere — risk-gate behavior is covered by
test_backtester_risk.py. These tests isolate the strategy.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

from engine.backtester import (
    Bar,
    Breakout,
    Engine,
    Momentum,
    Portfolio,
    RsiMeanReversion,
    SimulatedBroker,
    VolRegimeSwitch,
)


class _BarsFeed:
    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars

    def __iter__(self) -> Iterator[Bar]:
        return iter(self._bars)


def _make_bar(
    symbol: str,
    day: int,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
) -> Bar:
    start = datetime(2025, 1, 2, 21, 0, tzinfo=timezone.utc)
    return Bar(
        symbol=symbol,
        timestamp=start + timedelta(days=day),
        open=close - 0.1,
        high=(high if high is not None else close + 0.2),
        low=(low if low is not None else close - 0.2),
        close=close,
        volume=1_000_000,
    )


def _engine_for(strategy) -> Engine:
    return Engine(
        portfolio=Portfolio(starting_cash=100_000.0),
        strategy=strategy,
        broker=SimulatedBroker(),
        risk_gate=None,   # isolate strategy behavior
    )


def _assert_round_trip(fills, *, side_buy: str = "BUY") -> None:
    """Generic: ≥1 BUY + ≥1 SELL, SELL.qty matches BUY.qty."""
    buys = [f for f in fills if f.side.value if str(f.side).endswith(side_buy)]
    sells = [f for f in fills if str(f.side).endswith("SELL")]
    assert buys, f"expected at least one BUY fill; got {fills!r}"
    assert sells, f"expected at least one SELL fill; got {fills!r}"
    assert sells[0].qty == buys[0].qty, (
        f"SELL qty {sells[0].qty} != BUY qty {buys[0].qty} — held_qty tracking is off"
    )


# ─────────────────────────────────────────────────────────────────────
# RsiMeanReversion
# ─────────────────────────────────────────────────────────────────────


def test_rsi_mean_reversion_round_trip() -> None:
    # 25 down days drive RSI below 30; 25 up days drive RSI above 50.
    bars: list[Bar] = []
    price = 100.0
    for i in range(25):
        price -= 1.0
        bars.append(_make_bar("AAPL", i, price))
    for i in range(25):
        price += 1.5
        bars.append(_make_bar("AAPL", 25 + i, price))

    strategy = RsiMeanReversion(rsi_period=14, oversold=30.0, exit_threshold=50.0, qty=10)
    result = _engine_for(strategy).run(_BarsFeed(bars))
    _assert_round_trip(result.fills)


# ─────────────────────────────────────────────────────────────────────
# Momentum
# ─────────────────────────────────────────────────────────────────────


def test_momentum_round_trip() -> None:
    # lookback=30, skip=5. We need 31+ bars before the first signal. Build a
    # feed that goes up for 35 bars (positive 30-5 momentum → BUY), then
    # falls for 35 bars (negative momentum → SELL).
    bars: list[Bar] = []
    price = 100.0
    for i in range(35):
        price += 0.8
        bars.append(_make_bar("AAPL", i, price))
    for i in range(35):
        price -= 0.7
        bars.append(_make_bar("AAPL", 35 + i, price))

    strategy = Momentum(lookback_days=30, skip_days=5, qty=10)
    result = _engine_for(strategy).run(_BarsFeed(bars))
    _assert_round_trip(result.fills)


# ─────────────────────────────────────────────────────────────────────
# Breakout
# ─────────────────────────────────────────────────────────────────────


def test_breakout_round_trip() -> None:
    # entry_window=10. Need 10+ bars of consolidation, then a breakout above
    # the 10-day high. Then 5+ bars of fading, then a break below the 5-day low.
    bars: list[Bar] = []
    # 12 bars consolidating around 100
    for i in range(12):
        bars.append(_make_bar("AAPL", i, 100.0 + (i % 3) * 0.1, high=100.5, low=99.5))
    # Strong breakout: 6 bars marching up past 105
    for i in range(6):
        bars.append(_make_bar("AAPL", 12 + i, 105.0 + i, high=106.0 + i, low=104.5 + i))
    # 7 bars marching back down — will break below the 5-day low buffer.
    for i in range(7):
        bars.append(_make_bar("AAPL", 18 + i, 102.0 - i, high=103.0 - i, low=101.0 - i))

    strategy = Breakout(entry_window=10, exit_window=5, qty=10)
    result = _engine_for(strategy).run(_BarsFeed(bars))
    _assert_round_trip(result.fills)


# ─────────────────────────────────────────────────────────────────────
# VolRegimeSwitch
# ─────────────────────────────────────────────────────────────────────


def test_vol_regime_switch_round_trip() -> None:
    # Tighter windows so the test runs at reasonable length.
    # vol_window=5, regime_lookback=20, lookback=20, skip=2.
    bars: list[Bar] = []
    price = 100.0
    # 25 bars of low-vol uptrend (regime "normal", momentum positive) → BUY
    for i in range(25):
        price += 0.5 + (0.05 if i % 2 == 0 else -0.04)
        bars.append(_make_bar("AAPL", i, price))
    # 15 bars of low-vol DOWNtrend so momentum_score flips negative → SELL
    for i in range(15):
        price -= 0.5 + (0.05 if i % 2 == 0 else -0.04)
        bars.append(_make_bar("AAPL", 25 + i, price))

    strategy = VolRegimeSwitch(
        vol_window=5,
        regime_lookback=20,
        lookback_days=20,
        skip_days=2,
        high_vol_percentile=0.95,  # very few bars in 'high' — keeps test deterministic
        qty=10,
    )
    result = _engine_for(strategy).run(_BarsFeed(bars))
    _assert_round_trip(result.fills)
