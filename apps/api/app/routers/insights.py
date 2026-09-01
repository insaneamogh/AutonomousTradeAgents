"""/api/v1/ghost + /api/v1/risk + /api/v1/insights/funnel.

GET /api/v1/ghost/summary?windowDays=30
    "Risk saved you $X / your passes cost you $Y" headline numbers.

GET /api/v1/risk/vetoes?windowDays=30
    Per-rule veto ledger: count, blocked notional, prevented loss
    (where a finalized ghost outcome exists).

GET /api/v1/insights/funnel?windowDays=30&limit=20
    The contract funnel — how many candidates survived each selection
    stage, aggregated across the window plus the most recent runs.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.base import CamelCaseModel
from app.services.council.funnel_service import FunnelStage, build_funnel_report
from app.services.council.ghost_service import (
    build_ghost_summary,
    build_veto_exemplar,
    build_veto_ledger,
)
from engine.env import env_flag

router = APIRouter(tags=["insights"])


def _require_postgres() -> None:
    if not env_flag("USE_POSTGRES"):
        raise HTTPException(
            status_code=404, detail="insights require the Postgres store (USE_POSTGRES=1)"
        )


class GhostBucketDto(CamelCaseModel):
    count: int
    ghost_pnl: float
    pending_count: int
    # None when pendingCount is 0 (nothing left to explain) or when this
    # response predates the field. Both describe the OLDEST still-marking
    # row — the next one expected to finalize — so the frontend can say
    # WHEN "pending" resolves instead of just that it is pending.
    oldest_pending_triggered_at: str | None = None
    oldest_pending_remaining_trading_days: int | None = None
    # Marks-so-far, including rows still `partial`. PROVISIONAL: a partial
    # can still move before its horizon. Exists because a ghost finalizes
    # only after `horizonDays` TRADING days, so `ghostPnl` is 0.00 for the
    # first week of any account's life while real marked counterfactuals
    # already sit in the table. Render as "so far", never as the claim.
    marked_pnl: float = 0.0
    marked_count: int = 0


class GhostSummaryResponse(CamelCaseModel):
    window_days: int
    as_of: str
    vetoed: GhostBucketDto
    declined: GhostBucketDto
    saved_usd: float
    missed_usd: float
    # Provisional counterparts — see GhostBucketDto.markedPnl.
    saved_so_far_usd: float = 0.0
    missed_so_far_usd: float = 0.0


class VetoRuleDto(CamelCaseModel):
    rule: str
    count: int
    blocked_notional: float
    ghost_pnl: float | None = None
    prevented_loss_usd: float | None = None
    last_at: str | None = None


class TrimRuleDto(CamelCaseModel):
    rule: str
    count: int


class VetoLedgerResponse(CamelCaseModel):
    window_days: int
    total_vetoes: int
    total_blocked_notional: float
    rules: list[VetoRuleDto] = Field(default_factory=list)
    trims: list[TrimRuleDto] = Field(default_factory=list)
    """Partial refusals — rules that resized a trade instead of stopping
    it. A separate list, never summed into ``totalVetoes``: a trim let a
    (smaller) trade through, a veto did not."""
    total_trims: int = 0


class VetoExemplarResponse(CamelCaseModel):
    decision_id: str
    rule: str
    symbol: str
    side: str
    qty: int
    entry_price: float
    last_price: float | None = None
    ghost_pnl: float
    prevented_loss_usd: float
    is_option: bool
    occ_symbol: str | None = None
    bull_case: str
    bear_case: str
    rationale: str
    estimated_notional: float | None = None
    triggered_at: str
    horizon_days: int


@router.get("/ghost/summary", response_model=GhostSummaryResponse, response_model_by_alias=True)
async def ghost_summary(
    window_days: int = Query(default=30, ge=1, le=365, alias="windowDays"),
    user: AuthedUser = Depends(get_current_user),
) -> GhostSummaryResponse:
    _require_postgres()
    s = await build_ghost_summary(window_days, user_id=user.id)
    return GhostSummaryResponse(
        window_days=s.window_days,
        as_of=s.as_of.isoformat(),
        vetoed=GhostBucketDto(
            count=s.vetoed.count,
            ghost_pnl=s.vetoed.ghost_pnl,
            pending_count=s.vetoed.pending_count,
            oldest_pending_triggered_at=(
                s.vetoed.oldest_pending_triggered_at.isoformat()
                if s.vetoed.oldest_pending_triggered_at
                else None
            ),
            oldest_pending_remaining_trading_days=s.vetoed.oldest_pending_remaining_trading_days,
            marked_pnl=s.vetoed.marked_pnl,
            marked_count=s.vetoed.marked_count,
        ),
        declined=GhostBucketDto(
            count=s.declined.count,
            ghost_pnl=s.declined.ghost_pnl,
            pending_count=s.declined.pending_count,
            oldest_pending_triggered_at=(
                s.declined.oldest_pending_triggered_at.isoformat()
                if s.declined.oldest_pending_triggered_at
                else None
            ),
            oldest_pending_remaining_trading_days=s.declined.oldest_pending_remaining_trading_days,
            marked_pnl=s.declined.marked_pnl,
            marked_count=s.declined.marked_count,
        ),
        saved_usd=s.saved_usd,
        missed_usd=s.missed_usd,
        saved_so_far_usd=s.saved_so_far_usd,
        missed_so_far_usd=s.missed_so_far_usd,
    )


@router.get("/risk/vetoes", response_model=VetoLedgerResponse, response_model_by_alias=True)
async def veto_ledger(
    window_days: int = Query(default=30, ge=1, le=365, alias="windowDays"),
    user: AuthedUser = Depends(get_current_user),
) -> VetoLedgerResponse:
    _require_postgres()
    ledger = await build_veto_ledger(window_days, user_id=user.id)
    return VetoLedgerResponse(
        window_days=ledger.window_days,
        total_vetoes=ledger.total_vetoes,
        total_blocked_notional=ledger.total_blocked_notional,
        rules=[
            VetoRuleDto(
                rule=r.rule,
                count=r.count,
                blocked_notional=r.blocked_notional,
                ghost_pnl=r.ghost_pnl,
                prevented_loss_usd=r.prevented_loss_usd,
                last_at=r.last_at.isoformat() if r.last_at else None,
            )
            for r in ledger.rules
        ],
        trims=[TrimRuleDto(rule=t.rule, count=t.count) for t in ledger.trims],
        total_trims=ledger.total_trims,
    )


@router.get(
    "/risk/vetoes/{rule}/exemplar",
    response_model=VetoExemplarResponse,
    response_model_by_alias=True,
)
async def veto_exemplar(
    rule: str,
    user: AuthedUser = Depends(get_current_user),
) -> VetoExemplarResponse:
    """The single most extreme refusal under ``rule`` — largest
    ``abs(ghostPnl)`` among finalized ghosts, never the most recent."""
    _require_postgres()
    exemplar = await build_veto_exemplar(rule, user_id=user.id)
    if exemplar is None:
        raise HTTPException(
            status_code=404,
            detail=f"no finalized ghost outcome yet for rule {rule!r}",
        )
    return VetoExemplarResponse(
        decision_id=exemplar.decision_id,
        rule=exemplar.rule,
        symbol=exemplar.symbol,
        side=exemplar.side,
        qty=exemplar.qty,
        entry_price=exemplar.entry_price,
        last_price=exemplar.last_price,
        ghost_pnl=exemplar.ghost_pnl,
        prevented_loss_usd=exemplar.prevented_loss_usd,
        is_option=exemplar.is_option,
        occ_symbol=exemplar.occ_symbol,
        bull_case=exemplar.bull_case,
        bear_case=exemplar.bear_case,
        rationale=exemplar.rationale,
        estimated_notional=exemplar.estimated_notional,
        triggered_at=exemplar.triggered_at.isoformat(),
        horizon_days=exemplar.horizon_days,
    )


# ─────────────────────────────────────────────────────────────────────
# /api/v1/insights/funnel — the contract funnel
# ─────────────────────────────────────────────────────────────────────


class FunnelStageDto(CamelCaseModel):
    key: str
    label: str
    survivors: int
    dropped: int


class FunnelRunDto(CamelCaseModel):
    decision_id: str
    symbol: str
    triggered_at: str
    stages: list[FunnelStageDto] = Field(default_factory=list)
    rejection_reason: str | None = None
    rejection_stage: str | None = None
    selected_occ: str | None = None
    outcome: str


class FunnelAggregateDto(CamelCaseModel):
    """Summed across the window — the headline number."""

    stages: list[FunnelStageDto] = Field(default_factory=list)
    runs: int
    bought: int
    top_rejection_reasons: list[dict[str, Any]] = Field(default_factory=list)


class FunnelResponse(CamelCaseModel):
    window_days: int
    aggregate: FunnelAggregateDto
    recent: list[FunnelRunDto] = Field(default_factory=list)


def _stage_dtos(stages: list[FunnelStage]) -> list[FunnelStageDto]:
    return [
        FunnelStageDto(key=s.key, label=s.label, survivors=s.survivors, dropped=s.dropped)
        for s in stages
    ]


@router.get("/insights/funnel", response_model=FunnelResponse, response_model_by_alias=True)
async def funnel(
    window_days: int = Query(default=30, ge=1, le=365, alias="windowDays"),
    limit: int = Query(default=20, ge=1, le=200),
    user: AuthedUser = Depends(get_current_user),
) -> FunnelResponse:
    _require_postgres()
    report = await build_funnel_report(window_days, user_id=user.id, limit=limit)
    return FunnelResponse(
        window_days=report.window_days,
        aggregate=FunnelAggregateDto(
            stages=_stage_dtos(report.aggregate.stages),
            runs=report.aggregate.runs,
            bought=report.aggregate.bought,
            top_rejection_reasons=report.aggregate.top_rejection_reasons,
        ),
        recent=[
            FunnelRunDto(
                decision_id=r.decision_id,
                symbol=r.symbol,
                triggered_at=r.triggered_at.isoformat(),
                stages=_stage_dtos(r.stages),
                rejection_reason=r.rejection_reason,
                rejection_stage=r.rejection_stage,
                selected_occ=r.selected_occ,
                outcome=r.outcome,
            )
            for r in report.recent
        ],
    )
