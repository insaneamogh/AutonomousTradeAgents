"""Quant-feature math tests.

Two things are being defended here:

  1. **Known-answer correctness.** Where a statistic has a closed form we
     assert against a hand-computed value, not against "whatever the code
     returns today". Financial math that is silently wrong is worse than
     absent.
  2. **Degenerate inputs never lie.** Empty / single-bar / all-flat /
     non-positive-price series must produce ``None``, never a NaN, an inf,
     a ZeroDivisionError, or a plausible-looking zero.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from itertools import pairwise

import pytest

from engine.features.quant import (
    QuantFeatures,
    compute_quant,
    relative_strength_ranks,
)
from engine.features.technicals import DailyBar


def _bars(closes: list[float], *, spread: float = 0.01) -> list[DailyBar]:
    """Synthetic bars around a close path. ``spread`` is the H/L half-width."""
    start = date(2026, 1, 5)
    out: list[DailyBar] = []
    for i, c in enumerate(closes):
        out.append(
            DailyBar(
                day=start + timedelta(days=i),
                open=c,
                high=c * (1 + spread),
                low=c * (1 - spread),
                close=c,
                volume=1_000_000.0,
            )
        )
    return out


def _geometric(n: int, start: float, daily: float) -> list[float]:
    return [start * (1.0 + daily) ** i for i in range(n)]


# ─────────────────────────────────────────────────────────────────────
# Degenerate inputs
# ─────────────────────────────────────────────────────────────────────


def test_empty_bars_return_all_none_block() -> None:
    q = compute_quant([])
    assert isinstance(q, QuantFeatures)
    assert q.lookback_days == 0
    for name, value in q.as_dict().items():
        if name == "lookback_days":
            continue
        assert value is None, f"{name} should be None on empty input"


def test_single_bar_returns_all_none_block() -> None:
    q = compute_quant(_bars([100.0]))
    assert q.realized_vol_pct is None
    assert q.sharpe is None
    assert q.max_drawdown_pct is None


def test_flat_prices_have_zero_vol_and_no_ratios() -> None:
    """A perfectly flat series has zero volatility, so Sharpe/Sortino are
    undefined (0/0) — the code must say None, not 0.0 and not inf."""
    q = compute_quant(_bars([100.0] * 120, spread=0.0))
    assert q.realized_vol_pct == 0.0
    assert q.sharpe is None
    assert q.sortino is None
    assert q.price_zscore_20 is None  # zero stdev
    assert q.donchian_pct is None  # zero-width channel
    assert q.max_drawdown_pct == 0.0
    assert q.return_skew is None
    assert q.return_kurtosis is None


def test_non_positive_prices_do_not_produce_nan() -> None:
    """Bad data (a zero print) must be skipped, not log()'d into a NaN."""
    closes = [100.0, 101.0, 0.0, 102.0, 103.0] + [103.0 + i for i in range(60)]
    q = compute_quant(_bars(closes))
    for name, value in q.as_dict().items():
        if isinstance(value, float):
            assert math.isfinite(value), f"{name} is not finite"


def test_every_reported_number_is_finite_on_normal_data() -> None:
    q = compute_quant(_bars(_geometric(300, 100.0, 0.001)))
    for name, value in q.as_dict().items():
        if isinstance(value, float):
            assert math.isfinite(value), f"{name} is not finite"


# ─────────────────────────────────────────────────────────────────────
# Known answers
# ─────────────────────────────────────────────────────────────────────


def test_realized_vol_known_answer() -> None:
    """Alternating ±1% log-ish moves: stdev of the log-return series times
    sqrt(252). Computed independently here from the same definition."""
    closes = [100.0]
    for i in range(120):
        closes.append(closes[-1] * (1.01 if i % 2 == 0 else 1 / 1.01))
    q = compute_quant(_bars(closes))

    rets = [math.log(b / a) for a, b in pairwise(closes)][-63:]
    mu = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
    assert q.realized_vol_pct == pytest.approx(sd * math.sqrt(252) * 100.0, abs=1e-3)


