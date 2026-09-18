"""The circuit breaker must be checked BEFORE any paid model call.

`drawdown_halt_active` lives in the risk engine, which runs AFTER the
Bull/Bear council. So a halted account paid for a full debate on every
symbol of every sweep and was then refused every single time.

Measured on the live account: the breaker tripped 2026-09-08 14:37 at
-3.01% against a -3.00% threshold and latches until the user acknowledges
it (deliberate, PLAN.md section 12). Over the two days that followed,
$2.89 of model spend bought passes that could not possibly have traded —
roughly 27% of that account's lifetime LLM cost, and the direct cause of
the operator pulling the API key.
"""

from __future__ import annotations

import pytest
from tests.test_tool_guard import MARKET_OPEN_NOW

from engine.risk import MockRiskContextProvider, RiskCaps
from trading_agents.options.tools.guard import ToolGuard


def _guard(*, halted: bool) -> ToolGuard:
    """Clock pinned open: `market_closed` is checked before this rule, and
    both are free, so the ordering does not matter for cost — but the test
    must get past it to assert on the halt."""
    return ToolGuard(
        session_factory=None,
        context_provider=MockRiskContextProvider(drawdown_halted=halted),
        clock=lambda: MARKET_OPEN_NOW,
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTO_TRADE_ENABLED", "1")
    monkeypatch.setenv("ALPACA_PAPER", "1")


async def test_a_halted_account_is_refused_before_the_council_runs() -> None:
    v = await _guard(halted=True).preflight_can_open(
        user_id="u", caps=RiskCaps(options_disabled=False)
    )
    assert v.allow is False
    assert v.reason == "drawdown_halt_active"


async def test_the_refusal_carries_its_real_veto_rule() -> None:
    """Without the payload the pre-flight saves the LLM call but the
    refusal is invisible to `build_veto_ledger`, which filters on
    `risk_veto_rule IS NOT NULL` — the more the optimisation works, the
    blinder the Refusal Ledger gets. Same contract the other account-level
    pre-flights already follow."""
    v = await _guard(halted=True).preflight_can_open(
        user_id="u", caps=RiskCaps(options_disabled=False)
    )
    assert v.payload is not None
    assert v.payload["risk_veto_rule"] == "drawdown_halt_active"


async def test_an_unhalted_account_is_not_blocked_by_this_rule() -> None:
    v = await _guard(halted=False).preflight_can_open(
        user_id="u", caps=RiskCaps(options_disabled=False)
    )
    assert v.reason != "drawdown_halt_active"
