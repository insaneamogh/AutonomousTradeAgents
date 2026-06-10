"""Walk-forward harness — the honest way to compare strategies.

PLAN.md §6.1: "Walk-forward by default — train on rolling N months, test on
next M months." A single in-sample backtest tells you whether a strategy
*could* have made money on this exact feed; walk-forward tells you whether
it would have on data it didn't see yet.

Phase 0/1 scope: this is "rolling test with warm-up", not full walk-forward
optimization. Each window:
  1. Warm-up: feed train bars through ``strategy.on_bar``, discard the
     returned OrderRequests. Just fills indicator buffers (ATR, SMAs, RSI,
     vol history, etc.) so the first test-bar trade isn't on cold buffers.
  2. Test: run ``Engine.run()`` on the test bars with a fresh portfolio +
     broker. Capture the BacktestResult.

Phase 2 will add parameter optimization on the train slice (a real
walk-forward). The contract here will hold — ``strategy_factory()`` can
return a pre-tuned strategy then.

Sharpe convention: ``mean_sharpe`` is the **mean of per-window Sharpes**,
not the Sharpe of the pooled returns. That's more honest — it gives equal
weight to each test period rather than letting one big-window streak
dominate. Documented here so the next reader knows.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from statistics import mean, median

from engine.backtester.engine import BacktestResult, Engine
from engine.backtester.events import Bar
from engine.backtester.feed import BarFeed, InMemoryBarFeed
from engine.backtester.portfolio import Portfolio
from engine.backtester.risk_gate import RiskGate
from engine.backtester.sim_broker import SimulatedBroker
from engine.backtester.strategy import Strategy


@dataclass(frozen=True)
class WalkForwardWindow:
    index: int
    train_start: datetime
    train_end: datetime           # exclusive
    test_start: datetime
    test_end: datetime            # exclusive
    train_bars: int
    test_bars: int
    result: BacktestResult        # the full test-slice backtest
    # Convenience aggregates (flat copies of the deeper result fields).
    return_pct: float
    sharpe_daily: float
    max_drawdown_pct: float
    trades: int


@dataclass(frozen=True)
class WalkForwardReport:
    strategy_name: str
    windows: tuple[WalkForwardWindow, ...]
    # Cross-window aggregates.
    mean_return_pct: float
    median_return_pct: float
    pct_winning_windows: float    # 0..1; fraction with return_pct > 0
    mean_sharpe: float            # mean of per-window sharpes (NOT pooled)
    worst_drawdown_pct: float     # max drawdown observed in any window
    total_trades: int

    @property
    def n_windows(self) -> int:
        return len(self.windows)


def walk_forward(
    strategy_factory: Callable[[], Strategy],
    feed: BarFeed,
    *,
    train_bars: int,
    test_bars: int,
    portfolio_factory: Callable[[], Portfolio] = lambda: Portfolio(starting_cash=100_000.0),
    broker_factory: Callable[[], SimulatedBroker] = SimulatedBroker,
    risk_gate: RiskGate | None = None,
    strategy_name: str | None = None,
) -> WalkForwardReport:
    """Run rolling (train → test) backtests over ``feed``.

    Each window gets fresh strategy / portfolio / broker so state from
    the prior window can't leak in. Train bars warm up indicator buffers
    (no orders placed); test bars run through the full Engine + RiskGate.

    Returns a ``WalkForwardReport`` aggregating across all windows. If the
    feed has fewer than ``train_bars + test_bars`` bars, returns an empty
    report (no exception) — let the caller decide how to surface that.
    """
    if train_bars < 0 or test_bars <= 0:
        raise ValueError(f"train_bars must be >=0 and test_bars must be >0; got {train_bars}, {test_bars}")

    bars = list(feed)
    name = strategy_name or _infer_strategy_name(strategy_factory)
    windows: list[WalkForwardWindow] = []

    for idx, (train, test) in enumerate(_split_windows(bars, train_bars=train_bars, test_bars=test_bars)):
        strategy = strategy_factory()
        if train_bars > 0:
            _warmup_strategy(strategy, train)

        engine = Engine(
            portfolio=portfolio_factory(),
            strategy=strategy,
            broker=broker_factory(),
            risk_gate=risk_gate,
        )
        result = engine.run(InMemoryBarFeed(test))

        windows.append(
            WalkForwardWindow(
                index=idx,
                train_start=train[0].timestamp if train else test[0].timestamp,
                train_end=train[-1].timestamp if train else test[0].timestamp,
                test_start=test[0].timestamp,
                test_end=test[-1].timestamp,
                train_bars=len(train),
                test_bars=len(test),
                result=result,
                return_pct=result.return_pct,
                sharpe_daily=result.sharpe_daily,
                max_drawdown_pct=result.max_drawdown_pct,
                trades=result.trades,
            )
        )

    if not windows:
        return WalkForwardReport(
            strategy_name=name,
            windows=(),
            mean_return_pct=0.0,
            median_return_pct=0.0,
            pct_winning_windows=0.0,
            mean_sharpe=0.0,
            worst_drawdown_pct=0.0,
            total_trades=0,
        )

    returns = [w.return_pct for w in windows]
    sharpes = [w.sharpe_daily for w in windows]
    wins = sum(1 for r in returns if r > 0)
    return WalkForwardReport(
        strategy_name=name,
        windows=tuple(windows),
        mean_return_pct=mean(returns),
        median_return_pct=median(returns),
        pct_winning_windows=wins / len(returns),
        mean_sharpe=mean(sharpes),
        worst_drawdown_pct=max((w.max_drawdown_pct for w in windows), default=0.0),
        total_trades=sum(w.trades for w in windows),
    )


# ─────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────


def _split_windows(
    bars: list[Bar],
    *,
    train_bars: int,
    test_bars: int,
) -> Iterator[tuple[list[Bar], list[Bar]]]:
    """Yield (train_slice, test_slice) pairs by sliding ``test_bars`` forward
    each step. Stops when there aren't enough remaining bars."""
    cursor = 0
    while cursor + train_bars + test_bars <= len(bars):
        train = bars[cursor : cursor + train_bars]
        test = bars[cursor + train_bars : cursor + train_bars + test_bars]
        yield train, test
        cursor += test_bars


def _warmup_strategy(strategy: Strategy, train: list[Bar]) -> None:
    """Push train bars through the strategy, discard any emitted orders.

    The strategy's internal state (buffers, prev_close, _long, _held_qty)
    persists into the test slice. The portfolio + broker do NOT — those
    are fresh per window via the factories.

    Careful: if the strategy's `_long` flips during warm-up, the test
    slice starts already-long. That's a feature, not a bug — it mirrors
    a real "you started this period mid-trade" scenario. Tests should be
    aware (use ``train_bars=0`` for strict cold-start tests).
    """
    for bar in train:
        strategy.on_bar(bar)


def _infer_strategy_name(factory: Callable[[], Strategy]) -> str:
    """Best-effort: instantiate once + read ``.name``, else fall back to repr."""
    try:
        instance = factory()
    except Exception:  # noqa: BLE001
        return "strategy"
    name = getattr(instance, "name", None)
    if isinstance(name, str) and name:
        return name
    return type(instance).__name__
