"""Structural invalidation levels.

Built to replace percentage-of-premium exits with structure-based ones.
**Measurement then refuted that use** — see
`test_a_structural_exit_would_not_have_saved_the_two_worst_losers`, which
pins the reason so nobody re-derives the idea and ships it. The module
survives because the level is the right INPUT to an entry gate: a trade
whose premium stop sits closer than its own invalidation level will always
be stopped out before the thesis is falsified.
"""

from __future__ import annotations

import pytest

from engine.features.invalidation import (
    InvalidationLevel,
    invalidation_level,
    is_invalidated,
)


def _long(**over: object) -> InvalidationLevel | None:
    base: dict[str, object] = dict(
        direction="long", last_price=100.0, atr_14=2.0,
        donchian_low_10=97.0, donchian_low_20=94.0, sma20=98.5, sma50=92.0,
    )
    base.update(over)
    return invalidation_level(**base)  # type: ignore[arg-type]


def test_picks_the_tightest_level_outside_the_noise() -> None:
    """Tightest qualifying wins — it is the first place the thesis is
    genuinely in question, so a real break exits soonest."""
    lvl = _long()
    assert lvl is not None
    assert lvl.source == "sma20"
    assert lvl.price == 98.5
    assert lvl.distance_atr == 0.75


def test_rejects_a_level_inside_the_noise_band() -> None:
    """Closer than half an ATR is an ordinary session, not a failed thesis."""
    lvl = _long(donchian_low_10=99.5, donchian_low_20=None, sma20=None, sma50=None)
    assert lvl is None


def test_rejects_a_level_too_far_to_ever_be_reached() -> None:
    """Past 4 ATR the premium is worthless long before the underlying
    arrives, so the level would be decorative."""
    lvl = _long(donchian_low_10=80.0, donchian_low_20=None, sma20=None, sma50=None)
    assert lvl is None


def test_a_support_level_already_broken_is_not_support() -> None:
    """A stale feature must never produce a stop on the wrong side of the
    market — that would close the position on the very next tick."""
    lvl = _long(donchian_low_10=105.0, donchian_low_20=None, sma20=None, sma50=None)
    assert lvl is None


def test_short_thesis_looks_upward() -> None:
    lvl = invalidation_level(
        direction="short", last_price=100.0, atr_14=2.0,
        donchian_high_20=103.0, sma20=101.5,
    )
    assert lvl is not None and lvl.price == 101.5


def test_returns_none_rather_than_inventing_a_level() -> None:
    """A caller with no level must REFUSE the trade, not fall back to a
    number with no structural meaning. A thesis with no falsifying price is
    not a thesis."""
    assert _long(atr_14=None) is None
    assert _long(atr_14=0.0) is None
    assert _long(last_price=0.0) is None
    assert _long(donchian_low_10=None, donchian_low_20=None,
                 sma20=None, sma50=None) is None


def test_break_detection_is_directional() -> None:
    assert is_invalidated(direction="long", underlying_price=97.9, level_price=98.0)
    assert not is_invalidated(direction="long", underlying_price=98.1, level_price=98.0)
    assert is_invalidated(direction="short", underlying_price=98.1, level_price=98.0)
    assert not is_invalidated(direction="short", underlying_price=97.9, level_price=98.0)


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_break_detection_refuses_nonsense_prices(bad: float) -> None:
    assert not is_invalidated(direction="long", underlying_price=bad, level_price=98.0)
    assert not is_invalidated(direction="long", underlying_price=100.0, level_price=bad)


def test_a_structural_exit_would_not_have_saved_the_two_worst_losers() -> None:
    """THE refutation. Do not re-derive the structural-exit idea from the
    research and ship it — it was tested against the real book and fails.

    Measured effective leverage on our own positions has a median of
    **17.7x** (option % move per 1% of underlying). So the -40% premium
    stop corresponds to roughly a **2.3%** adverse move in the underlying,
    while the structural levels computed for those same names sat
    **3.2%-4.3%** away:

        AAPL  entry 328.22  level 314.20 (sma50, 4.27%)  low reached 317.86
        XLE   entry  64.63  level  62.54 (sma20, 3.24%)  low reached  63.38

    Neither level was ever touched, and both options still lost ~42%. The
    premium stop fires first, every time, so on a long option a structural
    exit is unreachable by construction. Structure works for equity, where
    loss is linear in the underlying; at 17.7x leverage it cannot.

    The level is still the right input to an ENTRY gate: if the premium
    stop is tighter than the distance to invalidation, the trade is
    guaranteed to stop out before the thesis is even tested.
    """
    median_leverage = 17.7
    premium_stop_pct = 40.0
    implied_underlying_move = premium_stop_pct / median_leverage

    assert implied_underlying_move == pytest.approx(2.26, abs=0.05)
    for name, level_distance_pct in (("AAPL", 4.27), ("XLE", 3.24)):
        assert implied_underlying_move < level_distance_pct, (
            f"{name}: if the premium stop ever became LOOSER than the "
            "structural level, a structural exit would start binding and "
            "this conclusion would need re-testing"
        )
