"""Macro analyst node — judges regime / rates / dollar fit for THIS symbol.

Sonnet-tier (regime reasoning benefits from a stronger model than Haiku).
Phase 2 swaps the synthetic features for FRED + symbol-sector-RS computed
from the feature store.

Reads from ``context["macro"]``:
    vix_level
    ten_year_yield_pct
    ten_year_change_63d_bp        10y change over ~3 months, basis points
    dxy_zscore_1y                 broad dollar vs its own trailing year
    sector_relative_strength      the SYMBOL's 21d return − SPY 21d return
                                  (named "sector" historically; it is not)
And the Router's ``regime`` and strategy_fit's ``selected_direction``.

The raw ``dxy_index`` level is deliberately NOT rendered. It is FRED's
broad index (Jan 2006 = 100), and the prompt used to compare it with 105,
the ICE DXY's scale, which made "strong dollar" permanently true.
"""

from __future__ import annotations

from trading_agents.llm import LLM, Model
from trading_agents.nodes._specialist import proposed_direction, render_features, run_specialist
from trading_agents.prompts import MACRO_ANALYST
from trading_agents.state import CouncilState

FEATURES = (
    "vix_level",
    "ten_year_yield_pct",
    "ten_year_change_63d_bp",
    "dxy_zscore_1y",
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
            f"Proposed direction: {proposed_direction(state)}\n"
            f"Regime (from Router): {state.get('regime', 'unknown')}\n\n"
            "Macro features:\n"
        ),
        # Wider labels than the other two analysts: sector_relative_strength
        # is the longest feature name in the council.
        body=render_features(
            state.get("context", {}).get("macro", {}), FEATURES, label_width=28
        ),
    )
