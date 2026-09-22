"""The default bar window must feed every feature the strategies read.

`quant.ret_252d_pct` needs 253 closes. The window was 320 calendar days
(~220 trading bars) until 2026-09-23, so that feature was None on every
live run and momentum's 12-month leg (weight 0.3) always scored neutral.
The 6-year backtest computed features over full history, so the live
signal was not the one the backtest measured.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from engine.features.bars import DEFAULT_LOOKBACK_DAYS
from engine.features.market_calendar import is_us_trading_day
from engine.features.provider import SPY_LOOKBACK_DAYS
from engine.features.quant import compute_quant
from engine.features.technicals import DailyBar

# ret_252d_pct = closes[-1] / closes[-253] - 1
LONGEST_FEATURE_WINDOW = 253


def _trading_days_in_window(end: date, lookback: int) -> int:
    # daily_bars requests [today - lookback, today); today's bar is not final.
    start = end - timedelta(days=lookback)
    return sum(
        1 for i in range((end - start).days) if is_us_trading_day(start + timedelta(days=i))
    )


@pytest.mark.parametrize(
    "end",
    [date(2024, 1, 2), date(2025, 7, 7), date(2026, 9, 23), date(2026, 12, 28)],
)
def test_the_default_window_holds_enough_trading_bars(end: date) -> None:
    got = _trading_days_in_window(end, DEFAULT_LOOKBACK_DAYS)
    assert got >= LONGEST_FEATURE_WINDOW, (
        f"{DEFAULT_LOOKBACK_DAYS} calendar days ending {end} yields {got} trading "
        f"bars; ret_252d_pct needs {LONGEST_FEATURE_WINDOW}"
    )


def test_spy_benchmark_uses_the_same_window() -> None:
    """Same number, one place: a different SPY window would split the
    `(symbol, day, lookback)` prefetch cache and cost an extra request."""
    assert SPY_LOOKBACK_DAYS == DEFAULT_LOOKBACK_DAYS


def test_a_default_window_of_bars_populates_the_12_month_return() -> None:
    """End to end through the real `compute_quant`, not just the arithmetic."""
    end = date(2026, 9, 23)
    days = [
        end - timedelta(days=DEFAULT_LOOKBACK_DAYS) + timedelta(days=i)
        for i in range(DEFAULT_LOOKBACK_DAYS)
    ]
    bars = [
        DailyBar(
            day=d,
            open=100.0 + i * 0.1,
            high=101.0 + i * 0.1,
            low=99.0 + i * 0.1,
            close=100.0 + i * 0.1,
            volume=1_000_000.0,
        )
        for i, d in enumerate(d for d in days if is_us_trading_day(d))
    ]
    assert compute_quant(bars).ret_252d_pct is not None
