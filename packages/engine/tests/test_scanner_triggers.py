"""Trigger-rule tests — the layer that decides how the LLM budget is spent.

Each rule gets: a positive case, a just-under-threshold negative, and
whatever degenerate input could make it lie (missing level, zero
denominator, no intraday tape).

The snapshots are hand-built rather than derived from bars, because the
point of these tests is the RULE, not the plumbing. ``test_scanner_engine``
covers the bars → snapshot half.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from engine.scanner.triggers import evaluate_triggers
from engine.scanner.types import ScannerConfig, SymbolSnapshot, TriggerRule

AT = datetime(2026, 6, 16, 15, 0, tzinfo=UTC)
CFG = ScannerConfig()


def snap(**over: object) -> SymbolSnapshot:
    """A deliberately inert baseline: nothing fires unless a test moves it.

    Price sits on the 20-DMA, RSI is neutral, volume is normal, the range
    is quiet and the channel is wide. Every assertion below is therefore a
    statement about the one field the test changed.
    """
    base: dict[str, object] = {
        "symbol": "TEST",
        "observed_at": AT,
        "last_price": 100.0,
        "session_open": 100.0,
        "session_high": 100.4,
        "session_low": 99.6,
        "session_volume": 1_000_000.0,
        "intraday_bars": 12,
        "prior_close": 100.0,
        "sma20": 100.0,
        "sma50": 95.0,
        "sma200": 80.0,
        "rsi_prior": 50.0,
        "rsi_live": 50.0,
        "atr_14": 2.0,
        "avg_volume_20d": 2_000_000.0,
        "donchian_high_20": 120.0,
        "donchian_low_20": 80.0,
        "donchian_low_10": 80.0,
        "close_mean_20": 100.0,
        "close_std_20": 4.0,
    }
    base.update(over)
    return SymbolSnapshot(**base)  # type: ignore[arg-type]


def rules(s: SymbolSnapshot, cfg: ScannerConfig | None = None) -> set[str]:
    return {sig.trigger_rule for sig in evaluate_triggers(s, cfg or CFG)}


# ─────────────────────────────────────────────────────────────────────
# Baseline + guards
# ─────────────────────────────────────────────────────────────────────


def test_inert_snapshot_fires_nothing() -> None:
    assert rules(snap()) == set()


def test_no_intraday_prints_fires_nothing() -> None:
    """A symbol with no tape today has no live price to cross anything."""
    assert rules(snap(intraday_bars=0, last_price=130.0)) == set()


def test_non_positive_prices_fire_nothing() -> None:
    assert rules(snap(last_price=0.0)) == set()
    assert rules(snap(prior_close=0.0)) == set()


def test_every_emitted_rule_is_in_the_registry() -> None:
    """A typo'd identifier would silently break cooldowns and audit greps."""
    fired = rules(snap(last_price=125.0, session_open=125.0, rsi_live=75.0,
                       session_volume=9_000_000.0, session_high=126.0, session_low=99.0))
    assert fired, "expected this snapshot to trip several rules"
    assert fired <= TriggerRule.ALL


def test_strength_is_always_within_band() -> None:
    for price in (100.5, 105.0, 130.0, 60.0):
        for sig in evaluate_triggers(snap(last_price=price, session_open=price)):
            assert 0.05 <= sig.strength <= 1.0


# ─────────────────────────────────────────────────────────────────────
# Moving-average crosses
# ─────────────────────────────────────────────────────────────────────


def test_dma20_cross_up_requires_the_prior_close_below() -> None:
    s = snap(prior_close=99.0, last_price=101.0, sma20=100.0)
    assert TriggerRule.DMA20_CROSS_UP in rules(s)


def test_dma20_cross_down() -> None:
    s = snap(prior_close=101.0, last_price=99.0, sma20=100.0, session_open=101.0)
    assert TriggerRule.DMA20_CROSS_DOWN in rules(s)


def test_no_cross_when_both_sides_are_above_the_level() -> None:
    """Already above yesterday and still above today is not a cross."""
    s = snap(prior_close=105.0, last_price=106.0, sma20=100.0)
    assert TriggerRule.DMA20_CROSS_UP not in rules(s)


