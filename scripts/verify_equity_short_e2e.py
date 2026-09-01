"""Drive the real council pipeline with a deliberately, strongly bearish
equity feature set and watch what happens end to end.

Implements `docs/PLAN_SHORTS.md` §5.3's "get one equity short to actually
fire" step: nothing in this codebase had ever exercised
`strategy_fit`'s SHORT-direction pick -> `drafter.py`'s
`side="SELL" if direction=="short"` -> the full risk-rule stack for a
plain equity (not an option). This proves the MECHANICAL path works,
deliberately decoupled from the still-open question of whether the
REAL technical-analyst LLM scores bearish setups fairly (`run_council`'s
mock LLM always returns a fixed technical_score=64.0 regardless of
direction -- see `llm.py::_mock_response` -- so this run can't move
that question either way; it only proves the pipe isn't clogged).

SAFE to run any time: `run_council()` stops at a `proposal` dict. It
never calls a broker. No network access, no ANTHROPIC_API_KEY needed
(forces mock mode explicitly).

Usage:
    uv run --package agents python ../../scripts/verify_equity_short_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from engine.risk.types import RiskCaps
from trading_agents.llm import LLM
from trading_agents.runtime import run_council

# `strategy_fit_node` (apps/agents/trading_agents/nodes/strategy_fit.py)
# reads `ALLOW_SHORTS` directly via `env_flag(...)` to decide whether to
# SCORE the short direction at all -- independently of whatever `RiskCaps`
# object a caller passes to `run_council`. `RiskCaps.forbid_short_phase_0`
# only governs the RISK-GATE veto, a separate, later check. In production
# both reads see the same env var so this never diverges -- but it means
# passing `risk_caps=RiskCaps.aggressive_paper(forbid_short_phase_0=False)`
# alone, as this script first tried, silently does nothing for direction
# scoring: `strategy_fit` still came back with `allow_shorts: false` and
# never considered a short winner. Setting the env var directly is the
# only thing that actually reaches that node.
os.environ["ALLOW_SHORTS"] = "1"

SYMBOL = "ZBEAR"  # not a real ticker -- avoids ever colliding with a live watchlist name


def strongly_bearish_equity_features(symbol: str, horizon: str = "short") -> dict[str, Any]:
    """Hand-built, deliberately one-sided bearish case.

    Every directional component in `strategies/fit.py`'s SHORT scorers
    (`_vol_regime_switch`, `_sma_crossover`, `_momentum`) is aimed at its
    high-scoring end for the short side -- see each function's `sign`
    handling for exactly which raw sign it wants. Not tuned against the
    real LLM (this repo's mock technical score is a fixed 64.0
    regardless of input -- see this file's own docstring), only against
    the deterministic `strategy_fit` scoring, so the numbers below are
    checked against `strategies/fit.py` directly, not guessed.
    """
    return {
        "symbol": symbol,
        "horizon": horizon,
        "universe": "US",
        "last_price": 120.00,
        "portfolio_equity": 100_000.0,
        "technicals": {
            "trend_regime": "downtrend",
            "dma20_pct": -3.5,
            "dma50_pct": -6.0,
            "dma200_pct": -12.0,
            "rsi_14": 42.0,
            "atr_14": 3.60,
            "vwap_position": "below",
            "mean_reversion_risk": 30.0,
            "trend_position_score": 12.0,
            "volume_ratio_20d": 1.3,
        },
        "quant": {
            "price_zscore_20": -1.0,
            "atr_zscore": -0.2,
            "donchian_pct": 5.0,
            "sharpe": -1.8,
            "ret_21d_pct": -9.0,
            "ret_63d_pct": -18.0,
            "ret_252d_pct": -25.0,
            "realized_vol_pct": 22.0,
            "corr_benchmark": 0.25,
        },
        "fundamentals": {
            "quality_score": 45.0,
            "business_quality_score": 45.0,
            "earnings_power_score": 40.0,
            "valuation_score": 40.0,
            "growth_trajectory": 35.0,
            "capital_efficiency": 40.0,
            "shareholder_returns": 40.0,
            "piotroski_f_score": 4,
        },
        # PLAN_SHORTS.md §3 -- unknown flags veto by design. Present +
        # tradable so this run isolates the score/direction path, not the
        # borrow-eligibility gate (already separately verified live).
        "asset": {
            "tradable": True,
            "fractionable": True,
            "shortable": True,
            "easy_to_borrow": True,
            "name": f"{symbol} (synthetic bearish, verify_equity_short_e2e.py)",
        },
        "feature_source": "synthetic",
        "macro": {
            "vix_level": 18.0,
            "ten_year_yield_pct": 4.0,
            "dxy_index": 103.0,
            "sector_relative_strength": -2.0,
        },
    }


async def main() -> None:
    llm = LLM(api_key=None)
    assert llm.mock is True, "refusing to run against a real LLM -- this script is mock-only"

    # Mirrors production's actual current profile: aggressive_paper base,
    # shorts explicitly enabled (ALLOW_SHORTS=1 on Railway per
    # PLAN_SHORTS.md §3). Constructed directly rather than via from_env()
    # so this script's behavior doesn't depend on unset local env vars.
    caps = RiskCaps.aggressive_paper(forbid_short_phase_0=False)

    result = await run_council(
        symbol=SYMBOL,
        llm=llm,
        risk_caps=caps,
        feature_provider=strongly_bearish_equity_features,
        instrument_preference="equity",
    )

    print(f"selected_strategy   = {result['selected_strategy']!r}")
    print(f"selected_direction  = {result['selected_direction']!r}")
    print(f"selector_confidence = {result['selector_confidence']}")
    tech = result.get("technical") or {}
    print(f"technical_score     = {tech.get('score')}")
    print(f"final_action        = {result['final_action']!r}")
    print(f"risk_approved       = {result['risk_approved']}")
    print(f"risk_veto_rule      = {result['risk_veto_rule']!r}")
    print(f"risk_reason         = {result['risk_reason']!r}")
    proposal = result.get("proposal")
    if proposal is not None:
        print("proposal:")
        print(json.dumps(proposal, indent=2, default=str))
    else:
        print("proposal: None")

    print()
    if result["final_action"] == "SELL" and proposal is not None and proposal.get("side") == "SELL":
        print("PASS: a real SELL-to-open equity proposal reached the end of the pipeline.")
    elif result["risk_veto_rule"] is not None:
        print(
            f"BLOCKED at the risk gate by a NAMED rule ({result['risk_veto_rule']!r}) -- "
            "the mechanical path reached the risk stack and a specific rule fired. "
            "Read that rule before concluding anything is broken."
        )
    else:
        print(
            f"UNEXPECTED: final_action={result['final_action']!r} with no veto rule recorded. "
            "strategy_fit likely did not pick the short direction for this feature set -- "
            "print result['strategy_fit'] to see what won and why."
        )
        print(json.dumps(result.get("strategy_fit"), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
