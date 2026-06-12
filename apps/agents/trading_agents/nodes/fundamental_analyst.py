"""Fundamental analyst node — reads pre-computed fundamental features. Sonnet-tier."""

from __future__ import annotations

import logging

from trading_agents.llm import LLM, Model, complete_json
from trading_agents.prompts import FUNDAMENTAL_ANALYST
from trading_agents.state import CouncilState

logger = logging.getLogger("agents.node.fundamental")


async def fundamental_analyst_node(state: CouncilState, llm: LLM) -> CouncilState:
    fund = state.get("context", {}).get("fundamentals", {})
    user = (
        f"Ticker: {state['symbol']}\n"
        f"Universe: {state.get('context', {}).get('universe', 'US')}\n\n"
        "Fundamental features:\n"
        f"  quality_score:           {fund.get('quality_score', 'n/a')}\n"
        f"  business_quality_score:  {fund.get('business_quality_score', 'n/a')}\n"
        f"  earnings_power_score:    {fund.get('earnings_power_score', 'n/a')}\n"
        f"  valuation_score:         {fund.get('valuation_score', 'n/a')}\n"
        f"  growth_trajectory:       {fund.get('growth_trajectory', 'n/a')}\n"
        f"  capital_efficiency:      {fund.get('capital_efficiency', 'n/a')}\n"
        f"  shareholder_returns:     {fund.get('shareholder_returns', 'n/a')}\n"
        f"  piotroski_f_score:       {fund.get('piotroski_f_score', 'n/a')}\n"
    )

    data, degraded = await complete_json(
        llm,
        system=FUNDAMENTAL_ANALYST, user=user, model=Model.SONNET, max_tokens=500
    )
    if data is None:
        logger.warning("fundamental degraded — neutral default")
        data = {"score": 50.0, "confidence": 0.2, "thesis": "Parse error — neutral default.", "citations": []}

    degraded_nodes = list(state.get("degraded_nodes") or [])
    if degraded:
        degraded_nodes.append("fundamental")

    return {
        **state,
        "fundamental": {
            "score": float(data.get("score", 50.0)),
            "confidence": float(data.get("confidence", 0.0)),
            "thesis": str(data.get("thesis", "")),
            "citations": list(data.get("citations", [])),
        },
        "degraded_nodes": degraded_nodes,
    }
