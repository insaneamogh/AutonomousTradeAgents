"""Technical analyst node — reads pre-computed technical features. Haiku-tier."""

from __future__ import annotations

import logging

from trading_agents.llm import LLM, Model, complete_json
from trading_agents.prompts import TECHNICAL_ANALYST
from trading_agents.state import CouncilState

logger = logging.getLogger("agents.node.technical")


async def technical_analyst_node(state: CouncilState, llm: LLM) -> CouncilState:
    tech = state.get("context", {}).get("technicals", {})
    user = (
        f"Ticker: {state['symbol']}\n"
        f"Horizon: {state.get('horizon', 'short')}\n\n"
        "Technical features:\n"
        f"  trend_regime:            {tech.get('trend_regime', 'n/a')}\n"
        f"  dma20_pct:               {tech.get('dma20_pct', 'n/a')}\n"
        f"  dma50_pct:               {tech.get('dma50_pct', 'n/a')}\n"
        f"  dma200_pct:              {tech.get('dma200_pct', 'n/a')}\n"
        f"  rsi_14:                  {tech.get('rsi_14', 'n/a')}\n"
        f"  vwap_position:           {tech.get('vwap_position', 'n/a')}\n"
        f"  mean_reversion_risk:     {tech.get('mean_reversion_risk', 'n/a')}\n"
        f"  trend_position_score:    {tech.get('trend_position_score', 'n/a')}\n"
        f"  volume_ratio_20d:        {tech.get('volume_ratio_20d', 'n/a')}\n"
    )

    data, degraded = await complete_json(
        llm,
        system=TECHNICAL_ANALYST, user=user, model=Model.HAIKU, max_tokens=500
    )
    if data is None:
        logger.warning("technical degraded — neutral default")
        data = {"score": 50.0, "confidence": 0.2, "thesis": "Parse error — neutral default.", "citations": []}

    degraded_nodes = list(state.get("degraded_nodes") or [])
    if degraded:
        degraded_nodes.append("technical")

    return {
        **state,
        "technical": {
            "score": float(data.get("score", 50.0)),
            "confidence": float(data.get("confidence", 0.0)),
            "thesis": str(data.get("thesis", "")),
            "citations": list(data.get("citations", [])),
        },
        "degraded_nodes": degraded_nodes,
    }
