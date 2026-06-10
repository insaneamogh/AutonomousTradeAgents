"""Wire schemas for /api/v1/notifications."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def _camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


class _Base(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class RegisterDeviceRequest(_Base):
    expo_push_token: str = Field(min_length=1, max_length=255)
    platform: Literal["ios", "android", "web"]
    label: str | None = Field(default=None, max_length=120)


class DeviceTokenResponse(_Base):
    id: str
    platform: str
    label: str | None
    created_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None


class DeviceListResponse(_Base):
    devices: list[DeviceTokenResponse]
