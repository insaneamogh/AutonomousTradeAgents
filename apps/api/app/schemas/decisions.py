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
    approval_mode: str
    """'ask' (human-approved, the default) or 'auto' (the auto-approve
    sweeper executed it with no human in the loop — see
    services/orders/auto_approver.py). Lets this list render an AUTO pill.
    NOT exposed on ApprovalProposalDto/the Picks (pending-approvals) list —
    a still-pending proposal has never been decided, so it can only ever
    read 'ask' there; this decision-history list is where a completed
    auto-approval is actually visible."""


class DecisionListResponse(CamelCaseModel):
    decisions: list[DecisionSummaryDto]
    total: int
    limit: int
    offset: int
