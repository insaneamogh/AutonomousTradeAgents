"""India paper trading: a persisted book traded against Kite's real quotes.

docs/PLAN_ZERODHA.md Z3/Z4. Zerodha has no paper account, and the old
in-memory paper engine (paper_broker.py) was neither a broker nor durable:
positions it opened had no exits, no order sync, no ledger, and the book
reset on every redeploy. This is a ``BrokerInterface`` instead, so the
executor, order sync, GTT exits, position manager, expiry handling,
snapshots and reports all run on it exactly as they would on Kite.

What is real and what is simulated:

  * market data is REAL: quotes, the instruments dump, daily history, the
    margin quote and the account's F&O permission all come from the
    user's Kite session (``market_data``, a ZerodhaBroker);
  * orders, fills, cash and positions are SIMULATED and persisted
    (engine.db.models.paper). No order ever reaches Kite, so paper needs
    the Kite Connect plan and the daily login, but NOT the static IP.

Fill model, deliberately conservative:

  * only while the NSE session is open;
  * a buy fills at the best ask (a sell at the best bid), falling back to
    the last price when the depth is empty; a LIMIT fills only when that
    price is at or through the limit, and never better than the book;
  * STOP / STOP_LIMIT elect on the last price crossing the trigger;
  * a GTT OCO leg triggers on the last price and then rests as a LIMIT at
    the leg's limit, as Kite's placed leg order does;
  * whole quantity or nothing (no partial fills);
  * Indian charges (engine.backtester.costs_india) come out of cash on
    every fill;
  * a DAY order still open after its session's close expires;
  * Kite's refusals are mirrored: a sell of more than is held (no short
    selling on CNC/NRML longs here), or a buy the cash cannot pay for, is
    REJECTED.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from broker.types import Order, OrderRequest, OrderStatus, Position, Side, TimeInForce
from broker.zerodha import _is_kite_option, split_symbol

logger = logging.getLogger("api.kite_paper")

_IST = ZoneInfo("Asia/Kolkata")
_SESSION_CLOSE = time(15, 30)
_BUYS = (Side.BUY, Side.BUY_TO_OPEN)
_OPEN = ("accepted", "triggered")
GTT_PAPER_PREFIX = "gtt:paper-"


def zerodha_paper_enabled() -> bool:
    """ZERODHA_PAPER=1: a Zerodha connection trades this paper book, never
    Kite. Off by default; with it off, a Zerodha order is a real order."""
    return os.environ.get("ZERODHA_PAPER", "").strip().lower() in ("1", "true", "yes", "on")


def _starting_cash() -> Decimal:
    from app.services.orders.paper_broker import _starting_cash as starting

    return Decimal(str(starting("IN")))


def _book(quote: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    depth = quote.get("depth") or {}

    def _best(side: str) -> float | None:
        levels = depth.get(side) or []
        px = float(levels[0].get("price") or 0.0) if levels else 0.0
        return px if px > 0 else None

    last = float(quote.get("last_price") or 0.0) or None
    return _best("buy"), _best("sell"), last


def _charges(symbol: str, *, buying: bool, value: float) -> float:
    from engine.backtester.costs_india import round_trip

    exchange, tradingsymbol = split_symbol(symbol)
    segment = "options" if _is_kite_option(exchange, tradingsymbol) else "equity_delivery"
    buy, sell = (value, 0.0) if buying else (0.0, value)
    return round_trip(segment, buy_value=buy, sell_value=sell).total


class KitePaperBroker:
    """``BrokerInterface`` over the persisted paper book; Kite for prices."""

    name = "zerodha"
    supports_brackets = False

    def __init__(
        self,
        *,
        market_data: Any,
        user_id: str,
        session_factory: Any,
        clock: Callable[[], datetime] | None = None,
        market_open: Callable[[datetime], bool] | None = None,
    ) -> None:
        self._md = market_data
        self._uid = uuid.UUID(str(user_id))
        self._sf = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        if market_open is None:
            from engine.features.market_calendar import is_in_market_open

            market_open = is_in_market_open
        self._market_open = market_open

    # ── market data: the real Kite session ───────────────────────────

    async def quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        return await self._md.quotes(symbols)

    async def instruments(self, exchange: str) -> list[dict[str, Any]]:
        return await self._md.instruments(exchange)

    async def historical_daily(self, instrument_token: int, *, start: datetime, end: datetime):
        return await self._md.historical_daily(instrument_token, start=start, end=end)

    async def order_margin(self, request: OrderRequest) -> float:
        return await self._md.order_margin(request)

    async def get_options_trading_level(self) -> int | None:
        return await self._md.get_options_trading_level()

    # ── account ──────────────────────────────────────────────────────

    async def _account(self, session: Any) -> Any:
        from engine.db.models import PaperAccount

        acct = await session.get(PaperAccount, (self._uid, self.name))
        if acct is None:
            cash = _starting_cash()
            acct = PaperAccount(user_id=self._uid, broker=self.name, cash=cash,
                                starting_cash=cash, charges_paid=Decimal("0"))
            session.add(acct)
            await session.flush()
        return acct

    async def _positions(self, session: Any) -> list[Any]:
        from engine.db.models import PaperPosition

        return list((await session.execute(
            select(PaperPosition).where(PaperPosition.user_id == self._uid,
                                        PaperPosition.broker == self.name)
        )).scalars().all())

    async def _marks(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        quotes = await self._md.quotes(symbols)
        out = {}
        for s in symbols:
            _bid, _ask, last = _book(quotes.get(s.upper()) or {})
            if last:
                out[s.upper()] = last
        return out

    async def list_positions(self) -> list[Position]:
        async with self._sf() as session:
            rows = await self._positions(session)
        marks = await self._marks([r.symbol for r in rows])
        out = []
        for r in rows:
            avg = float(r.avg_entry_price)
            last = marks.get(r.symbol, avg)
            cost = avg * r.qty
            pnl = (last - avg) * r.qty
            exchange, tradingsymbol = split_symbol(r.symbol)
            out.append(Position(
                symbol=r.symbol, qty=r.qty, avg_entry_price=avg, market_value=last * r.qty,
                unrealized_pl=pnl, unrealized_pl_pct=(pnl / abs(cost) * 100) if cost else 0.0,
                multiplier=1, is_option=_is_kite_option(exchange, tradingsymbol),
                raw={"paper": "1"},
            ))
        return out

    async def get_position(self, symbol: str) -> Position | None:
        wanted = symbol.upper() if ":" in symbol else f"NSE:{symbol.upper()}"
        return next((p for p in await self.list_positions() if p.symbol == wanted), None)

    async def get_account_equity(self) -> float:
        async with self._sf() as session:
            cash = float((await self._account(session)).cash)
            await session.commit()
        return cash + sum(p.market_value for p in await self.list_positions())

    async def get_buying_power(self) -> float:
        async with self._sf() as session:
            cash = float((await self._account(session)).cash)
            await session.commit()
        return cash

    # ── orders ───────────────────────────────────────────────────────

    def _today(self) -> date:
        return self._clock().astimezone(_IST).date()

    async def place_order(self, request: OrderRequest) -> Order:
        from engine.db.models import PaperOrder

        if request.take_profit_price is not None or request.stop_loss_price is not None:
            raise ValueError("Zerodha has no bracket order; the exit is a GTT after the fill")
        if request.time_in_force not in (TimeInForce.DAY, TimeInForce.IOC):
            raise ValueError(f"Zerodha regular orders support DAY/IOC only, got "
                             f"{request.time_in_force}")
        symbol = request.symbol.upper() if ":" in request.symbol else f"NSE:{request.symbol.upper()}"
        async with self._sf() as session:
            if request.client_order_id:
                existing = (await session.execute(
                    select(PaperOrder).where(PaperOrder.user_id == self._uid,
                                             PaperOrder.client_order_id == request.client_order_id,
                                             PaperOrder.status != "rejected")
                )).scalar_one_or_none()
                if existing is not None:
                    return self._as_order(existing)
            row = PaperOrder(
                user_id=self._uid, broker=self.name,
                broker_order_id=f"paper-{uuid.uuid4().hex[:16]}",
                client_order_id=request.client_order_id, symbol=symbol,
                side=request.side.value, qty=int(request.qty),
                order_type=request.order_type.value,
                limit_price=_dec(request.limit_price), stop_price=_dec(request.stop_price),
                status="accepted", filled_qty=0, trading_day=self._today(),
                submitted_at=self._clock(),
            )
            session.add(row)
            await session.flush()
            await self._try_fill(session, row)
            await session.commit()
            return self._as_order(row)

    async def place_exit_oco(
        self, *, symbol: str, qty: int, entry_side: Side, stop: float, target: float,
        last_price: float,
    ) -> str:
        """A paper two-leg GTT: sell (or cover) at the stop or the target.
        Same shape and checks as ZerodhaBroker.place_exit_oco."""
        from broker.zerodha import ZerodhaError, _tick
        from engine.db.models import PaperOrder

        closing = Side.SELL if entry_side in _BUYS else Side.BUY
        slip = -0.005 if closing is Side.SELL else 0.005
        lo, hi = sorted([stop, target])
        if not (lo < last_price < hi):
            raise ZerodhaError(f"GTT triggers {lo}/{hi} must straddle the last price {last_price}")
        legs = [
            {"kind": "stop", "trigger": stop, "limit": _tick(stop * (1 + slip))},
            {"kind": "limit", "trigger": target, "limit": _tick(target)},
        ]
        gtt_id = f"{GTT_PAPER_PREFIX}{uuid.uuid4().hex[:12]}"
        async with self._sf() as session:
            session.add(PaperOrder(
                user_id=self._uid, broker=self.name, broker_order_id=gtt_id,
                symbol=symbol.upper(), side=closing.value, qty=int(qty), order_type="GTT_OCO",
                gtt_legs=legs, status="accepted", filled_qty=0, trading_day=self._today(),
                submitted_at=self._clock(),
            ))
            await session.commit()
        return gtt_id

    async def get_order(self, broker_order_id: str) -> Order:
        from engine.db.models import PaperOrder

        async with self._sf() as session:
            row = (await session.execute(
                select(PaperOrder).where(PaperOrder.broker_order_id == broker_order_id,
                                         PaperOrder.user_id == self._uid)
            )).scalar_one_or_none()
            if row is None:
                raise KeyError(f"no paper order {broker_order_id}")
            if row.status in _OPEN:
                await self._try_fill(session, row)
                await session.commit()
            return self._as_order(row)

    async def cancel_order(self, broker_order_id: str) -> Order:
        from engine.db.models import PaperOrder

        async with self._sf() as session:
            row = (await session.execute(
                select(PaperOrder).where(PaperOrder.broker_order_id == broker_order_id,
                                         PaperOrder.user_id == self._uid)
            )).scalar_one_or_none()
            if row is None:
                raise KeyError(f"no paper order {broker_order_id}")
            if row.status in _OPEN:
                row.status, row.canceled_at = "canceled", self._clock()
            await session.commit()
            return self._as_order(row)

    async def cancel_open_orders(self, symbol: str) -> int:
        """Cancel the symbol's open orders AND its GTTs, as ZerodhaBroker
        does before every close."""
        from engine.db.models import PaperOrder

        wanted = symbol.upper() if ":" in symbol else f"NSE:{symbol.upper()}"
        async with self._sf() as session:
            rows = (await session.execute(
                select(PaperOrder).where(PaperOrder.user_id == self._uid,
                                         PaperOrder.symbol == wanted,
                                         PaperOrder.status.in_(_OPEN))
            )).scalars().all()
            for row in rows:
                row.status, row.canceled_at = "canceled", self._clock()
            await session.commit()
            return len(rows)

    # ── the simulation ───────────────────────────────────────────────

    async def _try_fill(self, session: Any, row: Any) -> None:
        now = self._clock()
        if row.order_type != "GTT_OCO" and self._session_over(row, now):
            row.status, row.canceled_at = "expired", now
            return
        if not self._market_open(now):
            return
        quote = (await self._md.quotes([row.symbol])).get(row.symbol) or {}
        bid, ask, last = _book(quote)
        buying = Side(row.side) in _BUYS
        px = (ask if buying else bid) or last
        if px is None or last is None:
            return

        price: float | None = None
        limit = float(row.limit_price) if row.limit_price is not None else None
        stop = float(row.stop_price) if row.stop_price is not None else None
        if row.order_type == "GTT_OCO":
            price = self._gtt_price(row, px=px, last=last)
        elif row.order_type == "MARKET":
            price = px
        elif row.order_type == "LIMIT":
            price = px if (px <= limit if buying else px >= limit) else None
        elif row.order_type in ("STOP", "STOP_LIMIT"):
            elected = last >= stop if buying else last <= stop
            if elected and row.order_type == "STOP":
                price = px
            elif elected:
                price = px if (px <= limit if buying else px >= limit) else None
        if price is None:
            return
        await self._book_fill(session, row, price=price, now=now)

    def _gtt_price(self, row: Any, *, px: float, last: float) -> float | None:
        """A GTT leg triggers on the last price, then rests as a LIMIT at
        its leg limit until the book crosses it."""
        selling = Side(row.side) is Side.SELL
        legs = {leg["kind"]: leg for leg in (row.gtt_legs or [])}
        if row.filled_leg is None:
            stop, target = legs["stop"], legs["limit"]
            if (last <= stop["trigger"]) if selling else (last >= stop["trigger"]):
                row.filled_leg, row.status = "stop", "triggered"
            elif (last >= target["trigger"]) if selling else (last <= target["trigger"]):
                row.filled_leg, row.status = "limit", "triggered"
            else:
                return None
        leg_limit = float(legs[row.filled_leg]["limit"])
        return px if (px >= leg_limit if selling else px <= leg_limit) else None

    def _session_over(self, row: Any, now: datetime) -> bool:
        close = datetime.combine(row.trading_day, _SESSION_CLOSE, tzinfo=_IST)
        return now >= close

    async def _book_fill(self, session: Any, row: Any, *, price: float, now: datetime) -> None:
        from engine.db.models import PaperPosition

        buying = Side(row.side) in _BUYS
        qty = int(row.qty)
        value = price * qty
        charges = _charges(row.symbol, buying=buying, value=value)
        acct = await self._account(session)
        pos = await session.get(PaperPosition, (self._uid, self.name, row.symbol))
        held = pos.qty if pos is not None else 0

        if buying and value + charges > float(acct.cash):
            row.status = "rejected"
            row.canceled_at = now
            logger.info("kite_paper: %s rejected, insufficient funds (%.2f > %.2f)",
                        row.symbol, value + charges, float(acct.cash))
            return
        if not buying and qty > held:
            row.status = "rejected"
            row.canceled_at = now
            logger.info("kite_paper: %s rejected, sell %d of %d held", row.symbol, qty, held)
            return

        signed = qty if buying else -qty
        acct.cash = Decimal(str(round(float(acct.cash) - signed * price - charges, 2)))
        acct.charges_paid = Decimal(str(round(float(acct.charges_paid) + charges, 2)))
        if pos is None:
            session.add(PaperPosition(user_id=self._uid, broker=self.name, symbol=row.symbol,
                                      qty=signed, avg_entry_price=Decimal(str(price))))
        elif held + signed == 0:
            await session.delete(pos)
        else:
            if buying:
                pos.avg_entry_price = Decimal(str(round(
                    (float(pos.avg_entry_price) * held + price * qty) / (held + qty), 4)))
            pos.qty = held + signed
        row.status, row.filled_qty = "filled", qty
        row.avg_fill_price, row.filled_at = Decimal(str(price)), now
        logger.info("kite_paper: filled %s %d %s @ %.2f (charges %.2f)",
                    row.side, qty, row.symbol, price, charges)

    def _as_order(self, row: Any) -> Order:
        status = {
            "accepted": OrderStatus.ACCEPTED, "triggered": OrderStatus.ACCEPTED,
            "filled": OrderStatus.FILLED, "canceled": OrderStatus.CANCELED,
            "expired": OrderStatus.EXPIRED, "rejected": OrderStatus.REJECTED,
        }[row.status]
        raw: dict[str, Any] = {"paper": "1"}
        if row.order_type == "GTT_OCO":
            raw["gtt_status"] = "triggered" if row.filled_leg else "active"
            if row.filled_leg:
                raw["order_type"] = row.filled_leg
        return Order(
            broker_order_id=row.broker_order_id, client_order_id=row.client_order_id,
            symbol=row.symbol, side=Side(row.side), qty=int(row.qty),
            filled_qty=int(row.filled_qty or 0),
            avg_fill_price=float(row.avg_fill_price) if row.avg_fill_price is not None else None,
            status=status, submitted_at=row.submitted_at, filled_at=row.filled_at, raw=raw,
        )


def _dec(v: float | None) -> Decimal | None:
    return Decimal(str(v)) if v is not None else None
