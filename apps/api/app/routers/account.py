"""/api/v1/account — broker connection status + cash / equity snapshot."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.middleware.auth import AuthedUser, get_current_user
from app.schemas.account import AccountResponse
from app.services.store import get_store

router = APIRouter(prefix="/account", tags=["account"])


@router.get("", response_model=AccountResponse, response_model_by_alias=True)
async def get_account(
    user: AuthedUser = Depends(get_current_user),
) -> AccountResponse:
    """Current broker-account snapshot. Phase 0 reads from the mock store;
    Phase 1 hits Alpaca + Postgres via the reconciler's cache.

    Auth: requires a valid Bearer access token OR ``DEV_AUTH_BYPASS=1`` for
    the Phase 3 transition. Per-user filtering ships once PostgresStore
    learns to read user_id (Phase 3 follow-on).
    """
    _ = user  # Phase 3 follow-on: pass user.id into store.get_account
    store = get_store()
    return await store.get_account()
