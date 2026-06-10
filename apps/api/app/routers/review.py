"""/api/v1/review — operator hand-grading for Phase 4 month-1 review.

  GET    /api/v1/review/queue?windowDays=30
         Decisions in window with realized_pnl set that the operator
         hasn't graded yet.

  POST   /api/v1/review/{decision_id}
         Body { grade: "good" | "bad" | "skip", notes?: str }.
         Idempotent upsert on (decision_id, operator_user_id).

  GET    /api/v1/review/agreement?windowDays=30
         Bucket stats + agreement_pct between operator grades and the
         strategy_confidence drift direction from Reflection.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.review import (
    AgreementResponse,
    GradeRequest,
    GradeResponse,
    ReviewQueueResponse,
)
from app.services.review_service import (
    DecisionNotReviewable,
    apply_grade,
    build_agreement,
    build_queue,
)

logger = logging.getLogger("api.router.review")

router = APIRouter(prefix="/review", tags=["review"])


@router.get(
    "/queue",
    response_model=ReviewQueueResponse,
    response_model_by_alias=True,
)
async def queue(
    window_days: int = Query(default=30, ge=1, le=365, alias="windowDays"),
    user: AuthedUser = Depends(get_current_user),
) -> ReviewQueueResponse:
    return await build_queue(operator_user_id=user.id, window_days=window_days)


@router.post(
    "/{decision_id}",
    response_model=GradeResponse,
    response_model_by_alias=True,
)
async def grade(
    decision_id: str,
    body: GradeRequest,
    user: AuthedUser = Depends(get_current_user),
) -> GradeResponse:
    try:
        return await apply_grade(
            operator_user_id=user.id,
            decision_id=decision_id,
            grade=body.grade,
            notes=body.notes,
        )
    except DecisionNotReviewable as exc:
        # 404 covers "no such decision" AND "decision is still open"
        # — both are "you can't grade this right now" from the caller's
        # perspective.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc


@router.get(
    "/agreement",
    response_model=AgreementResponse,
    response_model_by_alias=True,
)
async def agreement(
    window_days: int = Query(default=30, ge=1, le=365, alias="windowDays"),
    user: AuthedUser = Depends(get_current_user),
) -> AgreementResponse:
    return await build_agreement(operator_user_id=user.id, window_days=window_days)
