"""US trading-day gate — holidays, weekends, and the mcal/static agreement.

These assertions hold on BOTH code paths (pandas_market_calendars when
installed, static table otherwise) because 2026 is covered by both.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from engine.features.market_calendar import (
    is_us_market_open,
    is_us_trading_day,
    minutes_until_us_market_open,
    us_market_session_bounds,
)


def test_weekends_are_not_trading_days() -> None:
    assert is_us_trading_day(date(2026, 1, 3)) is False  # Saturday
    assert is_us_trading_day(date(2026, 1, 4)) is False  # Sunday


def test_new_years_day_is_closed() -> None:
    assert is_us_trading_day(date(2026, 1, 1)) is False  # New Year's Day (Thu)


def test_regular_weekday_is_open() -> None:
    assert is_us_trading_day(date(2026, 1, 2)) is True   # Friday, normal session


def test_thanksgiving_and_christmas_closed_2026() -> None:
    assert is_us_trading_day(date(2026, 11, 26)) is False  # Thanksgiving
    assert is_us_trading_day(date(2026, 12, 25)) is False  # Christmas


def test_july_3_2026_observed_closure() -> None:
    # Independence Day observed (Jul 4 2026 is a Saturday → market shut Jul 3).
    assert is_us_trading_day(date(2026, 7, 3)) is False


# ─────────────────────────────────────────────────────────────────────
# Intraday session gate — the scanner's "is the tape live now" question
# ─────────────────────────────────────────────────────────────────────


def _utc(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def test_market_is_open_mid_session_and_closed_outside_it() -> None:
    # Tue 2026-06-16. EDT → regular session is 13:30-20:00 UTC.
    assert is_us_market_open(_utc(2026, 6, 16, 15, 0)) is True
    assert is_us_market_open(_utc(2026, 6, 16, 12, 0)) is False  # pre-market
    assert is_us_market_open(_utc(2026, 6, 16, 21, 0)) is False  # after hours


def test_open_and_close_boundaries_are_half_open() -> None:
    """Open is inclusive, close is exclusive — a scan at exactly 16:00 ET
    must not run against a session that has just printed its last bar."""
    assert is_us_market_open(_utc(2026, 6, 16, 13, 30)) is True
    assert is_us_market_open(_utc(2026, 6, 16, 13, 29)) is False
    assert is_us_market_open(_utc(2026, 6, 16, 20, 0)) is False
    assert is_us_market_open(_utc(2026, 6, 16, 19, 59)) is True


def test_winter_session_shifts_with_dst() -> None:
    """EST → the same local session is 14:30-21:00 UTC. Tue 2026-01-13."""
    assert is_us_market_open(_utc(2026, 1, 13, 14, 30)) is True
    assert is_us_market_open(_utc(2026, 1, 13, 13, 30)) is False
    assert is_us_market_open(_utc(2026, 1, 13, 20, 30)) is True
    assert is_us_market_open(_utc(2026, 1, 13, 21, 0)) is False


def test_weekend_and_holiday_are_never_open() -> None:
    assert is_us_market_open(_utc(2026, 6, 20, 15, 0)) is False   # Saturday
    assert is_us_market_open(_utc(2026, 11, 26, 15, 0)) is False  # Thanksgiving
    assert us_market_session_bounds(date(2026, 11, 26)) is None


def test_session_bounds_are_utc_and_ordered() -> None:
    bounds = us_market_session_bounds(date(2026, 6, 16))
    assert bounds is not None
    open_utc, close_utc = bounds
    assert open_utc.tzinfo is not None and close_utc.tzinfo is not None
    assert open_utc < close_utc


def test_minutes_until_open_is_none_while_open() -> None:
    assert minutes_until_us_market_open(_utc(2026, 6, 16, 15, 0)) is None


def test_minutes_until_open_skips_the_weekend() -> None:
    """Saturday afternoon → the next open is Monday, ~2 days out."""
    mins = minutes_until_us_market_open(_utc(2026, 6, 20, 15, 0))
    assert mins is not None
    assert 40 * 60 < mins < 50 * 60


def test_minutes_until_open_after_the_close_is_the_next_morning() -> None:
    mins = minutes_until_us_market_open(_utc(2026, 6, 16, 21, 0))
    assert mins is not None
    assert 15 * 60 < mins < 18 * 60


def test_non_utc_input_is_handled() -> None:
    """Callers pass whatever tz they hold; the gate must normalize."""
    ny = ZoneInfo("America/New_York")
    assert is_us_market_open(datetime(2026, 6, 16, 10, 0, tzinfo=ny)) is True
    assert is_us_market_open(datetime(2026, 6, 16, 8, 0, tzinfo=ny)) is False
