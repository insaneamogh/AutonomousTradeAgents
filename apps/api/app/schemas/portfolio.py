"""Wire schemas for /api/v1/portfolio — per-broker profit window.

camelCase on the wire; snake_case in Python via ``alias_generator``.

One ``BrokerPortfolioDto`` per ACTIVE broker connection. Currencies are
NEVER mixed: Alpaca reports USD, Zerodha reports INR, and there is no
cross-broker total on purpose — a summed "₹+$" number would be a lie.
"""

from __future__ import annotations

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


class PortfolioPositionDto(_Base):
    symbol: str
    qty: int
    avg_entry_price: float
    market_value: float
    unrealized_pl: float
    unrealized_pl_pct: float


class ProfitWindowDto(_Base):
    window_days: int
    realized_pnl: float
    """Sum of closed-trade P&L in the window, from the agent's decision log,
    attributed to this broker by symbol market (NSE:/NFO:/… → zerodha,
    bare US symbols → alpaca)."""
    completed_trades: int
    wins: int
    losses: int
    unrealized_pnl: float
    """Live, from the broker's open positions right now."""
    attribution: str = "symbol_market"
    """How realized P&L was attributed to this broker. ``symbol_market``
    today; a broker column on the decision log is the Phase 4 upgrade."""


class BrokerPortfolioDto(_Base):
    broker: str
    is_paper: bool
    currency: str
    """USD for alpaca, INR for zerodha. Never summed across brokers."""
    status: str
    """ok | token_expired | unavailable."""
    detail: str | None = None
    """Human-readable explanation when status != ok."""
    account_number: str | None = None
    equity: float | None = None
    buying_power: float | None = None
    positions: list[PortfolioPositionDto] = Field(default_factory=list)
    profit_window: ProfitWindowDto | None = None


class PortfolioSummaryResponse(_Base):
    brokers: list[BrokerPortfolioDto] = Field(default_factory=list)
