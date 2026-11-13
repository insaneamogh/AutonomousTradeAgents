"""/api/v1/admin — operator controls.

GET  /admin/trading-mode  any signed-in user: current mode + whether *they* may change it
POST /admin/trading-mode  admin only: switch paper <-> live at runtime

The switch is ONE of the keys for a real order (the connection's own
consent and a live connection are the others) and never touches
auto-approve, which stays paper-only. See docs/TRADING_MODE.md.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.middleware.auth import AuthedUser, admin_emails, require_admin, require_real_auth
from app.schemas.admin import SetTradingModeRequest, TradingModeResponse
from app.services.platform import trading_mode as tm

logger = logging.getLogger("api.admin")
router = APIRouter(prefix="/admin", tags=["admin"])

LIVE_CONFIRM_PHRASE = "ENABLE LIVE TRADING"


async def _response(*, is_admin: bool) -> TradingModeResponse:
    state = await tm.live_trading_state()
    rec = await tm.last_change()
    return TradingModeResponse(
        mode="live" if state.enabled else "paper",
        source=state.source,
        locked=state.locked,
        is_admin=is_admin,
        updated_by=rec.updated_by if rec else None,
        updated_at=rec.updated_at if rec else None,
    )


@router.get("/trading-mode", response_model=TradingModeResponse, response_model_by_alias=True)
async def get_trading_mode(user: AuthedUser = Depends(require_real_auth)) -> TradingModeResponse:
    return await _response(is_admin=user.email.strip().lower() in admin_emails())


@router.post("/trading-mode", response_model=TradingModeResponse, response_model_by_alias=True)
async def set_trading_mode(
    body: SetTradingModeRequest, admin: AuthedUser = Depends(require_admin)
) -> TradingModeResponse:
    state = await tm.live_trading_state()
    if state.locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="trading mode is locked off by the deployment (LIVE_TRADING_ENABLED); "
            "the admin switch cannot override it",
        )
    want_live = body.mode == "live"
    if want_live and body.confirm != LIVE_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'switching to live requires confirm == "{LIVE_CONFIRM_PHRASE}"',
        )
    try:
        await tm.set_live_trading(want_live, changed_by=admin.email)
    except Exception as exc:
        logger.exception("admin: failed to persist trading mode")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="could not save the trading mode; nothing changed",
        ) from exc

    logger.warning(
        "ADMIN trading-mode change: %s set platform to %s (was %s, source=%s)",
        admin.email, body.mode, "live" if state.enabled else "paper", state.source,
    )
    try:  # best-effort; never fails the switch
        from app.services.notifications.ops_alerts import raise_ops_alert

        raise_ops_alert(
            "trading_mode_changed",
            title=f"Platform switched to {body.mode.upper()} trading",
            body=f"{admin.email} set trading mode to {body.mode}.",
            user_id=admin.id,
            key=f"{body.mode}:{admin.email}",
            min_interval_s=0.0,
        )
    except Exception:
        logger.exception("admin: ops alert for trading-mode change failed")
    return await _response(is_admin=True)
