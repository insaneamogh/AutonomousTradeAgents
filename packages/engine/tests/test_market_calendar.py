"""US trading-day gate — holidays, weekends, and the mcal/static agreement.

These assertions hold on BOTH code paths (pandas_market_calendars when
installed, static table otherwise) because 2026 is covered by both.
"""

from __future__ import annotations

from datetime import date

from engine.features.market_calendar import is_us_trading_day


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
