"""Approval / decision schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.base import CamelCaseModel
from app.schemas.orders import OrderResponse

DecisionOutcome = Literal["approved", "declined", "expired"]
Side = Literal["BUY", "SELL"]
RiskLevel = Literal[1, 2, 3, 4, 5]
ExitMode = Literal["agent", "manual"]


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
    # ── Exit plan (what the approval card promises) ──────────────────
    # "The agent will close this at stop X, target Y, or after N days."
    time_stop_days: int = 5
    # Reward:risk of the plan — (target − entry) / (entry − stop).
    r_multiple: float | None = None
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
    # Per-position close delegation, chosen on the approval card. 'agent'
    # (default): bracket legs + time-stop + agent early-exit. 'manual':
    # the user owns the close entirely.
    exit_mode: ExitMode = "agent"
    note: str | None = Field(default=None, max_length=2000)


class DecisionResponse(CamelCaseModel):
    proposal_id: str
    outcome: DecisionOutcome
    decided_at: datetime
    # ── Server-side execution result (approve now executes) ─────────
    executed: bool = False
    order: OrderResponse | None = None
    risk_blocked: bool = False
    risk_veto_rule: str | None = None
    risk_reason: str | None = None
