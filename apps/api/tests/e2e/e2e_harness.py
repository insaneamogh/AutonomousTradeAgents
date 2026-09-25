"""End-to-end scenario harness: a deterministic broker, a real database.

What is real in a scenario: Postgres (a fresh database per test, cloned
from a migrated template), every SQLAlchemy model and migration, the
executor, the risk engine, order_store, order_sync, option_stops, the
position manager's close path, the kill switch and the EOD report.

What is simulated: the broker. ``SimBroker`` implements the same
``BrokerInterface`` the Alpaca adapter does, plus the two structural
extras the app reaches through ``getattr`` (prior-close equity, option
lifecycle activities). Prices move only when a scenario says so, so every
fill is reproducible.

Why scenarios instead of more unit tests: the bugs that got through here
were wiring bugs between layers that each had green unit tests (a stop
row never polled, a fill labelled as a manual close, a contract
"closed externally" on its first tick). A scenario crosses those layers
the way production does, so it fails where they disagree.
"""

from __future__ import annotations

import contextlib
import itertools
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from broker.types import (
    AccountActivity,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
)

FIXTURE_USER = "00000000-0000-0000-0000-000000000001"

_SELLS = (Side.SELL, Side.SELL_TO_CLOSE)


def _is_occ(symbol: str) -> bool:
    return len(symbol) > 15 and symbol[-9] in "CP" and symbol[-8:].isdigit()


@dataclass
class _Held:
    qty: int
    avg: float


