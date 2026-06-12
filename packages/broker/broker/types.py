"""Wire types — broker-agnostic. Concrete brokers map their SDK types to/from these."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class TimeInForce(str, Enum):
    DAY = "DAY"
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELED = "canceled"
    EXPIRED = "expired"


@dataclass(frozen=True)
class OrderRequest:
    """What we hand to the broker. Validated by the deterministic risk gate first."""

    symbol: str
    side: Side
    qty: int
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    client_order_id: str | None = None  # for idempotent retries

    # Bracket legs — when BOTH are set the broker holds the exit plan
    # server-side (entry + OCO take-profit/stop-loss children). The
    # disclosed "agent will close at stop X / target Y" promise survives
    # even if our whole stack is down. Brokers without native brackets
    # must raise rather than silently drop the protection.
    take_profit_price: float | None = None
    stop_loss_price: float | None = None

    @property
    def is_bracket(self) -> bool:
        return self.take_profit_price is not None and self.stop_loss_price is not None


@dataclass(frozen=True)
class Order:
    """What the broker returns after acknowledging the request."""

    broker_order_id: str
    client_order_id: str | None
    symbol: str
    side: Side
    qty: int
    filled_qty: int
    avg_fill_price: float | None
    status: OrderStatus
    submitted_at: datetime
    filled_at: datetime | None = None
    raw: dict = field(default_factory=dict)  # broker-specific payload, for audit


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    avg_entry_price: float
    market_value: float
    unrealized_pl: float
    unrealized_pl_pct: float
    raw: dict = field(default_factory=dict)
