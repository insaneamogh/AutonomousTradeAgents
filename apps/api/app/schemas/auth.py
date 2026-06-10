"""Wire schemas for /api/v1/auth.

Naming is camelCase on the wire (the mobile app speaks camelCase TS); the
Python field is snake_case + ``alias=``. Same pattern the rest of the app
uses for approvals + activity.
"""

from __future__ import annotations

from datetime import datetime

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


# ─────────────────────────────────────────────────────────────────────
# request-login
# ─────────────────────────────────────────────────────────────────────


class RequestLoginRequest(_Base):
    email: str = Field(min_length=3, max_length=255)


class RequestLoginResponse(_Base):
    expires_at: datetime = Field(description="When this magic-link expires.")
    # ``dev_token`` is None outside of local/dev. Mobile reads it only when
    # the API is running in dev mode; in prod the token is emailed.
    dev_token: str | None = Field(
        default=None,
        description="DEV-ONLY: the raw magic-link token. None in production.",
    )


# ─────────────────────────────────────────────────────────────────────
# verify-magic-link
# ─────────────────────────────────────────────────────────────────────


class VerifyMagicLinkRequest(_Base):
    email: str = Field(min_length=3, max_length=255)
    token: str
    device_id: str | None = None
    device_label: str | None = None


class IssuedTokensResponse(_Base):
    user_id: str
    email: str
    access_token: str
    refresh_token: str
    access_expires_in_seconds: int
    refresh_expires_in_seconds: int


# ─────────────────────────────────────────────────────────────────────
# refresh
# ─────────────────────────────────────────────────────────────────────


class RefreshRequest(_Base):
    refresh_token: str


# ─────────────────────────────────────────────────────────────────────
# logout
# ─────────────────────────────────────────────────────────────────────


class LogoutRequest(_Base):
    """Optional. If absent we revoke the session embedded in the access token."""

    refresh_token: str | None = None


class LogoutResponse(_Base):
    revoked: bool


# ─────────────────────────────────────────────────────────────────────
# /me (authenticated identity probe)
# ─────────────────────────────────────────────────────────────────────


class MeResponse(_Base):
    user_id: str
    email: str
    auth_method: str