@dataclass
class SimBroker:
    """A broker account whose prices move only when told to."""

    cash: float = 100_000.0
    options_level: int = 3
    account_number: str = "SIM-0001"
    prices: dict[str, float] = field(default_factory=dict)
    held: dict[str, _Held] = field(default_factory=dict)
    orders: dict[str, Order] = field(default_factory=dict)
    requests: dict[str, OrderRequest] = field(default_factory=dict)
    legs_of: dict[str, tuple[str, ...]] = field(default_factory=dict)
    activities: list[AccountActivity] = field(default_factory=list)
    kite: bool = False
    """Behave like ZerodhaBroker: refuse bracket legs and GTC, exactly as
    the real adapter does (broker/zerodha.py place_order)."""
    activities_error: Exception | None = None
    exit_oco_error: Exception | None = None
    """Set to make GTT placement fail, as a Kite rejection would."""
    gtts: set[str] = field(default_factory=set)
    hold_gtts: bool = False
    """Kite evaluates a GTT with some latency; True keeps crossed GTT legs
    resting, to pin what happens in that gap."""
    """Set to make the activities read fail, as a 403 or timeout would."""
    _ids: Any = field(default_factory=lambda: itertools.count(1))
    _start_equity: float | None = None

    name = "sim"

    @property
    def supports_brackets(self) -> bool:
        return not self.kite

    # ── scenario controls ────────────────────────────────────────────

    def set_price(self, symbol: str, price: float) -> None:
        """Move a mark. Resting stop orders on that symbol elect on it."""
        self.prices[symbol] = price
        for oid, req in list(self.requests.items()):
            order = self.orders[oid]
            if req.symbol != symbol or order.status is not OrderStatus.ACCEPTED:
                continue
            if self.hold_gtts and any(oid in self.legs_of.get(g, ()) for g in self.gtts):
                continue
            if req.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
                elected = price <= req.stop_price if req.side in _SELLS else price >= req.stop_price
                limit_ok = req.limit_price is None or (
                    price >= req.limit_price if req.side in _SELLS else price <= req.limit_price
                )
                if elected and limit_ok:
                    self._fill(oid, price)
                    self._cancel_siblings(oid)
            elif req.order_type is OrderType.LIMIT:
                crosses = price >= req.limit_price if req.side in _SELLS else price <= req.limit_price
                if crosses:
                    self._fill(oid, req.limit_price)
                    self._cancel_siblings(oid)

    def expire(self, occ: str, on: date | None = None) -> None:
        """The contract expires worthless: position gone, OPEXP recorded."""
        held = self.held.pop(occ)
        self._cancel_open(occ)
        self._activity("OPEXP", occ, -held.qty, on)

    def exercise(self, occ: str, *, underlying: str, strike: float, on: date | None = None) -> None:
        """A long call is auto-exercised: the contract becomes 100 shares
        per contract bought at the strike (Alpaca's OPEXC + OPTRD pair)."""
        held = self.held.pop(occ)
        self._cancel_open(occ)
        shares = held.qty * 100
        self._activity("OPEXC", occ, -held.qty, on)
        self._activity("OPTRD", underlying, shares, on, price=strike)
        self.cash -= shares * strike
        self.held[underlying] = _Held(shares, strike)

    # ── BrokerInterface ──────────────────────────────────────────────

    async def place_order(self, request: OrderRequest) -> Order:
        if self.kite:
            from broker.types import TimeInForce

            if request.take_profit_price is not None or request.stop_loss_price is not None:
                raise RuntimeError("bracket exit legs are not supported on Zerodha")
            if request.time_in_force not in (TimeInForce.DAY, TimeInForce.IOC):
                raise ValueError("Zerodha regular orders support DAY/IOC only")
        if request.client_order_id and any(
            o.client_order_id == request.client_order_id for o in self.orders.values()
        ):
            raise RuntimeError(f"client_order_id {request.client_order_id} must be unique")
        oid = f"sim-{next(self._ids)}"
        order = Order(
            broker_order_id=oid,
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            filled_qty=0,
            avg_fill_price=None,
            status=OrderStatus.ACCEPTED,
            submitted_at=datetime.now(UTC),
        )
        self.orders[oid] = order
        self.requests[oid] = request
        price = self.prices.get(request.symbol)
        if request.order_type is OrderType.MARKET and price is not None:
            self._fill(oid, price)
        elif request.order_type is OrderType.LIMIT and price is not None:
            buy = request.side not in _SELLS
            if (buy and price <= request.limit_price) or (not buy and price >= request.limit_price):
                self._fill(oid, request.limit_price)
        if request.is_bracket and self.orders[oid].status is OrderStatus.FILLED:
            self._open_bracket_legs(oid, request)
        return await self.get_order(oid)

    async def place_exit_oco(self, *, symbol: str, qty: int, entry_side: Side, stop: float,
                             target: float, last_price: float) -> str:
        """Kite's two-leg GTT: a stop and a limit resting as OCO siblings,
        read back as ONE order ``gtt:<n>`` (ZerodhaBroker._gtt_as_order)."""
        if self.exit_oco_error is not None:
            raise self.exit_oco_error
        gtt = f"gtt:{next(self._ids)}"
        self._open_bracket_legs(gtt, OrderRequest(
            symbol=symbol, side=entry_side, qty=qty, take_profit_price=target,
            stop_loss_price=stop,
        ))
        self.gtts.add(gtt)
        return gtt

    async def get_order(self, broker_order_id: str) -> Order:
        from dataclasses import replace

        if broker_order_id in self.gtts:
            legs = [self.orders[c] for c in self.legs_of[broker_order_id]]
            filled = next((leg for leg in legs if leg.status is OrderStatus.FILLED), None)
            live = any(leg.status is OrderStatus.ACCEPTED for leg in legs)
            base = legs[0]
            if filled is not None:
                return replace(filled, broker_order_id=broker_order_id, legs=())
            return replace(base, broker_order_id=broker_order_id, filled_qty=0,
                           avg_fill_price=None, raw={},
                           status=OrderStatus.ACCEPTED if live else OrderStatus.CANCELED)
        order = self.orders[broker_order_id]
        legs = tuple(self.orders[c] for c in self.legs_of.get(broker_order_id, ()))
        return replace(order, legs=legs) if legs else order

    async def cancel_order(self, broker_order_id: str) -> Order:
        if broker_order_id in self.gtts:
            for child in self.legs_of[broker_order_id]:
                if self.orders[child].status is OrderStatus.ACCEPTED:
                    self._set(child, status=OrderStatus.CANCELED)
            return await self.get_order(broker_order_id)
        self._set(broker_order_id, status=OrderStatus.CANCELED)
        return self.orders[broker_order_id]

    async def cancel_open_orders(self, symbol: str) -> int:
        return self._cancel_open(symbol)

    async def list_positions(self) -> list[Position]:
        return [self._position(s) for s in self.held]

    async def get_position(self, symbol: str) -> Position | None:
        return self._position(symbol) if symbol in self.held else None

    async def get_account_equity(self) -> float:
        equity = self.cash + sum(self._mv(s) for s in self.held)
        if self._start_equity is None:
            self._start_equity = equity
        return equity

    async def get_buying_power(self) -> float:
        return self.cash * 2

    async def get_options_trading_level(self) -> int | None:
        return self.options_level

    async def get_account_number(self) -> str | None:
        return self.account_number

    async def get_prior_close_equity(self) -> float | None:
        return self._start_equity

    async def list_option_lifecycle_activities(self, *, since: date) -> list[AccountActivity]:
        if self.activities_error is not None:
            raise self.activities_error
        return [a for a in reversed(self.activities) if a.day >= since]

    # ── internals ────────────────────────────────────────────────────

    def _mult(self, symbol: str) -> int:
        return 100 if _is_occ(symbol) else 1

    def _mv(self, symbol: str) -> float:
        h = self.held[symbol]
        return h.qty * self.prices.get(symbol, h.avg) * self._mult(symbol)

    def _position(self, symbol: str) -> Position:
        h = self.held[symbol]
        mult = self._mult(symbol)
        mv = self._mv(symbol)
        cost = h.qty * h.avg * mult
        return Position(
            symbol=symbol, qty=h.qty, avg_entry_price=h.avg, market_value=mv,
            unrealized_pl=mv - cost,
            unrealized_pl_pct=((mv - cost) / abs(cost) * 100.0) if cost else 0.0,
            multiplier=mult, is_option=mult == 100,
        )

    def _fill(self, oid: str, price: float) -> None:
        req = self.requests[oid]
        mult = self._mult(req.symbol)
        signed = -req.qty if req.side in _SELLS else req.qty
        held = self.held.get(req.symbol)
        if held is None:
            self.held[req.symbol] = _Held(signed, price)
        else:
            new_qty = held.qty + signed
            if new_qty == 0:
                del self.held[req.symbol]
            elif (held.qty > 0) == (signed > 0):
                held.avg = (held.avg * held.qty + price * signed) / new_qty
                held.qty = new_qty
            else:
                held.qty = new_qty
        self.cash -= signed * price * mult
        self._set(oid, status=OrderStatus.FILLED, filled_qty=req.qty,
                  avg_fill_price=price, filled_at=datetime.now(UTC))

    def _open_bracket_legs(self, parent: str, req: OrderRequest) -> None:
        """Alpaca's bracket: once the entry fills, a take-profit LIMIT and a
        STOP rest as OCO siblings. They are the broker's orders, not ours:
        no client_order_id, no orders row."""
        exit_side = Side.SELL if req.side not in _SELLS else Side.BUY
        children = []
        for kind, px in ((OrderType.LIMIT, req.take_profit_price),
                         (OrderType.STOP, req.stop_loss_price)):
            if px is None:
                continue
            oid = f"sim-{next(self._ids)}"
            child = OrderRequest(
                symbol=req.symbol, side=exit_side, qty=req.qty, order_type=kind,
                limit_price=px if kind is OrderType.LIMIT else None,
                stop_price=px if kind is OrderType.STOP else None,
            )
            self.requests[oid] = child
            self.orders[oid] = Order(
                broker_order_id=oid, client_order_id=None, symbol=req.symbol,
                side=exit_side, qty=req.qty, filled_qty=0, avg_fill_price=None,
                status=OrderStatus.ACCEPTED, submitted_at=datetime.now(UTC),
                raw={"order_type": kind.value.lower()},
            )
            children.append(oid)
        self.legs_of[parent] = tuple(children)

    def _cancel_siblings(self, oid: str) -> None:
        for children in self.legs_of.values():
            if oid in children:
                for other in children:
                    if other != oid and self.orders[other].status is OrderStatus.ACCEPTED:
                        self._set(other, status=OrderStatus.CANCELED)

    def _cancel_open(self, symbol: str) -> int:
        n = 0
        for oid, order in self.orders.items():
            if order.symbol == symbol and order.status is OrderStatus.ACCEPTED:
                self._set(oid, status=OrderStatus.CANCELED)
                n += 1
        return n

    def _set(self, oid: str, **changes: Any) -> None:
        from dataclasses import replace

        self.orders[oid] = replace(self.orders[oid], **changes)

    def _activity(self, kind: str, symbol: str, qty: float, on: date | None,
                  price: float | None = None) -> None:
        self.activities.append(AccountActivity(
            activity_id=f"{kind}-{uuid.uuid4().hex[:8]}", activity_type=kind,
            symbol=symbol, qty=float(qty), day=on or datetime.now(UTC).date(), price=price,
        ))


