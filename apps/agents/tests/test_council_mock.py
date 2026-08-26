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


async def test_strategy_fit_and_drafter_both_fire() -> None:
    """The deterministic fit node picks a strategy; the Drafter turns it
    into a proposal.

    The pick is no longer an LLM's: ``selected_strategy`` comes from the
    precondition scorers and ``selector_rationale`` is a NAMED reason of
    the form ``<strategy>_<direction>:<component>+<component>``. Both
    surfaces land on the runtime result — they are the contract the
    Reflection Agent scores later.
    """
    llm = LLM(api_key=None)
    result = await run_council(symbol="NVDA", llm=llm)

    from trading_agents.strategies import STRATEGY_REGISTRY

    assert result["selected_strategy"] in STRATEGY_REGISTRY
    assert result["selected_direction"] in ("long", "short")
    assert 0.0 < result["selector_confidence"] <= 1.0
    # The named-reason shape, not model prose.
    assert result["selector_rationale"].startswith(result["selected_strategy"])
    assert result["selected_direction"] in result["selector_rationale"]

    # The full fit block is carried for the audit row + thesis view.
    fit = result["strategy_fit"]
    assert fit["winner"]["strategy_id"] == result["selected_strategy"]
    assert fit["winner"]["components"], "components explain WHY it fit"
    assert fit["ranked"], "the alternatives that lost are kept too"

    if result["proposal"] is not None:
        assert result["proposal"]["side"] in ("BUY", "SELL")
        assert result["proposal"]["bullCase"]
        assert result["proposal"]["bearCase"]


async def test_no_strategy_fit_holds_without_spending_an_llm_call() -> None:
    """The cost win, pinned: a symbol whose setup fits nothing must HOLD
    with ZERO calls to the LLM — not five.

    We count every ``complete`` invocation rather than asserting on the
    result alone, because "returns HOLD" was already true before; what is
    new is that it costs nothing.
    """
    calls: list[str] = []

    class _CountingLLM:
        mock = True

        async def complete(self, *, system, user, **_kw):
            calls.append(system[:40])
            raise AssertionError("no LLM call may happen on a no-fit symbol")

    def _featureless(symbol: str, horizon: str = "short") -> dict:
        # Every precondition maximally unsatisfied: no trend, mid-channel,
        # no momentum, exploded vol, perfectly correlated to the index.
        return {
            "symbol": symbol,
            "horizon": horizon,
            "last_price": 100.0,
            "portfolio_equity": 100_000.0,
            "technicals": {
                "trend_regime": "choppy",
                "dma20_pct": -0.05,
                "dma50_pct": -0.05,
                "rsi_14": 50.0,
                "atr_14": 2.0,
                "volume_ratio_20d": 0.5,
            },
            "quant": {
                "ret_252d_pct": -0.2,
                "ret_63d_pct": -0.3,
                "ret_21d_pct": -0.4,
                "sharpe": -0.4,
                "atr_zscore": 3.0,
                "realized_vol_pct": 85.0,
                "corr_benchmark": 0.99,
                "price_zscore_20": 0.0,
                "donchian_pct": 50.0,
            },
        }

    result = await run_council(
        symbol="NOFIT", llm=_CountingLLM(), feature_provider=_featureless
    )

    assert calls == [], f"expected zero LLM calls, got {calls}"
    assert result["final_action"] == "HOLD"
    assert result["selected_strategy"] is None
    assert result["proposal"] is None
    assert "clears the fit floor" in result["selector_rationale"]


async def test_fit_only_ever_returns_registry_ids() -> None:
    """A whole class of bug is gone: the old LLM Selector could hallucinate
    a strategy id and needed a fallback path. The scorers are keyed off the
    registry itself, so an unknown id is unrepresentable.
    """
    from trading_agents.strategies import STRATEGY_REGISTRY, rank_strategies
    from trading_agents.strategies.fit import _SCORERS

    assert set(_SCORERS) == set(STRATEGY_REGISTRY)
    ranked = rank_strategies({"technicals": {"trend_regime": "uptrend"}}, allow_shorts=True)
    assert {r.strategy_id for r in ranked} <= set(STRATEGY_REGISTRY)


async def test_drafter_skipped_when_fit_holds() -> None:
    """Integration: a HOLD from the deterministic fit node must skip the
    Router, every analyst, and the Drafter — the whole rest of the graph.
    """
    from trading_agents import graph as graph_mod

    async def _hold_fit(state):
        return {
            **state,
            "selected_strategy": None,
            "selected_direction": None,
            "selector_confidence": 0.0,
            "selector_rationale": "STUB-HOLD: forced HOLD from test.",
            "proposal": None,
            "final_action": "HOLD",
        }

    original = graph_mod.strategy_fit_node
    graph_mod.strategy_fit_node = _hold_fit  # type: ignore[assignment]
    try:
        llm = LLM(api_key=None)
        result = await run_council(symbol="META", llm=llm)
    finally:
        graph_mod.strategy_fit_node = original  # type: ignore[assignment]

    assert result["selected_strategy"] is None
    assert result["proposal"] is None
    assert result["final_action"] == "HOLD"
    assert "STUB-HOLD" in result["selector_rationale"]
    # The Router never ran, so there is no regime.
    assert result["regime"] is None


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
