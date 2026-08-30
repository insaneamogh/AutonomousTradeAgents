"""Tests for ``engine.features.patterns`` — candlestick pattern recognition.

Per ``docs/PLAN_CANDLE_PATTERNS.md`` §6: one positive AND one negative test
per pattern, plus the plan's own named revert-check matrix. A bar that
nearly qualifies but fails one condition must score 0 and must not be
named — that discrimination is where the value is, not "was something
detected" (a doji can pass a boolean "small body" check just as easily as a
hammer passes one; only the ramped, ATR-normalised score tells them apart).

**Per-pattern tests call the module's PRIVATE per-pattern scorers**
(``_hammer``, ``_bullish_engulfing``, …) directly rather than only going
through ``detect_patterns``. This is deliberate, not a shortcut:
``PatternBlock`` exposes only the seven AGGREGATED family fields (``max``
across several patterns each), and some families genuinely overlap on the
same two bars — a deep piercing line also partially resembles an engulfing,
and a clean engulfing also partially resembles a piercing line (see the
fade assertion inside ``test_piercing_line_...`` below, and
``test_three_weak_patterns_do_not_outscore_one_clean_one``). Going through
the aggregate alone cannot isolate "did THIS pattern fire cleanly" from
"did a sibling in the same family contribute instead". The tests that
exercise the plan's own named revert-check matrix, plus the guard /
aggregation / naming-wiring tests, DO go through the public
``detect_patterns``/``PatternBlock`` surface, since that is specifically
what they are pinning.

All fixtures use ``atr=2.0`` so ATR-unit ratios are round numbers. Every
number below was checked against the real implementation with a standalone
script before being written down here, not reasoned about from the
formulas alone (CLAUDE.md §4.3) — and each of the five named revert-check
tests was separately confirmed to FAIL against a deliberately broken
implementation (the ATR ramp short-circuited to 1.0, the trend-context gate
short-circuited to 1.0, ``max`` swapped for ``sum``, and both guards
disabled) before being restored (CLAUDE.md §4.1).
"""

from __future__ import annotations

from datetime import date, timedelta

from engine.features.patterns import (
    MIN_BARS_FOR_PATTERNS,
    PatternBlock,
    _bearish_engulfing,
    _bearish_harami,
    _bullish_engulfing,
    _bullish_harami,
    _dark_cloud_cover,
    _doji,
    _evening_star,
    _hammer,
    _inside_bar,
    _marubozu_bear,
    _marubozu_bull,
    _morning_star,
    _nr7,
    _outside_bar,
    _piercing_line,
    _shooting_star,
    _three_black_crows,
    _three_white_soldiers,
    detect_patterns,
)
from engine.features.technicals import DailyBar

ATR = 2.0
_DAY0 = date(2026, 1, 5)


def _bar(o: float, h: float, lo: float, c: float, day: date) -> DailyBar:
    """One daily bar for a fixture. Volume is fixed — no pattern here reads it."""
    return DailyBar(day=day, open=o, high=h, low=lo, close=c, volume=1_000_000.0)


def _d(n: int) -> date:
    return _DAY0 + timedelta(days=n)


def _filler(n: int, *, price: float = 100.0) -> list[DailyBar]:
    """``n`` neutral, flat-bodied bars (open == close) with a moderate,
    fixed range. Flat on purpose: every color-gated two/three-bar pattern
    checks ``_is_bearish``/``_is_bullish`` on its "prior" bar, and a flat
    bar satisfies neither — so filler can never masquerade as a real prior
    bar for engulfing/harami/piercing/star patterns in the guard and
    revert-check-matrix tests below, which go through the full
    ``detect_patterns`` aggregate rather than an isolated scorer."""
    return [_bar(price, price + 0.5, price - 0.5, price, _d(i)) for i in range(n)]


# ─────────────────────────────────────────────────────────────────────
# Guards — never raise, always the all-zero block
# ─────────────────────────────────────────────────────────────────────


def test_empty_bars_return_the_empty_block() -> None:
    pb = detect_patterns([], atr=ATR, trend_regime="downtrend")
    assert pb == PatternBlock(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, (), None, 0.0)