def test_max_drawdown_known_answer() -> None:
    """Up to 120, down to 90 → −25% from the peak, then recovery."""
    closes = [100.0, 110.0, 120.0, 105.0, 90.0, 95.0, 130.0]
    q = compute_quant(_bars(closes), lookback=len(closes))
    assert q.max_drawdown_pct == pytest.approx(-25.0, abs=1e-6)


def test_monotonically_rising_series_has_no_drawdown_and_positive_sharpe() -> None:
    q = compute_quant(_bars(_geometric(120, 50.0, 0.002)))
    assert q.max_drawdown_pct == pytest.approx(0.0)
    assert q.sharpe is not None and q.sharpe > 0
    assert q.sortino is None  # not one losing day → downside deviation is 0


def test_beta_and_correlation_against_self_are_one() -> None:
    bars = _bars(_geometric(200, 100.0, 0.0015))
    # Perturb so the series isn't a straight line (zero-variance benchmark).
    noisy = [b.close * (1.0 + (0.01 if i % 3 == 0 else -0.004)) for i, b in enumerate(bars)]
    bars = _bars(noisy)
    q = compute_quant(bars, benchmark_bars=bars)
    assert q.beta_benchmark == pytest.approx(1.0, abs=1e-6)
    assert q.corr_benchmark == pytest.approx(1.0, abs=1e-6)
    assert q.excess_return_pct == pytest.approx(0.0, abs=1e-6)


def test_beta_of_a_doubled_series_is_two() -> None:
    """A name whose daily log return is exactly 2x the market has beta 2."""
    bench_closes = [100.0]
    sym_closes = [100.0]
    steps = [0.004, -0.002, 0.006, -0.005, 0.001] * 30
    for s in steps:
        bench_closes.append(bench_closes[-1] * math.exp(s))
        sym_closes.append(sym_closes[-1] * math.exp(2 * s))
    q = compute_quant(_bars(sym_closes), benchmark_bars=_bars(bench_closes))
    assert q.beta_benchmark == pytest.approx(2.0, abs=1e-3)
    assert q.corr_benchmark == pytest.approx(1.0, abs=1e-3)


def test_beta_is_none_when_benchmark_is_flat() -> None:
    """Zero market variance → beta is a division by zero, so it must be None."""
    sym = _bars(_geometric(120, 100.0, 0.001))
    flat = _bars([100.0] * 120, spread=0.0)
    q = compute_quant(sym, benchmark_bars=flat)
    assert q.beta_benchmark is None
    assert q.corr_benchmark is None


def test_benchmark_alignment_uses_dates_not_positions() -> None:
    """A benchmark missing interior days must still align correctly."""
    sym = _bars(_geometric(120, 100.0, 0.001))
    bench_full = _bars(_geometric(120, 400.0, 0.001))
    # Drop every 7th benchmark bar — a halted/holiday-ish gap.
    bench_sparse = [b for i, b in enumerate(bench_full) if i % 7 != 0]
    q = compute_quant(sym, benchmark_bars=bench_sparse)
    assert q.beta_benchmark is not None
    assert q.corr_benchmark == pytest.approx(1.0, abs=1e-6)


def test_price_zscore_is_standardized() -> None:
    """z must equal (close − mean) / stdev over the same 20 closes."""
    closes = [98.0, 102.0] * 10
    q = compute_quant(_bars(closes), lookback=20)
    ref = closes[-20:]
    mu = sum(ref) / 20
    sd = math.sqrt(sum((c - mu) ** 2 for c in ref) / 19)
    assert q.price_zscore_20 == pytest.approx((ref[-1] - mu) / sd, abs=1e-3)


def test_donchian_pct_endpoints() -> None:
    """Close at the channel high → 100; at the low → 0."""
    rising = _bars([100.0 + i for i in range(40)], spread=0.0)
    q_hi = compute_quant(rising)
    assert q_hi.donchian_pct == pytest.approx(100.0)

    falling = _bars([140.0 - i for i in range(40)], spread=0.0)
    q_lo = compute_quant(falling)
    assert q_lo.donchian_pct == pytest.approx(0.0)


def test_skew_sign_matches_the_tail() -> None:
    """One big down day in an otherwise calm series → negative skew."""
    closes = [100.0]
    for i in range(80):
        closes.append(closes[-1] * (0.80 if i == 40 else 1.001))
    q = compute_quant(_bars(closes))
    assert q.return_skew is not None and q.return_skew < -1.0
    assert q.return_kurtosis is not None and q.return_kurtosis > 3.0


