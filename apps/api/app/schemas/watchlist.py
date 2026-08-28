"""Wire schemas for /api/v1/watchlist."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.base import CamelCaseModel


class WatchlistItemDto(CamelCaseModel):
    id: str
    symbol: str
    # Phase A widening: "equity" (stocks/ETFs) or "option" (long calls/puts
    # only — docs/OPTIONS_PLAN.md). Was Literal["equity"] pre-widening.
    asset_class: Literal["equity", "option"] = "equity"
    active: bool = True
    created_at: datetime


class AddWatchlistRequest(CamelCaseModel):
    symbol: str = Field(min_length=1, max_length=10)
    # Optional so pre-widening callers/tests that never sent this keep
    # constructing the request unchanged — defaults to the only value v1
    # ever had.
    asset_class: Literal["equity", "option"] = "equity"