@dataclass(frozen=True)
class SimConnection:
    id: str
    is_paper: bool = True
    broker: str = "alpaca"
    live_trading_consent: bool = False


# Modules that bind ``with_broker_client`` by name at import time; each
# needs its own attribute patched.
_BROKER_CLIENT_USERS = (
    "app.services.orders.order_sync",
    "app.services.orders.position_manager",
    "app.services.orders.option_stops",
    "app.services.orders.reconciler_fleet",
    "app.services.orders.executor",
    "app.services.orders.stale_entries",
    "app.services.orders.portfolio_service",
    "app.services.orders.exit_oco",
)


def patch_broker(monkeypatch: Any, sim: SimBroker, connection: SimConnection) -> None:
    """One simulated broker account, answering for its own broker name."""
    patch_brokers(monkeypatch, {connection.broker: (sim, connection)})


def patch_brokers(monkeypatch: Any, accounts: dict[str, tuple[SimBroker, SimConnection]]) -> None:
    """Several simulated accounts, one per broker name. A caller that asks
    for a broker gets that one (and a loud error if the scenario has none,
    which is how a loop hardcoded to the wrong broker shows up); a caller
    that asks for none gets them in BROKER_PREFERENCE order."""
    import importlib

    @contextlib.asynccontextmanager
    async def _client(_user_id: str, *_a: Any, broker: str | None = None, **_kw: Any):
        if broker is not None:
            if broker not in accounts:
                raise RuntimeError(f"scenario has no {broker!r} account")
            yield accounts[broker]
            return
        for name in ("alpaca", "zerodha"):
            if name in accounts:
                yield accounts[name]
                return
        raise RuntimeError("scenario has no broker account")

    for mod in _BROKER_CLIENT_USERS:
        monkeypatch.setattr(importlib.import_module(mod), "with_broker_client", _client)


