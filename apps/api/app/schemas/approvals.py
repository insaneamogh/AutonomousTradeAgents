"""Approval / decision schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.base import CamelCaseModel

DecisionOutcome = Literal["approved", "declined", "expired"]
Side = Literal["BUY", "SELL"]
RiskLevel = Literal[1, 2, 3, 4, 5]


class ApprovalProposalDto(CamelCaseModel):
    id: str
    symbol: str
    side: Side
    qty: int
    order_type: Literal["MARKET", "LIMIT"]
    limit_price: float | None = None
    estimated_notional: float
    # Risk-managed prices — populated by engine.sizing.atr_position_size.
    stop_loss: float | None = None
    target_price: float | None = None
    # Non-blocking signals from engine.risk.evaluate. Known: wash_sale_warning,
    # sector_unknown. UI dispatches on the literal string.
    informational_flags: list[str] = Field(default_factory=list)
    rationale: str
    bull_case: str
    bear_case: str
    risk_level: RiskLevel
    conviction_level: RiskLevel
    proposed_at: datetime
    expires_at: datetime | None = None


class DecisionRequest(CamelCaseModel):
    outcome: Literal["approved", "declined"]
    note: str | None = Field(default=None, max_length=2000)


class DecisionResponse(CamelCaseModel):
    proposal_id: str
    outcome: DecisionOutcome
    decided_at: datetime
