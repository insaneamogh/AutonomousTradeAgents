"""Premium exit rules — the price-based half of the options playbook.

Alpaca cannot bracket a single-leg option, so unlike every equity entry
there is no broker-side stop or target. These are the only price exits an
open option has.
"""

from __future__ import annotations

from engine.options.exits import option_exit_signal, option_ratchet_signal

CAPS = {"take_profit_pct": 60.0, "stop_loss_pct": 50.0}


def test_take_profit_fires_at_the_threshold() -> None:
    s = option_exit_signal(unrealized_pl_pct=60.0, **CAPS)
    assert s is not None and s.reason == "option_take_profit"
    assert s.pnl_pct == 60.0


def test_take_profit_does_not_fire_just_below() -> None:
    assert option_exit_signal(unrealized_pl_pct=59.9, **CAPS) is None


def test_stop_loss_fires_on_a_negative_reading() -> None:
    """The cap is a POSITIVE magnitude (50.0) but the reading is negative.
    Getting the sign convention wrong here closes every winner instead."""
    s = option_exit_signal(unrealized_pl_pct=-50.0, **CAPS)
    assert s is not None and s.reason == "option_stop_loss"
    assert s.pnl_pct == -50.0


def test_stop_loss_does_not_fire_just_above() -> None:
    assert option_exit_signal(unrealized_pl_pct=-49.9, **CAPS) is None


def test_a_flat_position_holds() -> None:
    assert option_exit_signal(unrealized_pl_pct=0.0, **CAPS) is None


def test_no_broker_mark_holds_rather_than_guessing() -> None:
    """A missing mark must never close a position. Degrading to 'hold' is
    the only safe direction — the time stop and expiry sweep still run."""
    assert option_exit_signal(unrealized_pl_pct=None, **CAPS) is None


def test_zero_threshold_disables_that_side_only() -> None:
    assert option_exit_signal(
        unrealized_pl_pct=500.0, take_profit_pct=0.0, stop_loss_pct=50.0
    ) is None
    s = option_exit_signal(
        unrealized_pl_pct=-90.0, take_profit_pct=0.0, stop_loss_pct=50.0
    )
    assert s is not None and s.reason == "option_stop_loss"


def test_a_negative_stop_cap_is_read_as_a_magnitude() -> None:
    """Callers state the rule as 'down 50%'. Accepting -50.0 too means a
    sign typo in an env var cannot silently disable the stop."""
    s = option_exit_signal(unrealized_pl_pct=-55.0, take_profit_pct=60.0, stop_loss_pct=-50.0)
    assert s is not None and s.reason == "option_stop_loss"


def test_take_profit_wins_when_both_could_somehow_apply() -> None:
    s = option_exit_signal(unrealized_pl_pct=80.0, take_profit_pct=60.0, stop_loss_pct=-200.0)
    assert s is not None and s.reason == "option_take_profit"


def test_detail_states_the_arithmetic_not_just_the_verdict() -> None:
    s = option_exit_signal(unrealized_pl_pct=-62.5, **CAPS)
    assert s is not None
    assert "-62.5%" in s.detail and "50.0%" in s.detail


# ─────────────────────────────────────────────────────────────────────
# option_ratchet_signal — the trailing ratchet (PLAN_EXIT_AGENT.md §3)
#
# Defaults below match RiskCaps: arm at +35%, give back 30% of the peak,
# hard take-profit backstop at +150%, stop at -50% (same field/meaning as
# CAPS above — the stop did not change between the two functions).
# ─────────────────────────────────────────────────────────────────────

RATCHET = {
    "arm_pct": 35.0,
    "giveback_frac": 0.30,
    "hard_take_profit_pct": 150.0,
    "stop_loss_pct": 50.0,
}


def test_a_fresh_position_is_not_armed_below_the_arm_threshold() -> None:
    o = option_ratchet_signal(unrealized_pl_pct=20.0, peak_pl_pct=None, **RATCHET)
    assert o.action == "HOLD"
    assert o.armed is False
    assert o.may_consult is False
    assert o.peak_pl_pct == 20.0
    assert o.trail_line_pct is None  # no trail line before arming


def test_arming_is_inclusive_of_the_threshold() -> None:
    o = option_ratchet_signal(unrealized_pl_pct=35.0, peak_pl_pct=None, **RATCHET)
    assert o.armed is True
    assert o.may_consult is True
    assert o.action == "HOLD"  # pl == peak, can't be below its own trail line


def test_trail_does_not_fire_before_arming() -> None:
    """A peak that never reached the arm threshold must not be tradeable
    against the trail formula even when the raw arithmetic would satisfy
    it — the guard is `armed`, not the giveback math.

    Break this by making `armed` unconditional (always compute and check
    the trail line): peak=30 gives trail_line=21, and this tick's pl=15
    would then satisfy `pl <= trail_line` and incorrectly fire a close,
    even though the position never reached the +35% arm threshold.
    """
    o = option_ratchet_signal(unrealized_pl_pct=15.0, peak_pl_pct=30.0, **RATCHET)
    assert o.action == "HOLD"
    assert o.armed is False
    assert o.may_consult is False
    assert o.peak_pl_pct == 30.0  # unchanged: max(30, 15) == 30


def test_ratchet_closes_on_a_peak_retracement() -> None:
    """peak=82.4 (this session's own worked example from the plan) with a
    30% giveback draws the trail line at 57.68. A retracement to 50 must
    close as a trail stop.

    Break this by making `trail_line` read `pl` instead of `peak`: it
    would become 50*0.7=35, and 50 <= 35 is False, so the position would
    incorrectly hold through an actual peak retracement.
    """
    o = option_ratchet_signal(unrealized_pl_pct=50.0, peak_pl_pct=82.4, **RATCHET)
    assert o.action == "CLOSE"
    assert o.reason == "option_trail_stop"
    assert o.peak_pl_pct == 82.4
    assert o.trail_line_pct == 57.68


