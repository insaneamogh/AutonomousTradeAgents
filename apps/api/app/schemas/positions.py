"""Wire schemas for /api/v1/positions — open agent positions + close.

One ``OpenPositionDto`` per open agent-managed decision (approved + filled
+ not closed). Carries the exit plan so the mobile screen can show "who
closes this and when" and offer a close-now button.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from app.schemas.base import CamelCaseModel


class OpenPositionDto(CamelCaseModel):
    # None for an unmanaged position — one that exists at the broker with
    # no agent decision behind it, so there is nothing to close *through*
    # the decision lifecycle.
    decision_id: str | None = None
    # False when the agent did not open this position (opened directly at
    # the broker, or before this deployment's decision history). Such a
    # position still counts against the account, so hiding it made
    # /account report open positions that /positions could not show.
    managed: bool = True
    symbol: str
    side: Literal["BUY", "SELL"]
    # Derived from the entry proposal's own "direction", falling back to
    # side == "SELL" for rows that predate that field. Always populated —
    # never optional — so the mobile/desktop direction badges never guess.
    direction: Literal["long", "short"]
    qty: int
    avg_entry_price: float | None = None
    # Live mark from the latest reconciler snapshot, when available.
    last_price: float | None = None
    unrealized_pnl: float | None = None
    # Exit plan (the user approved this at entry).
    exit_mode: Literal["agent", "manual"]
    stop_loss: float | None = None
    target_price: float | None = None
    time_stop_days: int | None = None
    opened_at: datetime


class ClosePositionResponse(CamelCaseModel):
    decision_id: str
    closed: bool
    # None on success; otherwise not_found / not_owner / already_closed /
    # no_open_position / close_in_flight / risk_vetoed.
    error: str | None = None
    detail: str | None = None