# ── scenario drivers ────────────────────────────────────────────────


def occ_for(underlying: str, expiry: date, kind: str, strike: float) -> str:
    return f"{underlying}{expiry:%y%m%d}{'C' if kind == 'call' else 'P'}{round(strike * 1000):08d}"


async def seed_account(
    *, auto_approve: bool = False, broker: str = "alpaca", live_consent: bool = False,
) -> SimConnection:
    """The fixture user and one active connection, as the connect flow
    would leave it: Alpaca paper, or Zerodha (always live: Kite has no
    paper environment)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from engine.db.models import BrokerConnection, User
    from engine.db.session import async_session_factory

    conn_id = uuid.uuid4()
    async with async_session_factory()() as s:
        await s.execute(pg_insert(User).values(
            id=uuid.UUID(FIXTURE_USER), email="e2e@local.test", display_name="E2E",
        ).on_conflict_do_nothing(index_elements=["id"]))
        is_paper = broker == "alpaca"
        s.add(BrokerConnection(
            id=conn_id, user_id=uuid.UUID(FIXTURE_USER), broker=broker, is_paper=is_paper,
            encrypted_access_token="sim", status="active", auto_approve_consent=auto_approve,
            live_trading_consent=live_consent,
        ))
        await s.commit()
    return SimConnection(id=str(conn_id), is_paper=is_paper, broker=broker,
                         live_trading_consent=live_consent)


def option_proposal(*, occ: str, underlying: str, strike: float, expiry: date,
                    kind: str = "call", qty: int = 1, limit: float = 2.55) -> Any:
    """A pending long-option proposal shaped like runtime._to_proposal_dto's."""
    from datetime import timedelta

    from app.schemas.approvals import ApprovalProposalDto

    now = datetime.now(UTC)
    return ApprovalProposalDto(
        id=f"agent-{uuid.uuid4().hex[:12]}", symbol=underlying, side="BUY",
        direction="long" if kind == "call" else "short",
        is_option=True, option_action="buy_to_open", occ_symbol=occ, strike=strike,
        expiry_date=expiry, contract_type=kind, multiplier=100, open_interest=900,
        volume=300, bid=round(limit - 0.10, 2), ask=limit, implied_volatility=0.30,
        qty=qty, order_type="LIMIT", limit_price=limit,
        estimated_notional=qty * limit * 100, time_stop_days=5,
        rationale="e2e", bull_case="e2e bull", bear_case="e2e bear",
        risk_level=2, conviction_level=3, council_confidence=0.6,
        proposed_at=now, expires_at=now + timedelta(hours=6),
    )


