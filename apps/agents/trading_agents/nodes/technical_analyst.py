"""Technical analyst node — reads pre-computed technical features. Haiku-tier."""

from __future__ import annotations

import logging

from trading_agents.llm import LLM, Model
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

    resp = await llm.complete(system=TECHNICAL_ANALYST, user=user, model=Model.HAIKU, max_tokens=500)
    try:
        data = LLM.parse_json(resp.text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("technical parse failed: %s", exc)
        data = {"score": 50.0, "confidence": 0.2, "thesis": "Parse error — neutral default.", "citations": []}

    return {
        **state,
        "technical": {
            "score": float(data.get("score", 50.0)),
            "confidence": float(data.get("confidence", 0.0)),
            "thesis": str(data.get("thesis", "")),
            "citations": list(data.get("citations", [])),
        },
    }