def test_cross_buffer_rejects_a_price_pinned_to_the_level() -> None:
    """The failure this buffer exists for: a name sitting on its 20-DMA
    re-firing on every scan as the last print wobbles a cent either way."""
    s = snap(prior_close=99.99, last_price=100.05, sma20=100.0)  # +0.05%, buffer 0.1%
    assert TriggerRule.DMA20_CROSS_UP not in rules(s)
    s = snap(prior_close=99.99, last_price=100.2, sma20=100.0)  # +0.2%
    assert TriggerRule.DMA20_CROSS_UP in rules(s)


def test_dma50_and_dma200_crosses_fire_independently() -> None:
    s = snap(prior_close=94.0, last_price=96.0, sma20=90.0, sma50=95.0, sma200=80.0)
    assert TriggerRule.DMA50_CROSS_UP in rules(s)
    s = snap(prior_close=79.0, last_price=81.0, sma20=70.0, sma50=75.0, sma200=80.0)
    assert TriggerRule.DMA200_CROSS_UP in rules(s)


def test_missing_sma200_does_not_crash_or_fire() -> None:
    s = snap(sma200=None, prior_close=99.0, last_price=101.0, sma20=100.0)
    fired = rules(s)
    assert TriggerRule.DMA200_CROSS_UP not in fired
    assert TriggerRule.DMA20_CROSS_UP in fired


# ─────────────────────────────────────────────────────────────────────
# RSI band transitions
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prior", "live", "expected"),
    [
        (28.0, 33.0, TriggerRule.RSI_EXIT_OVERSOLD),
        (74.0, 68.0, TriggerRule.RSI_EXIT_OVERBOUGHT),
        (34.0, 29.0, TriggerRule.RSI_ENTER_OVERSOLD),
        (66.0, 72.0, TriggerRule.RSI_ENTER_OVERBOUGHT),
    ],
)
def test_rsi_band_transitions(prior: float, live: float, expected: str) -> None:
    assert expected in rules(snap(rsi_prior=prior, rsi_live=live))


def test_rsi_inside_the_bands_does_not_fire() -> None:
    assert rules(snap(rsi_prior=45.0, rsi_live=58.0)) == set()


def test_rsi_deep_in_a_band_without_crossing_does_not_fire() -> None:
    """Staying oversold is not the same event as leaving oversold."""
    assert rules(snap(rsi_prior=22.0, rsi_live=25.0)) == set()


def test_rsi_missing_values_do_not_fire() -> None:
    assert rules(snap(rsi_prior=None, rsi_live=33.0)) == set()
    assert rules(snap(rsi_prior=28.0, rsi_live=None)) == set()


# ─────────────────────────────────────────────────────────────────────
# Volume
# ─────────────────────────────────────────────────────────────────────


def test_volume_spike_2x_and_3x_are_distinct_rules() -> None:
    assert TriggerRule.VOLUME_SPIKE_2X in rules(
        snap(session_volume=4_400_000.0, avg_volume_20d=2_000_000.0)
    )
    assert TriggerRule.VOLUME_SPIKE_3X in rules(
        snap(session_volume=7_000_000.0, avg_volume_20d=2_000_000.0)
    )


def test_volume_just_under_the_multiple_does_not_fire() -> None:
    assert rules(snap(session_volume=3_900_000.0, avg_volume_20d=2_000_000.0)) == set()


def test_volume_spike_direction_follows_the_price_move() -> None:
    up = evaluate_triggers(
        snap(session_volume=5_000_000.0, avg_volume_20d=2_000_000.0,
             last_price=104.0, prior_close=100.0, sma20=90.0)
    )
    down = evaluate_triggers(
        snap(session_volume=5_000_000.0, avg_volume_20d=2_000_000.0,
             last_price=96.0, prior_close=100.0, sma20=110.0)
    )
    assert [s.direction for s in up if "volume" in s.trigger_rule] == ["bullish"]
    assert [s.direction for s in down if "volume" in s.trigger_rule] == ["bearish"]


def test_volume_with_no_baseline_does_not_fire() -> None:
    assert rules(snap(avg_volume_20d=None, session_volume=9_000_000.0)) == set()
    assert rules(snap(avg_volume_20d=0.0, session_volume=9_000_000.0)) == set()


