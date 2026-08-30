"""Premium exit rules — the price-based half of the options playbook.

Alpaca cannot bracket a single-leg option, so unlike every equity entry
there is no broker-side stop or target. These are the only price exits an
open option has.
"""

from __future__ import annotations

from engine.options.exits import option_exit_signal

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
