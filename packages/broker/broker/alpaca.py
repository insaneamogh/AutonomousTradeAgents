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
from datetime import UTC, date, datetime
from typing import NamedTuple

from alpaca.data.enums import OptionsFeed
from alpaca.data.requests import OptionChainRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetClass,
    ContractType,
    OrderClass,
    OrderSide,
    PositionIntent,
    QueryOrderStatus,
)
from alpaca.trading.enums import (
    OrderStatus as _AlpacaStatus,
)
from alpaca.trading.enums import (
    OrderType as _AlpacaType,
)
from alpaca.trading.enums import TimeInForce as _AlpacaTif
from alpaca.trading.models import OptionContract
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLimitOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)
from alpaca.trading.requests import (
    OrderRequest as _AlpacaOrderRequest,
)

from broker.base import BrokerInterface
from broker.types import (
    OccSymbol,
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
    Side.BUY_TO_OPEN: OrderSide.BUY,
    Side.SELL_TO_CLOSE: OrderSide.SELL,
}

_SIDE_TO_POSITION_INTENT: dict[Side, PositionIntent] = {
    Side.BUY_TO_OPEN: PositionIntent.BUY_TO_OPEN,
    Side.SELL_TO_CLOSE: PositionIntent.SELL_TO_CLOSE,
}
"""Alpaca keeps ``side`` and ``position_intent`` as two separate fields on
an order request. A plain equity ``Side`` (BUY/SELL) has no entry here, so
``_build_alpaca_request`` leaves ``position_intent`` unset for equities —
only the options-only ``Side`` values carry one."""


