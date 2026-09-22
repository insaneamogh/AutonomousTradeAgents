"""Does `strategy_fit` predict anything — and at which horizon?

Every parameter question so far was answered against 21 recorded premium
paths and 4-7 completed trades, and six of seven hypotheses tested that way
were refuted, two of them after I had already reported them as findings.
That sample supports diagnosis, not decisions. This is the sample that
supports decisions: **58 symbols x ~1,530 trading days = 88,822 bars,
2020-07 to 2026-09.**

It tests the one thing that must be true for any of the rest to matter: at
the moment `strategy_fit` says "tradable", does the underlying actually go
the predicted way, and over what holding period?

**It drives the production code, not a copy.** `compute_technicals` and
`compute_quant` are pure functions over `DailyBar`, and `best_strategy` is
the same entrypoint `nodes/strategy_fit.py` calls. A reimplementation would
only ever measure the reimplementation.

**No look-ahead.** Features at day *t* are computed from bars `[0..t]`
inclusive and the forward return is measured from the CLOSE of *t* to the
close of *t+h*. The signal can never see a bar it would not have had.

What it deliberately does NOT test: the LLM council, contract selection, or
option P&L. If the underlying signal has no edge, none of those can rescue
it — and if it has edge at 20 days but not at 2, that is the whole finding
about horizon, measured at scale.

    python -m tests.eval.signal_backtest
    python -m tests.eval.signal_backtest --by-strategy
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import statistics as st
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from engine.alpha import AlphaModel
from engine.features.quant import compute_quant
from engine.features.technicals import DailyBar, compute_technicals
from trading_agents.strategies.alpha import StrategyFitAlpha
from trading_agents.strategies.horizon import strategy_horizon_days

_FIXTURE = Path(__file__).parent / "fixtures" / "bars.json.gz"

_MIN_HISTORY = 260
"""Bars needed before the first signal. `_momentum` reads `ret_252d_pct`,
so anything shorter silently scores that component NEUTRAL and we would be
measuring a different strategy than production runs."""

HORIZONS = (2, 5, 10, 20, 60)
"""2 is what the live system actually did (measured average hold: 2.1
days). The rest bracket the strategies' own windows."""


@dataclass(frozen=True)
class Signal:
    symbol: str
    day: date
    strategy: str
    direction: str
    score: float
    fwd: dict[int, float]
    """Forward % return of the UNDERLYING, per horizon, sign-adjusted for
    direction — so positive always means "the thesis was right"."""


def _load() -> dict[str, list[DailyBar]]:
    with gzip.open(_FIXTURE, "rt") as fh:
        raw = json.load(fh)
    out: dict[str, list[DailyBar]] = {}
    for sym, rows in raw["bars"].items():
        out[sym] = [
            DailyBar(
                day=date.fromisoformat(r[0]), open=float(r[1]), high=float(r[2]),
                low=float(r[3]), close=float(r[4]), volume=float(r[5]),
            )
            for r in rows
        ]
    return out


def _features(bars: list[DailyBar], bench: list[DailyBar] | None) -> dict:
    """The same blocks the council assembles, from bars up to and including
    the signal day."""
    tech = compute_technicals(bars)
    quant = compute_quant(bars, benchmark_bars=bench)
    return {
        "technicals": tech,
        "quant": quant.__dict__ if hasattr(quant, "__dict__") else dict(quant),
        # Absent blocks score NEUTRAL by design (see `_Features`), which is
        # also what happens live for a symbol with no news/pattern data.
        "patterns": {},
        "news": {},
        "events": {},
    }


def run(
    *,
    step: int = 5,
    allow_shorts: bool = True,
    model: AlphaModel | None = None,
) -> list[Signal]:
    """Walk every symbol forward, `step` days at a time.

    `step` is a sampling stride, not a holding rule: adjacent days produce
    near-identical features, so scoring all of them would inflate the count
    without adding information and make overlapping windows look like
    independent observations.

    `model` is any `AlphaModel`, defaulting to the shipped `strategy_fit`.
    This is the point of the seam: a new candidate signal is measured by
    passing it here, not by writing a second harness. Nothing reaches the
    live path without first clearing the measurement that refuted the
    incumbent.
    """
    model = model or StrategyFitAlpha(allow_shorts=allow_shorts)
    data = _load()
    bench = data.get("SPY")
    signals: list[Signal] = []
    longest = max(HORIZONS)

    for sym, bars in data.items():
        if sym == "SPY" or len(bars) < _MIN_HISTORY + longest:
            continue
        for t in range(_MIN_HISTORY, len(bars) - longest, step):
            window = bars[: t + 1]
            bench_window = (
                [b for b in bench if b.day <= bars[t].day] if bench else None
            )
            sig = model.evaluate(_features(window, bench_window), symbol=sym)
            # "Was a call made?" is `strategy_id`, NOT `value != 0`: a
            # strategy can clear the fit floor with zero conviction, which
            # makes `value` 0.0 for a genuine directional call. Abstentions
            # are dropped rather than scored as flat — counting a model that
            # never formed a view as a 50/50 call is how a broken signal
            # reads as merely mediocre.
            if sig.abstained or not sig.meta.get("strategy_id"):
                continue
            entry = bars[t].close
            sign = 1.0 if sig.meta["direction"] == "long" else -1.0
            signals.append(
                Signal(
                    symbol=sym, day=bars[t].day,
                    strategy=sig.meta["strategy_id"],
                    direction=sig.meta["direction"],
                    score=sig.meta["score"],
                    fwd={
                        h: sign * (bars[t + h].close - entry) / entry * 100.0
                        for h in HORIZONS
                    },
                )
            )
    return signals


