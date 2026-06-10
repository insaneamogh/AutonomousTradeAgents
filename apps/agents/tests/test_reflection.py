"""Reflection Agent tests.

All mock-LLM. The Reflection loop is deterministic-shaped under the mock
(see ``llm.py``'s "you are the reflection agent" branch) so we can assert
on the wiring: bounded deltas, decisions get marked reviewed, the Selector
reads priors when they're present.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_agents.llm import LLM
from trading_agents.memory import (
    DecisionEntry,
    InMemoryDecisionLog,
    InMemoryStrategyConfidenceStore,
)
from trading_agents.memory.strategy_confidence import (
    MAX_CONFIDENCE,
    MAX_CONFIDENCE_DELTA_PER_CYCLE,
    MIN_CONFIDENCE,
)
from trading_agents.nodes import reflection_agent_run
from trading_agents.runtime import run_council


def _completed_decision(
    *,
    symbol: str = "NVDA",
    strategy: str = "momentum",
    pnl: float = 100.0,
    triggered_at: datetime | None = None,
) -> DecisionEntry:
    return DecisionEntry(
        symbol=symbol,
        horizon="short",
        triggered_at=triggered_at or datetime.now(timezone.utc) - timedelta(hours=6),
        regime="bull",
        selected_strategy=strategy,
        selector_confidence=0.6,
        selector_rationale="seed",
        final_action="BUY",
        risk_approved=True,
        technical_score=65.0,
        fundamental_score=58.0,
        macro_score=60.0,
        fill_qty=10,
        fill_avg_price=200.0,
        realized_pnl=pnl,
        raw_state={"proposal": {"bull_case": "test bull", "bear_case": "test bear"}},
    )


# ─────────────────────────────────────────────────────────────────────
# Reflection Agent — bounded delta + reviewed_at
# ─────────────────────────────────────────────────────────────────────


async def test_reflection_marks_decisions_reviewed_and_applies_bounded_delta() -> None:
    llm = LLM(api_key=None)
    decision_log = InMemoryDecisionLog()
    confidence_store = InMemoryStrategyConfidenceStore()

    seeded = await decision_log.record(_completed_decision(pnl=120.0))
    await decision_log.record(_completed_decision(symbol="AAPL", pnl=80.0))
    await decision_log.record(_completed_decision(symbol="META", pnl=-50.0))

    before = await confidence_store.get("momentum")
    assert before.confidence == pytest.approx(0.5)

    summary = await reflection_agent_run(
        llm=llm,
        decision_log=decision_log,
        confidence_store=confidence_store,
    )

    # Reviewed all 3 decisions.
    assert summary["reviewed"] == 3
    assert "momentum" in summary["per_strategy"]

    per = summary["per_strategy"]["momentum"]
    # The mock branch returns delta=0.04; the store clamps it under the per-cycle cap
    # so the final delta lands inside ±MAX_CONFIDENCE_DELTA_PER_CYCLE.
    assert abs(per["delta_applied"]) <= MAX_CONFIDENCE_DELTA_PER_CYCLE
    # Lessons came through.
    assert per["lessons"]
    # Confidence increased (mock returns +delta).
    assert per["confidence_after"] > per["confidence_before"]

    # All 3 decisions marked reviewed.
    all_decisions = await decision_log.all_decisions()
    assert all(d.reviewed_at is not None for d in all_decisions)

    # Second run: no pending decisions → no-op (and no double-application).
    summary2 = await reflection_agent_run(
        llm=llm,
        decision_log=decision_log,
        confidence_store=confidence_store,
    )
    assert summary2["reviewed"] == 0

    after_second = await confidence_store.get("momentum")
    after_first = (await confidence_store.all())[0]  # NOTE: store seeds in registry order
    # Same confidence — no second nudge.
    assert (
        after_second.confidence
        == pytest.approx(per["confidence_after"], abs=1e-9)
    ), "Reflection re-ran on already-reviewed decisions — idempotence broken."


async def test_reflection_skips_decisions_without_realized_pnl() -> None:
    """Decisions with realized_pnl=None aren't yet closed; Reflection must skip them."""
    llm = LLM(api_key=None)
    decision_log = InMemoryDecisionLog()
    confidence_store = InMemoryStrategyConfidenceStore()

    open_entry = _completed_decision(pnl=0.0)
    open_entry.realized_pnl = None  # still open
    await decision_log.record(open_entry)

    summary = await reflection_agent_run(
        llm=llm,
        decision_log=decision_log,
        confidence_store=confidence_store,
    )
    assert summary["reviewed"] == 0
    # No write happened — confidence still at the seeded prior.
    row = await confidence_store.get("momentum")
    assert row.confidence == pytest.approx(0.5)


