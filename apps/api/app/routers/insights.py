"""/api/v1/ghost + /api/v1/risk — regret analytics + veto scorecard.

GET /api/v1/ghost/summary?windowDays=30
    "Risk saved you $X / your passes cost you $Y" headline numbers.

GET /api/v1/risk/vetoes?windowDays=30
    Per-rule veto ledger: count, blocked notional, prevented loss
    (where a finalized ghost outcome exists).
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.base import CamelCaseModel
from app.services.ghost_service import build_ghost_summary, build_veto_ledger

router = APIRouter(tags=["insights"])


def _require_postgres() -> None:
    v = os.environ.get("USE_POSTGRES")
    if not (v is not None and v.strip().lower() in ("1", "true", "yes", "on")):
        raise HTTPException(
            status_code=404, detail="insights require the Postgres store (USE_POSTGRES=1)"
        )


class GhostBucketDto(CamelCaseModel):
    count: int
    ghost_pnl: float
    pending_count: int


class GhostSummaryResponse(CamelCaseModel):
    window_days: int
    as_of: str
    vetoed: GhostBucketDto
    declined: GhostBucketDto
    saved_usd: float
    missed_usd: float


class VetoRuleDto(CamelCaseModel):
    rule: str
    count: int
    blocked_notional: float
    ghost_pnl: float | None = None
    prevented_loss_usd: float | None = None
    last_at: str | None = None


class VetoLedgerResponse(CamelCaseModel):
    window_days: int
    total_vetoes: int
    total_blocked_notional: float
    rules: list[VetoRuleDto] = Field(default_factory=list)


@router.get("/ghost/summary", response_model=GhostSummaryResponse, response_model_by_alias=True)
async def ghost_summary(
    window_days: int = Query(default=30, ge=1, le=365, alias="windowDays"),
    user: AuthedUser = Depends(get_current_user),
) -> GhostSummaryResponse:
    _ = user
    _require_postgres()
    s = await build_ghost_summary(window_days)
    return GhostSummaryResponse(
        window_days=s.window_days,
        as_of=s.as_of.isoformat(),
        vetoed=GhostBucketDto(
            count=s.vetoed.count, ghost_pnl=s.vetoed.ghost_pnl, pending_count=s.vetoed.pending_count
        ),
        declined=GhostBucketDto(
            count=s.declined.count,
            ghost_pnl=s.declined.ghost_pnl,
            pending_count=s.declined.pending_count,
        ),
        saved_usd=s.saved_usd,
        missed_usd=s.missed_usd,
    )


@router.get("/risk/vetoes", response_model=VetoLedgerResponse, response_model_by_alias=True)
async def veto_ledger(
    window_days: int = Query(default=30, ge=1, le=365, alias="windowDays"),
    user: AuthedUser = Depends(get_current_user),
) -> VetoLedgerResponse:
    _ = user
    _require_postgres()
    ledger = await build_veto_ledger(window_days)
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
    )
