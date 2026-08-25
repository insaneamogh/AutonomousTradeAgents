"""Technical analyst node — reads pre-computed technical features. Haiku-tier.

Cheapest tier on purpose: reading nine already-computed indicators into a
0-100 score is pattern-matching, not reasoning. The judgement calls live in
Macro (regime) and the Drafter (thesis).
"""

from __future__ import annotations

from trading_agents.llm import LLM, Model
from trading_agents.nodes._specialist import render_features, run_specialist
from trading_agents.prompts import TECHNICAL_ANALYST
from trading_agents.state import CouncilState

FEATURES = (
    "trend_regime",
    "dma20_pct",
    "dma50_pct",
    "dma200_pct",
    "rsi_14",
    "vwap_position",
    "mean_reversion_risk",
    "trend_position_score",
    "volume_ratio_20d",
)


async def technical_analyst_node(state: CouncilState, llm: LLM) -> CouncilState:
    """Score the symbol's technical setup 0-100 from ``context["technicals"]``."""
    return await run_specialist(
        state,
        llm,
        name="technical",
        system=TECHNICAL_ANALYST,
        model=Model.HAIKU,
        header=(
            f"Ticker: {state['symbol']}\n"
            f"Horizon: {state.get('horizon', 'short')}\n\n"
            "Technical features:\n"
        ),
        body=render_features(
            state.get("context", {}).get("technicals", {}), FEATURES, label_width=25
        ),
    )
