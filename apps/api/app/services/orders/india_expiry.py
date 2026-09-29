"""What happened to an NSE option that left the account at expiry.

docs/PLAN_ZERODHA.md Z1. Alpaca records expiry, exercise and assignment
as account activities (OPEXP/OPEXC/OPASN), and order_sync closes the
decision from them. Kite has no such feed: an NFO contract simply stops
appearing in positions after expiry. Without this, that vanish read as
the user selling at the broker (``external_broker``), with a push saying
so, and a stock delivered by exercise went unmentioned.

NSE settlement (as of 2026; NSE changes these by circular, so re-check):

  * index options are cash-settled at the index's closing value on expiry
    day. An in-the-money long is credited its intrinsic value; an
    out-of-the-money one expires worthless.
  * stock options are physically settled. An in-the-money long call
    delivers the shares bought at the strike (a long put, a sale), one
    unit per unit of the position, and the shares then sit in holdings
    with no stop and no time exit.

The settlement price is the underlying's close on expiry day, read from
Kite's quote: ``last_price`` after the expiry-day close, or ``ohlc.close``
("closing price of the instrument from the last trading day") until the
next trading session closes. After that no quote shows it, and the
caller falls back to the contract's last mark, as it does for any close
outside our order flow. The reason is still expiry, because the date says
the contract cannot have been sold at the broker after it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from engine.features.market_calendar import is_in_trading_day
from engine.options.kite_chain import INDEX_OPTION_NAMES
from engine.risk.markets import tradingsymbol_of

logger = logging.getLogger("api.india_expiry")

_IST = ZoneInfo("Asia/Kolkata")
NSE_CLOSE = time(15, 30)


@dataclass(frozen=True)
class ExpiryOutcome:
    reason: str
    """option_expired (worthless), option_settled (index, cash), or
    option_exercised (stock, shares delivered)."""
    exit_price: Decimal | None
    """Per unit. None when the settlement price could not be read; the
    caller then uses the contract's last mark."""
    settlement: float | None
    delivered_units: int
    """Signed shares delivered by a physically settled stock option: + for
    a long call (bought), - for a long put (sold). 0 otherwise."""
    closed_at: datetime


def is_index(underlying: str) -> bool:
    return tradingsymbol_of(underlying).upper() in INDEX_OPTION_NAMES


def expiry_close_time(expiry: date) -> datetime:
    return datetime.combine(expiry, NSE_CLOSE, tzinfo=_IST)


def _next_trading_day(d: date) -> date:
    nxt = d + timedelta(days=1)
    for _ in range(10):
        if is_in_trading_day(nxt):
            return nxt
        nxt += timedelta(days=1)
    return nxt


async def settlement_price(
    broker: Any, underlying: str, expiry: date, now: datetime
) -> float | None:
    """The underlying's expiry-day close, while Kite's quote still shows it."""
    quotes = getattr(broker, "quotes", None)
    if quotes is None:
        return None
    now_ist = now.astimezone(_IST)
    if now_ist < expiry_close_time(expiry):
        return None
    if now_ist > expiry_close_time(_next_trading_day(expiry)):
        return None
    try:
        q = (await quotes([underlying])).get(underlying.upper()) or {}
    except Exception:
        logger.warning("india_expiry: quote for %s failed", underlying, exc_info=True)
        return None
    raw = q.get("last_price") if now_ist.date() == expiry else (q.get("ohlc") or {}).get("close")
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


async def expiry_outcome(
    broker: Any, decision: Any, *, expiry: date | None, now: datetime
) -> ExpiryOutcome | None:
    """How a vanished long NSE option ended, or None when expiry has not
    passed (so the vanish was a sale at the broker, as before)."""
    if expiry is None or now.astimezone(_IST) < expiry_close_time(expiry):
        return None
    proposal = getattr(decision, "proposal", None) or {}
    underlying = str(decision.symbol).upper()
    kind = str(proposal.get("contractType", proposal.get("contract_type", "call")))
    closed_at = min(expiry_close_time(expiry), now)
    try:
        strike = float(proposal.get("strike"))
    except (TypeError, ValueError):
        strike = None

    settle = await settlement_price(broker, underlying, expiry, now) if strike else None
    if settle is None:
        # Expired, but the settlement price is gone from the quote.
        return ExpiryOutcome("option_settled", None, None, 0, closed_at)

    intrinsic = max(0.0, settle - strike) if kind == "call" else max(0.0, strike - settle)
    if intrinsic <= 0:
        return ExpiryOutcome("option_expired", Decimal("0"), settle, 0, closed_at)
    exit_price = Decimal(str(round(intrinsic, 4)))
    if is_index(underlying):
        return ExpiryOutcome("option_settled", exit_price, settle, 0, closed_at)
    units = int(getattr(decision, "fill_qty", 0) or 0)
    return ExpiryOutcome(
        "option_exercised", exit_price, settle, units if kind == "call" else -units, closed_at
    )
