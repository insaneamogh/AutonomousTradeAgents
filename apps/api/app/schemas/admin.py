"""Wire schemas for /api/v1/admin."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


def _camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


class _Base(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True)


class TradingModeResponse(_Base):
    mode: Literal["paper", "live"]
    source: Literal["env", "admin", "default"]
    locked: bool
    """True when env LIVE_TRADING_ENABLED pins it off; the switch cannot override."""
    is_admin: bool
    updated_by: str | None = None
    updated_at: datetime | None = None


class SetTradingModeRequest(_Base):
    mode: Literal["paper", "live"]
    confirm: str | None = None
    """Switching to live requires exactly ``ENABLE LIVE TRADING``."""
