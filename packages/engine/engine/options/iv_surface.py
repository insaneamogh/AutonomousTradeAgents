"""Constant-maturity ATM implied vol from one chain snapshot, and IV rank. Pure.

`options_context.iv_rank` / `atm_iv` / `term_structure_slope` have been
None since options shipped. There was no IV history to rank against, and
an IV rank cannot be bought retroactively on the free tier. It has to be
RECORDED, one snapshot a day, starting now. `jobs/iv_snapshot.py` does the
recording, this module the arithmetic.

ATM per expiry uses the call nearest +0.50 delta and the put nearest
-0.50 delta, averaged. Selecting by delta, not by strike versus spot, needs
no separate underlying quote, and delta is what the chain snapshot already
reports. A side further than `_ATM_DELTA_TOLERANCE` from 0.50 does not
count as ATM, so a chain with no near-the-money strikes yields nothing
rather than a wing IV passed off as ATM.

Constant maturity interpolates TOTAL VARIANCE (sigma^2 * t) linearly
between the two expiries bracketing the target. That is the standard
choice (VIX does the same) because variance, not vol, is additive in time.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

_ATM_DELTA_TOLERANCE = 0.15
_MIN_DTE = 7
"""Expiries inside a week are dominated by event and pin effects. They are
excluded from the curve rather than allowed to bend it."""
_EXTRAPOLATE_DAYS = 10
"""How far past the nearest expiry a constant-maturity point may reach
before it is reported as unknown rather than extrapolated."""

IV_RANK_WINDOW = 252
IV_RANK_MIN_OBS = 60
"""An IV rank over fewer than ~3 months of snapshots is noise dressed as a
percentile. Below this it is None."""


class _Quote(Protocol):
    contract_type: str
    expiry: date
    delta: float | None
    implied_volatility: float | None


@dataclass(frozen=True)
class IvPoint:
    dte: int
    atm_iv: float


def atm_iv_by_expiry(quotes: Iterable[_Quote], today: date) -> list[IvPoint]:
    by_expiry: dict[date, list[_Quote]] = defaultdict(list)
    for q in quotes:
        if q.delta is None or not q.implied_volatility or q.implied_volatility <= 0:
            continue
        by_expiry[q.expiry].append(q)

    points: list[IvPoint] = []
    for expiry, rows in by_expiry.items():
        dte = (expiry - today).days
        if dte < _MIN_DTE:
            continue
        ivs: list[float] = []
        for kind, target in (("call", 0.5), ("put", -0.5)):
            side = [q for q in rows if q.contract_type == kind]
            if not side:
                continue
            best = min(side, key=lambda q: abs((q.delta or 0.0) - target))
            if abs((best.delta or 0.0) - target) <= _ATM_DELTA_TOLERANCE:
                ivs.append(float(best.implied_volatility or 0.0))
        if ivs:
            points.append(IvPoint(dte=dte, atm_iv=sum(ivs) / len(ivs)))
    return sorted(points, key=lambda p: p.dte)


def constant_maturity_iv(points: list[IvPoint], target_dte: int) -> float | None:
    if not points:
        return None
    for p in points:
        if p.dte == target_dte:
            return p.atm_iv
    below = [p for p in points if p.dte < target_dte]
    above = [p for p in points if p.dte > target_dte]
    if below and above:
        lo, hi = below[-1], above[0]
        var_lo = lo.atm_iv**2 * lo.dte
        var_hi = hi.atm_iv**2 * hi.dte
        w = (target_dte - lo.dte) / (hi.dte - lo.dte)
        var_t = var_lo + w * (var_hi - var_lo)
        return math.sqrt(var_t / target_dte) if var_t > 0 else None
    nearest = below[-1] if below else above[0]
    if abs(nearest.dte - target_dte) <= _EXTRAPOLATE_DAYS:
        return nearest.atm_iv
    return None


def iv_rank(history: list[float], current: float) -> float | None:
    """Where `current` sits in the trailing window's min-max range, 0-100.
    `history` is the prior daily values, oldest first."""
    window = history[-IV_RANK_WINDOW:]
    if len(window) < IV_RANK_MIN_OBS:
        return None
    lo, hi = min(*window, current), max(*window, current)
    if hi <= lo:
        return None
    return round((current - lo) / (hi - lo) * 100.0, 1)