@contextlib.asynccontextmanager
async def api_client():
    """The real FastAPI app over ASGI, authenticated as the fixture user.
    No lifespan: background loops run only when a scenario ticks them."""
    import httpx

    from app.main import app
    from app.middleware.auth import AuthedUser, get_current_user, require_real_auth

    user = AuthedUser(id=FIXTURE_USER, email="e2e@local.test", auth_method="e2e")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_real_auth] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://e2e.local"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(require_real_auth, None)


async def fleet_tick(monkeypatch: Any, *, market_open: bool | dict[str, bool] = True) -> int:
    """One pass of the production reconciler fleet, with each market's clock
    pinned by the scenario instead of the wall clock: a bool for every
    market, or {"US": ..., "IN": ...}."""
    from app.services.broker.broker_store import get_broker_store
    from app.services.orders import reconciler_fleet
    from engine.db.session import async_session_factory

    def _open(market: str = "US") -> bool:
        return market_open.get(market, False) if isinstance(market_open, dict) else market_open

    monkeypatch.setattr(reconciler_fleet, "_exits_allowed_now", _open)
    fleet = reconciler_fleet.ReconcilerFleet(
        session_factory=async_session_factory(),
        broker_store=get_broker_store(),
        config=reconciler_fleet.FleetConfig(
            interval_seconds=30, halt_threshold_pct=-3.0, allow_mock_fallback=False
        ),
    )
    return await fleet.tick()


async def decision_row(proposal_id: str) -> Any:
    """The agent_decisions row behind a proposal id as the API shows it
    (the id the council minted, kept in the proposal JSON)."""
    from sqlalchemy import select

    from engine.db.models import AgentDecision
    from engine.db.session import async_session_factory

    async with async_session_factory()() as s:
        row = (await s.execute(
            select(AgentDecision).where(AgentDecision.proposal["id"].astext == proposal_id)
        )).scalar_one_or_none()
        if row is None:
            row = await s.get(AgentDecision, uuid.UUID(proposal_id))
        return row


async def order_rows(proposal_id: str) -> list[Any]:
    from sqlalchemy import select

    from engine.db.models import Order as OrderRow
    from engine.db.session import async_session_factory

    decision = await decision_row(proposal_id)
    async with async_session_factory()() as s:
        rows = await s.execute(
            select(OrderRow).where(OrderRow.agent_decision_id == decision.id)
            .order_by(OrderRow.submitted_at)
        )
        return list(rows.scalars().all())


def equity_proposal(*, symbol: str, qty: int, last: float, stop: float, target: float) -> Any:
    """A pending long-equity proposal with the ATR bracket the Drafter
    would disclose (runtime._to_proposal_dto's shape)."""
    from datetime import timedelta

    from app.schemas.approvals import ApprovalProposalDto

    now = datetime.now(UTC)
    risk = last - stop
    return ApprovalProposalDto(
        id=f"agent-{uuid.uuid4().hex[:12]}", symbol=symbol, side="BUY", direction="long",
        qty=qty, order_type="MARKET", estimated_notional=qty * last,
        stop_loss=stop, target_price=target, time_stop_days=28,
        r_multiple=round((target - last) / risk, 2) if risk > 0 else None,
        rationale="e2e", bull_case="e2e bull", bear_case="e2e bear",
        risk_level=2, conviction_level=3, council_confidence=0.7,
        proposed_at=now, expires_at=now + timedelta(hours=6),
    )
