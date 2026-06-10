"""Wire schemas for /api/v1/broker.

camelCase on the wire; snake_case in Python via ``alias_generator``.
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
# Start
# ─────────────────────────────────────────────────────────────────────


class StartOAuthRequest(_Base):
    is_paper: bool = Field(
        default=True,
        description="True = Alpaca paper trading; False = live. Default paper.",
    )


class StartOAuthResponse(_Base):
    authorize_url: str
    state: str = Field(description="CSRF token the client returns on /callback.")
    expires_at: datetime
    dev_warning: str | None = Field(
        default=None,
        description="Set when the API is using the dev fallback encryption key.",
    )


# ─────────────────────────────────────────────────────────────────────
# Callback
# ─────────────────────────────────────────────────────────────────────


class CallbackRequest(_Base):
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=512)


class BrokerConnectionResponse(_Base):
    id: str
    broker: str
    is_paper: bool
    account_number: str | None
    status: str
    """active | revoked | expired"""
    created_at: datetime
    last_used_at: datetime | None = None


class CallbackResponse(_Base):
    connection: BrokerConnectionResponse


# ─────────────────────────────────────────────────────────────────────
# Zerodha (Kite Connect) — request-token flow, not OAuth
# ─────────────────────────────────────────────────────────────────────


class StartZerodhaResponse(_Base):
    login_url: str = Field(description="Kite login page to open in a browser.")
    state: str = Field(description="CSRF token echoed back via redirect_params.")
    expires_at: datetime
    dev_warning: str | None = Field(
        default=None,
        description="Set when the API is using the dev fallback encryption key.",
    )


class ZerodhaCallbackRequest(_Base):
    request_token: str = Field(min_length=1, max_length=512)
    state: str = Field(min_length=1, max_length=512)
