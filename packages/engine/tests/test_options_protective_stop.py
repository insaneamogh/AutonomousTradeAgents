"""Levels for the resting broker-side option stop.

Pure arithmetic, so these are exact-value tests. The properties that
actually matter are monotonicity (a stop can only ever tighten) and
never emitting an unfillable order.
"""

from __future__ import annotations

import pytest

from engine.options.protective_stop import (
    protective_stop_levels,
    round_to_option_tick,
    should_replace,
)

# ── tick snapping ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (2.994, 2.99),   # penny grid below $3
        (2.9999, 2.99),  # rounds DOWN, never up
        (0.07, 0.07),
        (4.63, 4.60),    # nickel grid at/above $3
        (4.60, 4.60),    # already on the grid, unchanged
        (12.34, 12.30),
    ],
)
def test_snaps_to_the_venue_increment_always_downward(price: float, expected: float) -> None:
    assert round_to_option_tick(price) == pytest.approx(expected)


def test_a_non_positive_price_snaps_to_zero_rather_than_going_negative() -> None:
    assert round_to_option_tick(-1.0) == 0.0
    assert round_to_option_tick(0.0) == 0.0


# ── the level itself ─────────────────────────────────────────────────


def test_fixed_stop_sets_the_level_before_the_trail_arms() -> None:
    levels = protective_stop_levels(
        entry_premium=4.60, stop_loss_pct=40.0, slippage_pct=12.0
    )
    assert levels is not None
    # 4.60 * 0.60 = 2.76, on the penny grid (below $3).
    assert levels.stop_price == pytest.approx(2.76)
    assert levels.basis_pl_pct == pytest.approx(-40.0)
    assert levels.from_trail is False
    # The limit sits a slippage band below the trigger and never above it.
    assert levels.limit_price < levels.stop_price
    assert levels.limit_price == pytest.approx(2.42)


def test_an_armed_trail_line_tightens_the_level_above_the_fixed_stop() -> None:
    levels = protective_stop_levels(
        entry_premium=4.60,
        stop_loss_pct=40.0,
        slippage_pct=12.0,
        trail_line_pct=25.0,  # ratchet armed and in profit
    )
    assert levels is not None
    assert levels.from_trail is True
    assert levels.basis_pl_pct == pytest.approx(25.0)
    # 4.60 * 1.25 = 5.75 — above entry, which is the whole point of a trail.
    assert levels.stop_price == pytest.approx(5.75)


def test_a_trail_line_below_the_fixed_stop_never_loosens_it() -> None:
    """The trail is only ever the TIGHTER of the two. A trail line that
    reads worse than the fixed stop (a bad mark, a barely-armed trail on a
    losing position) must not widen the resting stop."""
    levels = protective_stop_levels(
        entry_premium=4.60,
        stop_loss_pct=40.0,
        slippage_pct=12.0,
        trail_line_pct=-70.0,
    )
    assert levels is not None
    assert levels.basis_pl_pct == pytest.approx(-40.0)
    assert levels.from_trail is False


def test_the_cme_contract_would_have_rested_a_real_stop() -> None:
    """CME261016P00270000: 5 @ $4.60, exited $2.20 (-52%).

    A resting stop would NOT have saved this trade — the mark gapped
    -26% -> -52% in one print and a broker stop elects on that same print.
    What this asserts is only that a placeable, correctly-priced order
    exists for it, so the position is covered while we are not running.
    """
    levels = protective_stop_levels(
        entry_premium=4.60, stop_loss_pct=35.0, slippage_pct=12.0
    )
    assert levels is not None
    assert levels.stop_price == pytest.approx(2.99)
    assert 0 < levels.limit_price < levels.stop_price


def test_no_stop_is_emitted_when_it_would_land_in_unfillable_dust() -> None:
    """A stop-limit whose limit rounds to zero can never fill; emitting one
    would put a dead order at the broker while the audit row claims the
    position is protected. Returning None keeps the software stop honest."""
    assert (
        protective_stop_levels(
            entry_premium=0.06, stop_loss_pct=90.0, slippage_pct=12.0
        )
        is None
    )


def test_a_zero_stop_with_no_trail_disables_the_resting_order() -> None:
    assert (
        protective_stop_levels(
            entry_premium=4.60, stop_loss_pct=0.0, slippage_pct=12.0
        )
        is None
    )


def test_a_non_positive_entry_premium_is_refused() -> None:
    assert (
        protective_stop_levels(entry_premium=0.0, stop_loss_pct=40.0, slippage_pct=12.0)
        is None
    )


def test_the_limit_is_always_strictly_below_the_trigger() -> None:
    """Both snap to the same tick on cheap contracts unless handled. A
    limit at or above the trigger is not a protective order — it is a
    stop that elects and then sits unfilled at a price the market has
    already left."""
    for premium in (0.10, 0.25, 0.50, 1.00, 2.99, 3.00, 3.05, 10.0, 25.0):
        levels = protective_stop_levels(
            entry_premium=premium, stop_loss_pct=40.0, slippage_pct=12.0
        )
        if levels is None:
            continue
        assert levels.limit_price < levels.stop_price, premium
        assert levels.limit_price > 0, premium


# ── monotonicity ─────────────────────────────────────────────────────


def test_replacement_is_monotone_a_looser_level_is_never_taken() -> None:
    """The one-way property the whole design rests on: a transient bad
    mark must not be able to cancel a tightened stop and re-place it
    lower."""
    assert not should_replace(
        current_basis_pl_pct=25.0, new_basis_pl_pct=-40.0, min_step_pct=5.0
    )
    assert not should_replace(
        current_basis_pl_pct=25.0, new_basis_pl_pct=25.0, min_step_pct=5.0
    )


def test_a_small_advance_does_not_pay_for_a_cancel_replace() -> None:
    assert not should_replace(
        current_basis_pl_pct=25.0, new_basis_pl_pct=28.0, min_step_pct=5.0
    )
    assert should_replace(
        current_basis_pl_pct=25.0, new_basis_pl_pct=30.0, min_step_pct=5.0
    )


def test_the_first_stop_always_places() -> None:
    assert should_replace(
        current_basis_pl_pct=None, new_basis_pl_pct=-40.0, min_step_pct=5.0
    )


def test_levels_are_monotone_as_the_trail_advances() -> None:
    """Walk a realistic ratchet: armed at +35%, peak climbing, trail line
    following. The resting stop must never step backwards."""
    previous = None
    for trail in (-40.0, 0.0, 24.5, 35.0, 56.0, 84.0, 140.0):
        levels = protective_stop_levels(
            entry_premium=4.60,
            stop_loss_pct=40.0,
            slippage_pct=12.0,
            trail_line_pct=trail,
        )
        assert levels is not None
        if previous is not None:
            assert levels.stop_price >= previous, (trail, levels.stop_price, previous)
        previous = levels.stop_price
