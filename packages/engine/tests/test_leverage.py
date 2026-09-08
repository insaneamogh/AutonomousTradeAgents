"""Effective leverage of a long option.

Ships as **measurement, not as a gate**. Three separate hypotheses for a
leverage-based entry rule were tested against the recorded book and all
three failed; the tests below pin each refutation so they are not
re-derived and shipped.
"""

from __future__ import annotations

import pytest

from engine.options.leverage import (
    effective_leverage,
    stop_implied_underlying_move_pct,
)


def test_matches_realised_leverage_on_the_positions_that_actually_moved() -> None:
    """The ex-ante formula is sound. Checked only against positions whose
    underlying moved more than 1% — below that the realised ratio has a
    denominator too small to compare against."""
    for spot, premium, delta, realised in (
        (328.22, 3.20, 0.30, 31.3),   # AAPL340C
        (228.54, 6.80, 0.50, 16.3),   # NVDA225C
        (64.63, 1.46, 0.40, 17.6),    # XLE67C
    ):
        lev = effective_leverage(delta=delta, spot=spot, premium=premium)
        assert lev is not None
        assert lev == pytest.approx(realised, rel=0.06)


def test_a_put_gives_positive_leverage() -> None:
    assert effective_leverage(delta=-0.4, spot=100.0, premium=5.0) == 8.0


@pytest.mark.parametrize(
    "delta,spot,premium", [(None, 100.0, 5.0), (0.5, 0.0, 5.0), (0.5, 100.0, 0.0)]
)
def test_unknown_inputs_give_none_never_zero(
    delta: float | None, spot: float, premium: float
) -> None:
    """A missing delta is exactly the case where we cannot tell a 6x
    contract from a 100x one. Returning 0 would read as 'no leverage'."""
    assert effective_leverage(delta=delta, spot=spot, premium=premium) is None
    assert stop_implied_underlying_move_pct(
        delta=delta, spot=spot, premium=premium, stop_loss_pct=40.0
    ) is None


def test_the_stop_means_different_things_on_different_contracts() -> None:
    """The finding that does hold: one configured stop percentage is not
    one risk setting. At 30.8x a -40% stop is a 1.3% adverse move; at 12.5x
    it is 3.2%. No single number could ever fit the whole book."""
    aapl = stop_implied_underlying_move_pct(
        delta=0.30, spot=328.22, premium=3.20, stop_loss_pct=40.0
    )
    cdns = stop_implied_underlying_move_pct(
        delta=0.45, spot=311.0, premium=11.20, stop_loss_pct=40.0
    )
    assert aapl is not None and cdns is not None
    assert aapl == pytest.approx(1.30, abs=0.05)
    assert cdns == pytest.approx(3.20, abs=0.05)
    assert cdns > 2 * aapl


def test_no_leverage_cap_is_shipped_because_none_separates_the_book() -> None:
    """THE refutation. Three hypotheses, all dead:

    1. A cap on REALISED leverage (option% / underlying%) appeared to split
       the book perfectly at 20x. It is circular — 4 of 12 rows were
       NEGATIVE (option fell while underlying rose, i.e. theta), and taking
       the magnitude makes a theta-killed trade look 111x-levered purely
       because it lost.
    2. A cap on EX-ANTE leverage does not separate: NVDA at 16.8x made
       +43.4% while XLE at 17.7x lost 41.8%.
    3. Normalising the implied stop move by the name's ATR refused NVDA,
       the best trade in the book, at every threshold tried.

    If a future change ships a cap, it needs new evidence, not this data.
    """
    nvda = effective_leverage(delta=0.50, spot=228.54, premium=6.80)
    xle = effective_leverage(delta=0.40, spot=64.63, premium=1.46)
    assert nvda is not None and xle is not None
    # A winner and a 42% loser, one leverage point apart. Any cap that
    # refuses one refuses the other.
    assert abs(nvda - xle) < 1.5, (
        "the winner and the worst loser sit at nearly identical ex-ante "
        "leverage — no threshold between them exists"
    )