# ─────────────────────────────────────────────────────────────────────
# Volatility
# ─────────────────────────────────────────────────────────────────────


def test_atr_expansion_fires_on_a_wide_range_day() -> None:
    s = snap(session_high=104.0, session_low=100.0, atr_14=2.0)  # TR 4.0 = 2.0x ATR
    assert TriggerRule.ATR_EXPANSION in rules(s)


def test_atr_expansion_counts_an_overnight_gap_in_the_true_range() -> None:
    """A 4% gap that then trades quietly is still a volatility event —
    Wilder's true range includes the distance from the prior close."""
    s = snap(prior_close=100.0, session_open=104.0, session_high=104.2,
             session_low=103.8, last_price=104.0, atr_14=2.0, sma20=90.0)
    assert TriggerRule.ATR_EXPANSION in rules(s)


def test_atr_expansion_quiet_day_does_not_fire() -> None:
    assert TriggerRule.ATR_EXPANSION not in rules(
        snap(session_high=101.0, session_low=100.0, atr_14=2.0)
    )


def test_atr_expansion_without_an_atr_does_not_fire() -> None:
    assert TriggerRule.ATR_EXPANSION not in rules(
        snap(atr_14=None, session_high=110.0, session_low=90.0)
    )


def test_gap_up_and_down() -> None:
    assert TriggerRule.GAP_UP in rules(
        snap(prior_close=100.0, session_open=103.0, last_price=103.0, sma20=90.0)
    )
    assert TriggerRule.GAP_DOWN in rules(
        snap(prior_close=100.0, session_open=97.0, last_price=97.0, sma20=110.0)
    )


def test_gap_under_the_threshold_does_not_fire() -> None:
    fired = rules(snap(prior_close=100.0, session_open=101.5, last_price=101.5, sma20=90.0))
    assert TriggerRule.GAP_UP not in fired


# ─────────────────────────────────────────────────────────────────────
# Donchian
# ─────────────────────────────────────────────────────────────────────


def test_donchian_breakout_and_breakdown() -> None:
    assert TriggerRule.DONCHIAN_BREAKOUT_UP in rules(
        snap(last_price=121.0, donchian_high_20=120.0, sma20=110.0, prior_close=119.0,
             session_open=119.0, close_mean_20=119.0, close_std_20=20.0)
    )
    assert TriggerRule.DONCHIAN_BREAKDOWN in rules(
        snap(last_price=79.0, donchian_low_10=80.0, sma20=90.0, prior_close=81.0,
             session_open=81.0, close_mean_20=81.0, close_std_20=20.0)
    )


def test_donchian_approach_is_mutually_exclusive_with_the_break() -> None:
    """Through the edge → break. Near it → approach. Never both."""
    near = rules(
        snap(last_price=119.5, donchian_high_20=120.0, sma20=110.0, prior_close=119.0,
             session_open=119.0, close_mean_20=119.0, close_std_20=20.0)
    )
    assert TriggerRule.DONCHIAN_UPPER_APPROACH in near
    assert TriggerRule.DONCHIAN_BREAKOUT_UP not in near

    through = rules(
        snap(last_price=121.0, donchian_high_20=120.0, sma20=110.0, prior_close=119.0,
             session_open=119.0, close_mean_20=119.0, close_std_20=20.0)
    )
    assert TriggerRule.DONCHIAN_BREAKOUT_UP in through
    assert TriggerRule.DONCHIAN_UPPER_APPROACH not in through


def test_donchian_mid_channel_does_not_fire() -> None:
    fired = rules(snap(last_price=100.0, donchian_high_20=120.0, donchian_low_20=80.0))
    assert TriggerRule.DONCHIAN_UPPER_APPROACH not in fired
    assert TriggerRule.DONCHIAN_LOWER_APPROACH not in fired


def test_donchian_approach_ignores_a_hairline_channel() -> None:
    """A name whose 20-day range is 1% wide is not 'approaching' anything —
    it is sitting in the middle of a very quiet channel. The percent-distance
    version of this rule fired here on every scan, forever."""
    fired = rules(
        snap(last_price=100.0, donchian_high_20=100.5, donchian_low_20=99.5,
             donchian_low_10=99.5, close_std_20=0.3)
    )
    assert TriggerRule.DONCHIAN_UPPER_APPROACH not in fired
    assert TriggerRule.DONCHIAN_LOWER_APPROACH not in fired


