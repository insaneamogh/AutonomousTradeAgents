"""/api/v1/decisions — decision-level audit reads.

GET /api/v1/decisions
    The browsable list — every council pass for the caller, newest
    first, whether or not it ever became a proposal. Before this there
    was no way to reach a decision's id at all unless it had been
    approved (Positions) or was still pending (approvals/pending) — a
    HOLD from a strategy-fit short-circuit, the majority of council runs
    on any given sweep, was invisible the instant the sweep moved on.

GET /api/v1/decisions/{decision_id}/timeline
    The trade biography: proposed → risk verdict → your decision →
    fills → close → grade, assembled from the audit tables.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.decisions import (
    DecisionListResponse,
    DecisionTimelineResponse,
    TimelineEventDto,
)
from app.services.council.biography_service import build_biography
from app.services.council.decisions_list import list_decisions
from engine.env import env_flag

router = APIRouter(prefix="/decisions", tags=["decisions"])


@router.get("", response_model=DecisionListResponse, response_model_by_alias=True)
async def list_all(
    user: AuthedUser = Depends(get_current_user),
    symbol: str | None = Query(default=None, max_length=10),
    action: str | None = Query(default=None, max_length=10),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> DecisionListResponse:
    """Every council decision for the caller, newest first.

    ``action`` filters on ``final_action`` (BUY/SELL/HOLD); ``symbol`` is
    an exact-match ticker filter. Mock-store dev mode has no decision
    ledger and returns an honest empty page rather than 404ing.
    """
    if not env_flag("USE_POSTGRES"):
        return DecisionListResponse(decisions=[], total=0, limit=limit, offset=offset)
    rows, total = await list_decisions(
        user_id=user.id,
        symbol=symbol.upper() if symbol else None,
        action=action.upper() if action else None,
        limit=limit,
        offset=offset,
    )
    return DecisionListResponse(decisions=rows, total=total, limit=limit, offset=offset)


@router.get(
    "/{decision_id}/timeline",
    response_model=DecisionTimelineResponse,
    response_model_by_alias=True,
)
async def timeline(
    decision_id: str,
    user: AuthedUser = Depends(get_current_user),
) -> DecisionTimelineResponse:
    if not env_flag("USE_POSTGRES"):
        raise HTTPException(
            status_code=404,
            detail="decision timelines require the Postgres store (USE_POSTGRES=1)",
        )
    bio = await build_biography(decision_id, user_id=user.id)
    if bio is None:
        # Same 404 for "no such decision" and "not yours" — the response
        # must not tell the caller which of the two it was.
        raise HTTPException(status_code=404, detail="decision not found")
    return DecisionTimelineResponse(
        decision_id=bio.decision_id,
        symbol=bio.symbol,
        side=bio.side,
        status=bio.status,
        events=[
            TimelineEventDto(
                kind=e.kind,
                at=e.at.isoformat() if e.at else None,
                title=e.title,
                detail=e.detail,
                data=e.data,
            )
            for e in bio.events
        ],
    )
