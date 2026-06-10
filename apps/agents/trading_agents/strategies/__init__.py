"""Strategy registry — id ↔ metadata for the council.

The council picks a strategy ID; downstream code (Reflection, future
executor, mobile UI) looks up display names + defaults here. The actual
strategy implementations live in ``packages/engine/engine/backtester/strategies/``
— this registry is the *agent-side* index, deliberately separated.

Strategy ids are the only contract between the council and the backtester.
Don't import backtester modules here — keep the two concerns decoupled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Horizon = Literal["intraday", "short", "mid", "long"]


@dataclass(frozen=True)
class StrategyMetadata:
    id: str
    display: str
    default_horizon: Horizon
    description: str


STRATEGY_REGISTRY: dict[str, StrategyMetadata] = {
    "sma_crossover": StrategyMetadata(
        id="sma_crossover",
        display="SMA Crossover",
        default_horizon="short",
        description="Fast/slow SMA cross. Trend-follower; loses in chop.",
    ),
    "rsi_mean_reversion": StrategyMetadata(
        id="rsi_mean_reversion",
        display="RSI Mean-Reversion",
        default_horizon="short",
        description="Buy oversold (RSI<30), exit at RSI>50. Counter-trend.",
    ),
    "momentum": StrategyMetadata(
        id="momentum",
        display="12-1 Momentum",
        default_horizon="mid",
        description="Return from 12mo ago to 1mo ago. Crowded factor; works in trends.",
    ),
    "breakout": StrategyMetadata(
        id="breakout",
        display="Donchian Breakout",
        default_horizon="short",
        description="Buy on close above 20d high; sell on close below 10d low.",
    ),
    "vol_regime_switch": StrategyMetadata(
        id="vol_regime_switch",
        display="Vol-Regime Switch",
        default_horizon="mid",
        description="Momentum trigger gated by realized-vol regime. Sits out high-vol.",
    ),
}


def resolve_strategy(strategy_id: str | None) -> StrategyMetadata:
    """Look up a strategy id, defaulting to ``momentum`` on unknown / None.

    Selector outputs go through this — guarantees the rest of the council
    always sees a valid id even if the LLM hallucinates an unknown one.
    """
    if strategy_id and strategy_id in STRATEGY_REGISTRY:
        return STRATEGY_REGISTRY[strategy_id]
    return STRATEGY_REGISTRY["momentum"]
