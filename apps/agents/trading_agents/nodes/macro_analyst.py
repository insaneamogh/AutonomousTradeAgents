"""Macro analyst node — judges regime / rates / dollar fit for THIS symbol.

Sonnet-tier (regime reasoning benefits from a stronger model than Haiku).
Phase 2 swaps the synthetic features for FRED + symbol-sector-RS computed
from the feature store.

Reads from ``context["macro"]``:
    vix_level
    ten_year_yield_pct
    dxy_index
    sector_relative_strength      symbol's sector 21d return − SPY 21d return
And the Router's ``regime`` if already set on state.
"""

from __future__ import annotations

import logging

from trading_agents.llm import LLM, Model
from trading_agents.prompts import MACRO_ANALYST
from trading_agents.state import CouncilState

logger = logging.getLogger("agents.node.macro")


async def macro_analyst_node(state: CouncilState, llm: LLM) -> CouncilState:
    macro = state.get("context", {}).get("macro", {})
    user = (
        f"Ticker: {state['symbol']}\n"
        f"Horizon: {state.get('horizon', 'short')}\n"
        f"Regime (from Router): {state.get('regime', 'unknown')}\n\n"
        "Macro features:\n"
        f"  vix_level:                  {macro.get('vix_level', 'n/a')}\n"
        f"  ten_year_yield_pct:         {macro.get('ten_year_yield_pct', 'n/a')}\n"
        f"  dxy_index:                  {macro.get('dxy_index', 'n/a')}\n"
        f"  sector_relative_strength:   {macro.get('sector_relative_strength', 'n/a')}\n"
    )

    resp = await llm.complete(system=MACRO_ANALYST, user=user, model=Model.SONNET, max_tokens=500)
    try:
        data = LLM.parse_json(resp.text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("macro parse failed: %s", exc)
        data = {"score": 50.0, "confidence": 0.2, "thesis": "Parse error — neutral default.", "citations": []}

    return {
        **state,
        "macro": {
            "score": float(data.get("score", 50.0)),
            "confidence": float(data.get("confidence", 0.0)),
            "thesis": str(data.get("thesis", "")),
            "citations": list(data.get("citations", [])),
        },
    }
