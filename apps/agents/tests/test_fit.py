"""Tests for ``trading_agents.strategies.fit`` — the deterministic
precondition scorers.

There was no ``test_fit.py`` anywhere in the repo before this file, despite
``fit.py``'s ``blind_weight_fraction`` docstring claiming (at the time of
writing, line 93): "The test suite asserts it stays below the floor for all
five strategies." It did not — see docs/PLAN_AGGRESSIVE_PROFILE.md §0. That
is a CLAUDE.md §4.2 doc-lies-about-code case, and pinning the real invariant
here is the only thing that would catch a future strategy (e.g. a
candlestick-pattern workstream) landing a direction-blind component heavy
enough to clear ``MIN_FIT_TO_TRADE`` on its own.

``test_blind_weight_stays_below_the_trade_floor`` was written and verified
passing against the ORIGINAL ``MIN_FIT_TO_TRADE = 0.45`` before that
constant was touched, per the plan's own instruction (§0, "write this
FIRST"). It imports the live constant rather than hardcoding 0.45, so it
keeps meaning the same thing after the floor moves to 0.42.
"""

from __future__ import annotations

import pytest

from trading_agents.strategies import STRATEGY_REGISTRY, rank_strategies
from trading_agents.strategies.fit import MIN_FIT_TO_TRADE, best_strategy, blind_weight_fraction

# ─────────────────────────────────────────────────────────────────────
# blind_weight_fraction — the invariant its own docstring claims is tested
# ─────────────────────────────────────────────────────────────────────


def test_blind_weight_stays_below_the_trade_floor() -> None:
    """Pins the exact invariant ``blind_weight_fraction``'s docstring has
    claimed all along with no test behind it.

    If any strategy's direction-blind weight share ever reaches
    ``MIN_FIT_TO_TRADE``, a perfect score on ONLY its direction-blind
    checks clears the trade floor with every directional check at zero —
    i.e. it could fire a confident SHORT on a name in a clean uptrend (or
    the reverse) on evidence that cannot tell long from short at all.
    Measured 2026-08-30: ``vol_regime_switch`` is the tightest case, at
    exactly 0.400 — see docs/PLAN_AGGRESSIVE_PROFILE.md §0's resulting hard
    floor of 0.41 on ``MIN_FIT_TO_TRADE``.
    """
    for sid in STRATEGY_REGISTRY:
        frac = blind_weight_fraction(sid)
        assert frac < MIN_FIT_TO_TRADE, (
            f"{sid}: direction-blind weight share {frac:.3f} reaches "
            f"MIN_FIT_TO_TRADE={MIN_FIT_TO_TRADE} — it could clear the "
            "trade gate on checks that cannot tell long from short"
        )


def test_blind_weight_fraction_still_works_on_an_empty_dict() -> None:
    """The empty-features evidence gate belongs in ``best_strategy`` ONLY.

    ``blind_weight_fraction`` deliberately calls the raw per-strategy
    scorer directly on ``_Features({})`` and must keep doing so completely
    unchanged — NEUTRAL component scores, a real fraction, never a
    refusal and never an exception. ``rank_strategies`` is the shared path
    underneath it and must show the same real (not gated) number the plan
    measured: an empty dict scores ``rsi_mean_reversion`` at 0.60. The
    ranking must keep reporting that score even after ``best_strategy``
    (tested below) starts refusing to call it a winner — if a future
    change "helpfully" moved the gate down into ``score_strategy`` or
    ``rank_strategies``, this is what would catch it.
    """
    for sid in STRATEGY_REGISTRY:
        frac = blind_weight_fraction(sid)
        assert 0.0 <= frac <= 1.0

    ranked = rank_strategies({})
    assert ranked, "an empty dict must still produce a full, real ranking"
    assert ranked[0].strategy_id == "rsi_mean_reversion"
    assert ranked[0].score == pytest.approx(0.60, abs=0.01)


# ─────────────────────────────────────────────────────────────────────
# The empty-features evidence gate — best_strategy ONLY
# ─────────────────────────────────────────────────────────────────────


