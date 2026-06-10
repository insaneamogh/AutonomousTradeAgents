"""Wire schemas for /api/v1/strategies/performance."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


def _camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


class _Base(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class StrategyPerformanceDto(_Base):
    strategy_id: str
    display_name: str
    """``StrategyMetadata.display`` from the agent registry."""
    confidence: float = Field(ge=0.0, le=1.0)
    """Current prior (post-Reflection nudges). 0..1."""

    decisions_in_window: int
    wins: int
    losses: int
    """Wins + losses sum to ``decisions_in_window`` for completed trades.
    Open / HOLD'd decisions are counted in ``decisions_in_window`` but
    NOT in wins/losses (yet)."""

    realized_pnl: float
    avg_winner_pct: float | None
    avg_loser_pct: float | None
    last_decision_at: datetime | None
    last_reflection_at: datetime | None


class StrategiesPerformanceResponse(_Base):
    window_days: int
    """Default 30."""
    strategies: list[StrategyPerformanceDto]
    """One row per ``STRATEGY_REGISTRY`` id, ordered by ``confidence`` desc."""
    generated_at: datetime
