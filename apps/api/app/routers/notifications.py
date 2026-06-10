"""/api/v1/notifications — device registration + revoke.

Phase 3 push-notifications surface. Routes:

  POST   /api/v1/notifications/register-device
         Body { expoPushToken, platform, label? }. Idempotent on
         (userId, expoPushToken). Returns the device row.

  GET    /api/v1/notifications/devices
         List the caller's devices (active + revoked).

  DELETE /api/v1/notifications/devices/{id}
         Revoke. Idempotent on already-revoked.

All routes require_real_auth — device tokens are inherently per-user.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.middleware.auth import AuthedUser, require_real_auth
from app.schemas.notifications import (
    DeviceListResponse,
    DeviceTokenResponse,
    RegisterDeviceRequest,
)
from app.services.notification_store import (
    DeviceTokenRecord,
    NotificationStore,
    get_notification_store,
)

logger = logging.getLogger("api.router.notifications")

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _to_response(rec: DeviceTokenRecord) -> DeviceTokenResponse:
    return DeviceTokenResponse(
        id=rec.id,
        platform=rec.platform,
        label=rec.label,
        created_at=rec.created_at,
        last_seen_at=rec.last_seen_at,
        revoked_at=rec.revoked_at,
    )


@router.post(
    "/register-device",
    response_model=DeviceTokenResponse,
    response_model_by_alias=True,
)
async def register_device(
    body: RegisterDeviceRequest,
    user: AuthedUser = Depends(require_real_auth),
    store: NotificationStore = Depends(get_notification_store),
) -> DeviceTokenResponse:
    rec = await store.register_device(
        user_id=user.id,
        expo_push_token=body.expo_push_token,
        platform=body.platform,
        label=body.label,
    )
    logger.info(
        "notifications: registered device user=%s platform=%s label=%s",
        user.id, body.platform, body.label,
    )
    return _to_response(rec)


@router.get(
    "/devices",
    response_model=DeviceListResponse,
    response_model_by_alias=True,
)
async def list_devices(
    user: AuthedUser = Depends(require_real_auth),
    store: NotificationStore = Depends(get_notification_store),
) -> DeviceListResponse:
    rows = await store.list_devices(user.id)
    return DeviceListResponse(devices=[_to_response(r) for r in rows])


@router.delete(
    "/devices/{device_id}",
    response_model=DeviceTokenResponse,
    response_model_by_alias=True,
)
async def revoke_device(
    device_id: str,
    user: AuthedUser = Depends(require_real_auth),
    store: NotificationStore = Depends(get_notification_store),
) -> DeviceTokenResponse:
    rec = await store.get_device(device_id)
    if rec is None or rec.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="device not found",
        )
    await store.revoke_device(device_id)
    fresh = await store.get_device(device_id)
    assert fresh is not None
    return _to_response(fresh)
