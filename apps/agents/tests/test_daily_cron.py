"""Daily-cron tests.

Idempotency contract: a second run on the same UTC day with the same
(user, symbol) MUST skip the council call. We assert that by counting
calls into a stubbed ``run_council``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture(autouse=True)
def _reset_decision_log() -> None:
    from trading_agents.memory import reset_memory_stores_for_tests

    reset_memory_stores_for_tests()


@pytest.fixture(autouse=True)
def _force_trading_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the market-calendar gate open.

    ``main(force=False)`` short-circuits before the council on weekends and
    holidays, so without this the idempotency/resilience assertions below
    only hold Mon-Fri (and the "already decided" test passed for the wrong
    reason at the weekend). These tests cover the per-symbol loop, not the
    calendar; the gate itself has its own tests in packages/engine.

    ``daily_cron.main`` imports the helper inside the function body, so we
    patch it on the defining module rather than on daily_cron.
    """
    import engine.features

    monkeypatch.setattr(engine.features, "is_us_trading_day", lambda _d: True)


async def test_skip_when_already_decided_today(monkeypatch) -> None:
    """Second call for (user, NVDA) on the same day must skip."""
    from trading_agents.jobs import daily_cron
    from trading_agents.memory import DecisionEntry, get_decision_log

    user_id = "00000000-0000-0000-0000-000000000001"

    # Seed a decision dated today.
    log = get_decision_log()
    await log.record(
        DecisionEntry(
            user_id=user_id,
            symbol="NVDA",
            horizon="short",
            triggered_at=datetime.now(UTC),
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
    from trading_agents.jobs import daily_cron
    from trading_agents.memory import DecisionEntry, get_decision_log

    user_id = "00000000-0000-0000-0000-000000000001"

    log = get_decision_log()
    await log.record(
        DecisionEntry(
            user_id=user_id,
            symbol="AAPL",
            horizon="short",
            triggered_at=datetime.now(UTC),
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


async def test_skip_calendar_gate_still_honors_dedup(monkeypatch) -> None:
    """``skip_calendar_gate`` must NOT bypass the once-per-symbol-per-day
    dedup check — only ``--force`` does that.

    This is the regression test for the bug where the scheduler's trigger
    loop called ``main(force=True, ...)`` and that single flag silently
    bypassed BOTH the calendar gate AND this dedup guard, so a
    scanner-triggered run could re-spend LLM cost on a symbol the baseline
    sweep (or an earlier trigger) had already decided today.
    """
    from trading_agents.jobs import daily_cron
    from trading_agents.memory import DecisionEntry, get_decision_log

    user_id = "00000000-0000-0000-0000-000000000001"

    log = get_decision_log()
    await log.record(
        DecisionEntry(
            user_id=user_id,
            symbol="META",
            horizon="short",
            triggered_at=datetime.now(UTC),
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
                "selector_confidence": 0.6, "decision_id": "dec-skipcal"}

    monkeypatch.setattr(daily_cron, "run_council", fake_run_council)

    rc = await daily_cron.main(
        user_id, ["META"], force=False, skip_calendar_gate=True
    )
    assert rc == 0
    assert called == 0  # already decided today — dedup still blocked it


async def test_skip_calendar_gate_bypasses_only_the_calendar(monkeypatch) -> None:
    """``skip_calendar_gate`` DOES bypass the calendar gate, independent of
    dedup: with no existing decision and the market reported closed, the
    council still runs when ``skip_calendar_gate=True`` — proving the two
    gates are now orthogonal rather than both keyed off ``force``.
    """
    import engine.features
    from trading_agents.jobs import daily_cron

    # Override the autouse `_force_trading_day` fixture for this test only —
    # monkeypatch unwinds both changes at teardown regardless of order.
    monkeypatch.setattr(engine.features, "is_us_trading_day", lambda _d: False)

    called = 0

    async def fake_run_council(**kwargs):
        nonlocal called
        called += 1
        return {"final_action": "BUY", "selected_strategy": "momentum",
                "selector_confidence": 0.6, "decision_id": "dec-calbypass"}

    monkeypatch.setattr(daily_cron, "run_council", fake_run_council)

    user_id = "00000000-0000-0000-0000-000000000001"
    rc = await daily_cron.main(
        user_id, ["GOOG"], force=False, skip_calendar_gate=True
    )
    assert rc == 0
    assert called == 1  # market "closed", but skip_calendar_gate ran it anyway


async def test_prior_day_does_not_block(monkeypatch) -> None:
    """A decision from yesterday should NOT block today's run."""
    from trading_agents.jobs import daily_cron
    from trading_agents.memory import DecisionEntry, get_decision_log

    user_id = "00000000-0000-0000-0000-000000000001"

    log = get_decision_log()
    await log.record(
        DecisionEntry(
            user_id=user_id,
            symbol="MSFT",
            horizon="short",
            triggered_at=datetime.now(UTC) - timedelta(days=2),
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
    from trading_agents.jobs import daily_cron

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
