"""DTE math — the one place every options rule that cares about time-to-
expiry computes it.

``OptionLegDetails.dte`` is deliberately NOT a stored field (see that
type's docstring in ``engine.risk.types``) — every rule recomputes it
fresh from ``expiry`` + the evaluation clock, so a contract drafted at 8
DTE that is re-risk-checked days later at 3 DTE is never silently
under-protected by a stale precomputed number.

``now`` follows the SAME now-injection convention as
``engine.risk.rules.mis_square_off`` (``context.now_utc`` when the caller
injected one, else the real wall clock) — grep that module for the
pattern this mirrors. Phase 0/1 simplification, called out rather than
hidden: DTE is a plain calendar-day difference against whatever tzinfo
``now`` carries (UTC in production), not a NY-market-calendar count.
Phase 1.5 can swap this for ``pandas_market_calendars`` the same way
``wash_sale``'s lookback is already flagged to.
"""

from __future__ import annotations

from datetime import date, datetime


def dte(expiry: date, now: datetime) -> int:
    """Calendar days from ``now``'s date to ``expiry``. 0 on expiry day
    itself; negative once the contract has already expired."""
    return (expiry - now.date()).days


def is_expiry_day(expiry: date, now: datetime) -> bool:
    """True on the contract's own expiry date (``dte`` == 0)."""
    return dte(expiry, now) == 0