def test_fewer_than_seven_bars_does_not_raise() -> None:
    """Revert-check: remove the length guard and this raises IndexError
    (single-bar patterns index ``bars[-1]``) before even reaching ``nr7``'s
    ``bars[-7:]``. Confirmed live against the unmodified guard removed."""
    bars = _filler(MIN_BARS_FOR_PATTERNS - 1)
    pb = detect_patterns(bars, atr=ATR, trend_regime="downtrend")
    assert pb.names == ()
    assert pb.reversal_bull == 0.0


def test_zero_atr_returns_an_empty_block_without_raising() -> None:
    """Revert-check: remove the ``atr <= 0`` guard and every ``/ atr``
    division below this line raises ``ZeroDivisionError`` — confirmed live."""
    bars = _filler(MIN_BARS_FOR_PATTERNS)
    pb = detect_patterns(bars, atr=0.0, trend_regime="downtrend")
    assert pb == PatternBlock(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, (), None, 0.0)


def test_negative_atr_returns_an_empty_block_without_raising() -> None:
    bars = _filler(MIN_BARS_FOR_PATTERNS)
    pb = detect_patterns(bars, atr=-1.0, trend_regime="downtrend")
    assert pb.names == ()


# ─────────────────────────────────────────────────────────────────────
# The plan's named revert-check matrix (detect_patterns / PatternBlock level)
# ─────────────────────────────────────────────────────────────────────


def test_a_micro_range_hammer_scores_zero() -> None:
    """Perfect hammer geometry (lower wick 3x the body, negligible upper
    wick), but the whole bar's range is a fraction of one ATR.

    Revert-check: with ``_magnitude`` short-circuited to always return 1.0,
    this fixture scores ``reversal_bull == 1.0`` and names ``"hammer"`` —
    confirmed live, then the ramp was restored. If this test passes with no
    code at all, the ATR normalisation is decorative, not load-bearing.
    """
    bars = [*_filler(6), _bar(100.0, 100.01, 99.81, 100.004, _d(6))]
    pb = detect_patterns(bars, atr=ATR, trend_regime="downtrend")
    assert pb.reversal_bull == 0.0
    assert "hammer" not in pb.names


def test_hammer_in_an_uptrend_is_heavily_discounted() -> None:
    """Identical bars to a clean hammer fixture; only ``trend_regime``
    changes. A hammer belongs to a downtrend — scoring it in an uptrend
    must be heavily discounted (0.15x context vs 1.0x), not ignored.

    Revert-check: with ``_reversal_context`` short-circuited to always
    return 1.0, uptrend and downtrend score identically (1.0 == 1.0) —
    confirmed live, then the gate was restored.
    """
    tail = [_bar(100.00, 100.15, 97.15, 100.06, _d(6))]
    bars = _filler(6) + tail
    pb_downtrend = detect_patterns(bars, atr=ATR, trend_regime="downtrend")
    pb_uptrend = detect_patterns(bars, atr=ATR, trend_regime="uptrend")
    assert pb_downtrend.reversal_bull >= 0.9
    assert pb_uptrend.reversal_bull < pb_downtrend.reversal_bull * 0.3


def test_three_weak_patterns_do_not_outscore_one_clean_one() -> None:
    """One 2-bar tail where hammer, bullish engulfing, and piercing line
    ALL fire — weakly and to different degrees — simultaneously (a small
    up-body with a long lower wick that also happens to engulf, and also
    happens to pierce, a small bearish prior). ``reversal_bull`` must equal
    the single strongest of the three, not their sum.

    Revert-check: with the aggregate changed from ``max(...)`` to
    ``sum([...])``, ``reversal_bull`` reports ~0.58 (the sum) instead of
    ~0.29 (the max) — confirmed live, then ``max`` was restored.
    """
    prior = _bar(100.5, 100.5, 100.5, 100.1, _d(20))
    cur = _bar(100.0, 100.65, 98.5, 100.6, _d(21))
    bars = [*_filler(5), prior, cur]

    hammer_score = _hammer(bars, ATR, "downtrend")
    engulf_score = _bullish_engulfing(bars, ATR, "downtrend")
    pierce_score = _piercing_line(bars, ATR, "downtrend")
    # All three genuinely fire, and to different degrees — otherwise this
    # isn't testing max-vs-sum discrimination at all.
    assert hammer_score > 0.0
    assert engulf_score > 0.0
    assert pierce_score > 0.0
    assert len({round(hammer_score, 6), round(engulf_score, 6), round(pierce_score, 6)}) == 3

    pb = detect_patterns(bars, atr=ATR, trend_regime="downtrend")
    expected_max = max(hammer_score, engulf_score, pierce_score)
    expected_sum = hammer_score + engulf_score + pierce_score
    assert pb.reversal_bull == expected_max
    assert pb.reversal_bull < expected_sum - 0.01


