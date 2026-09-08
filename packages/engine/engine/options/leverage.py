"""Effective leverage of a long option, and the move its stop implies.

Leverage is the variable that best separates this book's winners from its
losers, and nothing in the system measured it until 2026-09-08.

    effective_leverage = delta * spot / premium

i.e. how many percent the OPTION moves per percent the UNDERLYING moves.
A 0.30-delta $340 call on a $328 stock trading at $3.20 is
``0.30 * 328 / 3.20 = 30.8x`` — a 1.3% dip in AAPL is a 40% loss on the
contract.

**Do not build a leverage cap on REALISED leverage.** Dividing the observed
option move by the observed underlying move looks like leverage and is not:
4 of 12 recorded positions produced a NEGATIVE ratio, because the option
fell while the underlying rose. That is theta, not leverage, and taking its
magnitude makes a theta-killed trade look like a 111x-levered one purely
BECAUSE it lost. Sorting by that and finding it "predicts" losses is
circular — the metric embeds the outcome. Two more rows had a
sub-1% denominator, where the ratio is numerically unstable.

Ex-ante leverage, the honest version computed here, was validated against
realised leverage on the three positions whose underlying actually moved
more than 1%::

    AAPL   formula 30.8x   realised 31.3x
    NVDA   formula 16.8x   realised 16.3x
    XLE    formula 17.7x   realised 17.6x

and it does **not** separate winners from losers::

    NVDA 16.8x -> +43.4%     XLE  17.7x -> -41.8%
    XLF  18.2x -> -28.8%     CDNS 12.5x -> +22.3%

Nor does normalising the implied stop move by the name's own ATR: every
threshold tried refused NVDA, the best trade in the book. So this module
ships as **measurement, not as a gate** — there is no evidence for a cap.

Two things it does establish, and they point in the same direction:

1. **The premium stop's real meaning is leverage-dependent.** At 17.7x a
   -40% stop is a ~2.3% adverse move in the underlying; at 51x it is 0.8%,
   which is noise. The same configured number means completely different
   things on different contracts, which is why one stop setting could never
   fit the whole book.
2. **A structural stop cannot bind on a high-leverage contract.** The
   invalidation levels computed for AAPL and XLE sat 3.2%-4.3% away while
   their premium stops sat at 1.3% and 2.6% — the premium stop fires first,
   always. See ``engine/features/invalidation.py``.

So leverage belongs at ENTRY, in contract selection, not in the exit.
"""

from __future__ import annotations


def effective_leverage(
    *, delta: float | None, spot: float, premium: float
) -> float | None:
    """Percent the option moves per percent of the underlying.

    ``None`` when it cannot be computed — the feed gave no delta, or spot
    or premium is non-positive. Callers must treat that as "unknown", never
    as zero or as safe: a missing delta is exactly the case where we cannot
    tell a 6x contract from a 100x one.

    ``delta`` is taken as a magnitude, so a put's negative delta gives the
    same positive leverage as the equivalent call.
    """
    if delta is None or spot <= 0 or premium <= 0:
        return None
    lev = abs(float(delta)) * spot / premium
    return lev if lev > 0 else None


def stop_implied_underlying_move_pct(
    *, delta: float | None, spot: float, premium: float, stop_loss_pct: float
) -> float | None:
    """How far the UNDERLYING must move against us for the premium stop to
    fire. ``None`` when leverage is unknown.

    This is the number the stop actually means. It is what makes a -40%
    stop reasonable on one contract and pure noise on another, and it is
    directly comparable against the distance to a structural invalidation
    level — if this is the smaller of the two, the trade will be stopped
    out before its thesis is ever tested.
    """
    lev = effective_leverage(delta=delta, spot=spot, premium=premium)
    if lev is None or stop_loss_pct <= 0:
        return None
    return abs(stop_loss_pct) / lev
