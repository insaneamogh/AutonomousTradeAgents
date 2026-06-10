"""CLI smoke test for the backtester.

Usage:
    uv run python -m engine.backtester --smoke
    uv run python -m engine.backtester --stress
    uv run python -m engine.backtester --walk-forward
    uv run python -m engine.backtester --csv path/to/bars.csv --symbol AAPL

The smoke run generates 500 days of synthetic AAPL-shaped daily bars with a
trend + noise, runs SMA(20, 50) crossover with 10 shares per trade, and
prints the result. End-to-end validation that the contracts fit.

--walk-forward runs all 5 reference strategies through rolling train/test
windows and prints a comparison table.
"""

from __future__ import annotations

import argparse
import logging
import math
import random
from datetime import datetime, timedelta, timezone

from engine.backtester.engine import BacktestResult, Engine
from engine.backtester.events import Bar
from engine.backtester.feed import CsvBarFeed, InMemoryBarFeed
from engine.backtester.portfolio import Portfolio, Position
from engine.backtester.sim_broker import SimulatedBroker
from engine.backtester.strategies.breakout import Breakout
from engine.backtester.strategies.momentum import Momentum
from engine.backtester.strategies.rsi_mean_reversion import RsiMeanReversion
from engine.backtester.strategies.sma_crossover import SmaCrossover
from engine.backtester.strategies.vol_regime_switch import VolRegimeSwitch
from engine.backtester.walk_forward import WalkForwardReport, walk_forward

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s — %(message)s")
log = logging.getLogger("engine.backtester")


def _synthetic_bars(symbol: str, n_days: int = 500, seed: int = 42) -> list[Bar]:
    """Geometric-Brownian-ish daily series with a mild positive drift."""
    rng = random.Random(seed)
    start = datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc)
    bars: list[Bar] = []
    price = 100.0
    drift = 0.0003       # ~7.5% annualized
    vol = 0.018          # ~28% annualized
    for i in range(n_days):
        ts = start + timedelta(days=i)
        ret = drift + vol * rng.gauss(0, 1)
        new_close = price * math.exp(ret)
        intraday_range = abs(price - new_close) + price * 0.005
        high = max(price, new_close) + rng.random() * intraday_range
        low = min(price, new_close) - rng.random() * intraday_range
        bars.append(Bar(
            symbol=symbol,
            timestamp=ts,
            open=price,
            high=high,
            low=max(low, 0.01),
            close=new_close,
            volume=int(rng.uniform(2_000_000, 8_000_000)),
        ))
        price = new_close
    return bars


def _print_result(label: str, r: BacktestResult) -> None:
    log.info("=== %s ===", label)
    log.info("Bars:           %d", r.bars)
    log.info("Trades (fills): %d", r.trades)
    log.info("Risk trims:     %d", len(r.risk_trims))
    log.info("Risk vetoes:    %d", len(r.risk_vetoes))
    if r.risk_vetoes:
        veto_counts: dict[str, int] = {}
        for v in r.risk_vetoes:
            veto_counts[v.veto_rule] = veto_counts.get(v.veto_rule, 0) + 1
        top = sorted(veto_counts.items(), key=lambda x: -x[1])[:3]
        log.info("  top rules:    %s", ", ".join(f"{r}={c}" for r, c in top))
    log.info("Starting cash:  $%.2f", r.starting_cash)
    log.info("Ending equity:  $%.2f", r.ending_equity)
    log.info("Return:         %.2f%%", r.return_pct)
    log.info("Max drawdown:   %.2f%%", r.max_drawdown_pct)
    log.info("Sharpe (daily): %.2f", r.sharpe_daily)


