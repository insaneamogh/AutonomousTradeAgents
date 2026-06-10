"""Agent-run schemas."""

from __future__ import annotations

from typing import Literal

from app.schemas.approvals import ApprovalProposalDto
from app.schemas.base import CamelCaseModel


class AgentRunRequest(CamelCaseModel):
    symbol: str
    horizon: Literal["intraday", "short", "mid", "long"] = "short"


class AgentRunResponse(CamelCaseModel):
    """Result of a council run. ``proposal`` is null when the council holds or
    risk vetoes the trade — in that case ``risk_reason`` explains why."""

    proposal: ApprovalProposalDto | None
    final_action: Literal["BUY", "SELL", "HOLD", "VETOED"]
    risk_approved: bool
    risk_reason: str
    risk_veto_rule: str | None = None
    regime: str | None = None
    llm_mock: bool


class AgentRunStartResponse(CamelCaseModel):
    """202 from POST /agent/run/start — poll the progress endpoint next."""

    run_id: str
    symbol: str


class CouncilProgressEvent(CamelCaseModel):
    """One node transition in the council theater feed."""

    seq: int
    node: Literal[
        "router", "technical", "fundamental", "macro", "selector", "drafter", "risk_officer"
    ]
    status: Literal["started", "completed", "skipped"]
    at: str
    summary: dict | None = None


class CouncilProgressResponse(CamelCaseModel):
    """Polled run state. ``result`` appears once status leaves 'running'."""

    run_id: str
    status: Literal["running", "completed", "failed"]
    events: list[CouncilProgressEvent]
    result: AgentRunResponse | None = None
    error: str | None = None
