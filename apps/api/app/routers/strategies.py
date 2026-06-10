"""/api/v1/strategies — per-strategy performance + priors view.

GET /api/v1/strategies/performance?windowDays=30
    → StrategiesPerformanceResponse.

Read-only. The mobile Strategies tab calls this. Phase 4 month-1 review
will lean on the same shape to hand-grade the agent's decisions.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.strategies import StrategiesPerformanceResponse
from app.services.strategies_perf import build_strategies_performance

router = APIRouter(prefix="/strategies", tags=["strategies"])


@router.get(
    "/performance",
    response_model=StrategiesPerformanceResponse,
    response_model_by_alias=True,
)
async def performance(
    window_days: int = Query(default=30, ge=1, le=365, alias="windowDays"),
    user: AuthedUser = Depends(get_current_user),
) -> StrategiesPerformanceResponse:
    _ = user  # Per-user filter when DecisionLog learns user_id
    return await build_strategies_performance(window_days=window_days)