def smoke(stress: bool = False) -> int:
    bars = _synthetic_bars("AAPL", n_days=500)
    feed = InMemoryBarFeed(bars)

    portfolio = Portfolio(starting_cash=100_000.0)
    # Default qty=10 keeps the strategy comfortably under the 5% cap so
    # no trims fire — useful as a sanity baseline. --stress sizes at 80
    # which lands between the 5% position cap and the 25% sector cap, so
    # position_size_cap TRIMS most BUYs (the most informative gate demo).
    qty = 80 if stress else 10
    strategy = SmaCrossover(fast=20, slow=50, qty=qty)
    broker = SimulatedBroker()

    engine = Engine(portfolio=portfolio, strategy=strategy, broker=broker)
    result = engine.run(feed)

    label = (
        f"Smoke (STRESS qty={qty}, trims expected): SMA(20,50) on 500d synthetic AAPL"
        if stress else f"Smoke (qty={qty}): SMA(20,50) on 500d synthetic AAPL"
    )
    _print_result(label, result)

    # Buy-and-hold baseline for context.
    bh_portfolio = Portfolio(starting_cash=100_000.0)
    bh_qty = int(bh_portfolio.cash // bars[0].open)
    bh_portfolio.cash -= bh_qty * bars[0].open
    bh_portfolio.positions[bars[0].symbol] = Position(qty=bh_qty, avg_entry_price=bars[0].open)
    last_close = {b.symbol: b.close for b in bars}
    bh_equity = bh_portfolio.mark_to_market(last_close)
    log.info(
        "--- Buy-and-hold baseline: equity=$%.2f, return=%.2f%% ---",
        bh_equity,
        (bh_equity / 100_000.0 - 1) * 100,
    )
    return 0


def walk_forward_smoke() -> int:
    """Compare all 5 reference strategies on rolling train/test windows.

    500 bars total; train=180, test=60 → roughly 6 windows of out-of-sample
    test. Strategies use shorter configurable lookbacks so they actually
    trade within a 60-bar test window.
    """
    bars = _synthetic_bars("AAPL", n_days=500)
    feed = InMemoryBarFeed(bars)

    # Factories — fresh instance per window so state can't leak across.
    # Lookbacks tuned to fit a 240-bar warm-up + 60-bar test slice.
    strategies: list[tuple[str, callable]] = [
        ("SmaCrossover",     lambda: SmaCrossover(fast=10, slow=30, qty=10)),
        ("RsiMeanReversion", lambda: RsiMeanReversion(rsi_period=14, oversold=30.0,
                                                      exit_threshold=50.0, qty=10)),
        ("Momentum",         lambda: Momentum(lookback_days=120, skip_days=10, qty=10)),
        ("Breakout",         lambda: Breakout(entry_window=20, exit_window=10, qty=10)),
        ("VolRegimeSwitch",  lambda: VolRegimeSwitch(vol_window=20, regime_lookback=60,
                                                     lookback_days=60, skip_days=5,
                                                     high_vol_percentile=0.80, qty=10)),
    ]

    reports: list[WalkForwardReport] = []
    for name, factory in strategies:
        report = walk_forward(
            factory,
            feed,
            train_bars=180,
            test_bars=60,
            strategy_name=name,
        )
        reports.append(report)

    _print_walk_forward_table(reports)
    return 0


def _print_walk_forward_table(reports: list[WalkForwardReport]) -> None:
    log.info("=== Walk-forward comparison (train=180, test=60 on 500d synthetic AAPL) ===")
    log.info(
        "%-20s %8s %10s %8s %12s %10s %8s",
        "Strategy", "Windows", "MeanRet%", "Win%", "MeanSharpe", "WorstDD%", "Trades",
    )
    log.info("%s", "-" * 80)
    for r in reports:
        log.info(
            "%-20s %8d %10.2f %8.1f %12.3f %10.2f %8d",
            r.strategy_name,
            r.n_windows,
            r.mean_return_pct,
            r.pct_winning_windows * 100.0,
            r.mean_sharpe,
            r.worst_drawdown_pct,
            r.total_trades,
        )
    log.info("%s", "-" * 80)
    log.info("Sharpe = mean of per-window Sharpes (not pooled). See walk_forward.py docstring.")


def from_csv(path: str, symbol: str) -> int:
    feed = CsvBarFeed(path, symbol)
    portfolio = Portfolio(starting_cash=100_000.0)
    strategy = SmaCrossover(fast=20, slow=50, qty=10)
    broker = SimulatedBroker()
    engine = Engine(portfolio=portfolio, strategy=strategy, broker=broker)
    result = engine.run(feed)
    _print_result(f"CSV: {path} ({symbol})", result)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Event-driven backtester smoke test.")
    parser.add_argument("--smoke", action="store_true", help="run a smoke backtest on synthetic data")
    parser.add_argument(
        "--stress",
        action="store_true",
        help="--smoke + qty=80 to force RiskGate trims (demonstrates the gate)",
    )
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="compare all 5 reference strategies via rolling train/test windows",
    )
    parser.add_argument("--csv", help="path to a CSV (timestamp,open,high,low,close,volume)")
    parser.add_argument("--symbol", default="AAPL")
    args = parser.parse_args()

    if args.csv:
        return from_csv(args.csv, args.symbol)
    if args.walk_forward:
        return walk_forward_smoke()
    if args.smoke or args.stress:
        return smoke(stress=args.stress)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