def test_peak_is_monotone_across_ticks() -> None:
    """A dip that does not breach the trail line must not lower the peak.

    Break this by letting `peak` take `pl` directly instead of
    `max(peak_persisted, pl)`: tick 2's peak would drop to 30 instead of
    staying at 40.
    """
    tick1 = option_ratchet_signal(unrealized_pl_pct=40.0, peak_pl_pct=None, **RATCHET)
    assert tick1.peak_pl_pct == 40.0

    tick2 = option_ratchet_signal(
        unrealized_pl_pct=30.0, peak_pl_pct=tick1.peak_pl_pct, **RATCHET
    )
    assert tick2.action == "HOLD"  # 30 > trail_line (40*0.7=28) — no close yet
    assert tick2.peak_pl_pct == 40.0  # NOT 30 — the peak must not regress
    assert tick2.peak_advanced is False


def test_stop_wins_over_trail_on_a_gap_through_zero() -> None:
    """A gap from +50 to -60 satisfies both 'below the trail line' and
    'past the stop'. The stop must win — a giveback through zero reads
    more honestly in the ledger as a stop than as a trail.

    Break this by reordering rules 1 and 3 (check the trail before the
    stop): -60 <= 35 (the trail line off a peak of 50) is also true, so a
    trail-first ordering would misreport this as `option_trail_stop`.
    """
    o = option_ratchet_signal(unrealized_pl_pct=-60.0, peak_pl_pct=50.0, **RATCHET)
    assert o.action == "CLOSE"
    assert o.reason == "option_stop_loss"


def test_hard_take_profit_backstop_fires_above_the_ceiling() -> None:
    """Set far above the arm point on purpose — the trail is expected to
    catch almost every real winner first. This ceiling exists only for a
    single-tick gap that jumps straight past it."""
    o = option_ratchet_signal(unrealized_pl_pct=160.0, peak_pl_pct=None, **RATCHET)
    assert o.action == "CLOSE"
    assert o.reason == "option_take_profit"


def test_no_mark_holds_and_leaves_the_peak_alone() -> None:
    """A missing broker mark must never manufacture a data point.

    Break this by treating `None` as `0.0`: `pnl_pct` would read `0.0`
    instead of `None`, a fabricated reading the audit row would then show
    as if the broker had actually reported a flat position.
    """
    o = option_ratchet_signal(unrealized_pl_pct=None, peak_pl_pct=45.0, **RATCHET)
    assert o.action == "HOLD"
    assert o.reason is None
    assert o.pnl_pct is None
    assert o.peak_pl_pct == 45.0
    assert o.peak_advanced is False
    assert o.may_consult is False  # never consult about a mark we don't have


def test_proportional_giveback_matches_the_plan_worked_examples() -> None:
    """peak +80 -> line +56; peak +200 -> line +140 (PLAN_EXIT_AGENT.md §3).
    Point-giveback would instead draw both lines 30 points below peak
    (+50 / +170) — this pins the PROPORTIONAL formula specifically."""
    o80 = option_ratchet_signal(unrealized_pl_pct=80.0, peak_pl_pct=None, **RATCHET)
    assert o80.trail_line_pct == 56.0

    o200 = option_ratchet_signal(unrealized_pl_pct=200.0, peak_pl_pct=None, **RATCHET)
    # 200 >= hard_take_profit_pct(150) closes before the trail is read in
    # the pnl sense, but the trail line itself must still compute correctly.
    assert o200.trail_line_pct == 140.0
    assert o200.action == "CLOSE"
    assert o200.reason == "option_take_profit"


def test_peak_advanced_is_true_only_when_the_peak_actually_moved() -> None:
    first = option_ratchet_signal(unrealized_pl_pct=20.0, peak_pl_pct=None, **RATCHET)
    assert first.peak_advanced is True  # 0 -> 20

    held = option_ratchet_signal(unrealized_pl_pct=10.0, peak_pl_pct=20.0, **RATCHET)
    assert held.peak_advanced is False  # a dip must not advance the peak

    new_high = option_ratchet_signal(unrealized_pl_pct=25.0, peak_pl_pct=20.0, **RATCHET)
    assert new_high.peak_advanced is True  # 20 -> 25


def test_zero_stop_threshold_disables_the_stop_only() -> None:
    o = option_ratchet_signal(
        unrealized_pl_pct=-90.0, peak_pl_pct=None, arm_pct=35.0,
        giveback_frac=0.30, hard_take_profit_pct=150.0, stop_loss_pct=0.0,
    )
    assert o.action == "HOLD"


def test_zero_hard_take_profit_disables_that_ceiling_only() -> None:
    o = option_ratchet_signal(
        unrealized_pl_pct=500.0, peak_pl_pct=None, arm_pct=35.0,
        giveback_frac=0.30, hard_take_profit_pct=0.0, stop_loss_pct=50.0,
    )
    # Armed, way above the trail line's own peak (can't be below itself on
    # a fresh high), and the hard ceiling is off — this ought to hold, with
    # the trail as the only thing that can still close it on a retracement.
    assert o.action == "HOLD"
    assert o.armed is True


def test_negative_stop_loss_pct_is_read_as_a_magnitude() -> None:
    """Same convention as `option_exit_signal`: a sign typo in an env var
    must not silently disable the stop."""
    o = option_ratchet_signal(
        unrealized_pl_pct=-55.0, peak_pl_pct=None, arm_pct=35.0,
        giveback_frac=0.30, hard_take_profit_pct=150.0, stop_loss_pct=-50.0,
    )
    assert o.action == "CLOSE"
    assert o.reason == "option_stop_loss"
