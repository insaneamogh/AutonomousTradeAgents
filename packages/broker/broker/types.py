"""Wire types — broker-agnostic. Concrete brokers map their SDK types to/from these."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    # Options-only (Phase A: long calls/puts). BUY_TO_CLOSE/SELL_TO_OPEN are
    # deliberately not defined yet — Phase A never constructs a short option
    # leg, and adding them now would remove the type-level nudge that a
    # short leg is a separate, later decision (Phase B/C).
    BUY_TO_OPEN = "BUY_TO_OPEN"
    SELL_TO_CLOSE = "SELL_TO_CLOSE"


_OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


@dataclass(frozen=True)
class OccSymbol:
    """A parsed OCC option symbol, e.g. ``AAPL260828C00250000``.

    Format: ``{underlying}{YYMMDD expiry}{C|P}{strike * 1000, zero-padded
    to 8 digits}``. ``raw`` is kept alongside the parsed fields so callers
    never have to re-derive/re-concatenate the wire string by hand.
    """

    underlying: str
    expiry: date
    contract_type: str  # "call" | "put"
    strike: float
    raw: str

    def __str__(self) -> str:
        return self.raw

    @classmethod
    def parse(cls, occ: str) -> OccSymbol:
        m = _OCC_RE.match(occ)
        if m is None:
            raise ValueError(f"not a valid OCC option symbol: {occ!r}")
        underlying, yy, mm, dd, cp, strike_digits = m.groups()
        return cls(
            underlying=underlying,
            expiry=date(2000 + int(yy), int(mm), int(dd)),
            contract_type="call" if cp == "C" else "put",
            strike=int(strike_digits) / 1000.0,
            raw=occ,
        )

    @classmethod
    def try_parse(cls, symbol: str) -> OccSymbol | None:
        """Non-raising parse for call sites deciding "is this an option
        order" (e.g. the bracket-on-options guard) — an exception is the
        wrong control-flow tool for a routine shape check."""
        try:
            return cls.parse(symbol)
        except ValueError:
            return None


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
    multiplier: int = 1
    """Contract multiplier — 1 for equities, 100 for standard US equity
    options. ``market_value``/``unrealized_pl`` are already correctly
    scaled by the broker; this field exists so callers converting
    ``avg_entry_price`` (always per-share/per-contract-unit) into a
    notional do ``qty * avg_entry_price * multiplier`` instead of
    guessing."""
    is_option: bool = False
    raw: dict = field(default_factory=dict)
