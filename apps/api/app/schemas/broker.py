"""Wire schemas for /api/v1/broker.

camelCase on the wire; snake_case in Python via ``alias_generator``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.notifications.notification_store import Platform


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
    platform: Platform | None = Field(
        default=None,
        description=(
            "Client hint used ONLY to pick which of two fixed, server-known "
            "redirect URIs the server sends to Alpaca — never a caller-"
            "supplied URL (that would be an open-redirect / auth-code-"
            "hijack risk). 'web' selects the HTTPS browser-redirect landing "
            "page (GET /connect/alpaca/redirect); anything else — including "
            "unset, which is what the native app sends — keeps today's "
            "native deep-link default untouched."
        ),
    )


class StartOAuthResponse(_Base):
    authorize_url: str
    state: str = Field(description="CSRF token the client returns on /callback.")
    expires_at: datetime
    dev_warning: str | None = Field(
        default=None,
        description=(
            "Human-readable, for display. May combine multiple independent "
            "warnings (dev encryption key, unconfigured OAuth client) — not "
            "meant to be parsed. See oauth_not_configured for the specific, "
            "machine-readable signal that following authorize_url is "
            "guaranteed to fail."
        ),
    )
    oauth_not_configured: bool = Field(
        default=False,
        description=(
            "True when ALPACA_OAUTH_CLIENT_ID/_SECRET are unset — the dev "
            "placeholder client id would go out in authorize_url, which "
            "Alpaca's own OAuth app always rejects. Callers should show "
            "dev_warning instead of navigating to authorize_url when this "
            "is true; string-matching dev_warning's prose is NOT a stable "
            "contract, this field is."
        ),
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
    live_trading_consent: bool = False
    """Per-connection real-money opt-in. Live orders also require the global
    LIVE_TRADING_ENABLED env."""
    connection_source: Literal["environment", "oauth"] = "oauth"
    """Not persisted — recomputed on every response (see
    ``routers.broker._connection_source``) by decrypting the stored token
    and comparing it against ``env_bootstrap.ALPACA_ENV_SENTINEL``.
    "environment" means ``ensure_env_broker_connection`` created this row
    from the API process's own ``ALPACA_API_KEY``/``ALPACA_SECRET_KEY``
    rather than a per-user OAuth grant — the UI uses this to warn that
    revoking it will relink automatically on the next boot while the server
    still has those keys configured."""
    created_at: datetime
    last_used_at: datetime | None = None


class SetConsentRequest(_Base):
    enabled: bool


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
