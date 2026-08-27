"""Decision schemas — the browsable list + the trade biography."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.base import CamelCaseModel


class TimelineEventDto(CamelCaseModel):
    kind: str
    at: str | None
    title: str
    detail: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class DecisionTimelineResponse(CamelCaseModel):
    decision_id: str
    symbol: str
    side: str | None
    status: str
    events: list[TimelineEventDto]


class DecisionSummaryDto(CamelCaseModel):
    """One row in the decisions list. Every council pass produces one of
    these, whether or not it ever became a proposal — a HOLD from a
    strategy-fit short-circuit is exactly as listed as an approved BUY.
    """

    id: str
    symbol: str
    final_action: str
    triggered_at: datetime
    risk_approved: bool
    risk_veto_rule: str | None
    selected_strategy: str | None
    selector_confidence: float
    selector_rationale: str
    regime: str | None
    analyst_subset: list[str] | None
    user_response: str | None


class DecisionListResponse(CamelCaseModel):
    decisions: list[DecisionSummaryDto]
    total: int
    limit: int
    offset: int
