"""US trading-day gate — holidays, weekends, and the mcal/static agreement.

These assertions hold on BOTH code paths (pandas_market_calendars when
installed, static table otherwise) because 2026 is covered by both.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from engine.features.market_calendar import (
    is_us_market_open,
    is_us_trading_day,
    minutes_until_us_market_open,
    us_market_session_bounds,
)


def test_weekends_are_not_trading_days() -> None:
    assert is_us_trading_day(date(2026, 1, 3)) is False  # Saturday
    assert is_us_trading_day(date(2026, 1, 4)) is False  # Sunday


def test_regular_weekday_is_open() -> None:
    assert is_us_trading_day(date(2026, 1, 2)) is True   # Friday, normal session


# ─────────────────────────────────────────────────────────────────────
# Intraday session gate — the scanner's "is the tape live now" question
# ─────────────────────────────────────────────────────────────────────


def _utc(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


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


def test_minutes_until_open_skips_the_weekend() -> None:
    """Saturday afternoon → the next open is Monday, ~2 days out."""
    mins = minutes_until_us_market_open(_utc(2026, 6, 20, 15, 0))
    assert mins is not None
    assert 40 * 60 < mins < 50 * 60


def test_minutes_until_open_after_the_close_is_the_next_morning() -> None:
    mins = minutes_until_us_market_open(_utc(2026, 6, 16, 21, 0))
    assert mins is not None
    assert 15 * 60 < mins < 18 * 60


@pytest.mark.parametrize(
    ("when", "open_"),
    [
        (datetime(2026, 9, 25, 4, 0, tzinfo=UTC), True),     # 09:30 IST, a Friday session
        (datetime(2026, 9, 25, 3, 44, tzinfo=UTC), False),   # 09:14 IST, pre-open
        (datetime(2026, 9, 25, 10, 0, tzinfo=UTC), False),   # 15:30 IST, the close
        (datetime(2026, 10, 2, 5, 0, tzinfo=UTC), False),    # Gandhi Jayanti (NSE list)
        (datetime(2026, 1, 15, 5, 0, tzinfo=UTC), False),    # municipal-election closure
        (datetime(2026, 9, 26, 5, 0, tzinfo=UTC), False),    # Saturday
    ],
)
def test_nse_session_gate(when: datetime, open_: bool) -> None:
    from engine.features import is_market_open

    assert is_market_open("IN", when) is open_
    # 04:00 UTC on a US trading day is not the US session: the markets are
    # separate clocks, never one gate.
    assert is_market_open("US", datetime(2026, 9, 25, 4, 0, tzinfo=UTC)) is False
