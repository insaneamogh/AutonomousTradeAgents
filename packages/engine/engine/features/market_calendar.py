"""US (NYSE) trading-day gate — deterministic, no external dependency.

Weekends + the static full-closure holiday list below. Early-close days
(day after Thanksgiving, Christmas Eve) count as TRADING days — a daily-bar
swing product only cares whether a close prints.

Fail-open by design: a year missing from the table logs loudly and reports
the day as open. Running the council on a surprise holiday wastes one cron
pass (proposals expire unseen); silently skipping a real trading day loses
a live trading day — the worse failure.

Swap to ``pandas_market_calendars`` when intraday (v1.5) raises the stakes.
"""

from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger("engine.features.market_calendar")

# NYSE full-closure holidays. Extend a year ahead each December.
US_MARKET_HOLIDAYS: frozenset[date] = frozenset(
    {
        # 2026
        date(2026, 1, 1),    # New Year's Day
        date(2026, 1, 19),   # Martin Luther King Jr. Day
        date(2026, 2, 16),   # Washington's Birthday
        date(2026, 4, 3),    # Good Friday
        date(2026, 5, 25),   # Memorial Day
        date(2026, 6, 19),   # Juneteenth
        date(2026, 7, 3),    # Independence Day (observed — Jul 4 is a Saturday)
        date(2026, 9, 7),    # Labor Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
        # 2027
        date(2027, 1, 1),    # New Year's Day
        date(2027, 1, 18),   # Martin Luther King Jr. Day
        date(2027, 2, 15),   # Washington's Birthday
        date(2027, 3, 26),   # Good Friday
        date(2027, 5, 31),   # Memorial Day
        date(2027, 6, 18),   # Juneteenth (observed — Jun 19 is a Saturday)
        date(2027, 7, 5),    # Independence Day (observed — Jul 4 is a Sunday)
        date(2027, 9, 6),    # Labor Day
        date(2027, 11, 25),  # Thanksgiving
        date(2027, 12, 24),  # Christmas (observed — Dec 25 is a Saturday)
    }
)

_COVERED_YEARS = frozenset(d.year for d in US_MARKET_HOLIDAYS)


def is_us_trading_day(d: date) -> bool:
    """True when NYSE prints a daily close on ``d``."""
    if d.weekday() >= 5:  # Saturday / Sunday
        return False
    if d.year not in _COVERED_YEARS:
        logger.warning(
            "market_calendar: %s not in the holiday table (covered: %s) — "
            "treating as OPEN. Extend US_MARKET_HOLIDAYS.",
            d.year, sorted(_COVERED_YEARS),
        )
        return True
    return d not in US_MARKET_HOLIDAYS
