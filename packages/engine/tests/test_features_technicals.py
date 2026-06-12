"""Technical-indicator math tests — hand-constructed bar series with
verifiable expected values. These are the numbers the agents reason over
and the sizer trades on; they must never drift silently.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from engine.features import (
    DailyBar,
    InsufficientBarsError,
    compute_technicals,
    sector_relative_strength,
)


def _bars(closes: list[float], *, spread: float = 1.0, volume: float = 100.0) -> list[DailyBar]:
    """Bars with symmetric high/low around the close and no overnight gaps
    beyond the close-to-close move itself."""
    start = date(2026, 1, 1)
    out: list[DailyBar] = []
    for i, c in enumerate(closes):
        out.append(
            DailyBar(
                day=start + timedelta(days=i),
                open=c,
                high=c + spread / 2,
                low=c - spread / 2,
                close=c,
                volume=volume,
            )
        )
    return out


def test_insufficient_bars_raises() -> None:
    with pytest.raises(InsufficientBarsError):
        compute_technicals(_bars([100.0] * 59))


def test_strictly_rising_series_is_a_clean_uptrend() -> None:
    closes = [100.0 + i for i in range(250)]  # 250 bars, +1/day
    t = compute_technicals(_bars(closes))

    assert t["trend_regime"] == "uptrend"
    assert t["rsi_14"] == 100.0  # no down days → Wilder RSI pegs at 100
    assert t["dma20_pct"] > 0
    assert t["dma50_pct"] > 0
    assert t["dma200_pct"] > 0
    assert t["trend_position_score"] == 100.0
    assert t["vwap_position"] == "above"
    assert t["mean_reversion_risk"] == 100.0  # RSI 100 = maximally stretched


def test_strictly_falling_series_is_a_downtrend() -> None:
    closes = [350.0 - i for i in range(250)]
    t = compute_technicals(_bars(closes))

    assert t["trend_regime"] == "downtrend"
    assert t["rsi_14"] == 0.0
    assert t["trend_position_score"] == 0.0
    assert t["dma200_pct"] < 0


def test_atr_constant_range_no_gap_equals_the_range() -> None:
    """Flat closes with a constant $2 daily range → true range = 2 every
    day → Wilder ATR = exactly 2."""
    closes = [100.0] * 100
    t = compute_technicals(_bars(closes, spread=2.0))
    assert t["atr_14"] == pytest.approx(2.0)


def test_dma200_is_none_below_200_bars() -> None:
    closes = [100.0 + i * 0.1 for i in range(120)]
    t = compute_technicals(_bars(closes))
    assert t["dma200_pct"] is None
    # SMA200 legs get neutral credit: close>20 (25) + close>50 (25) + 25.
    assert t["trend_position_score"] == 75.0


def test_volume_ratio_reflects_a_spike() -> None:
    closes = [100.0] * 100
    bars = _bars(closes)
    spiked = bars[:-1] + [
        DailyBar(
            day=bars[-1].day,
            open=100.0,
            high=100.5,
            low=99.5,
            close=100.0,
            volume=2000.0,  # 19 days at 100 + this → avg 195
        )
    ]
    t = compute_technicals(spiked)
    assert t["volume_ratio_20d"] == pytest.approx(2000.0 / 195.0, abs=0.01)


def test_sector_relative_strength_symbol_vs_spy() -> None:
    # Symbol +10% over the 21-day window, SPY +5% → +5.00 points.
    sym = _bars([100.0] * 30 + [100.0 + i * (10.0 / 21.0) for i in range(22)])
    spy = _bars([100.0] * 30 + [100.0 + i * (5.0 / 21.0) for i in range(22)])
    rs = sector_relative_strength(sym, spy)
    assert rs == pytest.approx(5.0, abs=0.1)


def test_same_bars_same_output() -> None:
    """Determinism: identical inputs → identical dicts."""
    closes = [100.0 + ((i * 7) % 13) - 6 for i in range(250)]
    a = compute_technicals(_bars(closes))
    b = compute_technicals(_bars(closes))
    assert a == b