# ─────────────────────────────────────────────────────────────────────
# Single-bar patterns
# ─────────────────────────────────────────────────────────────────────


def test_hammer_detects_a_clean_reversal_setup() -> None:
    bars = [_bar(100.00, 100.15, 97.15, 100.06, _d(0))]
    assert _hammer(bars, ATR, "downtrend") >= 0.9


def test_hammer_with_a_short_lower_wick_scores_zero() -> None:
    """Same big range as the positive fixture, but the lower wick is only
    1x the body (needs >= 2x) — fails the ONE geometry condition, not the
    magnitude one."""
    bars = [_bar(100.0, 102.0, 99.0, 101.0, _d(0))]
    assert _hammer(bars, ATR, "downtrend") == 0.0


def test_shooting_star_detects_a_clean_reversal_setup() -> None:
    bars = [_bar(100.00, 102.91, 99.91, 100.06, _d(0))]
    assert _shooting_star(bars, ATR, "uptrend") >= 0.9


def test_shooting_star_with_a_short_upper_wick_scores_zero() -> None:
    bars = [_bar(100.0, 102.0, 99.0, 101.0, _d(0))]
    assert _shooting_star(bars, ATR, "uptrend") == 0.0


def test_doji_detects_a_negligible_body_on_a_real_range() -> None:
    bars = [_bar(100.00, 101.51, 98.51, 100.02, _d(0))]
    assert _doji(bars, ATR) >= 0.9


def test_doji_with_a_large_body_scores_zero() -> None:
    """Same real range as the positive fixture; the body is 30% of it
    (needs <= 15% for any credit) — a real candle, not indecision."""
    bars = [_bar(100.00, 101.95, 98.95, 100.90, _d(0))]
    assert _doji(bars, ATR) == 0.0


def test_marubozu_bull_detects_a_full_bodied_up_bar() -> None:
    bars = [_bar(100.00, 102.95, 99.95, 102.90, _d(0))]
    assert _marubozu_bull(bars, ATR, "uptrend") >= 0.9


def test_marubozu_bull_with_a_weak_body_scores_zero() -> None:
    """Same range, but the body is only half of it (needs >= 70%)."""
    bars = [_bar(100.00, 102.25, 99.25, 101.50, _d(0))]
    assert _marubozu_bull(bars, ATR, "uptrend") == 0.0


def test_marubozu_bear_detects_a_full_bodied_down_bar() -> None:
    bars = [_bar(102.90, 102.95, 99.95, 100.00, _d(0))]
    assert _marubozu_bear(bars, ATR, "downtrend") >= 0.9


def test_marubozu_bear_on_a_bullish_bar_scores_zero() -> None:
    """The bull marubozu's own positive fixture, read as a BEAR marubozu —
    wrong color entirely."""
    bars = [_bar(100.00, 102.95, 99.95, 102.90, _d(0))]
    assert _marubozu_bear(bars, ATR, "downtrend") == 0.0


# ─────────────────────────────────────────────────────────────────────
# Two-bar patterns
# ─────────────────────────────────────────────────────────────────────


def test_bullish_engulfing_detects_a_clean_engulf() -> None:
    prior = _bar(102.00, 102.05, 100.95, 101.00, _d(0))
    cur = _bar(100.4, 103.0, 100.3, 102.9, _d(1))
    assert _bullish_engulfing([prior, cur], ATR, "downtrend") >= 0.5