async def test_reflection_skips_hold_decisions() -> None:
    """HOLDs have selected_strategy=None; reflection has no strategy to score them against."""
    llm = LLM(api_key=None)
    decision_log = InMemoryDecisionLog()
    confidence_store = InMemoryStrategyConfidenceStore()

    hold = _completed_decision(pnl=0.0)
    hold.selected_strategy = None
    hold.final_action = "HOLD"
    await decision_log.record(hold)

    summary = await reflection_agent_run(
        llm=llm,
        decision_log=decision_log,
        confidence_store=confidence_store,
    )
    # No per-strategy bucket created; nothing was reviewed.
    assert summary["per_strategy"] == {}


# ─────────────────────────────────────────────────────────────────────
# StrategyConfidenceStore — delta + abs clamping
# ─────────────────────────────────────────────────────────────────────


async def test_strategy_confidence_clamps_per_cycle_delta() -> None:
    """Apply a wild +1.0 delta; store must clamp it to MAX_CONFIDENCE_DELTA_PER_CYCLE."""
    store = InMemoryStrategyConfidenceStore()
    before = await store.get("momentum")
    after = await store.apply_delta("momentum", confidence_delta=1.0)
    assert after.confidence - before.confidence == pytest.approx(MAX_CONFIDENCE_DELTA_PER_CYCLE)


async def test_strategy_confidence_clamps_to_abs_bounds() -> None:
    """Repeatedly nudge a strategy positive; it must cap at MAX_CONFIDENCE, never exceed."""
    store = InMemoryStrategyConfidenceStore()
    for _ in range(20):  # more than enough to saturate at 0.10/cycle
        await store.apply_delta("momentum", confidence_delta=0.10)
    row = await store.get("momentum")
    assert row.confidence == pytest.approx(MAX_CONFIDENCE)

    # And the other way.
    for _ in range(20):
        await store.apply_delta("momentum", confidence_delta=-0.10)
    row = await store.get("momentum")
    assert row.confidence == pytest.approx(MIN_CONFIDENCE)


# ─────────────────────────────────────────────────────────────────────
# Runtime + Reflection round-trip
# ─────────────────────────────────────────────────────────────────────


async def test_run_council_writes_decision_when_log_provided() -> None:
    """End-to-end: council with a decision_log must persist one row + return the id."""
    llm = LLM(api_key=None)
    decision_log = InMemoryDecisionLog()
    confidence_store = InMemoryStrategyConfidenceStore()

    result = await run_council(
        symbol="NVDA",
        llm=llm,
        decision_log=decision_log,
        confidence_store=confidence_store,
    )

    assert result["decision_id"] is not None
    all_d = await decision_log.all_decisions()
    assert len(all_d) == 1
    recorded = all_d[0]
    assert recorded.symbol == "NVDA"
    # The mock Selector picks momentum at 0.58; the runtime captures it.
    assert recorded.selected_strategy == "momentum"
    assert recorded.selector_confidence == pytest.approx(0.58)
    assert recorded.final_action in ("BUY", "SELL", "HOLD", "VETOED")


async def test_run_council_omits_decision_when_log_not_provided() -> None:
    """No log = no write. The opt-in stays opt-in."""
    llm = LLM(api_key=None)
    result = await run_council(symbol="NVDA", llm=llm)
    assert result["decision_id"] is None


async def test_selector_prompt_includes_priors_when_present(monkeypatch) -> None:
    """When the runtime injects strategy_priors, the Selector node must surface
    them in its user prompt — otherwise the LLM can't weigh them.

    We intercept the LLM ``complete`` call to capture the user prompt then
    assert on its content.
    """
    captured: dict[str, str] = {}

    real_complete = LLM.complete

    async def _spy_complete(self, *, system, user, model, max_tokens, cache_system=True):
        if "you are the strategy selector" in system[:120].lower():
            captured["user"] = user
        return await real_complete(self, system=system, user=user, model=model,
                                   max_tokens=max_tokens, cache_system=cache_system)

    monkeypatch.setattr(LLM, "complete", _spy_complete)

    llm = LLM(api_key=None)
    decision_log = InMemoryDecisionLog()
    confidence_store = InMemoryStrategyConfidenceStore()
    # Nudge one strategy so the priors render with non-uniform values.
    await confidence_store.apply_delta("momentum", confidence_delta=0.08)

    await run_council(
        symbol="NVDA",
        llm=llm,
        decision_log=decision_log,
        confidence_store=confidence_store,
    )

    assert "user" in captured, "Selector LLM call was never made."
    assert "Strategy priors" in captured["user"]
    assert "momentum:" in captured["user"]
