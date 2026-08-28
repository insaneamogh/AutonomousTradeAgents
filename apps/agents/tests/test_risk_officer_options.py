"""risk_officer_node's options-awareness — the connective-tissue fix that
makes ``RiskCaps.options_disabled`` (and the whole options rule package)
actually reachable from the live graph.

Before this fix, the node built ``RiskProposal`` from ``state["proposal"]``
with zero knowledge of the option fields the Drafter's options branch
writes — an approved options proposal would silently run through only the
equity rule set, and the fail-closed ``options_disabled`` master switch
would never fire. These tests pin the construction directly (via a spy on
``evaluate``), independent of the options rule package's own dispatch
logic, which is separate work.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import patch

from engine.risk.types import RiskDecision, RiskProposal
from trading_agents.nodes.risk_officer import risk_officer_node
from trading_agents.state import CouncilState


def _state(proposal: dict[str, Any]) -> CouncilState:
    return {
        "symbol": "AAPL",
        "user_id": "user-1",
        "context": {"last_price": 250.0, "portfolio_equity": 100_000.0, "asset": {}},
        "proposal": proposal,
    }


async def _capture_evaluate(store: dict[str, RiskProposal], state: CouncilState) -> None:
    # engine.risk.evaluate is a plain sync function (no LLM, no I/O) — the
    # fake must match that, not be async, or risk_officer_node's unawaited
    # call site hands back a coroutine object instead of a RiskDecision.
    def _fake_evaluate(
        proposal: RiskProposal, context: object, caps: object, *, specialists: object
    ) -> RiskDecision:
        store["proposal"] = proposal
        return RiskDecision(approved=True, reason="ok", checks_passed=())

    with patch("trading_agents.nodes.risk_officer.evaluate", _fake_evaluate):
        await risk_officer_node(state)


async def test_equity_proposal_builds_is_option_false() -> None:
    """Regression guard: an ordinary equity proposal (no option fields at
    all) must produce is_option=False, option=None — unchanged from before
    this fix."""
    state = _state(
        {"side": "BUY", "qty": 10, "estimated_notional": 2500.0, "confidence": 0.7}
    )
    captured: dict[str, RiskProposal] = {}
    await _capture_evaluate(captured, state)

    rp = captured["proposal"]
    assert rp.is_option is False
    assert rp.option is None


async def test_options_proposal_builds_option_leg_details() -> None:
    """The actual fix: an options proposal dict (matching exactly what the
    Drafter's options branch writes into state["proposal"]) must produce a
    fully-populated OptionLegDetails, not just an is_option flag."""
    proposal = {
        "side": "BUY",
        "qty": 2,
        "estimated_notional": 640.0,
        "confidence": 0.65,
        "is_option": True,
        "option_action": "buy_to_open",
        "occ_symbol": "AAPL260828C00250000",
        "strike": 250.0,
        "expiry_date": "2026-08-28",
        "contract_type": "call",
        "multiplier": 100,
        "open_interest": 500,
        "volume": 42,
        "bid": 3.10,
        "ask": 3.30,
        "implied_volatility": 0.28,
        "days_to_earnings": 12,
    }
    state = _state(proposal)
    captured: dict[str, RiskProposal] = {}
    await _capture_evaluate(captured, state)

    rp = captured["proposal"]
    assert rp.is_option is True
    assert rp.option is not None
    assert rp.option.underlying_symbol == "AAPL"
    assert rp.option.occ_symbol == "AAPL260828C00250000"
    assert rp.option.contract_type == "call"
    assert rp.option.strike == 250.0
    assert rp.option.expiry == date(2026, 8, 28)
    assert rp.option.multiplier == 100
    assert rp.option.action == "buy_to_open"
    assert rp.option.open_interest == 500
    assert rp.option.volume == 42
    assert rp.option.bid == 3.10
    assert rp.option.ask == 3.30
    assert rp.option.implied_volatility == 0.28
    assert rp.option.days_to_earnings == 12


async def test_options_proposal_with_missing_optional_fields_uses_safe_defaults() -> None:
    """A minimally-populated options proposal (only the required fields) must
    not raise — missing liquidity/IV/earnings snapshot fields become None,
    which is exactly what iv_unavailable/illiquid_contract are designed to
    veto on, not a construction-time crash."""
    proposal = {
        "side": "BUY",
        "qty": 1,
        "estimated_notional": 320.0,
        "confidence": 0.6,
        "is_option": True,
    }
    state = _state(proposal)
    captured: dict[str, RiskProposal] = {}
    await _capture_evaluate(captured, state)

    rp = captured["proposal"]
    assert rp.is_option is True
    assert rp.option is not None
    assert rp.option.action == "buy_to_open"
    assert rp.option.open_interest is None
    assert rp.option.implied_volatility is None
