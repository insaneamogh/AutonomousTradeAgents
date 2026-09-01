"""/api/v1/positions — open agent positions + user-initiated close + history.

GET  /api/v1/positions                        open agent-managed positions
GET  /api/v1/positions/history                 closed positions, newest first
POST /api/v1/positions/{decision_id}/close     close one now (manual override)
POST /api/v1/positions/unmanaged/{symbol}/close  close a position with NO
                                                  decision behind it at all

The close is the in-app counterpart to "let the agent handle it": the user
can flatten a position themselves at any time. It routes through the SAME
deterministic risk gate + bracket-cancel + audit persist as the agent's
own closes — only the recorded ``close_reason`` differs ('user_manual').
Entries are never auto-placed; this is purely an exit control.

The second POST route exists because the first can't reach every row the
open-positions GET above lists: an "unmanaged" position (``managed=False``,
no ``decision_id``) was opened outside this app, or predates this
deployment's decision history, so there is no ``AgentDecision`` row to
look up by id. It is keyed by ``symbol`` instead — the broker's own
position key (OCC for an option, ticker for equity) — and ownership is
enforced structurally: it only ever acts inside the CALLING user's own
broker connection, so there is no cross-user id to guess in the first
place. See ``position_manager.close_unmanaged_position_now`` for the full
reasoning.

GET /history answers "what was opened and closed, and what did it realize"
— the question the open-positions list and the account-level P&L tile
cannot: an open position only shows unrealized P&L, and the equity number
never breaks down which trades actually contributed to it. See
``positions_service.list_closed_positions`` for the one gap (a position
this app never saw open at all has nothing here to join against either).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.middleware.auth import AuthedUser, get_current_user, require_real_auth
from app.schemas.positions import (
    ClosedPositionListResponse,
    ClosePositionResponse,
    OpenPositionDto,
)
from app.services.orders.positions_service import list_closed_positions, list_open_positions

logger = logging.getLogger("api.router.positions")

router = APIRouter(prefix="/positions", tags=["positions"])

# error code → HTTP status for the close endpoint.
_CLOSE_ERROR_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "not_owner": status.HTTP_404_NOT_FOUND,  # don't reveal another user's id exists
    "already_closed": status.HTTP_409_CONFLICT,
    "no_open_position": status.HTTP_409_CONFLICT,
    # The entry never filled and the order that would have filled it is
    # already gone (filled between the list and the tap, or cancelled
    # already) — nothing left to cancel.
    "no_pending_order": status.HTTP_409_CONFLICT,
    "close_in_flight": status.HTTP_409_CONFLICT,
}


@router.get("", response_model=list[OpenPositionDto], response_model_by_alias=True)
async def open_positions(
    user: AuthedUser = Depends(get_current_user),
) -> list[OpenPositionDto]:
    """Open agent-managed positions for the caller, with live marks + exit plan."""
    return await list_open_positions(user.id)


@router.get(
    "/history",
    response_model=ClosedPositionListResponse,
    response_model_by_alias=True,
)
async def closed_positions(
    user: AuthedUser = Depends(get_current_user),
    symbol: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ClosedPositionListResponse:
    """Closed positions for the caller, newest-close-first: what was
    opened, when, at what price, when and why it closed, and the realized
    P&L — see ``positions_service.list_closed_positions`` for exactly
    which closes this can (and cannot) see. Same read-only auth as the
    open-positions list above, so a read-only demo/judge session sees this
    too. Mock-store dev mode has no position ledger and returns an honest
    empty page rather than 404ing, matching GET /decisions.
    """
    rows, total = await list_closed_positions(
        user.id,
        symbol=symbol.upper() if symbol else None,
        limit=limit,
        offset=offset,
    )
    return ClosedPositionListResponse(positions=rows, total=total, limit=limit, offset=offset)


@router.post(
    "/unmanaged/{symbol}/close",
    response_model=ClosePositionResponse,
    response_model_by_alias=True,
)
async def close_unmanaged_position(
    symbol: str,
    user: AuthedUser = Depends(require_real_auth),
) -> ClosePositionResponse:
    """Flatten a broker position with NO agent decision behind it — see the
    module docstring. Registered ahead of ``/{decision_id}/close`` in this
    file (path shape differs — 3 segments vs. 2 — so the two never
    actually compete for the same request, but the more specific route
    reads clearer listed first).

    Ownership: enforced structurally, not by an owner-field comparison —
    this only ever looks inside the CALLING user's OWN broker connection
    (``with_broker_client(user.id, ...)`` inside
    ``close_unmanaged_position_now``), so there is no other user's
    position reachable through this path at all, regardless of what
    ``symbol`` is passed.
    """
    from app.services.orders.position_manager import close_unmanaged_position_now
    from engine.db.session import async_session_factory

    result = await close_unmanaged_position_now(
        user_id=user.id,
        symbol=symbol,
        session_factory=async_session_factory(),
    )

    err = result.get("error")
    if err in _CLOSE_ERROR_STATUS:
        raise HTTPException(status_code=_CLOSE_ERROR_STATUS[err], detail=err)

    return ClosePositionResponse(
        symbol=symbol,
        closed=bool(result.get("closed")),
        error=err,
        detail=(
            "Close blocked by a risk rule — try again shortly."
            if err == "risk_vetoed"
            else None
        ),
    )


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
