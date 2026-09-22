"""The result that supersedes every parameter question asked so far.

13,403 tradable signals over 58 symbols and 6 years (2020-07 to 2026-09),
driven through the production `best_strategy` on the production
`compute_technicals` / `compute_quant`:

    horizon      n     hit     mean       z
       2d    13403    50.1%   -0.02%   +0.29    <- what we actually held
       5d     7500    49.8%   -0.04%   -0.37
      10d     5203    49.8%   -0.06%   -0.35
      20d     2882    50.9%   +0.19%   +0.93
      60d     1083    49.3%   +0.56%   -0.46

n counts INDEPENDENT observations — non-overlapping forward windows. That
correction matters: sampling every 5 days while measuring a 60-day forward
return means twelve consecutive observations share nearly the same window.
Before fixing it, the 2-day cell read z=+0.29 at one stride and z=-2.81 at
another. Neither was real.

**Not one cell is distinguishable from a coin flip**, and by strategy the
best is `momentum` at its own 60-day horizon: 51.5%, z=+0.83, n=771.

This retroactively explains every negative result in this investigation:
no exit ladder helped, no leverage cap separated winners from losers, and
the Refusal Ledger's own finding that 49 of 100 refusals would have won.
They were all measuring a signal with no edge.

It also CORRECTS a claim I made in the previous commit: that the
signal/holding horizon mismatch explained the live 35% directional
accuracy. It does not. The signal is ~50% at every horizon INCLUDING the
2 days we actually held, so the live 35% was small-sample noise (n=17,
95% Wilson interval 17-59%), not a horizon effect.
"""

from __future__ import annotations

import pytest
from tests.eval.signal_backtest import (
    HORIZONS,
    non_overlapping,
    run,
    z_score,
)


@pytest.fixture(scope="module")
def signals():
    return run(step=5)


def _z(sigs, h: int) -> float:
    """Always through `non_overlapping` — see that function for why a
    stride change once made the 2-day cell read z=-2.81 when it is a coin
    flip."""
    return z_score([s.fwd[h] for s in non_overlapping(sigs, h)])


def test_the_backtest_produces_a_usable_sample(signals) -> None:
    """Guards the fixture: a truncated bars file would quietly shrink the
    sample and make every assertion below meaningless."""
    assert len(signals) > 1_000, f"only {len(signals)} signals"
    assert {s.strategy for s in signals} >= {"momentum", "sma_crossover"}


def test_no_look_ahead_in_the_forward_returns(signals) -> None:
    """Every horizon must be a DIFFERENT number. If features could see the
    future, or the entry were taken at the wrong bar, the horizons would
    collapse toward each other."""
    for s in signals[:200]:
        assert len({round(s.fwd[h], 6) for h in HORIZONS}) > 1


def test_the_signal_has_no_demonstrable_edge_at_any_horizon(signals) -> None:
    """THE result. If this ever fails, something real has changed — read
    the full report before deleting the test.

    Stated as "no demonstrable edge", not "negative edge": these are ~50%,
    which is the absence of evidence for an edge, not evidence of an
    anti-edge.
    """
    for h in HORIZONS:
        z = _z(signals, h)
        assert abs(z) < 1.96, (
            f"{h}d horizon reached z={z:+.2f} — a real edge may have appeared, "
            "or the feature pipeline changed. Re-run "
            "`python -m tests.eval.signal_backtest --by-strategy` and read it "
            "before trusting this."
        )


def test_the_two_day_hold_is_not_specially_bad(signals) -> None:
    """The previous commit claimed the ~100x signal/holding horizon
    mismatch explained the live 35% accuracy. It does not: 2 days is
    statistically the same as every other horizon. Keeping this so the
    corrected claim cannot quietly revert."""
    two = _z(signals, 2)
    longest = _z(signals, 60)
    assert abs(two - longest) < 2.0, (
        "2d and 60d now differ materially — the horizon hypothesis would be "
        "back on the table and worth re-testing properly"
    )


def test_the_shipped_signal_fails_the_acceptance_bar_at_every_horizon(signals) -> None:
    """The "no edge" finding, re-stated through the bar every future
    candidate must clear. If this ever passes, something changed in the
    signal or the bar, and either change needs a build-log entry."""
    from tests.eval.acceptance import Observation, evaluate

    for h in HORIZONS:
        obs = [Observation(day=s.day, ret_pct=s.fwd[h]) for s in non_overlapping(signals, h)]
        verdict = evaluate(obs, n_tests=len(HORIZONS))
        assert not verdict.passed, f"{h}d unexpectedly passed: {verdict.line()}"
