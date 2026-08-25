"""US (NYSE) trading-day gate.

Primary source is ``pandas_market_calendars`` (the XNYS calendar) — the
authoritative, self-updating holiday schedule. When that package isn't
installed (e.g. a slim runtime that hasn't ``uv sync``'d it) we fall back
to the static full-closure table below, and if the requested year is also
outside that table we fail OPEN (report the weekday as a trading day).

Rationale for fail-open: running the council on a surprise holiday wastes
one cron pass (proposals expire unseen); silently skipping a real trading
day loses a live trading day — the worse failure.

Early-close days (day after Thanksgiving, Christmas Eve) count as TRADING
days — a daily-bar swing product only cares whether a close prints.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from datetime import time as _dtime
from zoneinfo import ZoneInfo

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
        # 2028 (Jan 1 is a Saturday → no NYSE observance; weekend rule covers it)
        date(2028, 1, 17),   # Martin Luther King Jr. Day
        date(2028, 2, 21),   # Washington's Birthday
        date(2028, 4, 14),   # Good Friday
        date(2028, 5, 29),   # Memorial Day
        date(2028, 6, 19),   # Juneteenth
        date(2028, 7, 4),    # Independence Day
        date(2028, 9, 4),    # Labor Day
        date(2028, 11, 23),  # Thanksgiving
        date(2028, 12, 25),  # Christmas
        # 2029 New Year (Jan 1 is a Monday) so the Dec→Jan rollover is covered
        date(2029, 1, 1),    # New Year's Day
    }
)

_COVERED_YEARS = frozenset(d.year for d in US_MARKET_HOLIDAYS)

# Lazily-built (valid_days_set, min_date, max_date) from pandas_market_calendars,
# or None when the package isn't importable. Sentinel ``False`` = not yet tried.
_MCAL_CACHE: object = False


def _mcal_valid_days() -> tuple[frozenset[date], date, date] | None:
    """Trading days from the XNYS calendar over a wide window, cached once.
    Returns None if pandas_market_calendars isn't installed / errors."""
    global _MCAL_CACHE
    if _MCAL_CACHE is not False:
        return _MCAL_CACHE  # type: ignore[return-value]
    try:
        import pandas_market_calendars as mcal

        cal = mcal.get_calendar("XNYS")
        start, end = date(2024, 1, 1), date(2031, 12, 31)
        idx = cal.valid_days(start_date=start.isoformat(), end_date=end.isoformat())
        days = frozenset(ts.date() for ts in idx)
        _MCAL_CACHE = (days, start, end)
        logger.info("market_calendar: using pandas_market_calendars XNYS (%d days cached)", len(days))
    except Exception as exc:  # any failure → static fallback
        logger.info("market_calendar: pandas_market_calendars unavailable (%s) — static table", exc)
        _MCAL_CACHE = None
    return _MCAL_CACHE  # type: ignore[return-value]


def is_us_trading_day(d: date) -> bool:
    """True when NYSE prints a daily close on ``d``."""
    if d.weekday() >= 5:  # Saturday / Sunday — cheap short-circuit
        return False

    mcal = _mcal_valid_days()
    if mcal is not None:
        days, lo, hi = mcal
        if lo <= d <= hi:
            return d in days
        # Outside the cached window — fall through to the static table.

    if d.year not in _COVERED_YEARS:
        logger.warning(
            "market_calendar: %s not in the holiday table (covered: %s) and "
            "pandas_market_calendars unavailable — treating as OPEN.",
            d.year, sorted(_COVERED_YEARS),
        )
        return True
    return d not in US_MARKET_HOLIDAYS


# ─────────────────────────────────────────────────────────────────────
# Intraday session gate
#
# ``is_us_trading_day`` answers "does a close print today". The continuous
# scanner needs the narrower question: "is the tape live RIGHT NOW". Those
# are different gates — scanning a closed market re-reads yesterday's bars
# and can fire triggers on stale data.
# ─────────────────────────────────────────────────────────────────────

_NY = "America/New_York"