def test_bullish_engulfing_that_does_not_fully_cover_the_prior_body_scores_zero() -> None:
    """``cur`` closes at 101.8 — inside the prior's [101, 102] body, not
    beyond its top — so it does not engulf."""
    prior = _bar(102.00, 102.05, 100.95, 101.00, _d(0))
    cur = _bar(100.4, 102.0, 100.2, 101.8, _d(1))
    assert _bullish_engulfing([prior, cur], ATR, "downtrend") == 0.0


def test_bearish_engulfing_detects_a_clean_engulf() -> None:
    prior = _bar(101.00, 102.05, 100.95, 102.00, _d(0))
    cur = _bar(102.9, 103.0, 100.3, 100.4, _d(1))
    assert _bearish_engulfing([prior, cur], ATR, "uptrend") >= 0.5


def test_bearish_engulfing_that_does_not_fully_cover_the_prior_body_scores_zero() -> None:
    prior = _bar(101.00, 102.05, 100.95, 102.00, _d(0))
    cur = _bar(101.8, 102.0, 100.2, 100.4, _d(1))
    assert _bearish_engulfing([prior, cur], ATR, "uptrend") == 0.0


def test_bullish_harami_detects_a_small_body_nested_in_a_big_one() -> None:
    prior = _bar(103.00, 103.05, 99.95, 100.00, _d(0))
    cur = _bar(101.3, 101.75, 101.25, 101.7, _d(1))
    assert _bullish_harami([prior, cur], ATR, "downtrend") >= 0.7


def test_bullish_harami_that_extends_past_the_prior_body_scores_zero() -> None:
    """``cur`` closes at 103.2 — 0.2 above the prior's high of 103 — so it
    is not nested inside the prior body at all."""
    prior = _bar(103.00, 103.05, 99.95, 100.00, _d(0))
    cur = _bar(101.3, 103.25, 101.25, 103.2, _d(1))
    assert _bullish_harami([prior, cur], ATR, "downtrend") == 0.0


def test_bearish_harami_detects_a_small_body_nested_in_a_big_one() -> None:
    prior = _bar(100.00, 103.05, 99.95, 103.00, _d(0))
    cur = _bar(101.7, 101.75, 101.25, 101.3, _d(1))
    assert _bearish_harami([prior, cur], ATR, "uptrend") >= 0.7


def test_bearish_harami_that_extends_past_the_prior_body_scores_zero() -> None:
    prior = _bar(100.00, 103.05, 99.95, 103.00, _d(0))
    cur = _bar(103.2, 103.25, 101.25, 101.3, _d(1))
    assert _bearish_harami([prior, cur], ATR, "uptrend") == 0.0


def test_piercing_line_detects_a_deep_but_incomplete_penetration() -> None:
    prior = _bar(102.00, 102.05, 100.95, 101.00, _d(0))
    cur = _bar(100.7, 102.9, 99.7, 101.85, _d(1))
    assert _piercing_line([prior, cur], ATR, "downtrend") >= 0.5


def test_piercing_line_under_halfway_scores_zero() -> None:
    """``cur`` closes only 40% into the prior's [101, 102] body — under the
    "more than half" threshold."""
    prior = _bar(102.00, 102.05, 100.95, 101.00, _d(0))
    cur = _bar(100.7, 101.5, 100.6, 101.40, _d(1))
    assert _piercing_line([prior, cur], ATR, "downtrend") == 0.0


def test_piercing_line_fades_out_on_a_full_engulf() -> None:
    """A piercing line is classically defined as NOT fully engulfing the
    prior body — that is what distinguishes it from an engulfing pattern.
    Reusing the bullish-engulfing positive fixture here (a full, clean
    engulf) must score close to zero as a piercing line, even though the
    close-price penetration alone (>100%) would otherwise ramp to 1.0."""
    prior = _bar(102.00, 102.05, 100.95, 101.00, _d(0))
    cur = _bar(100.4, 103.0, 100.3, 102.9, _d(1))
    assert _piercing_line([prior, cur], ATR, "downtrend") < 0.1


def test_dark_cloud_cover_detects_a_deep_but_incomplete_penetration() -> None:
    prior = _bar(101.00, 102.05, 100.95, 102.00, _d(0))
    cur = _bar(102.3, 102.9, 99.7, 101.15, _d(1))
    assert _dark_cloud_cover([prior, cur], ATR, "uptrend") >= 0.5