def test_donchian_lower_approach_fires_near_the_channel_low() -> None:
    fired = rules(
        snap(last_price=82.0, donchian_high_20=120.0, donchian_low_20=80.0,
             donchian_low_10=80.0, sma20=90.0, sma50=95.0, prior_close=83.0,
             session_open=83.0, close_mean_20=83.0, close_std_20=20.0)
    )
    assert TriggerRule.DONCHIAN_LOWER_APPROACH in fired


# ─────────────────────────────────────────────────────────────────────
# Standardized stretch
# ─────────────────────────────────────────────────────────────────────


def test_zscore_stretch_up_and_down() -> None:
    assert TriggerRule.ZSCORE_STRETCH_UP in rules(
        snap(last_price=109.0, close_mean_20=100.0, close_std_20=4.0,
             prior_close=108.0, session_open=108.0, sma20=99.0, donchian_high_20=200.0)
    )
    assert TriggerRule.ZSCORE_STRETCH_DOWN in rules(
        snap(last_price=91.0, close_mean_20=100.0, close_std_20=4.0,
             prior_close=92.0, session_open=92.0, sma20=101.0, donchian_low_10=50.0)
    )


def test_zscore_is_scale_free_across_volatility_regimes() -> None:
    """The reason this rule is standardized: the same 3% move must fire on
    a calm name and not on a volatile one."""
    calm = snap(last_price=103.0, close_mean_20=100.0, close_std_20=1.0,
                prior_close=102.0, session_open=102.0, sma20=99.0)
    wild = snap(last_price=103.0, close_mean_20=100.0, close_std_20=10.0,
                prior_close=102.0, session_open=102.0, sma20=99.0)
    assert TriggerRule.ZSCORE_STRETCH_UP in rules(calm)
    assert TriggerRule.ZSCORE_STRETCH_UP not in rules(wild)


def test_zscore_with_zero_dispersion_does_not_fire() -> None:
    assert rules(snap(close_std_20=0.0, last_price=200.0)) <= TriggerRule.ALL
    assert TriggerRule.ZSCORE_STRETCH_UP not in rules(
        snap(close_std_20=0.0, last_price=200.0)
    )
    assert TriggerRule.ZSCORE_STRETCH_UP not in rules(
        snap(close_mean_20=None, last_price=200.0)
    )


# ─────────────────────────────────────────────────────────────────────
# Signal payload
# ─────────────────────────────────────────────────────────────────────


def test_signal_carries_the_numbers_it_compared() -> None:
    sigs = evaluate_triggers(snap(prior_close=99.0, last_price=101.0, sma20=100.0))
    cross = next(s for s in sigs if s.trigger_rule == TriggerRule.DMA20_CROSS_UP)
    assert cross.context["level"] == pytest.approx(100.0)
    assert cross.context["last_price"] == pytest.approx(101.0)
    assert cross.observed_at == AT
    assert cross.direction == "bullish"
    assert "20-DMA" in cross.detail


def test_signal_as_dict_is_json_shaped() -> None:
    sig = evaluate_triggers(snap(prior_close=99.0, last_price=101.0, sma20=100.0))[0]
    d = sig.as_dict()
    assert d["rule"] == TriggerRule.DMA20_CROSS_UP
    assert isinstance(d["observed_at"], str)
    assert isinstance(d["context"], dict)


def test_evaluation_is_deterministic() -> None:
    """Two evaluations of the same snapshot must be identical — the
    cooldown's correctness depends on it."""
    s = snap(prior_close=99.0, last_price=104.0, sma20=100.0, session_open=104.0)
    assert [x.trigger_rule for x in evaluate_triggers(s)] == [
        x.trigger_rule for x in evaluate_triggers(s)
    ]


def test_custom_config_moves_the_thresholds() -> None:
    loose = ScannerConfig(gap_pct=0.5)
    s = snap(prior_close=100.0, session_open=101.0, last_price=101.0, sma20=90.0)
    assert TriggerRule.GAP_UP not in rules(s)
    assert TriggerRule.GAP_UP in rules(s, loose)