def test_empty_features_are_not_tradable() -> None:
    """``best_strategy({})`` must never hand out a winner.

    Measured 2026-08-30 (docs/PLAN_AGGRESSIVE_PROFILE.md §0): without this
    guard, an empty feature dict scores ``rsi_mean_reversion`` at 0.60 —
    ABOVE ``MIN_FIT_TO_TRADE`` — because ``not_a_trend_break`` reads
    ``trend_regime != "downtrend"`` and the missing-value sentinel
    "unknown" satisfies that as a genuine TRUE. Raising
    ``MIN_FIT_TO_TRADE`` does not close this leak; only an explicit
    evidence gate does. A data outage must HOLD, not spend five LLM calls
    and originate a trade. This must fail loudest of every test here — it
    currently (pre-fix) passes trivially with no code at all.
    """
    winner, ranked = best_strategy({})
    assert winner is None
    assert ranked, "the ranking itself is unaffected — only the winner is refused"
    assert ranked[0].strategy_id == "rsi_mean_reversion"
    assert ranked[0].tradable, (
        "the raw score must still clear MIN_FIT_TO_TRADE on its own — "
        "that is the whole bug. best_strategy must refuse it anyway."
    )


def test_near_empty_features_missing_trend_regime_are_not_tradable() -> None:
    """Technicals present but no ``trend_regime`` key at all still defaults
    to "unknown" via ``_Features.trend_regime`` — same failure mode, a
    differently-shaped input."""
    winner, _ranked = best_strategy({"technicals": {"rsi_14": 50.0}})
    assert winner is None


def test_thin_quant_block_is_not_tradable() -> None:
    """A real trend regime and non-empty technicals, but fewer than 3 of
    the quant keys the scorers actually read, is still not enough
    evidence to call anything tradable."""
    winner, _ranked = best_strategy(
        {
            "technicals": {"trend_regime": "uptrend"},
            "quant": {"sharpe": 1.0, "ret_21d_pct": 5.0},
        }
    )
    assert winner is None


def test_usable_features_with_real_data_are_unaffected() -> None:
    """The gate must not touch a genuinely data-rich pass. Full technicals
    + quant blocks (mirrors the clears-the-fit-floor fixture
    ``test_options_drafter.py`` already uses) must still win exactly as
    before."""
    features = {
        "technicals": {
            "trend_regime": "uptrend",
            "dma20_pct": 2.0,
            "dma50_pct": 4.0,
            "rsi_14": 55.0,
            "atr_14": 2.0,
            "volume_ratio_20d": 1.6,
        },
        "quant": {
            "ret_252d_pct": 20.0,
            "ret_63d_pct": 10.0,
            "ret_21d_pct": 6.0,
            "sharpe": 1.0,
            "atr_zscore": 0.5,
            "realized_vol_pct": 25.0,
            "corr_benchmark": 0.5,
            "price_zscore_20": 0.5,
            "donchian_pct": 90.0,
        },
    }
    winner, _ranked = best_strategy(features)
    assert winner is not None


def test_usable_features_with_no_fit_still_reports_no_strategy_clears() -> None:
    """A data-RICH pass that genuinely fits nothing (every precondition
    unsatisfied — the ``test_council_mock.py``/``test_options_drafter.py``
    ``_featureless``/``_no_fit_features`` fixture) must NOT be caught by
    the evidence gate — it has real technicals and 9 of 9 quant keys. This
    is the "genuinely marginal" case the gate must leave alone."""
    features = {
        "technicals": {
            "trend_regime": "choppy",
            "dma20_pct": -0.05,
            "dma50_pct": -0.05,
            "rsi_14": 50.0,
            "atr_14": 2.0,
            "volume_ratio_20d": 0.5,
        },
        "quant": {
            "ret_252d_pct": -0.2,
            "ret_63d_pct": -0.3,
            "ret_21d_pct": -0.4,
            "sharpe": -0.4,
            "atr_zscore": 3.0,
            "realized_vol_pct": 85.0,
            "corr_benchmark": 0.99,
            "price_zscore_20": 0.0,
            "donchian_pct": 50.0,
        },
    }
    winner, ranked = best_strategy(features)
    assert winner is None
    assert ranked
    assert not ranked[0].tradable, "this fixture is a genuine sub-floor score, not a gate refusal"
