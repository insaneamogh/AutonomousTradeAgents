"""Alpaca implementation of ``BrokerInterface``.

The official ``alpaca-py`` SDK is synchronous; we wrap its calls in
``asyncio.to_thread`` so the rest of the system can stay async. This is fine
for v1 — order-placement latency is dominated by the broker's RTT, not the
thread hop.

Paper vs live is controlled by ``base_url`` (Alpaca's convention). We expose
it as an explicit ``paper: bool`` flag so callers don't accidentally point
to live with a paper key (which silently 401s with a confusing message).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import NamedTuple

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderClass,
    OrderSide,
    QueryOrderStatus,
)
from alpaca.trading.enums import (
    OrderStatus as _AlpacaStatus,
)
from alpaca.trading.enums import (
    OrderType as _AlpacaType,
)
from alpaca.trading.enums import TimeInForce as _AlpacaTif
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)

from broker.base import BrokerInterface
from broker.types import (
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    TimeInForce,
)

logger = logging.getLogger("broker.alpaca")


# ─────────────────────────────────────────────────────────────────────
# Enum / value mapping  (broker-agnostic ⇄ alpaca-py)
# ─────────────────────────────────────────────────────────────────────


_SIDE_TO_ALPACA: dict[Side, OrderSide] = {
    Side.BUY: OrderSide.BUY,
    Side.SELL: OrderSide.SELL,
}

_TYPE_TO_ALPACA: dict[OrderType, _AlpacaType] = {
    OrderType.MARKET: _AlpacaType.MARKET,
    OrderType.LIMIT: _AlpacaType.LIMIT,
    OrderType.STOP: _AlpacaType.STOP,
    OrderType.STOP_LIMIT: _AlpacaType.STOP_LIMIT,
}

_TIF_TO_ALPACA: dict[TimeInForce, _AlpacaTif] = {
    TimeInForce.DAY: _AlpacaTif.DAY,
    TimeInForce.GTC: _AlpacaTif.GTC,
    TimeInForce.IOC: _AlpacaTif.IOC,
    TimeInForce.FOK: _AlpacaTif.FOK,
}


def _status_from_alpaca(s: _AlpacaStatus) -> OrderStatus:
    # Alpaca has more granular statuses than we need; collapse to ours.
    name = s.value.lower() if hasattr(s, "value") else str(s).lower()
    if name in ("new", "accepted", "pending_new", "accepted_for_bidding"):
        return OrderStatus.ACCEPTED
    if name == "partially_filled":
        return OrderStatus.PARTIALLY_FILLED
    if name == "filled":
        return OrderStatus.FILLED
    if name in ("canceled", "pending_cancel"):
        return OrderStatus.CANCELED
    if name == "rejected":
        return OrderStatus.REJECTED
    if name == "expired":
        return OrderStatus.EXPIRED
    if name in ("pending_replace", "replaced"):
        return OrderStatus.ACCEPTED
    if name in ("done_for_day", "stopped", "suspended", "calculated", "held"):
        return OrderStatus.ACCEPTED
    return OrderStatus.SUBMITTED


# ─────────────────────────────────────────────────────────────────────
# Implementation
# ─────────────────────────────────────────────────────────────────────


class AlpacaBroker(BrokerInterface):
    """Alpaca Markets trading client.

    Two auth paths:
      1. **API key + secret** (legacy) — used by `from_env()` for the smoke
         harness + the Phase 0 paper smoke.
      2. **OAuth access token** — used by the production executor route
         once a user has connected their Alpaca account via the OAuth
         flow. The token is decrypted-on-use from ``broker_connections``
         and handed in here. See ``app.services.broker_use``.

    Idempotency: ``OrderRequest.client_order_id`` is forwarded to Alpaca's
    ``client_order_id`` field. Alpaca de-dupes on it within ~24h, so safe
    retries are free.
    """

    name = "alpaca"

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        *,
        paper: bool = True,
        oauth_token: str | None = None,
    ) -> None:
        """Construct with either (api_key + secret_key) or (oauth_token).

        Exactly one auth path must be specified. We keep the positional
        signature compatible with the existing env-key callers (smoke,
        broker --smoke, paper tests) — adding a kwarg-only ``oauth_token``
        is purely additive.
        """
        self.is_paper = paper

        if oauth_token is not None:
            if api_key or secret_key:
                raise ValueError(
                    "AlpacaBroker: pass oauth_token OR api_key+secret_key, not both"
                )
            # alpaca-py's TradingClient accepts an oauth_token kwarg — when
            # set, the client uses Bearer auth instead of the api-key header.
            self._client = TradingClient(oauth_token=oauth_token, paper=paper)
            return

        if not api_key or not secret_key:
            raise ValueError(
                "AlpacaBroker: must pass either api_key+secret_key or oauth_token"
            )
        self._client = TradingClient(api_key=api_key, secret_key=secret_key, paper=paper)

    @classmethod
    def from_env(cls) -> AlpacaBroker:
        """Build from ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_BASE_URL env.

        ``ALPACA_API_SECRET`` is accepted as a legacy alias: this module
        historically read that name while ``engine.features`` /
        ``engine.prices`` read ``ALPACA_SECRET_KEY``, so a deployment
        configured for market data would KeyError the moment it tried to
        trade. Both names now resolve.
        """
        key = os.environ.get("ALPACA_API_KEY", "").strip()
        secret = (
            os.environ.get("ALPACA_SECRET_KEY", "").strip()
            or os.environ.get("ALPACA_API_SECRET", "").strip()
        )
        if not key or not secret:
            raise RuntimeError(
                "AlpacaBroker.from_env: set ALPACA_API_KEY and ALPACA_SECRET_KEY"
            )
        # Alpaca's SDK selects paper vs live from the `paper` kwarg, not the
        # URL. Default to PAPER when unset — an unconfigured environment must
        # never fall through to real money.
        base = os.environ.get("ALPACA_BASE_URL", "").strip()
        paper = "paper" in base.lower() if base else True
        return cls(api_key=key, secret_key=secret, paper=paper)

    @classmethod
    def from_oauth_token(cls, oauth_token: str, *, paper: bool = True) -> AlpacaBroker:
        """Build from a decrypted OAuth access token. Used by the executor."""
        return cls(oauth_token=oauth_token, paper=paper)

    # ── Orders ───────────────────────────────────────────────────────

    async def place_order(self, request: OrderRequest) -> Order:
        alpaca_request = self._build_alpaca_request(request)
        raw = await asyncio.to_thread(self._client.submit_order, alpaca_request)
        return self._order_from_alpaca(raw)

    async def cancel_order(self, broker_order_id: str) -> Order:
        # Alpaca's `cancel_order_by_id` returns None on success; we re-fetch
        # to return a fresh Order object (matches the BrokerInterface contract).
        await asyncio.to_thread(self._client.cancel_order_by_id, broker_order_id)
        return await self.get_order(broker_order_id)

    async def get_order(self, broker_order_id: str) -> Order:
        raw = await asyncio.to_thread(self._client.get_order_by_id, broker_order_id)
        return self._order_from_alpaca(raw)

    async def cancel_open_orders(self, symbol: str) -> int:
        """Cancel every open order on a symbol. Returns the cancel count.

        Used by the position manager before an agent-initiated early close:
        a bracket entry leaves OCO children resting at the broker, and a
        market SELL would be rejected for unavailable qty while those
        children hold the shares.
        """
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol.upper()])
        open_orders = await asyncio.to_thread(self._client.get_orders, req)
        canceled = 0
        for raw in open_orders:
            order_id = str(getattr(raw, "id", ""))
            if not order_id:
                continue
            try:
                await asyncio.to_thread(self._client.cancel_order_by_id, order_id)
                canceled += 1
            except Exception as exc:  # already-filled races are fine
                logger.warning(
                    "cancel_open_orders: %s on %s failed — %s", order_id, symbol, exc
                )
        return canceled

    # ── Positions ────────────────────────────────────────────────────

    async def list_positions(self) -> list[Position]:
        raw = await asyncio.to_thread(self._client.get_all_positions)
        return [self._position_from_alpaca(p) for p in raw]

    async def get_position(self, symbol: str) -> Position | None:
        try:
            raw = await asyncio.to_thread(self._client.get_open_position, symbol)
        except Exception as exc:  # alpaca-py raises 404 as APIError
            if "position does not exist" in str(exc).lower():
                return None
            raise
        return self._position_from_alpaca(raw)

    # ── Account ──────────────────────────────────────────────────────

    async def _get_account(self) -> object:
        return await asyncio.to_thread(self._client.get_account)

    async def get_account_equity(self) -> float:
        acct = await self._get_account()
        return float(acct.equity)  # type: ignore[attr-defined]

    async def get_buying_power(self) -> float:
        acct = await self._get_account()
        return float(acct.buying_power)  # type: ignore[attr-defined]

    # ── Mappers ──────────────────────────────────────────────────────

    def _build_alpaca_request(self, request: OrderRequest) -> object:
        side = _SIDE_TO_ALPACA[request.side]
        tif = _TIF_TO_ALPACA[request.time_in_force]
        common = {
            "symbol": request.symbol,
            "qty": request.qty,
            "side": side,
            "time_in_force": tif,
            "client_order_id": request.client_order_id,
        }
        if request.is_bracket:
            # Entry + OCO children held BROKER-side. The stop/target the
            # user approved survive an outage of our entire stack.
            common["order_class"] = OrderClass.BRACKET
            common["take_profit"] = TakeProfitRequest(
                limit_price=round(request.take_profit_price, 2)
            )
            common["stop_loss"] = StopLossRequest(
                stop_price=round(request.stop_loss_price, 2)
            )
        elif request.take_profit_price is not None or request.stop_loss_price is not None:
            raise ValueError(
                "Bracket orders need BOTH take_profit_price and stop_loss_price — "
                "a one-legged bracket silently drops half the exit plan."
            )
        if request.order_type is OrderType.MARKET:
            return MarketOrderRequest(**common)
        if request.order_type is OrderType.LIMIT:
            if request.limit_price is None:
                raise ValueError("LIMIT order requires limit_price")
            return LimitOrderRequest(**common, limit_price=request.limit_price)
        if request.order_type is OrderType.STOP:
            if request.stop_price is None:
                raise ValueError("STOP order requires stop_price")
            return StopOrderRequest(**common, stop_price=request.stop_price)
        if request.order_type is OrderType.STOP_LIMIT:
            if request.stop_price is None or request.limit_price is None:
                raise ValueError("STOP_LIMIT order requires both stop_price and limit_price")
            return StopLimitOrderRequest(
                **common, stop_price=request.stop_price, limit_price=request.limit_price
            )
        raise ValueError(f"Unsupported order type: {request.order_type}")

    def _order_from_alpaca(self, raw: object) -> Order:
        # alpaca-py returns Pydantic v2 models; getattr keeps mypy quiet.
        broker_order_id = str(getattr(raw, "id", ""))
        symbol = str(getattr(raw, "symbol", ""))
        side_val = getattr(raw, "side", None)
        side = Side(str(side_val).upper().split(".")[-1]) if side_val else Side.BUY
        qty = int(float(getattr(raw, "qty", 0) or 0))
        filled_qty = int(float(getattr(raw, "filled_qty", 0) or 0))
        avg_price = getattr(raw, "filled_avg_price", None)
        submitted = getattr(raw, "submitted_at", None) or datetime.now(UTC)
        filled = getattr(raw, "filled_at", None)
        status = _status_from_alpaca(getattr(raw, "status", _AlpacaStatus.NEW))

        return Order(
            broker_order_id=broker_order_id,
            client_order_id=getattr(raw, "client_order_id", None),
            symbol=symbol,
            side=side,
            qty=qty,
            filled_qty=filled_qty,
            avg_fill_price=float(avg_price) if avg_price is not None else None,
            status=status,
            submitted_at=submitted,
            filled_at=filled,
            raw={
                k: str(v)
                for k, v in (getattr(raw, "model_dump", lambda: {})() or {}).items()
            },
        )

    def _position_from_alpaca(self, raw: object) -> Position:
        return Position(
            symbol=str(getattr(raw, "symbol", "")),
            qty=int(float(getattr(raw, "qty", 0) or 0)),
            avg_entry_price=float(getattr(raw, "avg_entry_price", 0) or 0),
            market_value=float(getattr(raw, "market_value", 0) or 0),
            unrealized_pl=float(getattr(raw, "unrealized_pl", 0) or 0),
            unrealized_pl_pct=float(getattr(raw, "unrealized_plpc", 0) or 0) * 100,
            raw={
                k: str(v)
                for k, v in (getattr(raw, "model_dump", lambda: {})() or {}).items()
            },
        )


# ─────────────────────────────────────────────────────────────────────
# Symbol validation
#
# The ticker regex accepts any 1-10 uppercase-ish string, so "APPLE"
# and "BANANA" pass it — they're well-formed, they just don't exist.
# Sending one into the council burns six LLM calls before Alpaca
# rejects the order at the very end. Ask the broker first.
# ─────────────────────────────────────────────────────────────────────


def _opt_bool(value: object) -> bool | None:
    """Coerce a broker flag to a tri-state bool.

    ``None`` must survive as ``None`` rather than collapsing to False:
    "the broker says this is not shortable" and "we never learned whether
    it is" produce the same veto today, but they are different facts and
    the audit log should be able to tell them apart.
    """
    if value is None:
        return None
    return bool(value)


class AssetInfo(NamedTuple):
    """What the broker knows about a symbol.

    ``shortable`` / ``easy_to_borrow`` are the borrow side of the record and
    exist for the risk engine's ``shortable_check``. Both are ``None`` when
    the broker did not report them — the rule treats unknown as "do not
    short", because an unverified borrow is not a borrow.
    """

    symbol: str
    name: str
    tradable: bool
    fractionable: bool
    shortable: bool | None = None
    """Broker accepts short-sale orders in this name at all."""
    easy_to_borrow: bool | None = None
    """On the ETB list: locate is automatic and the borrow is effectively
    free. Off it (HTB) the borrow accrues a daily fee and the lender can
    force a buy-in — neither is modelled by the sizer."""


async def lookup_asset(symbol: str, *, api_key: str, secret_key: str) -> AssetInfo | None:
    """Alpaca's record for ``symbol``, or None when it isn't a US equity.

    Returns None for both "no such symbol" and "not tradable here" — the
    caller only needs to know whether it can act on it.
    """
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetAssetsRequest

    def _fetch() -> AssetInfo | None:
        client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        try:
            a = client.get_asset(symbol.upper())
        except Exception:
            return None
        if a is None:
            return None
        return AssetInfo(
            symbol=str(a.symbol),
            name=str(getattr(a, "name", "") or ""),
            tradable=bool(getattr(a, "tradable", False)),
            fractionable=bool(getattr(a, "fractionable", False)),
            shortable=_opt_bool(getattr(a, "shortable", None)),
            easy_to_borrow=_opt_bool(getattr(a, "easy_to_borrow", None)),
        )

    _ = GetAssetsRequest  # imported for callers that want to list; keep the dep explicit
    return await asyncio.to_thread(_fetch)


async def list_tradable_assets(*, api_key: str, secret_key: str) -> list[AssetInfo]:
    """Every active, tradable US equity/ETF the broker will accept.

    ~13.4k rows, ~2s over the wire. Callers are expected to cache this —
    the set changes on listing/delisting, i.e. daily at most, so hitting
    Alpaca per keystroke would be absurd.
    """
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest

    def _fetch() -> list[AssetInfo]:
        client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        assets = client.get_all_assets(
            GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
        )
        return [
            AssetInfo(
                symbol=str(a.symbol),
                name=str(getattr(a, "name", "") or ""),
                tradable=True,
                fractionable=bool(getattr(a, "fractionable", False)),
                shortable=_opt_bool(getattr(a, "shortable", None)),
                easy_to_borrow=_opt_bool(getattr(a, "easy_to_borrow", None)),
            )
            for a in assets
            if getattr(a, "tradable", False)
        ]

    return await asyncio.to_thread(_fetch)