def test_dark_cloud_cover_under_halfway_scores_zero() -> None:
    prior = _bar(101.00, 102.05, 100.95, 102.00, _d(0))
    cur = _bar(102.3, 102.4, 101.5, 101.60, _d(1))
    assert _dark_cloud_cover([prior, cur], ATR, "uptrend") == 0.0


# ─────────────────────────────────────────────────────────────────────
# Three-bar patterns
# ─────────────────────────────────────────────────────────────────────


def test_morning_star_detects_the_full_three_bar_reversal() -> None:
    bar1 = _bar(103.00, 103.05, 99.95, 100.00, _d(0))
    bar2 = _bar(100.00, 100.40, 99.80, 100.20, _d(1))
    bar3 = _bar(100.50, 103.35, 100.35, 103.20, _d(2))
    assert _morning_star([bar1, bar2, bar3], ATR, "downtrend") >= 0.9


def test_morning_star_with_a_weak_third_bar_scores_zero() -> None:
    """bar3 barely closes above bar1's close (16% penetration into bar1's
    body) — nowhere near the "closes back above the midpoint" requirement."""
    bar1 = _bar(103.00, 103.05, 99.95, 100.00, _d(0))
    bar2 = _bar(100.00, 100.40, 99.80, 100.20, _d(1))
    bar3 = _bar(100.30, 100.55, 100.25, 100.50, _d(2))
    assert _morning_star([bar1, bar2, bar3], ATR, "downtrend") == 0.0


def test_evening_star_detects_the_full_three_bar_reversal() -> None:
    bar1 = _bar(100.00, 103.05, 99.95, 103.00, _d(0))
    bar2 = _bar(103.00, 103.40, 102.80, 103.20, _d(1))
    bar3 = _bar(102.50, 102.60, 99.60, 99.80, _d(2))
    assert _evening_star([bar1, bar2, bar3], ATR, "uptrend") >= 0.9


def test_evening_star_with_a_weak_third_bar_scores_zero() -> None:
    bar1 = _bar(100.00, 103.05, 99.95, 103.00, _d(0))
    bar2 = _bar(103.00, 103.40, 102.80, 103.20, _d(1))
    bar3 = _bar(102.70, 102.75, 102.45, 102.50, _d(2))
    assert _evening_star([bar1, bar2, bar3], ATR, "uptrend") == 0.0


def test_three_white_soldiers_detects_sustained_upward_progress() -> None:
    b1 = _bar(100.00, 102.85, 99.85, 102.70, _d(0))
    b2 = _bar(102.70, 105.55, 102.55, 105.40, _d(1))
    b3 = _bar(105.40, 108.25, 105.25, 108.10, _d(2))
    assert _three_white_soldiers([b1, b2, b3], ATR, "uptrend") >= 0.9


def test_three_white_soldiers_with_no_progress_on_the_third_bar_scores_zero() -> None:
    """b3 is still bullish, but closes well BELOW b2's close — the
    "each bar closes higher" condition fails."""
    b1 = _bar(100.00, 102.85, 99.85, 102.70, _d(0))
    b2 = _bar(102.70, 105.55, 102.55, 105.40, _d(1))
    b3 = _bar(104.50, 105.00, 104.40, 104.90, _d(2))
    assert _three_white_soldiers([b1, b2, b3], ATR, "uptrend") == 0.0


def test_three_black_crows_detects_sustained_downward_progress() -> None:
    b1 = _bar(108.10, 108.25, 105.25, 105.40, _d(0))
    b2 = _bar(105.40, 105.55, 102.55, 102.70, _d(1))
    b3 = _bar(102.70, 102.85, 99.85, 100.00, _d(2))
    assert _three_black_crows([b1, b2, b3], ATR, "downtrend") >= 0.9


def test_three_black_crows_with_no_progress_on_the_third_bar_scores_zero() -> None:
    b1 = _bar(108.10, 108.25, 105.25, 105.40, _d(0))
    b2 = _bar(105.40, 105.55, 102.55, 102.70, _d(1))
    b3 = _bar(103.50, 103.55, 102.95, 103.00, _d(2))
    assert _three_black_crows([b1, b2, b3], ATR, "downtrend") == 0.0


