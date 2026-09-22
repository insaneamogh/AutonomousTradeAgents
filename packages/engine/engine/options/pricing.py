"""Black-Scholes for European options on a non-dividend underlying. Pure.

`engine.options.greeks` is deliberately NOT a pricing model: live entries
read greeks off Alpaca's own snapshot, and nothing on the live path needs
to price a contract from scratch. Two things now do:

  - the options-aware backtest (tests/eval/option_backtest.py), which has
    to price a contract on days no quote was ever recorded, to learn
    whether a direction signal survives theta and spread;
  - the `expected_move_below_breakeven` gate (PLAN_PLATFORM Phase 4),
    which needs theta and the breakeven move for a contract before it is
    bought.

US single-name options are American. For the calls and puts this system
buys (10-45 DTE, near the money, no dividend modelled) the early-exercise
premium is small, and ignoring it UNDER-prices puts slightly. That makes a
put strategy look marginally better than it is, which is the direction to
remember when reading a backtest.

Units: `t_years` in years, `vol` and `rate` as decimals (0.25, not 25).
Every function returns intrinsic value, or a hard limit, at t=0 or vol=0
rather than dividing by zero.
"""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Literal

Kind = Literal["call", "put"]

_N = NormalDist()


def _d1_d2(spot: float, strike: float, t: float, vol: float, rate: float) -> tuple[float, float]:
    srt = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / srt
    return d1, d1 - srt


def price(
    spot: float, strike: float, t_years: float, vol: float, *, kind: Kind, rate: float = 0.0
) -> float:
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if t_years <= 0 or vol <= 0:
        intrinsic = spot - strike if kind == "call" else strike - spot
        return max(0.0, intrinsic)
    d1, d2 = _d1_d2(spot, strike, t_years, vol, rate)
    disc = math.exp(-rate * t_years)
    if kind == "call":
        return spot * _N.cdf(d1) - strike * disc * _N.cdf(d2)
    return strike * disc * _N.cdf(-d2) - spot * _N.cdf(-d1)


def delta(
    spot: float, strike: float, t_years: float, vol: float, *, kind: Kind, rate: float = 0.0
) -> float:
    """Call delta in (0, 1), put delta in (-1, 0)."""
    if t_years <= 0 or vol <= 0:
        itm = spot > strike if kind == "call" else spot < strike
        return (1.0 if itm else 0.0) * (1 if kind == "call" else -1)
    d1, _ = _d1_d2(spot, strike, t_years, vol, rate)
    return _N.cdf(d1) if kind == "call" else _N.cdf(d1) - 1.0


def theta_per_day(
    spot: float, strike: float, t_years: float, vol: float, *, kind: Kind, rate: float = 0.0
) -> float:
    """Premium lost per CALENDAR day with everything else unchanged.
    Negative for a long option. Finite difference, which is exact enough
    at a one-day step and cannot disagree with `price`."""
    step = 1.0 / 365.0
    later = max(0.0, t_years - step)
    return price(spot, strike, later, vol, kind=kind, rate=rate) - price(
        spot, strike, t_years, vol, kind=kind, rate=rate
    )


def strike_for_delta(
    spot: float, t_years: float, vol: float, *, kind: Kind, target_abs_delta: float,
    rate: float = 0.0,
) -> float:
    """The strike whose |delta| equals `target_abs_delta` (0, 1), in closed
    form. It inverts N(d1) directly rather than searching, so it cannot
    fail to converge."""
    if not 0.0 < target_abs_delta < 1.0:
        raise ValueError("target_abs_delta must be in (0, 1)")
    if t_years <= 0 or vol <= 0:
        return spot
    n_d1 = target_abs_delta if kind == "call" else 1.0 - target_abs_delta
    d1 = _N.inv_cdf(n_d1)
    srt = vol * math.sqrt(t_years)
    return spot * math.exp(-(d1 * srt - (rate + 0.5 * vol * vol) * t_years))


def implied_vol(
    target_price: float, spot: float, strike: float, t_years: float, *, kind: Kind,
    rate: float = 0.0, lo: float = 0.01, hi: float = 5.0,
) -> float | None:
    """Bisection. None when the price sits outside what [lo, hi] vol can
    produce (below intrinsic, or absurdly rich), never a clamped guess."""
    if t_years <= 0 or target_price <= 0:
        return None
    f_lo = price(spot, strike, t_years, lo, kind=kind, rate=rate) - target_price
    f_hi = price(spot, strike, t_years, hi, kind=kind, rate=rate) - target_price
    if f_lo > 0 or f_hi < 0:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        f_mid = price(spot, strike, t_years, mid, kind=kind, rate=rate) - target_price
        if abs(f_mid) < 1e-6:
            return mid
        if f_mid < 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
