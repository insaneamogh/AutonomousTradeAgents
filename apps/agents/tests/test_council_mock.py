"""Council tests.

Two flavors:
  - **Mock-LLM** (default): exercises the council under the deterministic
    mock responses. Runs in CI, no API key needed.
  - **Real-LLM** (opt-in via ``RUN_REAL_LLM_TESTS=1``): hits real Anthropic.
    Costs real money. Skipped automatically when the env var is unset OR
    ``ANTHROPIC_API_KEY`` isn't available.
"""

from __future__ import annotations

import os

import pytest

from trading_agents.llm import LLM
from trading_agents.runtime import run_council


# ─────────────────────────────────────────────────────────────────────
# Mock-LLM tests — always run
# ─────────────────────────────────────────────────────────────────────


async def test_mock_council_produces_buy_proposal_for_nvda() -> None:
    llm = LLM(api_key=None)  # force mock mode
    assert llm.mock is True

    result = await run_council(symbol="NVDA", llm=llm)
    assert result["llm_mock"] is True
    assert result["final_action"] in ("BUY", "SELL", "HOLD", "VETOED")
    # Three analysts ran (router includes them all in mock mode).
    assert result["technical"] is not None
    assert result["fundamental"] is not None
    assert result["macro"] is not None
    # Macro analyst gives a real score (not the parse-error default).
    assert result["macro"]["score"] == pytest.approx(60.0)
    assert result["macro"]["confidence"] == pytest.approx(0.50)


async def test_mock_council_proposal_carries_sizing_metadata() -> None:
    llm = LLM(api_key=None)
    result = await run_council(symbol="AAPL", llm=llm)
    if result["proposal"] is not None:
        assert result["proposal"]["stopLoss"] is not None
        assert result["proposal"]["targetPrice"] is not None
        assert isinstance(result["proposal"]["informationalFlags"], list)


async def test_mock_council_selector_and_drafter_both_fire() -> None:
    """Selector picks a strategy id; Drafter turns it into a proposal.

    The mock Selector returns ``strategy='momentum'`` with confidence ~0.58
    and the mock Drafter then emits BUY. Both surfaces should land on the
    runtime result dict — they're the contract the Reflection Agent will
    score later.
    """
    llm = LLM(api_key=None)
    result = await run_council(symbol="NVDA", llm=llm)

    # Selector ran and surfaced its pick.
    assert result["selected_strategy"] == "momentum"
    assert 0.0 < result["selector_confidence"] <= 1.0
    assert result["selector_rationale"]  # non-empty
    assert "momentum" in result["selector_rationale"].lower() or "MOCK" in result["selector_rationale"]

    # Drafter ran and surfaced a proposal (subject to risk-officer approval).
    # If risk approves, proposal is non-None; otherwise we still have evidence
    # the drafter fired by inspecting final_action.
    if result["proposal"] is not None:
        assert result["proposal"]["side"] in ("BUY", "SELL")
        # Drafter inherits the Selector's strategy id verbatim.
        # Strategy id isn't on the camelCase DTO — verify via final_action only here.
        assert result["proposal"]["bullCase"]
        assert result["proposal"]["bearCase"]


async def test_selector_null_strategy_emits_hold() -> None:
    """Direct unit test: when the Selector LLM returns ``strategy: null``,
    the node must return a HOLD state — no selected_strategy, no proposal,
    final_action="HOLD". This is the contract the graph leans on to skip
    the Drafter entirely.
    """
    import json

    from trading_agents.llm import LLMResponse
    from trading_agents.nodes import selector_node

    class _FakeHoldLLM:
        mock = True

        async def complete(self, **kwargs):
            return LLMResponse(
                text=json.dumps(
                    {
                        "strategy": None,
                        "confidence": 0.0,
                        "rationale": "MOCK-HOLD: regime ambiguous, no strategy fits.",
                    }
                ),
                model="haiku+mock",
            )

    state = {
        "symbol": "TSLA",
        "horizon": "short",
        "regime": "choppy",
        "technical": {"score": 40, "confidence": 0.3, "thesis": "weak"},
    }
    result = await selector_node(state, _FakeHoldLLM())

    assert result["selected_strategy"] is None
    assert result["proposal"] is None
    assert result["final_action"] == "HOLD"
    assert "MOCK-HOLD" in result["selector_rationale"]


