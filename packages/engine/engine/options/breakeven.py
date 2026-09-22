"""How far must the underlying move for a long option to break even? Pure.

The missing gate from docs/PLAN_ENTRY_EDGE.md. Every strategy here
predicts DIRECTION, and a long option needs MAGNITUDE and SPEED as well.
Measured on the recorded book, three right calls lost double digits
because the move was too small:

    GILD155 call  underlying +0.21%  ->  option -23.5%
    GILD150 call  underlying +0.64%  ->  option -13.8%
    AMD     put   underlying -1.45%  ->  option -10.1%

`required_move_pct` answers the question with the same Black-Scholes the
backtest prices with. It finds the underlying move, in the thesis
direction, that lets the option be SOLD after `hold_days` (theta paid, IV
unchanged, exit at mark less the half-spread) for what was PAID at entry.
That single number carries theta, the spread in both directions, and the
contract's leverage, so none of them needs its own rule.

`expected_move_pct` is what the stock typically does over the same hold:
the mean ABSOLUTE move, E|dS/S| = sigma * sqrt(t) * sqrt(2/pi), from
realized (not implied) vol. That is what a correct thesis earns on an
ordinary day, not a lucky one. Realized, because when IV is rich against
how the stock actually moves, the breakeven sits far out and the gate
should notice. Pricing with IV and judging with IV would cancel exactly
the information this exists to catch.
"""

from __future__ import annotations

import math
from typing import Literal

from engine.options.pricing import price

TYPICAL_ABS_MOVE_FACTOR = math.sqrt(2.0 / math.pi)
"""E|Z| for a standard normal, ~0.798. The ratio of the mean absolute move
to one standard deviation."""


def required_move_pct(
    *,
    spot: float,
    strike: float,
    kind: Literal["call", "put"],
    dte_days: int,
    iv: float,
    paid: float,
    half_spread_pct: float,
    hold_days: int,
) -> float | None:
    """Percent move of the underlying, in the thesis direction, at which
    the position breaks even after `hold_days` calendar days. None when it
    cannot be priced, or when no move within 3x of spot recovers the
    premium (a contract that expires inside the hold, say)."""
    if spot <= 0 or strike <= 0 or iv <= 0 or paid <= 0 or dte_days <= 0:
        return None
    t_exit = max(0.0, (dte_days - hold_days) / 365.0)
    keep = 1.0 - half_spread_pct / 100.0

    def proceeds(s: float) -> float:
        return price(s, strike, t_exit, iv, kind=kind) * keep

    if proceeds(spot) >= paid:
        return 0.0
    # Search in the thesis direction only: up for a call, down for a put.
    lo, hi = (spot, spot * 3.0) if kind == "call" else (spot * (1 / 3.0), spot)
    if kind == "call" and proceeds(hi) < paid:
        return None
    if kind == "put" and proceeds(lo) < paid:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if (proceeds(mid) >= paid) == (kind == "call"):
            hi = mid
        else:
            lo = mid
    breakeven = 0.5 * (lo + hi)
    return abs(breakeven / spot - 1.0) * 100.0


def expected_move_pct(*, realized_vol_pct: float, hold_days: int) -> float | None:
    """Mean absolute % move over `hold_days` CALENDAR days, from annualised
    realized vol in percent. Time is in calendar years (hold/365), the same
    clock `required_move_pct` decays theta on, so the two sides of the
    comparison can't disagree about how long the hold is."""
    if realized_vol_pct <= 0 or hold_days <= 0:
        return None
    return realized_vol_pct * math.sqrt(hold_days / 365.0) * TYPICAL_ABS_MOVE_FACTOR
