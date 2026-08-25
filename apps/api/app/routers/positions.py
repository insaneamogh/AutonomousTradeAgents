"""/api/v1/positions — open agent positions + user-initiated close.

GET  /api/v1/positions                     open agent-managed positions
POST /api/v1/positions/{decision_id}/close close one now (manual override)

The close is the in-app counterpart to "let the agent handle it": the user
can flatten a position themselves at any time. It routes through the SAME
deterministic risk gate + bracket-cancel + audit persist as the agent's
own closes — only the recorded ``close_reason`` differs ('user_manual').
Entries are never auto-placed; this is purely an exit control.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.middleware.auth import AuthedUser, get_current_user, require_real_auth
from app.schemas.positions import ClosePositionResponse, OpenPositionDto
from app.services.orders.positions_service import list_open_positions

logger = logging.getLogger("api.router.positions")

router = APIRouter(prefix="/positions", tags=["positions"])

# error code → HTTP status for the close endpoint.
_CLOSE_ERROR_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "not_owner": status.HTTP_404_NOT_FOUND,  # don't reveal another user's id exists
    "already_closed": status.HTTP_409_CONFLICT,
    "no_open_position": status.HTTP_409_CONFLICT,
    "close_in_flight": status.HTTP_409_CONFLICT,
}


@router.get("", response_model=list[OpenPositionDto], response_model_by_alias=True)
async def open_positions(
    user: AuthedUser = Depends(get_current_user),
) -> list[OpenPositionDto]:
    """Open agent-managed positions for the caller, with live marks + exit plan."""
    return await list_open_positions(user.id)


@router.post(
    "/{decision_id}/close",
    response_model=ClosePositionResponse,
    response_model_by_alias=True,
)
async def close_position(
    decision_id: str,
    user: AuthedUser = Depends(require_real_auth),
) -> ClosePositionResponse:
    """Flatten the position behind ``decision_id`` now. Idempotent-ish: a
    close already in flight returns 409 rather than double-submitting."""
    from app.services.orders.position_manager import close_position_now
    from engine.db.session import async_session_factory

    result = await close_position_now(
        user_id=user.id,
        decision_id=decision_id,
        session_factory=async_session_factory(),
    )

    err = result.get("error")
    if err in _CLOSE_ERROR_STATUS:
        raise HTTPException(status_code=_CLOSE_ERROR_STATUS[err], detail=err)

    # risk_vetoed (or any other non-fatal) → 200 with closed=False so the
    # client can show "couldn't close — risk rule blocked it".
    return ClosePositionResponse(
        decision_id=decision_id,
        closed=bool(result.get("closed")),
        error=err,
        detail=(
            "Close blocked by a risk rule — try again shortly."
            if err == "risk_vetoed"
            else None
        ),
    )
