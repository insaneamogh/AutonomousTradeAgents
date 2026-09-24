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


# ── monotonicity ─────────────────────────────────────────────────────


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