def non_overlapping(signals: list[Signal], horizon: int) -> list[Signal]:
    """Keep at most one signal per symbol per forward window.

    Sampling every 5 days but measuring a 60-day forward return means
    twelve consecutive observations share almost the whole window — they
    are not independent, and treating them as such overstates the
    precision of any z-score computed from them.

    This mattered in practice: at `step=5` the 2-day cell read z=+0.29,
    and at `step=20` the same cell read **z=-2.81**, which looks
    significant. Neither was real — the two strides were slicing
    overlapping windows differently. With genuinely independent
    observations both collapse to a coin flip. Any future claim of an edge
    must come through here first.
    """
    last: dict[str, date] = {}
    kept: list[Signal] = []
    for s in sorted(signals, key=lambda x: (x.symbol, x.day)):
        prev = last.get(s.symbol)
        # Calendar padding: `horizon` is trading days, `day` deltas are
        # calendar days, so 1.5x is a deliberate over-estimate. Erring
        # toward fewer, cleaner observations is the safe direction.
        if prev is None or (s.day - prev).days >= horizon * 1.5:
            kept.append(s)
            last[s.symbol] = s.day
    return kept


def z_score(vals: list[float]) -> float:
    """Standard-normal z for "hit rate differs from 50%"."""
    n = len(vals)
    if n == 0:
        return float("nan")
    p = sum(1 for v in vals if v > 0) / n
    return (p - 0.5) / math.sqrt(0.25 / n)


def _stats(vals: list[float]) -> tuple[float, float, float]:
    """(hit rate %, mean %, median %)"""
    if not vals:
        return (float("nan"),) * 3
    hits = sum(1 for v in vals if v > 0) / len(vals) * 100.0
    return hits, st.mean(vals), st.median(vals)


def report(signals: list[Signal], *, by_strategy: bool = False) -> None:
    print(f"\n{len(signals):,} tradable signals, 58 symbols, 2020-07 to 2026-09\n")
    print(f"  {'horizon':>8} {'n':>6} {'hit':>7} {'mean':>8} {'median':>8} {'z':>6}")
    print("  " + "-" * 66)
    for h in HORIZONS:
        indep = non_overlapping(signals, h)
        vals = [s.fwd[h] for s in indep]
        hit, mean, med = _stats(vals)
        z = z_score(vals)
        tag = "  <- what we actually held" if h == 2 else ""
        print(
            f"  {h:>6}d  {len(vals):>6} {hit:>7.1f}% {mean:>+7.2f}% "
            f"{med:>+7.2f}% {z:>+6.2f}{tag}"
        )

    print("\n  n is INDEPENDENT observations (non-overlapping forward windows).")
    print("  |z| < 1.96 means not distinguishable from a coin flip, and five")
    print("  horizons are tested — one crossing by chance would be unremarkable.")

    # The verdict, drawn by code rather than by reading the table above.
    # Equity costs (10 bps round trip); an options candidate must also pass
    # tests.eval.option_backtest, which charges spread and theta.
    from tests.eval.acceptance import Observation, evaluate

    print("\n  acceptance bar (net of 10 bps, Bonferroni over the horizons):\n")
    for h in HORIZONS:
        obs = [Observation(day=s.day, ret_pct=s.fwd[h]) for s in non_overlapping(signals, h)]
        print(f"  {h:>3}d  {evaluate(obs, n_tests=len(HORIZONS)).line()}")

    if not by_strategy:
        return
    print("\n  by strategy, at each strategy's OWN horizon:\n")
    print(f"  {'strategy':20} {'n':>6} {'own h':>6} {'hit':>7} {'mean':>8} {'z':>6}")
    print("  " + "-" * 60)
    grouped: dict[str, list[Signal]] = defaultdict(list)
    for s in signals:
        grouped[s.strategy].append(s)
    for name, rows in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        own = strategy_horizon_days(name)
        h = min(HORIZONS, key=lambda x: abs(x - own))
        indep = non_overlapping(rows, h)
        vals = [r.fwd[h] for r in indep]
        hit, mean, _ = _stats(vals)
        print(
            f"  {name:20} {len(vals):>6} {h:>5}d {hit:>6.1f}% {mean:>+7.2f}% "
            f"{z_score(vals):>+6.2f}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--by-strategy", action="store_true")
    ap.add_argument("--step", type=int, default=5)
    args = ap.parse_args()
    report(run(step=args.step), by_strategy=args.by_strategy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