# ─────────────────────────────────────────────────────────────────────
# Range patterns — direction-neutral
# ─────────────────────────────────────────────────────────────────────


def test_inside_bar_detects_a_clean_coil() -> None:
    prior = _bar(100.0, 103.0, 98.0, 100.0, _d(0))
    cur = _bar(100.1, 100.3, 100.0, 100.2, _d(1))
    assert _inside_bar([prior, cur], ATR) >= 0.9


def test_inside_bar_that_pokes_above_the_prior_high_scores_zero() -> None:
    prior = _bar(100.0, 103.0, 98.0, 100.0, _d(0))
    cur = _bar(100.1, 103.1, 100.0, 100.2, _d(1))
    assert _inside_bar([prior, cur], ATR) == 0.0


def test_outside_bar_detects_a_clean_expansion() -> None:
    prior = _bar(100.0, 100.3, 99.7, 100.0, _d(0))
    cur = _bar(99.0, 103.0, 98.0, 102.0, _d(1))
    assert _outside_bar([prior, cur], ATR) >= 0.9


def test_outside_bar_that_does_not_clear_the_prior_low_scores_zero() -> None:
    """``cur`` engulfs the prior's high side but its low (99.85) stays
    above the prior's low (99.7) — not a full engulf of the prior range."""
    prior = _bar(100.0, 100.3, 99.7, 100.0, _d(0))
    cur = _bar(99.0, 103.0, 99.85, 102.0, _d(1))
    assert _outside_bar([prior, cur], ATR) == 0.0


def test_nr7_detects_the_narrowest_range_of_the_trailing_seven() -> None:
    context = [_bar(100.0, 100.5, 99.5, 100.0, _d(i)) for i in range(6)]
    cur = _bar(100.0, 100.15, 99.85, 100.0, _d(6))
    assert _nr7([*context, cur], ATR) >= 0.9


def test_nr7_that_is_not_the_narrowest_scores_zero() -> None:
    """``cur``'s range (1.5) is wider than every one of the six context
    bars (1.0 each) — the opposite of "narrowest of 7"."""
    context = [_bar(100.0, 100.5, 99.5, 100.0, _d(i)) for i in range(6)]
    cur = _bar(100.0, 100.75, 99.25, 100.0, _d(6))
    assert _nr7([*context, cur], ATR) == 0.0


# ─────────────────────────────────────────────────────────────────────
# Aggregation / naming wiring (detect_patterns / PatternBlock level)
# ─────────────────────────────────────────────────────────────────────


def test_names_lists_the_naming_threshold_and_only_that() -> None:
    bars = [*_filler(6), _bar(100.0, 100.15, 97.15, 100.06, _d(6))]
    pb = detect_patterns(bars, atr=ATR, trend_regime="downtrend")
    assert all(name for name in pb.names)
    assert pb.top_pattern == pb.names[0]
    assert pb.top_pattern_score == max(
        _hammer(bars, ATR, "downtrend"),
        # every other family's raw score is <= its own aggregate, and no
        # aggregate here exceeds reversal_bull for this fixture
        pb.reversal_bull,
    )


def test_top_pattern_is_none_when_nothing_clears_the_threshold() -> None:
    bars = _filler(MIN_BARS_FOR_PATTERNS)
    pb = detect_patterns(bars, atr=ATR, trend_regime="choppy")
    assert pb.top_pattern is None
    assert pb.top_pattern_score == 0.0
    assert pb.names == ()


def test_as_dict_round_trips_every_field() -> None:
    bars = [*_filler(6), _bar(100.0, 102.95, 99.95, 102.9, _d(6))]
    pb = detect_patterns(bars, atr=ATR, trend_regime="uptrend")
    d = pb.as_dict()
    assert d["continuation_bull"] == pb.continuation_bull
    assert d["names"] == pb.names
    assert d["top_pattern"] == pb.top_pattern
    assert set(d.keys()) == {
        "reversal_bull",
        "reversal_bear",
        "continuation_bull",
        "continuation_bear",
        "indecision",
        "compression",
        "expansion",
        "names",
        "top_pattern",
        "top_pattern_score",
    }
