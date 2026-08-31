"""Approval / decision schemas."""

from __future__ import annotations

from datetime import date, datetime
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
    # ── Short-side facts (all optional so existing partial-kwarg call
    # sites, tests included, keep constructing this DTO unchanged) ────
    # "long" is the correct default: every pre-short-support caller is
    # implicitly long, and the drafter always sets this explicitly now.
    direction: Literal["long", "short"] = "long"
    opens_short: bool = False
    # Broker borrow flags off state["context"]["asset"] — None means
    # "never verified," which the risk engine's shortable_check treats as
    # a veto, not as False. Never populated by the LLM.
    shortable: bool | None = None
    easy_to_borrow: bool | None = None
    # ── Options facts (Phase A: long calls/puts only; all optional so
    # existing equity call sites/tests keep constructing this DTO
    # unchanged) ──────────────────────────────────────────────────────
    is_option: bool = False
    # Restricting to these two literal values is a free, defense-in-depth
    # 422 before engine.risk.evaluate() ever runs — complementary to, not
    # a replacement for, the naked_short_forbidden risk rule, since
    # engine.risk.types is deliberately Pydantic-free and gets no
    # validation of its own at this boundary.
    option_action: Literal["buy_to_open", "sell_to_close"] | None = None
    occ_symbol: str | None = None
    strike: float | None = None
    expiry_date: date | None = None
    contract_type: Literal["call", "put"] | None = None
    multiplier: int = 1
    # Liquidity + pricing snapshot at Drafter-time — mirrors
    # ``engine.risk.types.OptionLegDetails`` field-for-field so the
    # executor's execution-time re-risk-check (``illiquid_contract``,
    # ``iv_unavailable``, ``earnings_blackout``) can rebuild the SAME
    # ``OptionLegDetails`` it re-verifies against, reading this DTO back
    # off the persisted decision row rather than re-fetching the chain.
    # All optional so every existing equity call site/test keeps
    # constructing this DTO unchanged.
    open_interest: int | None = None
    volume: int | None = None
    bid: float | None = None
    ask: float | None = None
    implied_volatility: float | None = None
    days_to_earnings: int | None = None
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
    # The council's confidence (0-1) in this trade. Optional because rows
    # written before it was persisted have no value — None means "not
    # recorded", which makes min_council_confidence self-gate out at the
    # approval-time re-check instead of scoring a fabricated stand-in.
    # NOT interchangeable with conviction_level: that is a 1-5 bet size.
    council_confidence: float | None = None
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
