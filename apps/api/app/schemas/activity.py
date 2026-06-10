"""Activity feed schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from app.schemas.base import CamelCaseModel

ActivityKind = Literal["proposal", "approved", "declined", "filled", "vetoed"]
Side = Literal["BUY", "SELL"]
Verdict = Literal["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"]


class ActivityEntryDto(CamelCaseModel):
    id: str
    kind: ActivityKind
    symbol: str
    side: Side
    qty: int | None = None
    price: float | None = None
    verdict: Verdict | None = None
    headline: str
    timestamp: datetime
