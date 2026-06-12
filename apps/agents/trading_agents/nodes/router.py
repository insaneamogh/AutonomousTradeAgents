"""Router node — classify regime, pick analyst subset. Haiku-tier."""

from __future__ import annotations

import logging

from trading_agents.llm import LLM, Model, complete_json
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

    data, degraded = await complete_json(
        llm,
        system=ROUTER, user=user, model=Model.HAIKU, max_tokens=300
    )
    if data is None:
        logger.warning("router degraded — neutral fallback subset")
        data = {
            "regime": "choppy",
            "analyst_subset": ["technical"],
            "rationale": "fallback after parse error",
        }

    subset = [str(a) for a in data.get("analyst_subset", ["technical"])]

    # Deterministic post-filter: an analyst whose feature block doesn't
    # exist has nothing real to read. The real provider OMITS the
    # fundamentals key when no filings data source is wired — running the
    # Fundamental Analyst over nothing (or worse, synthetic numbers) is
    # exactly what the audit flagged. Code-level, not prompt-level.
    if "fundamentals" not in ctx and "fundamental" in subset:
        logger.info("router: no fundamentals in context — dropping fundamental analyst")
        subset = [a for a in subset if a != "fundamental"]
    if not subset:
        subset = ["technical"]

    degraded_nodes = list(state.get("degraded_nodes") or [])
    if degraded:
        degraded_nodes.append("router")

    return {
        **state,
        "regime": str(data.get("regime", "choppy")),
        "analyst_subset": subset,
        "router_rationale": str(data.get("rationale", "")),
        "degraded_nodes": degraded_nodes,
    }
