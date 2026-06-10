"""/api/v1/portfolio — per-broker profit window + live positions.

GET /portfolio/summary?windowDays=30

One entry per active broker connection (alpaca USD, zerodha INR — never
summed). A broken broker (expired Zerodha daily token, network) degrades
to ``status: token_expired | unavailable`` instead of failing the route.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.portfolio import PortfolioSummaryResponse
from app.services.portfolio_service import build_portfolio_summary

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get(
    "/summary",
    response_model=PortfolioSummaryResponse,
    response_model_by_alias=True,
)
async def portfolio_summary(
    user: AuthedUser = Depends(get_current_user),
    window_days: int = Query(default=30, ge=1, le=365, alias="windowDays"),
) -> PortfolioSummaryResponse:
    """Per-broker equity, positions, and a realized+unrealized profit window."""
    return await build_portfolio_summary(user.id, window_days=window_days)
