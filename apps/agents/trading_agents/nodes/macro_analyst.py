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

from trading_agents.llm import LLM, Model
from trading_agents.nodes._specialist import render_features, run_specialist
from trading_agents.prompts import MACRO_ANALYST
from trading_agents.state import CouncilState

FEATURES = (
    "vix_level",
    "ten_year_yield_pct",
    "dxy_index",
    "sector_relative_strength",
)


async def macro_analyst_node(state: CouncilState, llm: LLM) -> CouncilState:
    """Score macro fit for the symbol 0-100 from ``context["macro"]``."""
    return await run_specialist(
        state,
        llm,
        name="macro",
        system=MACRO_ANALYST,
        model=Model.SONNET,
        header=(
            f"Ticker: {state['symbol']}\n"
            f"Horizon: {state.get('horizon', 'short')}\n"
            f"Regime (from Router): {state.get('regime', 'unknown')}\n\n"
            "Macro features:\n"
        ),
        # Wider labels than the other two analysts: sector_relative_strength
        # is the longest feature name in the council.
        body=render_features(
            state.get("context", {}).get("macro", {}), FEATURES, label_width=28
        ),
    )
