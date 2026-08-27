"""Activity feed schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from app.schemas.base import CamelCaseModel

ActivityKind = Literal["proposal", "approved", "declined", "filled", "vetoed", "hold"]
Side = Literal["BUY", "SELL"]
Verdict = Literal["STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"]


class ActivityEntryDto(CamelCaseModel):
    id: str
    kind: ActivityKind
    symbol: str
    # None for "hold" — a HOLD that never became a proposal has no side.
    # Defaulting it to "BUY" used to make every HOLD in this feed read as
    # an unnamed BUY that got vetoed.
    side: Side | None = None
    qty: int | None = None
    price: float | None = None
    verdict: Verdict | None = None
    headline: str
    timestamp: datetime
