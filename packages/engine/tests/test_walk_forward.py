"""Walk-forward harness tests.

Cover the math + lifecycle, not strategy P&L. Strategy correctness is
already pinned by test_backtester_strategies.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean as _mean

from engine.backtester import (
    Bar,
    InMemoryBarFeed,
    SmaCrossover,
    walk_forward,
)


def _bars(n: int, *, start_price: float = 100.0, drift: float = 0.5) -> list[Bar]:
    """Linearly trending bars, deterministic. Cheap to reason about in asserts."""
    start = datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc)
    bars: list[Bar] = []
    price = start_price
    for i in range(n):
        bars.append(
            Bar(
                symbol="TEST",
                timestamp=start + timedelta(days=i),
                open=price,
                high=price + 0.5,
                low=price - 0.5,
                close=price + drift,
                volume=1_000_000,
            )
        )
        price += drift
    return bars


def test_walk_forward_produces_expected_window_count() -> None:
    """100 bars, train=20, test=10 → cursor steps 0, 10, 20, ... 70.
    Each cursor must satisfy cursor + 20 + 10 <= 100 → cursor <= 70 → 8 windows.
    """
    bars = _bars(100)
    report = walk_forward(
        lambda: SmaCrossover(fast=5, slow=10, qty=1),
        InMemoryBarFeed(bars),
        train_bars=20,
        test_bars=10,
    )
    assert report.n_windows == 8


def test_walk_forward_each_window_runs_independently() -> None:
    """Every window starts with starting_cash == 100_000.

    Bug check: if portfolios leaked across windows, the per-window
    BacktestResult.starting_cash would diverge from the factory default.
    """
    bars = _bars(80)
    report = walk_forward(
        lambda: SmaCrossover(fast=5, slow=10, qty=1),
        InMemoryBarFeed(bars),
        train_bars=20,
        test_bars=10,
    )
    assert report.n_windows >= 2  # need at least 2 to detect leakage
    for w in report.windows:
        assert w.result.starting_cash == 100_000.0


def test_walk_forward_aggregates_match_per_window() -> None:
    """mean_return_pct + pct_winning_windows = aggregate of per-window numbers."""
    bars = _bars(80)
    report = walk_forward(
        lambda: SmaCrossover(fast=5, slow=10, qty=1),
        InMemoryBarFeed(bars),
        train_bars=20,
        test_bars=10,
    )
    expected_mean = _mean(w.return_pct for w in report.windows)
    assert abs(report.mean_return_pct - expected_mean) < 1e-9
    wins = sum(1 for w in report.windows if w.return_pct > 0)
    assert abs(report.pct_winning_windows - (wins / len(report.windows))) < 1e-9


def test_walk_forward_with_zero_train_bars() -> None:
    """train_bars=0 means cold-start each test window. Should still produce
    windows + aggregates without raising — useful for low-history regressions."""
    bars = _bars(60)
    report = walk_forward(
        lambda: SmaCrossover(fast=5, slow=10, qty=1),
        InMemoryBarFeed(bars),
        train_bars=0,
        test_bars=20,
    )
    # 60 / 20 = 3 windows.
    assert report.n_windows == 3
    for w in report.windows:
        assert w.train_bars == 0


def test_walk_forward_insufficient_data_yields_empty_report() -> None:
    """Feed shorter than train+test → 0 windows, zero aggregates, no exception."""
    bars = _bars(15)
    report = walk_forward(
        lambda: SmaCrossover(fast=5, slow=10, qty=1),
        InMemoryBarFeed(bars),
        train_bars=20,
        test_bars=10,
    )
    assert report.n_windows == 0
    assert report.mean_return_pct == 0.0
    assert report.total_trades == 0


def test_walk_forward_strategy_name_inferred_from_factory() -> None:
    """If strategy has .name, use it; else fall back to class name."""
    report = walk_forward(
        lambda: SmaCrossover(fast=5, slow=10, qty=1),
        InMemoryBarFeed(_bars(50)),
        train_bars=10,
        test_bars=10,
    )
    assert report.strategy_name == "sma_crossover"

    explicit = walk_forward(
        lambda: SmaCrossover(fast=5, slow=10, qty=1),
        InMemoryBarFeed(_bars(50)),
        train_bars=10,
        test_bars=10,
        strategy_name="MyCustomLabel",
    )
    assert explicit.strategy_name == "MyCustomLabel"
