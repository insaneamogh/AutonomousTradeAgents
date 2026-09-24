"""Partial profit-taking signal, and the per-position stop resolver.

Both come from the same measurement: the trail arms at +35% and exactly
ONE of 21 recorded positions ever reached it, so twenty had no profit
protection at all. Median MFE capture ratio was -86% against an industry
benchmark where under +40% already reads as noise-driven exits.

The obvious fix — arm earlier — is wrong, and the tests below pin why.
"""

from __future__ import annotations

import pytest

from engine.options.exits import (
    _STOP_LOSS_BAND,
    effective_stop_loss_pct,
    option_ratchet_signal,
)


def _sig(pl: float, **over: object):
    base: dict[str, object] = dict(
        unrealized_pl_pct=pl, peak_pl_pct=None, arm_pct=35.0,
        giveback_frac=0.30, hard_take_profit_pct=150.0, stop_loss_pct=40.0,
        scale_out_at_pct=5.0, scale_out_frac=0.5, already_scaled_out=False,
    )
    base.update(over)
    return option_ratchet_signal(**base)  # type: ignore[arg-type]


# ── scale-out ─────────────────────────────────────────────────────────


def test_banks_a_fraction_at_the_first_target() -> None:
    o = _sig(6.0)
    assert o.action == "SCALE_OUT"
    assert o.reason == "option_scale_out"
    assert o.scale_out_frac == 0.5


def test_is_off_by_default_so_existing_callers_are_unchanged() -> None:
    o = option_ratchet_signal(
        unrealized_pl_pct=20.0, peak_pl_pct=None, arm_pct=35.0,
        giveback_frac=0.30, hard_take_profit_pct=150.0, stop_loss_pct=40.0,
    )
    assert o.action == "HOLD"
    assert o.scale_out_frac is None


@pytest.mark.parametrize("frac", [0.0, 1.0, -0.5, 1.5])
def test_a_nonsensical_fraction_disables_it_rather_than_acting(frac: float) -> None:
    """1.0 would be a full close wearing a partial's name; 0 and negatives
    are meaningless. None of them should reach the caller as an action."""
    assert _sig(20.0, scale_out_frac=frac).action != "SCALE_OUT"


def test_every_full_close_rule_outranks_it() -> None:
    """SCALE_OUT is the weakest action deliberately: a tick that satisfies
    both a full close and the partial must close everything, not bank a
    fraction of a position that is already leaving."""
    assert _sig(-45.0).reason == "option_stop_loss"
    assert _sig(160.0).reason == "option_take_profit"
    # armed trail: peak 60 -> line 42; a reading of 40 is through it
    assert _sig(40.0, peak_pl_pct=60.0).reason == "option_trail_stop"


def test_the_runner_is_not_choked() -> None:
    """The constraint that ruled out simply arming earlier. On the recorded
    data an arm at +5% turned the two real winners into scratches
    (+43.4% -> +8.8%, +22.3% -> +1.8%): a high win rate bought by cutting
    the tail that pays for the losers. Scale-out must leave the remainder
    on the ORIGINAL trail, so a runner still exits where it always would."""
    o = _sig(50.0, peak_pl_pct=50.0)
    assert o.action == "SCALE_OUT"
    assert o.armed is True, "the trail must still be armed for the remainder"
    assert o.trail_line_pct == pytest.approx(35.0)  # 50 * (1 - 0.30)


# ── per-position stop resolution ──────────────────────────────────────


def test_a_tighter_agent_stop_is_honoured() -> None:
    assert effective_stop_loss_pct(decision_stop_pct=35.0, cap_stop_pct=40.0) == 35.0


@pytest.mark.parametrize(
    "bad", [None, "abc", "", -5.0, 0.0, float("nan"), 999.0, 10.0, [1]]
)
def test_anything_unusable_falls_back_to_the_cap(bad: object) -> None:
    """Re-validated at READ time, not trusted because the guard clamped it
    once: the value survives in Postgres across deploys and band changes."""
    assert effective_stop_loss_pct(decision_stop_pct=bad, cap_stop_pct=40.0) == 40.0


def test_bands_match_the_guard() -> None:
    """`_STOP_LOSS_BAND` is duplicated here because packages/engine must not
    import apps/agents. If the guard's band moves, this must move with it —
    the same-number-in-two-places trap from CLAUDE.md section 4.4."""
    from trading_agents.options.tools.guard import _STOP_LOSS_BAND as guard_band

    assert guard_band == _STOP_LOSS_BAND