async def test_selector_unknown_strategy_falls_back_to_momentum() -> None:
    """If the Selector hallucinates a strategy id not in STRATEGY_REGISTRY,
    it must fall back to ``momentum`` with capped confidence — not crash
    the council on a bad id.
    """
    import json

    from trading_agents.llm import LLMResponse
    from trading_agents.nodes import selector_node

    class _FakeUnknownLLM:
        mock = True

        async def complete(self, **kwargs):
            return LLMResponse(
                text=json.dumps(
                    {
                        "strategy": "pairs_arb_v2",  # not in registry
                        "confidence": 0.95,
                        "rationale": "MOCK: tried to pick a strategy that doesn't exist.",
                    }
                ),
                model="haiku+mock",
            )

    state = {
        "symbol": "AAPL",
        "horizon": "short",
        "regime": "bull",
        "technical": {"score": 64, "confidence": 0.6, "thesis": "ok"},
    }
    result = await selector_node(state, _FakeUnknownLLM())

    assert result["selected_strategy"] == "momentum"
    # Confidence got capped at 0.3 — the LLM's overconfident 0.95 is gone.
    assert result["selector_confidence"] <= 0.3
    assert "fallback" in result["selector_rationale"].lower()


async def test_drafter_skipped_when_selector_holds() -> None:
    """Integration test: HOLD from the Selector must skip the Drafter even
    when the rest of the council ran. We swap the selector_node to a
    deterministic HOLD-returning function and confirm proposal=None,
    final_action=HOLD.
    """
    from trading_agents import graph as graph_mod

    async def _hold_selector(state, _llm):
        return {
            **state,
            "selected_strategy": None,
            "selector_confidence": 0.0,
            "selector_rationale": "STUB-HOLD: forced HOLD from test.",
            "proposal": None,
            "final_action": "HOLD",
        }

    original = graph_mod.selector_node
    graph_mod.selector_node = _hold_selector  # type: ignore[assignment]
    try:
        llm = LLM(api_key=None)
        result = await run_council(symbol="META", llm=llm)
    finally:
        graph_mod.selector_node = original  # type: ignore[assignment]

    assert result["selected_strategy"] is None
    assert result["proposal"] is None
    assert result["final_action"] == "HOLD"
    assert "STUB-HOLD" in result["selector_rationale"]


# ─────────────────────────────────────────────────────────────────────
# Real-LLM smoke — opt-in, costs money
# ─────────────────────────────────────────────────────────────────────


def _real_llm_available() -> bool:
    if os.environ.get("RUN_REAL_LLM_TESTS", "").strip().lower() not in ("1", "true", "yes"):
        return False
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark_real_llm = pytest.mark.skipif(
    not _real_llm_available(),
    reason="Real-LLM tests opt-in via RUN_REAL_LLM_TESTS=1 + ANTHROPIC_API_KEY set.",
)


@pytestmark_real_llm
async def test_real_anthropic_council_produces_proposal() -> None:
    """Hits real Anthropic. Costs ~$0.001 with Haiku for the analyst calls."""
    llm = LLM()  # picks up ANTHROPIC_API_KEY
    assert llm.mock is False

    result = await run_council(symbol="NVDA", llm=llm)
    assert result["final_action"] in ("BUY", "SELL", "HOLD", "VETOED")
    # No MOCK markers in the analyst output.
    for key in ("technical", "fundamental", "macro"):
        if result.get(key):
            assert "MOCK" not in result[key].get("thesis", ""), (
                f"{key} thesis still contains MOCK marker — wrapper picked mock path"
            )
