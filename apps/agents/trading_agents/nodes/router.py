"""Router node — classify regime, pick analyst subset. Haiku-tier."""

from __future__ import annotations

import logging

from trading_agents.llm import LLM, Model
from trading_agents.prompts import ROUTER
from trading_agents.state import CouncilState

logger = logging.getLogger("agents.node.router")


async def router_node(state: CouncilState, llm: LLM) -> CouncilState:
    ctx = state.get("context", {})
    user = (
        f"Ticker: {state['symbol']}\n"
        f"Horizon: {state.get('horizon', 'short')}\n\n"
        "Feature snapshot:\n"
        f"  trend_regime:       {ctx.get('technicals', {}).get('trend_regime', 'n/a')}\n"
        f"  dma200_pct:         {ctx.get('technicals', {}).get('dma200_pct', 'n/a')}\n"
        f"  rsi_14:             {ctx.get('technicals', {}).get('rsi_14', 'n/a')}\n"
        f"  quality_score:      {ctx.get('fundamentals', {}).get('quality_score', 'n/a')}\n"
        f"  earnings_power:     {ctx.get('fundamentals', {}).get('earnings_power_score', 'n/a')}\n"
    )

    resp = await llm.complete(system=ROUTER, user=user, model=Model.HAIKU, max_tokens=300)
    try:
        data = LLM.parse_json(resp.text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("router parse failed: %s", exc)
        data = {"regime": "choppy", "analyst_subset": ["technical"], "rationale": "fallback after parse error"}

    return {
        **state,
        "regime": str(data.get("regime", "choppy")),
        "analyst_subset": list(data.get("analyst_subset", ["technical"])),
        "router_rationale": str(data.get("rationale", "")),
    }
