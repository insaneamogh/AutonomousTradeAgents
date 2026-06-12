"""Wire schemas for /api/v1/watchlist."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.base import CamelCaseModel


class WatchlistItemDto(CamelCaseModel):
    id: str
    symbol: str
    # v1 is stocks + ETFs only (locked). The field exists so options can
    # appear later without a wire-format change.
    asset_class: Literal["equity"] = "equity"
    active: bool = True
    created_at: datetime


class AddWatchlistRequest(CamelCaseModel):
    symbol: str = Field(min_length=1, max_length=10)
