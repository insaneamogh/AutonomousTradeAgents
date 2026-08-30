"""Tests for the candlestick-pattern integration into ``trading_agents.strategies.fit``.

Per ``docs/PLAN_CANDLE_PATTERNS.md`` §6. ``test_blind_weight_stays_below_the_trade_floor``
is deliberately NOT duplicated here — it already exists in ``test_fit.py``
(written by the Aggressive Profile work) and was re-run, unmodified, after
the two ``candle_*`` components below were added: it still passes, with
``vol_regime_switch`` unchanged at exactly 0.400 and both touched strategies
(``rsi_mean_reversion`` 0.150 -> 0.130, ``breakout`` 0.350 -> 0.318) still
comfortably below ``MIN_FIT_TO_TRADE`` (0.42). That is the single test that
would catch a collision between this work and the Aggressive Profile's floor
change — see that file for the up-to-date numbers, verified against the live
code, not re-derived here.
"""

from __future__ import annotations

from trading_agents.runtime import _SNAPSHOT_BLOCKS
from trading_agents.strategies.fit import (
    _SCORERS,
    _breakout,
    _Features,
    _rsi_mean_reversion,
    _weighted,
)

# A data-rich, clearly-tradable feature dict (mirrors the fixture
# ``test_fit.py::test_usable_features_with_real_data_are_unaffected`` uses),
# deliberately WITHOUT a "patterns" key — the MOCK-provider / thin-history /
# "this symbol's pass predates the feature" shape.
_RICH_FEATURES_NO_PATTERNS = {
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


def test_every_pattern_component_is_directional() -> None:
    """The plan's §0 constraint 4, enforced directly: every ``candle_*``
    component on every strategy, in both directions, must be
    ``directional=True``. Marking one ``False`` because "a candle doesn't
    know direction" would be wrong — ``reversal_bull``/``reversal_bear``
    (and ``continuation_bull``/``continuation_bear``) are separate fields
    specifically so a candle-derived check CAN tell long from short."""
    seen_candle_components = 0
    for strategy_id, scorer in _SCORERS.items():
        for direction in ("long", "short"):
            for component in scorer(_Features({}), direction):
                if component.name.startswith("candle_"):
                    seen_candle_components += 1
                    assert component.directional is True, (
                        f"{strategy_id}.{component.name} (direction={direction}) "
                        "must be directional=True"
                    )
    # Two strategies x two directions x one candle_* component each.
    assert seen_candle_components == 4


def test_absent_patterns_block_barely_moves_the_fit() -> None:
    """Same feature dict, scored with and without the new ``candle_*``
    component (patterns absent -> it degrades to NEUTRAL 0.5, exactly as
    every other component here degrades on a missing input) — the
    renormalised mean must not move by more than 0.03, on both the
    degenerate empty dict AND a realistic, data-rich fixture.

    Verified against the live code (not derived from the formula alone,
    per CLAUDE.md §4.3): the empty-dict case is the same 0.60 -> 0.587
    ``test_fit.py::test_blind_weight_fraction_still_works_on_an_empty_dict``
    pins; the rich-fixture case is a DIFFERENT, previously-unverified
    number this test adds coverage for.

    This is deliberately the "component present but scored neutral because
    the data is absent" comparison, not "component present with a real,
    typically-near-zero reading" — see ``test_a_typical_pattern_reading_can_
    move_the_fit_by_more_than_the_absent_case_does`` below, which measures
    that DIFFERENT (and larger) effect honestly rather than assuming the
    0.03 bound generalises to it.
    """
    for features in ({}, _RICH_FEATURES_NO_PATTERNS):
        f = _Features(features)

        rsi_components = _rsi_mean_reversion(f, "long")
        assert rsi_components[-1].name == "candle_reversal_confirms"
        fit_without_candle = _weighted(rsi_components[:-1])
        fit_with_candle_absent = _weighted(rsi_components)
        assert abs(fit_with_candle_absent - fit_without_candle) <= 0.03, (
            f"rsi_mean_reversion moved {fit_with_candle_absent - fit_without_candle:.4f} "
            f"on {features!r}"
        )

        breakout_components = _breakout(f, "long")
        assert breakout_components[-1].name == "candle_confirms_break"
        fit_without_candle_b = _weighted(breakout_components[:-1])
        fit_with_candle_absent_b = _weighted(breakout_components)
        assert abs(fit_with_candle_absent_b - fit_without_candle_b) <= 0.03, (
            f"breakout moved {fit_with_candle_absent_b - fit_without_candle_b:.4f} "
            f"on {features!r}"
        )


def test_a_typical_pattern_reading_can_move_the_fit_by_more_than_the_absent_case_does() -> None:
    """Honest, measured finding beyond the plan's own estimate: a REAL
    "no notable candlestick pattern today" reading (``reversal_bull`` near
    0, not the NEUTRAL 0.5 an absent block defaults to) shifts
    ``rsi_mean_reversion``'s fit by MORE than 0.03 — about -0.063 on the
    rich fixture above, measured directly, not reasoned about. This is not
    a bug: it is the same renormalisation the plan described, just larger
    than its own worked example implied, because 0.0 is farther from
    NEUTRAL than the "absent" default is. Pinned here so a future reader
    who re-measures this does not mistake a real, intentional effect for a
    regression.
    """
    features_with_a_quiet_patterns_block = dict(_RICH_FEATURES_NO_PATTERNS)
    features_with_a_quiet_patterns_block["patterns"] = {
        "reversal_bull": 0.0,
        "reversal_bear": 0.0,
        "continuation_bull": 0.0,
        "continuation_bear": 0.0,
        "indecision": 0.0,
        "compression": 0.0,
        "expansion": 0.0,
        "names": (),
        "top_pattern": None,
        "top_pattern_score": 0.0,
    }
    fit_absent = _weighted(_rsi_mean_reversion(_Features(_RICH_FEATURES_NO_PATTERNS), "long"))
    fit_quiet = _weighted(_rsi_mean_reversion(_Features(features_with_a_quiet_patterns_block), "long"))
    delta = fit_quiet - fit_absent
    assert delta < -0.03, f"expected the quiet-but-present reading to move the fit by more than the absent case does; got {delta:.4f}"
    assert delta > -0.10, f"a single component at weight 0.15 should not move the mean by this much; got {delta:.4f}"


def test_patterns_reach_the_audit_row() -> None:
    """``"patterns"`` must be in ``_SNAPSHOT_BLOCKS`` — the plan's own named
    easy-to-forget step. Without this, the block computes, feeds the fit,
    and changes real decisions, but never appears in
    ``reasoning.feature_snapshot`` where anyone could check the machine's
    homework on it."""
    assert "patterns" in _SNAPSHOT_BLOCKS
