"""Fundamental analyst node — reads pre-computed fundamental features. Sonnet-tier.

Sonnet rather than Haiku because weighing quality against valuation is a
trade-off judgement, not a lookup.
"""

from __future__ import annotations

from trading_agents.llm import LLM, Model
from trading_agents.nodes._specialist import render_features, run_specialist
from trading_agents.prompts import FUNDAMENTAL_ANALYST
from trading_agents.state import CouncilState

FEATURES = (
    "quality_score",
    "business_quality_score",
    "earnings_power_score",
    "valuation_score",
    "growth_trajectory",
    "capital_efficiency",
    "shareholder_returns",
    "piotroski_f_score",
)


async def fundamental_analyst_node(state: CouncilState, llm: LLM) -> CouncilState:
    """Score the symbol's fundamentals 0-100 from ``context["fundamentals"]``."""
    return await run_specialist(
        state,
        llm,
        name="fundamental",
        system=FUNDAMENTAL_ANALYST,
        model=Model.SONNET,
        header=(
            f"Ticker: {state['symbol']}\n"
            f"Universe: {state.get('context', {}).get('universe', 'US')}\n\n"
            "Fundamental features:\n"
        ),
        body=render_features(
            state.get("context", {}).get("fundamentals", {}), FEATURES, label_width=25
        ),
    )
