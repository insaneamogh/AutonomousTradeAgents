"""Wire schemas for /api/v1/review.

camelCase on the wire. The Review tab swipes through one item at a
time, so the queue endpoint returns a compact card-shaped DTO with
just what the swipe deck renders.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

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


Grade = Literal["good", "bad", "skip"]


# ─────────────────────────────────────────────────────────────────────
# Queue item
# ─────────────────────────────────────────────────────────────────────


class ReviewQueueItem(_Base):
    """One card in the swipe deck."""

    decision_id: str
    triggered_at: datetime
    symbol: str
    side: str
    """BUY / SELL / HOLD — for HOLD we still show the card so the operator
    can review the regime context."""
    qty: int | None
    fill_qty: int | None
    fill_avg_price: float | None
    realized_pnl: float | None
    selected_strategy: str | None
    selector_confidence: float
    bull_case: str = ""
    bear_case: str = ""
    regime: str | None = None


class ReviewQueueResponse(_Base):
    items: list[ReviewQueueItem]
    total_in_window: int
    """Total completed decisions in the window (graded + ungraded)."""
    graded_in_window: int
    """How many of those the caller has already graded."""


# ─────────────────────────────────────────────────────────────────────
# Grade upsert
# ─────────────────────────────────────────────────────────────────────


class GradeRequest(_Base):
    grade: Grade
    notes: str | None = Field(default=None, max_length=2000)


class GradeResponse(_Base):
    id: str
    decision_id: str
    grade: Grade
    notes: str | None
    reviewed_at: datetime


# ─────────────────────────────────────────────────────────────────────
# Agreement stat
# ─────────────────────────────────────────────────────────────────────


class AgreementBucket(_Base):
    operator_grade: Grade
    reflection_direction: Literal["positive", "negative", "neutral"]
    """Sign of the reflection-applied confidence delta on the matching
    strategy. positive = Reflection nudged confidence UP; negative = DOWN;
    neutral = no change (either no Reflection run yet, or clamped to 0)."""
    count: int


class AgreementResponse(_Base):
    window_days: int
    total_reviewed: int
    agreement_pct: float
    """Percentage where operator_grade matches reflection_direction sign.
    ``good ↔ positive`` and ``bad ↔ negative`` count as agreement;
    ``skip`` is excluded from the denominator."""
    buckets: list[AgreementBucket]


class ScorecardMonth(_Base):
    """One month's agreement bucket (keyed YYYY-MM of reviewed_at)."""

    month: str
    total_reviewed: int
    agreement_pct: float


class OverrideStats(_Base):
    """When you disagreed with Reflection and the trade closed — who won?"""

    count: int
    operator_wins: int
    reflection_wins: int
    operator_win_rate_pct: float


class ScorecardResponse(_Base):
    window_days: int
    agreement_pct: float
    months: list[ScorecardMonth]
    overrides: OverrideStats
