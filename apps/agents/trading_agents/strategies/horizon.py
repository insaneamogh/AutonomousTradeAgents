"""How long a strategy's thesis is supposed to take to play out.

Nothing in this system ever stated one, and the consequence is the single
largest defect found so far: **the signals predict months and the positions
are held 2.1 days.**

    _momentum        ret_252d_pct (12 months) + ret_63d_pct (3 months)
    _sma_crossover   20-day and 50-day moving averages
                              |
              measured average hold:  2.1 days

`_momentum` is textbook 12-1 momentum — the Jegadeesh-Titman factor,
documented to work over 3-12 month holding periods. Held for two days it is
not merely edgeless: short-horizon daily returns show *reversal*, so a
medium-horizon momentum signal traded over two days points into the wrong
regime. That predicts the 6/17 = 35% directional accuracy we measured far
better than any of the mechanical hypotheses tested and rejected (tighter
stops, scale-out, breakeven stops, structural exits, leverage caps,
ATR-normalised stop distance).

And it is not a corner of the book: of 1,427 strategy selections, 1,208
(85%) were trend strategies, while `rsi_mean_reversion` — the only one whose
natural horizon matches a two-day hold — was 33 of them.

**Each number below is the strategy's OWN measurement window, not a
preference.** A signal built from a 50-day moving average cannot resolve in
two days; one built from RSI-14 extremes is not a three-month thesis. Read
the scorer in `fit.py` before changing any of them.
"""

from __future__ import annotations

_HORIZON_DAYS: dict[str, int] = {
    # 20/50-day moving averages. A crossover resolves over the slower of
    # the two, not over the next session.
    "sma_crossover": 20,
    # RSI-14 extremes with a reversal candle: the snapback is the thesis,
    # and it is days. The ONLY strategy whose horizon matches how we have
    # actually been holding — and the least-selected, at 2.3%.
    "rsi_mean_reversion": 5,
    # 252-day and 63-day trailing returns. The literature's holding period
    # for this factor is 3-12 months; 60 trading days is already the short
    # end of defensible, chosen because a paper account cannot sit in one
    # option for a quarter.
    "momentum": 60,
    # Donchian 20-day channel break, volume-confirmed. The channel defines
    # the window.
    "breakout": 20,
    # Compression resolving into expansion, keyed on ATR z-score and NR7 /
    # inside-bar patterns. The compression is measured in days; the
    # expansion that follows runs a few weeks.
    "vol_regime_switch": 15,
}

_DEFAULT_HORIZON_DAYS = 20
"""Used only for a strategy id not in the table. Deliberately NOT the
shortest value: an unknown strategy defaulting to a two-day hold is how the
current mismatch would quietly reappear."""


def strategy_horizon_days(strategy_id: str | None) -> int:
    """Trading days the thesis needs, from the strategy's own window.

    Never returns None — every proposal must carry a horizon, and a missing
    one is what let a 12-month signal be held for two days without anything
    in the system noticing the contradiction.
    """
    if not strategy_id:
        return _DEFAULT_HORIZON_DAYS
    return _HORIZON_DAYS.get(str(strategy_id).strip().lower(), _DEFAULT_HORIZON_DAYS)


_TRADING_DAYS_PER_WEEK = 5
_CALENDAR_DAYS_PER_WEEK = 7


def horizon_calendar_days(strategy_id: str | None) -> int:
    """The horizon converted to CALENDAR days, for comparing against DTE.

    The units genuinely differ and mixing them is a real error, not a
    rounding one: 60 trading days is ~84 calendar days, so comparing a
    trading-day horizon directly against an expiry date understates the
    contract a thesis needs by a fifth.
    """
    trading = strategy_horizon_days(strategy_id)
    return -(-trading * _CALENDAR_DAYS_PER_WEEK // _TRADING_DAYS_PER_WEEK)


def is_horizon_consistent(*, strategy_id: str | None, dte: int | None) -> bool:
    """Can a contract with this DTE actually hold the thesis to resolution?

    An option that expires before its thesis is due is a bet on timing, not
    on the thesis — and that is the shape of the recorded book: 30-DTE
    contracts bought on signals whose own window is months.

    `dte` is CALENDAR days (what `OptionLegDetails.expiry` yields); the
    horizon is converted to match.

    `None` returns True — unknown is not a veto, and a contract with no
    usable expiry is already refused by name elsewhere (`min_dte`).
    """
    if dte is None:
        return True
    return dte >= horizon_calendar_days(strategy_id)
