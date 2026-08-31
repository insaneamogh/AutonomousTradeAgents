"""Options get a cooldown, equities keep the once-per-day lock.

Once-per-DAY is right for a swing equity position and wrong for options:
a symbol with no setup at 14:00 can be a clean one at 15:30. The daily
dedup silently capped every underlying at ONE look per session, which made
"continuous options scanning" impossible no matter how often the
deterministic scanner ran.
"""

from __future__ import annotations

import pytest

from trading_agents.jobs import daily_cron as cron


class _Log:
    def __init__(self, *, today: bool = False, minutes: float | None = None) -> None:
        self._today, self._minutes = today, minutes

    async def has_decision_today(self, *, user_id, symbol, day_utc):
        return self._today

    async def minutes_since_last_decision(self, *, user_id, symbol):
        return self._minutes


def _patch(monkeypatch: pytest.MonkeyPatch, log: _Log) -> None:
    monkeypatch.setattr(cron, "get_decision_log", lambda: log)


async def test_equity_still_locked_once_per_day(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _Log(today=True))
    assert await cron._should_skip("u", "AAPL", "equity") is True


async def test_equity_runs_when_not_yet_decided(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _Log(today=False))
    assert await cron._should_skip("u", "AAPL", "equity") is False


async def test_option_ignores_the_daily_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decided today, but the cooldown has elapsed — options must look again."""
    monkeypatch.delenv("OPTIONS_RESCAN_COOLDOWN_MINUTES", raising=False)
    _patch(monkeypatch, _Log(today=True, minutes=120.0))
    assert await cron._should_skip("u", "SPY", "option") is False


async def test_option_inside_the_cooldown_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPTIONS_RESCAN_COOLDOWN_MINUTES", raising=False)
    _patch(monkeypatch, _Log(today=True, minutes=5.0))
    assert await cron._should_skip("u", "SPY", "option") is True


async def test_option_never_looked_at_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _Log(minutes=None))
    assert await cron._should_skip("u", "SPY", "option") is False


async def test_cooldown_is_env_tunable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTIONS_RESCAN_COOLDOWN_MINUTES", "10")
    _patch(monkeypatch, _Log(minutes=20.0))
    assert await cron._should_skip("u", "SPY", "option") is False
    _patch(monkeypatch, _Log(minutes=5.0))
    assert await cron._should_skip("u", "SPY", "option") is True


async def test_malformed_cooldown_keeps_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must never remove the bound."""
    monkeypatch.setenv("OPTIONS_RESCAN_COOLDOWN_MINUTES", "not-a-number")
    assert cron._options_rescan_cooldown_minutes() == 45
    _patch(monkeypatch, _Log(minutes=5.0))
    assert await cron._should_skip("u", "SPY", "option") is True


async def test_zero_cooldown_disables_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTIONS_RESCAN_COOLDOWN_MINUTES", "0")
    _patch(monkeypatch, _Log(minutes=0.1))
    assert await cron._should_skip("u", "SPY", "option") is False
