"""/api/v1/health/full — aggregated system-status view.

Distinct from ``GET /health`` (the public liveness probe). The /full
endpoint returns per-component status the mobile Home screen renders
as a status strip.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.health import HealthResponse
from app.services.health import build_health_report

router = APIRouter(prefix="/health", tags=["health"])


@router.get(
    "/full",
    response_model=HealthResponse,
    response_model_by_alias=True,
)
async def full_health(
    user: AuthedUser = Depends(get_current_user),
) -> HealthResponse:
    """Per-component liveness for the calling user.

    Uses ``get_current_user`` (NOT ``require_real_auth``) so the strip
    still renders when DEV_AUTH_BYPASS=1 against the fixture user.
    """
    return await build_health_report(user_id=user.id)
