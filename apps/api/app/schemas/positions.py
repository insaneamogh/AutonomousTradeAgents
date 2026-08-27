"""Wire schemas for /api/v1/positions — open agent positions + close.

One ``OpenPositionDto`` per open agent-managed decision (approved + not
closed) OR per unmanaged broker position. Carries the exit plan so the
mobile screen can show "who closes this and when" and offer a close-now
button.

Includes rows still awaiting a fill (``status="pending_fill"``): an
approved proposal used to disappear from ``/approvals/pending`` the
instant it was decided and only reappear here once the broker filled it —
invisible in between, most visibly outside market hours, when an order
can sit accepted-but-unfilled for hours. There was no other surface that
listed it: ``/orders`` has no GET, so an approved order was nowhere in
the app until the market opened.
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
    # "pending_fill": the order was placed at the broker but hasn't filled
    # yet (common outside market hours) — there is no entry price, no
    # live mark, and nothing to close, only an order to watch. "open":
    # a real position, filled and live. Unmanaged rows are always "open"
    # — they exist at the broker BECAUSE they already filled.
    status: Literal["open", "pending_fill"] = "open"
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
