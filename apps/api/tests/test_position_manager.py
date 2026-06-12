"""Position-manager exit-condition tests — pure logic, mocked session.

The broker-touching close path follows the executor's already-tested
plumbing; what must be pinned here is WHEN the agent decides to close:

  - time stop fires at the proposal's disclosed horizon, not before
  - a newer council SELL on the same symbol fires the signal exit
  - manual-mode positions are never selected (query-level, asserted via
    the worker's filter in integration; here we pin the per-decision rule)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services.position_manager import _exit_reason

NOW = datetime(2026, 6, 12, 15, 0, tzinfo=UTC)


def _decision(*, days_held: int, time_stop_days: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        symbol="NVDA",
        horizon="short",
        proposal={"timeStopDays": time_stop_days},
        user_responded_at=NOW - timedelta(days=days_held),
        triggered_at=NOW - timedelta(days=days_held),
    )


def _session(newer_sell_exists: bool) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(
        return_value=uuid.uuid4() if newer_sell_exists else None
    )
    session.execute = AsyncMock(return_value=result)
    return session


async def test_time_stop_fires_at_horizon() -> None:
    reason = await _exit_reason(_session(False), _decision(days_held=5), NOW)
    assert reason == "agent_time"


async def test_no_exit_before_horizon_without_signal() -> None:
    reason = await _exit_reason(_session(False), _decision(days_held=2), NOW)
    assert reason is None


async def test_newer_council_sell_fires_signal_exit() -> None:
    reason = await _exit_reason(_session(True), _decision(days_held=2), NOW)
    assert reason == "agent_signal"


async def test_time_stop_wins_over_signal_check() -> None:
    """At horizon, the time stop is reported even if a signal also exists —
    the labels matter for the audit trail."""
    reason = await _exit_reason(_session(True), _decision(days_held=9), NOW)
    assert reason == "agent_time"


async def test_old_proposals_without_time_stop_use_horizon_fallback() -> None:
    decision = _decision(days_held=5)
    decision.proposal = {}  # pre-0009 proposal shape
    reason = await _exit_reason(_session(False), decision, NOW)
    assert reason == "agent_time"  # 'short' horizon → 5d fallback
