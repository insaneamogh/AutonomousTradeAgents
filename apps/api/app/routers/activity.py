"""/api/v1/activity — agent decision feed for the Home dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.activity import ActivityEntryDto
from app.services.store import get_store

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("", response_model=list[ActivityEntryDto], response_model_by_alias=True)
async def list_activity(
    limit: int = Query(default=50, ge=1, le=200),
    user: AuthedUser = Depends(get_current_user),
) -> list[ActivityEntryDto]:
    """Recent agent activity, newest first. Phase 0 reads the mock store;
    Phase 1 paginates over the ``agent_decisions`` + ``order_fills`` tables.

    Auth: requires a valid Bearer access token OR ``DEV_AUTH_BYPASS=1``.
    """
    _ = user  # Phase 3 follow-on: per-user filter
    store = get_store()
    return await store.list_activity(limit=limit)