# Regular NYSE session in exchange-local time.
REGULAR_OPEN = _dtime(9, 30)
REGULAR_CLOSE = _dtime(16, 0)

# NOTE on early closes (day after Thanksgiving, Christmas Eve — 13:00 ET):
# pandas_market_calendars knows them exactly. The static fallback does not,
# so it fails OPEN for the extra three hours. Consequence of the fallback
# being wrong: the scanner polls a closed tape, sees bars that haven't
# moved, and fires nothing. Wasted requests, never a wrong trade.
#
# Lazily-built {date: (open_utc, close_utc)} from pandas_market_calendars.
# Sentinel ``False`` = not yet tried, ``None`` = package unavailable.
_SCHEDULE_CACHE: object = False


def _mcal_schedule() -> dict[date, tuple[datetime, datetime]] | None:
    """Exact per-day session bounds in UTC, cached once. None without mcal."""
    global _SCHEDULE_CACHE
    if _SCHEDULE_CACHE is not False:
        return _SCHEDULE_CACHE  # type: ignore[return-value]
    try:
        import pandas_market_calendars as mcal

        cal = mcal.get_calendar("XNYS")
        start, end = date(2024, 1, 1), date(2031, 12, 31)
        sched = cal.schedule(start_date=start.isoformat(), end_date=end.isoformat())
        out: dict[date, tuple[datetime, datetime]] = {}
        for ts, row in sched.iterrows():
            out[ts.date()] = (
                row["market_open"].to_pydatetime().astimezone(UTC),
                row["market_close"].to_pydatetime().astimezone(UTC),
            )
        _SCHEDULE_CACHE = out
        logger.info("market_calendar: XNYS intraday schedule cached (%d sessions)", len(out))
    except Exception as exc:
        logger.info(
            "market_calendar: intraday schedule unavailable (%s) — "
            "static 09:30-16:00 ET fallback (early closes will read as open)",
            exc,
        )
        _SCHEDULE_CACHE = None
    return _SCHEDULE_CACHE  # type: ignore[return-value]


def us_market_session_bounds(d: date) -> tuple[datetime, datetime] | None:
    """(open, close) in UTC for ``d``, or None when the market is shut.

    Uses the XNYS schedule when available — that is the only source that
    knows about 13:00 early closes. Otherwise 09:30-16:00 America/New_York
    on any day ``is_us_trading_day`` calls open, which correctly handles
    the DST shift because it converts from a real tz-aware local time.
    """
    sched = _mcal_schedule()
    if sched is not None:
        return sched.get(d)

    if not is_us_trading_day(d):
        return None
    tz = ZoneInfo(_NY)
    open_local = datetime.combine(d, REGULAR_OPEN, tzinfo=tz)
    close_local = datetime.combine(d, REGULAR_CLOSE, tzinfo=tz)
    return open_local.astimezone(UTC), close_local.astimezone(UTC)


def is_us_market_open(now: datetime) -> bool:
    """True when ``now`` (any tz-aware instant) falls inside the regular session.

    Pre-market and after-hours read as CLOSED: the scanner's triggers are
    built from IEX consolidated bars, whose extended-hours prints are thin
    enough that a single odd-lot can manufacture a gap or a channel break.
    """
    now_utc = now.astimezone(UTC)
    bounds = us_market_session_bounds(now_utc.astimezone(ZoneInfo(_NY)).date())
    if bounds is None:
        return False
    open_utc, close_utc = bounds
    return open_utc <= now_utc < close_utc


def minutes_until_us_market_open(now: datetime) -> float | None:
    """Minutes until the next regular open, or None when already open.

    Looks ahead up to 10 days so a long weekend plus a holiday still
    resolves. Returns None when the market is open right now.
    """
    now_utc = now.astimezone(UTC)
    if is_us_market_open(now_utc):
        return None
    probe = now_utc.astimezone(ZoneInfo(_NY)).date()
    for _ in range(11):
        bounds = us_market_session_bounds(probe)
        if bounds is not None and bounds[0] > now_utc:
            return (bounds[0] - now_utc).total_seconds() / 60.0
        probe += timedelta(days=1)
    return None
