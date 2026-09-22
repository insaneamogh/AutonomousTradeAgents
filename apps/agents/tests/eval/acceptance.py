"""The bar a candidate signal must clear before it may trade. Pure functions.

docs/PLAN_PLATFORM.md Phase 3. The 6-year backtest showed the shipped
signal is a coin flip, and the investigation before it refuted six of
seven hypotheses, two of them after they had been reported as findings.
Both failures had the same root: the pass/fail line was drawn after
looking at the numbers. This module draws it BEFORE, as code, so a
verdict cannot be argued into existence.

What "passes" means, every clause of which has already bitten here:

  - **Independent observations.** The caller hands in one return per
    non-overlapping window (see `signal_backtest.non_overlapping`).
    Overlapping windows made a coin flip read z=-2.81.
  - **Net of costs.** A signal that is right before costs and wrong after
    them is wrong. Costs are charged per observation, not averaged in.
  - **Mean return, not hit rate.** Hit rate ignores magnitude, which is
    what an options book actually loses on (entry_quality: right
    direction, too small a move, still -23.5%).
  - **Significant after the number of tests run.** Five horizons tested
    at 5% each gives ~23% odds that one crosses by chance. The threshold
    rises with `n_tests` (Bonferroni).
  - **Stable.** The first and second halves of the sample, split by date,
    must each point the same way with some strength of their own. One
    good regime is not an edge.
  - **Enough history.** At least `min_years` of signal span, so the
    sample has seen more than one market (2021-22's drawdown and the
    recoveries either side of it, on the current fixture).
"""

from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist


@dataclass(frozen=True)
class Observation:
    day: date
    ret_pct: float
    """Sign-adjusted GROSS return in percent: positive means the call was
    right. Costs are charged by `evaluate`, never pre-netted by the caller,
    so every candidate pays the same cost the same way."""


@dataclass(frozen=True)
class AcceptanceBar:
    min_n: int = 100
    min_t: float = 2.0
    """Floor on the t-statistic of the mean net return. The effective
    threshold is the larger of this and the Bonferroni-adjusted two-sided
    5% critical value for `n_tests`."""
    min_half_t: float = 1.0
    cost_pct: float = 0.10
    """Round-trip cost per observation, in percent of notional. 0.10% (10
    bps) is a liquid-equity figure. An options candidate must pass its
    OWN cost (spread plus theta), which is far higher. Use
    `option_backtest` for that, not this default."""
    min_years: float = 4.0
    """Calendar span of the SIGNALS, not of the bars. Set at 5.0 first, then
    lowered after the first run showed the 6-year bar fixture yields only
    ~4.8 years of signals: `_momentum` needs 252 bars of warm-up before its
    first call. At 5.0, every candidate failed for a data reason rather
    than a signal reason. Changed AFTER seeing output, which this module
    exists to prevent, so it is recorded: no verdict changed, because
    every horizon of the shipped signal fails on other clauses too."""


@dataclass(frozen=True)
class Verdict:
    passed: bool
    n: int
    mean_net_pct: float
    t_stat: float
    required_t: float
    hit_rate: float
    half_t: tuple[float, float]
    span_years: float
    failures: tuple[str, ...]
    """Named, greppable reasons. Empty exactly when `passed`."""

    def line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        why = "" if self.passed else "  [" + ", ".join(self.failures) + "]"
        return (
            f"{status}  n={self.n}  mean_net={self.mean_net_pct:+.3f}%  "
            f"t={self.t_stat:+.2f} (need {self.required_t:.2f})  "
            f"hit={self.hit_rate:.1%}  halves t={self.half_t[0]:+.2f}/{self.half_t[1]:+.2f}  "
            f"span={self.span_years:.1f}y{why}"
        )


def required_t(bar: AcceptanceBar, n_tests: int) -> float:
    """max(bar.min_t, two-sided Bonferroni critical value at 5%)."""
    alpha = 0.05 / max(1, n_tests)
    return max(bar.min_t, NormalDist().inv_cdf(1.0 - alpha / 2.0))


def t_stat(values: list[float]) -> float:
    """t of the mean against zero. 0.0 for fewer than two points or no
    variance: "no evidence", never a fabricated extreme."""
    if len(values) < 2:
        return 0.0
    sd = st.stdev(values)
    if sd == 0.0:
        return 0.0
    return st.mean(values) / (sd / math.sqrt(len(values)))


def evaluate(
    observations: list[Observation],
    bar: AcceptanceBar | None = None,
    *,
    n_tests: int = 1,
) -> Verdict:
    bar = bar or AcceptanceBar()
    need = required_t(bar, n_tests)
    obs = sorted(observations, key=lambda o: o.day)
    net = [o.ret_pct - bar.cost_pct for o in obs]
    n = len(net)

    if n == 0:
        return Verdict(
            passed=False, n=0, mean_net_pct=0.0, t_stat=0.0, required_t=need,
            hit_rate=0.0, half_t=(0.0, 0.0), span_years=0.0,
            failures=("no_observations",),
        )

    t = t_stat(net)
    mean = st.mean(net)
    hit = sum(1 for v in net if v > 0) / n
    mid = n // 2
    halves = (t_stat(net[:mid]), t_stat(net[mid:]))
    span = (obs[-1].day - obs[0].day).days / 365.25

    failures: list[str] = []
    if n < bar.min_n:
        failures.append("too_few_observations")
    if span < bar.min_years:
        failures.append("history_too_short")
    if mean <= 0:
        failures.append("negative_after_costs")
    if t < need:
        failures.append("not_significant")
    sign = 1.0 if mean > 0 else -1.0
    if any(h * sign < bar.min_half_t for h in halves):
        failures.append("unstable_across_halves")

    return Verdict(
        passed=not failures, n=n, mean_net_pct=mean, t_stat=t, required_t=need,
        hit_rate=hit, half_t=halves, span_years=span, failures=tuple(failures),
    )
