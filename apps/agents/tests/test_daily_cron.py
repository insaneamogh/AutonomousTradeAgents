"""Daily-cron tests.

Idempotency contract: a second run on the same UTC day with the same
(user, symbol) MUST skip the council call. We assert that by counting
calls into a stubbed ``run_council``.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Add the cron script's parent dir to the path so we can import it as a module.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def _reset_decision_log() -> None:
    from trading_agents.memory import reset_memory_stores_for_tests

    reset_memory_stores_for_tests()


async def test_skip_when_already_decided_today(monkeypatch) -> None:
    """Second call for (user, NVDA) on the same day must skip."""
    import daily_cron

    from trading_agents.memory import DecisionEntry, get_decision_log

    user_id = "00000000-0000-0000-0000-000000000001"

    # Seed a decision dated today.
    log = get_decision_log()
    await log.record(
        DecisionEntry(
            user_id=user_id,
            symbol="NVDA",
            horizon="short",
            triggered_at=datetime.now(timezone.utc),
            selected_strategy="momentum",
            selector_confidence=0.6,
            final_action="BUY",
        )
    )

    called = 0

    async def fake_run_council(**kwargs):
        nonlocal called
        called += 1
        return {"final_action": "BUY", "selected_strategy": "momentum",
                "selector_confidence": 0.6, "decision_id": "dec-xxx"}

    monkeypatch.setattr(daily_cron, "run_council", fake_run_council)

    rc = await daily_cron.main(user_id, ["NVDA"], force=False)
    assert rc == 0
    assert called == 0  # council never called — pre-existing row blocked it


async def test_force_runs_even_when_already_decided(monkeypatch) -> None:
    """--force overrides the idempotency check."""
    import daily_cron

    from trading_agents.memory import DecisionEntry, get_decision_log

    user_id = "00000000-0000-0000-0000-000000000001"

    log = get_decision_log()
    await log.record(
        DecisionEntry(
            user_id=user_id,
            symbol="AAPL",
            horizon="short",
            triggered_at=datetime.now(timezone.utc),
            selected_strategy="momentum",
            selector_confidence=0.6,
            final_action="BUY",
        )
    )

    called = 0

    async def fake_run_council(**kwargs):
        nonlocal called
        called += 1
        return {"final_action": "BUY", "selected_strategy": "momentum",
                "selector_confidence": 0.6, "decision_id": "dec-yyy"}

    monkeypatch.setattr(daily_cron, "run_council", fake_run_council)

    await daily_cron.main(user_id, ["AAPL"], force=True)
    assert called == 1


async def test_prior_day_does_not_block(monkeypatch) -> None:
    """A decision from yesterday should NOT block today's run."""
    import daily_cron

    from trading_agents.memory import DecisionEntry, get_decision_log

    user_id = "00000000-0000-0000-0000-000000000001"

    log = get_decision_log()
    await log.record(
        DecisionEntry(
            user_id=user_id,
            symbol="MSFT",
            horizon="short",
            triggered_at=datetime.now(timezone.utc) - timedelta(days=2),
            selected_strategy="momentum",
            selector_confidence=0.6,
            final_action="BUY",
        )
    )

    called = 0

    async def fake_run_council(**kwargs):
        nonlocal called
        called += 1
        return {"final_action": "BUY", "selected_strategy": "momentum",
                "selector_confidence": 0.6, "decision_id": "dec-zzz"}

    monkeypatch.setattr(daily_cron, "run_council", fake_run_council)

    await daily_cron.main(user_id, ["MSFT"], force=False)
    assert called == 1


async def test_continues_past_per_symbol_failures(monkeypatch, caplog) -> None:
    """One symbol throwing must NOT stop the rest of the watchlist."""
    import daily_cron

    user_id = "00000000-0000-0000-0000-000000000001"

    calls: list[str] = []

    async def fake_run_council(**kwargs):
        calls.append(kwargs["symbol"])
        if kwargs["symbol"] == "BROKE":
            raise RuntimeError("simulated council failure")
        return {"final_action": "BUY", "selected_strategy": "momentum",
                "selector_confidence": 0.5, "decision_id": "dec-ok"}

    monkeypatch.setattr(daily_cron, "run_council", fake_run_council)

    rc = await daily_cron.main(user_id, ["GOOD1", "BROKE", "GOOD2"], force=False)
    # All three were attempted.
    assert calls == ["GOOD1", "BROKE", "GOOD2"]
    # Return code reflects the failure but the loop completed.
    assert rc == 1
