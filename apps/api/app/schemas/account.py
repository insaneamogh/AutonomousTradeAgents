"""Account schemas — broker connection status + cash / equity / P&L snapshot."""

from __future__ import annotations

from typing import Literal

from app.schemas.base import CamelCaseModel

AccountStatus = Literal["connected", "disconnected", "expiring"]


class AccountResponse(CamelCaseModel):
    equity: float
    cash: float
    buying_power: float
    today_pnl: float
    today_pnl_pct: float
    open_positions: int
    status: AccountStatus
    broker_name: str
    is_paper: bool