def test_parkinson_and_garman_klass_are_positive_and_ordered_sanely() -> None:
    """Both range estimators should land in the same ballpark as
    close-to-close when the bars are symmetric around the close."""
    q = compute_quant(_bars(_geometric(150, 100.0, 0.0), spread=0.01))
    # Zero close-to-close drift and dispersion, but a real intraday range:
    # close-to-close says 0 vol, the range estimators say otherwise. That
    # divergence is the point of carrying all three.
    assert q.realized_vol_pct == pytest.approx(0.0)
    assert q.parkinson_vol_pct is not None and q.parkinson_vol_pct > 0
    assert q.garman_klass_vol_pct is not None and q.garman_klass_vol_pct > 0


def test_garman_klass_survives_a_pinned_range_bar() -> None:
    """A bar whose close sits outside a degenerate H==L range can drive the
    GK variance negative; it must clamp at 0, not NaN out of sqrt()."""
    bars = _bars([100.0] * 60, spread=0.0)
    bad = bars[-1]
    bars[-1] = DailyBar(
        day=bad.day, open=100.0, high=100.0, low=100.0, close=140.0, volume=1.0
    )
    q = compute_quant(bars)
    assert q.garman_klass_vol_pct is not None
    assert math.isfinite(q.garman_klass_vol_pct)


def test_atr_pct_matches_technicals_atr_over_close() -> None:
    """The quant block's ATR% must be exactly the technicals block's ATR-14
    divided by the last close — two disagreeing ATRs would be a real bug."""
    from engine.features.technicals import compute_technicals

    bars = _bars([100.0 + math.sin(i / 5) * 8 for i in range(160)], spread=0.012)
    q = compute_quant(bars)
    t = compute_technicals(bars)
    assert q.atr_pct is not None
    expected = t["atr_14"] / bars[-1].close * 100.0
    assert q.atr_pct == pytest.approx(expected, rel=1e-3)


def test_atr_zscore_needs_history_and_is_none_when_thin() -> None:
    thin = compute_quant(_bars(_geometric(40, 100.0, 0.001)))
    assert thin.atr_zscore is None
    thick = compute_quant(_bars([100.0 + math.sin(i / 7) * 5 for i in range(300)]))
    assert thick.atr_zscore is not None


def test_trailing_returns_need_the_full_window() -> None:
    q = compute_quant(_bars(_geometric(100, 100.0, 0.001)))
    assert q.ret_21d_pct is not None
    assert q.ret_63d_pct is not None
    assert q.ret_252d_pct is None  # only 100 bars


def test_lookback_shorter_than_history_is_honoured() -> None:
    bars = _bars(_geometric(300, 100.0, 0.001))
    q = compute_quant(bars, lookback=20)
    assert q.lookback_days == 20


# ─────────────────────────────────────────────────────────────────────
# Cross-sectional relative strength
# ─────────────────────────────────────────────────────────────────────


def test_relative_strength_ranks_span_zero_to_hundred() -> None:
    ranks = relative_strength_ranks({"A": -5.0, "B": 0.0, "C": 3.0, "D": 12.0})
    assert ranks["A"] == 0.0
    assert ranks["D"] == 100.0
    assert ranks["B"] < ranks["C"] < ranks["D"]


def test_relative_strength_ties_share_the_average_rank() -> None:
    ranks = relative_strength_ranks({"A": 1.0, "B": 1.0, "C": 1.0})
    assert set(ranks.values()) == {50.0}


def test_relative_strength_drops_unrankable_symbols() -> None:
    """A None return must be omitted, never ranked as the worst name."""
    ranks = relative_strength_ranks({"A": 1.0, "B": None, "C": float("nan"), "D": 5.0})
    assert set(ranks) == {"A", "D"}
    assert ranks["A"] == 0.0
    assert ranks["D"] == 100.0


def test_relative_strength_empty_and_single() -> None:
    assert relative_strength_ranks({}) == {}
    assert relative_strength_ranks({"A": None}) == {}
    assert relative_strength_ranks({"A": 2.0}) == {"A": 50.0}
