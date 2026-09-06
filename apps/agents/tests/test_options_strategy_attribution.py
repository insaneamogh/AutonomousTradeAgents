"""Every options decision must carry the strategy that produced it.

Until 2026-09-05 it did not, and the shape of the gap was the worst
possible one. `selected_strategy` WAS populated on the equity path — 298
sma_crossover, 118 momentum — but that path almost never filled (6 fills,
0 closes). Every decision that actually carried realised P&L was an
options decision, and every one of those had `selected_strategy = NULL`.

So the Reflection worker, which exists and is wired into daily_cron, could
never attribute an outcome to a strategy even when it ran. After 742
decisions and 27 fills all five `strategy_confidence` rows were still at
their migration seed: confidence 0.500, wins 0, losses 0. The system had
no way to learn which of its five strategies makes money.
"""

from __future__ import annotations

from trading_agents.memory import DecisionEntry


def test_decision_entry_carries_selected_strategy() -> None:
    """The field the whole feedback loop hangs off. Reflection groups by
    it, `strategies_perf` aggregates by it, and `review_service` looks up
    its prior by it — all of which silently no-op on None."""
    e = DecisionEntry(
        id="x", user_id="u", symbol="NVDA", horizon="short",
        final_action="BUY", selected_strategy="momentum",
    )
    assert e.selected_strategy == "momentum"


def test_selected_strategy_defaults_to_none_not_a_placeholder() -> None:
    """None must stay distinguishable from a real strategy id — an
    unattributed decision has to be visibly unattributed, never bucketed
    under some fallback name that would corrupt the per-strategy P&L."""
    e = DecisionEntry(
        id="x", user_id="u", symbol="NVDA", horizon="short", final_action="HOLD",
    )
    assert e.selected_strategy is None


def test_guard_ledger_refusal_accepts_a_strategy() -> None:
    """A refusal is attributable too: `strategy_confidence` should be able
    to learn that a strategy's ideas are the ones being refused, not only
    that its fills lost money."""
    import inspect

    from trading_agents.options.tools.guard import ToolGuard

    sig = inspect.signature(ToolGuard._ledger_refusal)
    assert "strategy" in sig.parameters, (
        "_ledger_refusal must take the strategy so VETOED options rows are "
        "attributable — every one in production had selected_strategy NULL"
    )


def test_every_ledger_refusal_call_site_passes_the_strategy() -> None:
    """Threading the parameter is useless if a call site forgets it. This
    is the assertion that would have caught the original bug: the field
    existed and the DB write existed; only the call sites never set it."""
    import re
    from pathlib import Path

    import trading_agents.options.tools.guard as guard_mod

    src = Path(guard_mod.__file__).read_text()
    calls = re.findall(r"self\._ledger_refusal\((.*?)\n\s*\)", src, re.S)
    assert calls, "expected to find _ledger_refusal call sites"
    missing = [c for c in calls if "strategy=" not in c]
    assert not missing, f"{len(missing)} _ledger_refusal call site(s) omit strategy="