class OptionBracketNotSupportedError(ValueError):
    """Raised when asked to build a bracket-class order for an option
    symbol.

    Alpaca's ``OrderClass`` only allows ``simple``/``mleg`` for
    ``us_option`` assets — no bracket, no OCO. Phase A never constructs
    this combination upstream (the executor forces ``use_bracket=False``
    for every options proposal — a broker bracket is structurally
    impossible for a single-leg option order), so reaching this exception
    means an upstream invariant broke. Belt-and-suspenders: better to fail
    loudly here, with a named error, than let Alpaca's API reject the
    request with an opaque 422.
    """


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

    async def get_options_trading_level(self) -> int | None:
        acct = await self._get_account()
        level = getattr(acct, "options_trading_level", None)
        return int(level) if level is not None else None

    async def get_account_number(self) -> str | None:
        """The broker's own identifier for the account these keys open.

        Exists so a KEY SWAP is detectable. Everything the reconciler
        derives — the drawdown halt, which positions are "ours", which
        ``orders`` rows have a live ``broker_order_id`` — is keyed on our
        ``user_id``, not on the account, so pointing the same user at a
        different Alpaca account silently inherits all of it: a halt
        raised by the old account's drawdown, open decisions for positions
        the new account does not hold, and order ids that now 404.

        Same structural-resolution contract as
        ``get_prior_close_equity``: NOT on the ``BrokerInterface``
        Protocol (it is ``runtime_checkable``, so a new required method
        would un-satisfy every existing stub), reached by callers through
        ``getattr``. Returns None rather than raising when absent.
        """
        acct = await self._get_account()
        value = getattr(acct, "account_number", None)
        return str(value) if value else None

    async def get_prior_close_equity(self) -> float | None:
        """Equity as of the PREVIOUS trading session's close.

        Alpaca computes this itself (``account.last_equity``) against the
        real market calendar. That matters because the obvious substitute
        — "the earliest equity snapshot we hold with today's UTC date" —
        is wrong by construction: the US session runs 13:30-20:00 UTC, so
        every snapshot written between 00:00 and 13:30 UTC carries
        YESTERDAY's closing equity while already having today's UTC date.
        Baselining off one of those makes the whole overnight move
        disappear from the day's P&L.

        Not on ``BrokerInterface``: that Protocol is ``runtime_checkable``
        and structural, so adding a method to it would silently
        un-satisfy every stub and mock that does not implement it. Callers
        reach this through ``getattr(broker, "get_prior_close_equity",
        None)`` and fall back when it is absent.

        Returns None rather than raising when the broker omits the field
        (a brand-new account has no prior session) — the caller's own
        fallback is the correct behaviour there, not a zero.
        """
        acct = await self._get_account()
        value = getattr(acct, "last_equity", None)
        if value is None:
            return None
        try:
            equity = float(value)
        except (TypeError, ValueError):
            return None
        return equity if equity > 0 else None

    # ── Mappers ──────────────────────────────────────────────────────

    def _build_alpaca_request(self, request: OrderRequest) -> _AlpacaOrderRequest:
        side = _SIDE_TO_ALPACA[request.side]
        tif = _TIF_TO_ALPACA[request.time_in_force]
        common = {
            "symbol": request.symbol,
            "qty": request.qty,
            "side": side,
            "time_in_force": tif,
            "client_order_id": request.client_order_id,
        }
        position_intent = _SIDE_TO_POSITION_INTENT.get(request.side)
        if position_intent is not None:
            common["position_intent"] = position_intent
        if request.is_bracket:
            if OccSymbol.try_parse(request.symbol) is not None:
                # Belt-and-suspenders — the executor should never construct
                # this combination (it forces use_bracket=False for every
                # options proposal), but the broker layer is the last line
                # before the wire, so it's the one place this can't slip
                # through as an opaque Alpaca 422 instead.
                raise OptionBracketNotSupportedError(
                    f"Cannot build a bracket order for option symbol "
                    f"{request.symbol!r} — Alpaca's OrderClass only allows "
                    "simple/mleg for us_option; no bracket, no OCO."
                )
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
        # AssetClass is a (str, Enum) — comparing to the enum member works
        # regardless of whether alpaca-py handed back the member itself or
        # its raw string value.
        is_option = getattr(raw, "asset_class", None) == AssetClass.US_OPTION
        return Position(
            symbol=str(getattr(raw, "symbol", "")),
            qty=int(float(getattr(raw, "qty", 0) or 0)),
            avg_entry_price=float(getattr(raw, "avg_entry_price", 0) or 0),
            market_value=float(getattr(raw, "market_value", 0) or 0),
            unrealized_pl=float(getattr(raw, "unrealized_pl", 0) or 0),
            unrealized_pl_pct=float(getattr(raw, "unrealized_plpc", 0) or 0) * 100,
            # Phase A default: no non-standard-multiplier US equity option
            # is currently listed, so 100 is hardcoded rather than looked
            # up. OptionContract.size is the source of truth if this
            # assumption ever needs revisiting — not built now since
            # nothing today requires the extra round-trip.
            multiplier=100 if is_option else 1,
            is_option=is_option,
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


# ─────────────────────────────────────────────────────────────────────
# Options — contract lookup (Phase A: long calls/puts only)
# ─────────────────────────────────────────────────────────────────────


async def lookup_option_contract(
    symbol_or_id: str, *, api_key: str, secret_key: str
) -> OptionContract | dict[str, object] | None:
    """Alpaca's record for one OCC option symbol (or contract id), or None
    when it doesn't exist. Thin pass-through to
    ``TradingClient.get_option_contract`` — same error-swallowing pattern
    as ``lookup_asset``: the caller only needs to know whether it can act
    on it, not why a lookup failed.

    ``dict`` is part of the return type (not just ``OptionContract``)
    because alpaca-py's own ``get_option_contract`` is typed to return
    either — the same raw-response fallback ``lookup_asset`` already
    tolerates for ``get_asset``, read via ``getattr(..., default)`` rather
    than direct attribute access for exactly that reason.
    """
    from alpaca.trading.client import TradingClient

    def _fetch() -> OptionContract | dict[str, object] | None:
        client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        try:
            return client.get_option_contract(symbol_or_id)
        except Exception:
            return None

    return await asyncio.to_thread(_fetch)


_CONTRACT_PAGE_LIMIT = 10_000
"""Alpaca's documented max page size for /v2/options/contracts. Large
pages mean a full chain is usually one request, not fifty."""

_MAX_CONTRACT_PAGES = 20
"""Hard stop on the paging loop. 20 x 10k covers any single underlying's
chain many times over; the cap exists so a malformed next_page_token
cannot spin forever inside a council pass."""


async def list_option_contracts(
    underlying_symbols: list[str],
    *,
    api_key: str,
    secret_key: str,
    expiration_date_gte: date | None = None,
    expiration_date_lte: date | None = None,
    contract_type: ContractType | None = None,
) -> list[OptionContract]:
    """Active option contracts on the given underlyings, optionally
    windowed by expiry and/or filtered to calls-only/puts-only. Thin
    pass-through to ``TradingClient.get_option_contracts`` — same pattern
    as ``list_tradable_assets`` (caller is expected to cache; a full chain
    scan per keystroke would be as absurd here as it is for equities).

    Returns ``[]`` on a broker-side failure rather than raising — mirrors
    ``lookup_option_contract``'s "the caller only needs to know whether it
    can act on it" contract, and an empty chain is the correct input for
    every downstream selection rule to self-gate on (no contract to pick).
    """
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetStatus

    def _fetch() -> list[OptionContract]:
        client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        # PAGINATED. This endpoint defaults to 100 rows and does NOT
        # auto-paginate (unlike the market-data client, whose
        # `_get_marketdata` loops on next_page_token for us).
        #
        # Measured live 2026-08-31 on SPY, 7-60 DTE:
        #     chain snapshot        4674 contracts
        #     this call, unpaged     100 contracts   <- one page
        #     merged open_interest     58 populated
        #     open_interest >= 100     10
        #
        # This is the ONLY source of open_interest, and
        # `engine.options.selection._passes_liquidity` hard-fails on
        # `open_interest is None` by design. So an unpaged fetch left 98%
        # of every chain with no OI, every one of them failed the
        # liquidity stage, and the funnel emptied to zero on EVERY symbol
        # -- including SPY, the most liquid options chain there is.
        # Options could never trade. Invisible behind a green suite
        # because no test exercised this endpoint's paging shape.
        out: list[OptionContract] = []
        page_token: str | None = None
        for _page in range(_MAX_CONTRACT_PAGES):
            request = GetOptionContractsRequest(
                underlying_symbols=underlying_symbols,
                status=AssetStatus.ACTIVE,
                expiration_date_gte=expiration_date_gte,
                expiration_date_lte=expiration_date_lte,
                type=contract_type,
                limit=_CONTRACT_PAGE_LIMIT,
                page_token=page_token,
            )
            try:
                response = client.get_option_contracts(request)
            except Exception:
                # Partial data beats none: an OI-less contract merely fails
                # the liquidity gate, which is the safe direction.
                return out
            out.extend(getattr(response, "option_contracts", None) or [])
            page_token = getattr(response, "next_page_token", None)
            if not page_token:
                break
        return out

    return await asyncio.to_thread(_fetch)


class ChainQuote(NamedTuple):
    """One contract's normalized chain-SNAPSHOT market data, from
    ``OptionHistoricalDataClient.get_option_chain`` — ``docs/OPTIONS_PLAN.md``
    §0's live-verified ``/v1beta1/options/snapshots/{underlying}`` endpoint.
    Distinct from ``list_option_contracts`` above (``/v2/options/contracts``):
    that one is contract *metadata* (and the only place ``open_interest``
    lives); this one is market data (bid/ask/delta/IV), and carries none of
    open interest or cumulative daily volume — see
    ``engine.options.contracts.fetch_option_candidates``, which merges both.

    ``contract_type``/``strike``/``expiry`` are NOT separate fields on
    alpaca-py's ``OptionsSnapshot`` — confirmed against the installed
    model (only ``.symbol``/``.latest_trade``/``.latest_quote``/
    ``.implied_volatility``/``.greeks`` exist) — so they are parsed from
    ``.symbol`` via ``OccSymbol.try_parse``, not read as snapshot fields.
    """

    occ_symbol: str
    underlying_symbol: str
    contract_type: str  # "call" | "put"
    strike: float
    expiry: date
    bid: float | None
    ask: float | None
    delta: float | None
    implied_volatility: float | None
    last_trade_size: float | None
    """Size of the single LAST trade, NOT cumulative daily volume — no
    field on this endpoint (or ``list_option_contracts``) reports the
    latter; true daily volume would need a separate per-contract bars
    call. A documented, imperfect liquidity proxy."""


def _default_options_feed() -> OptionsFeed:
    """``ALPACA_OPTIONS_FEED`` env override (``"opra"``/``"indicative"``,
    case-insensitive) — fails closed to ``INDICATIVE``, the free Basic-tier
    feed every account already has (``docs/OPTIONS_PLAN.md`` §0: derived
    quotes, 15-minute delay — not the full OPRA tape). Never silently
    requests a data tier the account may not be entitled to."""
    raw = os.environ.get("ALPACA_OPTIONS_FEED", "").strip().lower()
    return OptionsFeed.OPRA if raw == "opra" else OptionsFeed.INDICATIVE


async def list_option_chain_quotes(
    underlying_symbol: str,
    *,
    api_key: str,
    secret_key: str,
    feed: OptionsFeed | None = None,
    expiration_date_gte: date | None = None,
    expiration_date_lte: date | None = None,
    contract_type: ContractType | None = None,
) -> list[ChainQuote]:
    """Chain snapshot for one underlying — bid/ask/delta/IV per contract.

    Thin pass-through to ``OptionHistoricalDataClient.get_option_chain``,
    windowed the same way ``list_option_contracts`` already is (caller
    supplies the expiry bounds). Returns ``[]`` on a broker-side
    (network/auth) failure — same convention as every other lookup in this
    module.

    Does NOT swallow a per-item mapping failure the same way: an
    unparseable OCC symbol is skipped (logged) and the batch continues,
    but nothing here reads a field via ``getattr(..., default)``. That is
    deliberate, not an oversight — the bug this function replaces
    (``_to_contract_quote`` in ``trading_agents.nodes.drafter``, now
    deleted) read attribute names that do not exist on the real Alpaca
    model via exactly that pattern, and every real contract silently
    became ``None`` and got filtered out — invisible across 736 passing
    tests because nothing ever exercised a real response shape. Real
    attribute access means a future SDK field rename raises immediately,
    in a test built on real model instances, instead of quietly degrading
    a filter stage until every candidate vanishes three layers away.
    """
    from alpaca.data.historical.option import OptionHistoricalDataClient

    resolved_feed = feed or _default_options_feed()

    def _fetch() -> list[ChainQuote]:
        client = OptionHistoricalDataClient(api_key=api_key, secret_key=secret_key)
        request = OptionChainRequest(
            underlying_symbol=underlying_symbol,
            feed=resolved_feed,
            type=contract_type,
            expiration_date_gte=expiration_date_gte,
            expiration_date_lte=expiration_date_lte,
        )
        try:
            snapshots = client.get_option_chain(request)
        except Exception:
            logger.exception("list_option_chain_quotes: fetch failed for %s", underlying_symbol)
            return []

        quotes: list[ChainQuote] = []
        for occ_symbol, snapshot in (snapshots or {}).items():
            parsed = OccSymbol.try_parse(occ_symbol)
            if parsed is None:
                logger.warning(
                    "list_option_chain_quotes: unparseable OCC symbol %r for %s — skipping",
                    occ_symbol,
                    underlying_symbol,
                )
                continue
            q = snapshot.latest_quote
            t = snapshot.latest_trade
            g = snapshot.greeks
            quotes.append(
                ChainQuote(
                    occ_symbol=occ_symbol,
                    underlying_symbol=parsed.underlying,
                    contract_type=parsed.contract_type,
                    strike=parsed.strike,
                    expiry=parsed.expiry,
                    bid=q.bid_price if q is not None else None,
                    ask=q.ask_price if q is not None else None,
                    delta=g.delta if g is not None else None,
                    implied_volatility=snapshot.implied_volatility,
                    last_trade_size=t.size if t is not None else None,
                )
            )
        return quotes

    return await asyncio.to_thread(_fetch)
