"""/api/v1/decisions — decision-level audit reads.

GET /api/v1/decisions/{decision_id}/timeline
    The trade biography: proposed → risk verdict → your decision →
    fills → close → grade, assembled from the audit tables.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.decisions import DecisionTimelineResponse, TimelineEventDto
from app.services.biography_service import build_biography

router = APIRouter(prefix="/decisions", tags=["decisions"])


def _postgres_active() -> bool:
    v = os.environ.get("USE_POSTGRES")
    return v is not None and v.strip().lower() in ("1", "true", "yes", "on")


@router.get(
    "/{decision_id}/timeline",
    response_model=DecisionTimelineResponse,
    response_model_by_alias=True,
)
async def timeline(
    decision_id: str,
    user: AuthedUser = Depends(get_current_user),
) -> DecisionTimelineResponse:
    _ = user  # Phase-0 single-user store; auth gate only.
    if not _postgres_active():
        raise HTTPException(
            status_code=404,
            detail="decision timelines require the Postgres store (USE_POSTGRES=1)",
        )
    bio = await build_biography(decision_id)
    if bio is None:
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
