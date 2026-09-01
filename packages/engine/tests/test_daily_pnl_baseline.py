"""The session-P&L baseline.

Pinned here because ``_daily_pnl`` feeds two very different consumers: the
number the dashboard shows, and ``reconciler.breaker``'s -3%
``daily_drawdown_halt_pct`` trip. A baseline that understates the day's
loss understates it for the halt too.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from engine.reconciler.snapshot import _daily_pnl


def _session_with_first_snapshot(equity: float | None) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    row = None if equity is None else MagicMock(account_equity=equity)
    result.scalar_one_or_none = MagicMock(return_value=row)
    session.execute = AsyncMock(return_value=result)
    return session


async def test_broker_prior_close_is_preferred_over_the_utc_day_snapshot() -> None:
    """The live case, 2026-09-01 08:16 ET: Alpaca said equity 100297.33
    against last_equity 100871.17 (a real -573.84 session), while the
    earliest snapshot bearing today's UTC date still held 100244.00 —
    written at 02:00 UTC, i.e. before the session even opened. The
    snapshot baseline turns a -0.57% day into a +0.05% one."""
    session = _session_with_first_snapshot(100_244.00)

    pnl, pct = await _daily_pnl(
        session,
        user_id=uuid.uuid4(),
        current_equity=100_297.33,
        prior_close_equity=100_871.17,
    )

    assert round(pnl, 2) == -573.84
    assert round(pct, 2) == -0.57


async def test_falls_back_to_the_first_snapshot_when_the_broker_has_no_baseline() -> None:
    session = _session_with_first_snapshot(100_000.00)

    pnl, pct = await _daily_pnl(
        session, user_id=uuid.uuid4(), current_equity=101_000.00, prior_close_equity=None
    )

    assert round(pnl, 2) == 1000.00
    assert round(pct, 2) == 1.00


async def test_a_nonpositive_broker_baseline_is_ignored_not_divided_by() -> None:
    session = _session_with_first_snapshot(100_000.00)

    pnl, _ = await _daily_pnl(
        session, user_id=uuid.uuid4(), current_equity=101_000.00, prior_close_equity=0.0
    )

    assert round(pnl, 2) == 1000.00


async def test_no_baseline_anywhere_reports_zero_rather_than_inventing_one() -> None:
    session = _session_with_first_snapshot(None)

    assert await _daily_pnl(
        session, user_id=uuid.uuid4(), current_equity=100_297.33, prior_close_equity=None
    ) == (0.0, 0.0)
