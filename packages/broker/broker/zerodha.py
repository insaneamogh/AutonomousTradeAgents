"""Zerodha (Kite Connect v3) implementation of ``BrokerInterface``.

Hand-rolled async client over ``httpx`` — the official ``kiteconnect`` SDK
is synchronous and drags in heavy deps; everything we need is five REST
endpoints with a stable v3 contract.

Auth model (very different from Alpaca OAuth — read this before touching):
  - The operator registers a Kite Connect app → gets ``api_key`` +
    ``api_secret`` (ours, env-level — NOT per-user).
  - A user logs in at ``https://kite.zerodha.com/connect/login?v=3&api_key=…``
    → Zerodha redirects to the app's registered redirect URL with a
    single-use ``request_token``.
  - ``request_token`` + sha256(api_key + request_token + api_secret) is
    exchanged at ``/session/token`` for an ``access_token``.
  - **Access tokens expire daily** (~06:00 IST the next morning). There is
    no refresh token. The user re-logs-in every trading day. The API layer
    stores the expiry and surfaces "reconnect Zerodha" instead of a
    confusing broker 403.

Symbol convention:
  ``EXCHANGE:TRADINGSYMBOL`` — e.g. ``NSE:RELIANCE``, ``NFO:NIFTY24DECFUT``,
  ``NFO:NIFTY2461923500CE``, ``BSE:SENSEX``. A bare symbol defaults to NSE.
  This is exactly Kite's quote-API convention, so symbols round-trip
  through logs and the mobile app without a second mapping table.

Product selection (CNC / MIS / NRML):
  - NSE/BSE equity defaults to CNC (delivery). Pass ``default_product="MIS"``
    (or set ``KITE_DEFAULT_PRODUCT=MIS``) for intraday.
  - NFO/MCX/CDS derivatives default to NRML — futures + options can't be CNC.

Idempotency:
  Kite has NO server-side client-order-id dedupe (the ``tag`` field is an
  annotation, not a key). ``place_order`` therefore does a best-effort
  guard: it lists today's orders first and returns the existing order when
  one carries the same tag and isn't dead (REJECTED/CANCELLED). This gives
  retry semantics equivalent to Alpaca's within the trading day.

Currency: everything here is INR. ``BrokerInterface`` floats are in the
account's native currency; the risk engine is currency-agnostic (ratios).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

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

logger = logging.getLogger("broker.zerodha")

DEFAULT_API_BASE = "https://api.kite.trade"
DEFAULT_LOGIN_BASE = "https://kite.zerodha.com/connect/login"

IST = timezone(timedelta(hours=5, minutes=30))
# Kite flushes access tokens around 06:00 IST every morning.
TOKEN_FLUSH_IST = time(hour=6, minute=0)

_EQUITY_EXCHANGES = frozenset({"NSE", "BSE"})
_DERIVATIVE_EXCHANGES = frozenset({"NFO", "BFO", "MCX", "CDS", "BCD"})

_TYPE_TO_KITE: dict[OrderType, str] = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.STOP: "SL-M",
    OrderType.STOP_LIMIT: "SL",
}

_TIF_TO_KITE: dict[TimeInForce, str] = {
    TimeInForce.DAY: "DAY",
    TimeInForce.IOC: "IOC",
    # GTC/FOK have no Kite equivalent for regular orders — mapped below
    # with an explicit error so callers don't silently get DAY.
}

# Kite order statuses → ours. Anything *PENDING* / *RECEIVED* is in-flight.
_STATUS_FROM_KITE: dict[str, OrderStatus] = {
    "COMPLETE": OrderStatus.FILLED,
    "OPEN": OrderStatus.ACCEPTED,
    "CANCELLED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
    "TRIGGER PENDING": OrderStatus.ACCEPTED,
}

_DEAD_KITE_STATUSES = frozenset({"REJECTED", "CANCELLED", "EXPIRED"})


class ZerodhaError(Exception):
    """Kite API returned an error envelope or unexpected payload."""


def split_symbol(symbol: str) -> tuple[str, str]:
    """``'NFO:NIFTY24DECFUT'`` → ``('NFO', 'NIFTY24DECFUT')``. Bare → NSE."""
    if ":" in symbol:
        exchange, tradingsymbol = symbol.split(":", 1)
        return exchange.upper(), tradingsymbol.upper()
    return "NSE", symbol.upper()


def join_symbol(exchange: str, tradingsymbol: str) -> str:
    """Inverse of ``split_symbol`` — always exchange-qualified."""
    return f"{exchange.upper()}:{tradingsymbol.upper()}"


def login_url(api_key: str, *, redirect_params: str | None = None) -> str:
    """The Kite login page the user opens in a browser.

    ``redirect_params`` (e.g. ``"state=abc123"``) is appended by Zerodha to
    the registered redirect URL — our CSRF state rides along on it.
    """
    base = os.environ.get("KITE_LOGIN_BASE", "").strip() or DEFAULT_LOGIN_BASE
    params: dict[str, str] = {"v": "3", "api_key": api_key}
    if redirect_params:
        params["redirect_params"] = redirect_params
    return f"{base}?{urlencode(params)}"


def session_checksum(api_key: str, request_token: str, api_secret: str) -> str:
    """Kite's session-token checksum: sha256(api_key + request_token + secret)."""
    return hashlib.sha256(
        f"{api_key}{request_token}{api_secret}".encode("ascii")
    ).hexdigest()


def next_token_expiry(now: datetime | None = None) -> datetime:
    """The next 06:00 IST after ``now`` — when Kite flushes access tokens.

    Returned in UTC so it slots straight into ``access_token_expires_at``.
    """
    now_ist = (now or datetime.now(timezone.utc)).astimezone(IST)
    flush = now_ist.replace(
        hour=TOKEN_FLUSH_IST.hour, minute=TOKEN_FLUSH_IST.minute,
        second=0, microsecond=0,
    )
    if now_ist >= flush:
        flush += timedelta(days=1)
    return flush.astimezone(timezone.utc)


def _tag_from_client_order_id(client_order_id: str | None) -> str | None:
    """Kite tags are alphanumeric, max 20 chars. Keep the TAIL — our ids
    look like ``agent-exec-<uuid>`` and the uuid end carries the entropy.
    """
    if not client_order_id:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", client_order_id)
    return cleaned[-20:] if cleaned else None


def _status_from_kite(raw_status: str, filled_qty: int) -> OrderStatus:
    status = _STATUS_FROM_KITE.get(raw_status.upper())
    if status is OrderStatus.ACCEPTED and filled_qty > 0:
        return OrderStatus.PARTIALLY_FILLED
    if status is not None:
        return status
    return OrderStatus.SUBMITTED


def _parse_kite_ts(value: Any) -> datetime | None:
    """Kite timestamps are naive IST strings like ``2026-06-10 09:21:03``."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=IST)
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    except ValueError:
        return None


class ZerodhaBroker(BrokerInterface):
    """Zerodha Kite Connect trading client (live only — Kite has no paper env).

    Construct with the app's ``api_key`` + the user's daily ``access_token``.
    ``transport`` is injectable so tests run against ``httpx.MockTransport``
    without network.
    """

    name = "zerodha"
    is_paper = False

    def __init__(
        self,
        api_key: str,
        access_token: str,
        *,
        default_product: str | None = None,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not api_key or not access_token:
            raise ValueError("ZerodhaBroker: api_key and access_token are required")
        self._api_key = api_key
        self._access_token = access_token
        self._default_product = (
            default_product
            or os.environ.get("KITE_DEFAULT_PRODUCT", "").strip().upper()
            or "CNC"
        )
        self._base_url = (
            base_url
            or os.environ.get("KITE_API_BASE", "").strip()
            or DEFAULT_API_BASE
        )
        self._transport = transport
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> ZerodhaBroker:
        """Build from KITE_API_KEY / KITE_ACCESS_TOKEN env (smoke / CLI use)."""
        return cls(
            api_key=os.environ["KITE_API_KEY"],
            access_token=os.environ["KITE_ACCESS_TOKEN"],
        )

    # ── HTTP plumbing ────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "X-Kite-Version": "3",
            "Authorization": f"token {self._api_key}:{self._access_token}",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """One Kite call. Unwraps the ``{status, data}`` envelope; raises
        ``ZerodhaError`` with Kite's message on anything non-success.
        """
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            try:
                resp = await client.request(
                    method, path, data=data, headers=self._headers()
                )
            except httpx.HTTPError as exc:
                raise ZerodhaError(f"network error reaching Kite: {exc}") from exc

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ZerodhaError(
                f"Kite returned non-JSON (HTTP {resp.status_code})"
            ) from exc

        if resp.status_code >= 400 or payload.get("status") == "error":
            message = payload.get("message", f"HTTP {resp.status_code}")
            error_type = payload.get("error_type", "unknown")
            raise ZerodhaError(f"{error_type}: {message}")
        return payload.get("data")

    # ── Orders ───────────────────────────────────────────────────────

    def _product_for(self, exchange: str) -> str:
        if exchange in _DERIVATIVE_EXCHANGES:
            return "NRML" if self._default_product == "CNC" else self._default_product
        return self._default_product

    async def place_order(self, request: OrderRequest) -> Order:
        exchange, tradingsymbol = split_symbol(request.symbol)
        tag = _tag_from_client_order_id(request.client_order_id)

        # Kite has no client_order_id dedupe — emulate it via the tag so a
        # retried executor call can't double-submit within the day.
        if tag is not None:
            existing = await self._find_order_by_tag(tag)
            if existing is not None:
                logger.info(
                    "zerodha: tag %s already has live order %s — returning it",
                    tag, existing.broker_order_id,
                )
                return existing

        if request.time_in_force not in _TIF_TO_KITE:
            raise ValueError(
                f"Zerodha regular orders support DAY/IOC only, got {request.time_in_force}"
            )

        form: dict[str, Any] = {
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": request.side.value,
            "order_type": _TYPE_TO_KITE[request.order_type],
            "quantity": request.qty,
            "product": self._product_for(exchange),
            "validity": _TIF_TO_KITE[request.time_in_force],
        }
        if request.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            if request.limit_price is None:
                raise ValueError(f"{request.order_type.value} order requires limit_price")
            form["price"] = request.limit_price
        if request.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            if request.stop_price is None:
                raise ValueError(f"{request.order_type.value} order requires stop_price")
            form["trigger_price"] = request.stop_price
        if tag is not None:
            form["tag"] = tag

        data = await self._request("POST", "/orders/regular", data=form)
        order_id = str(data["order_id"])
        return await self.get_order(order_id)

    async def cancel_order(self, broker_order_id: str) -> Order:
        await self._request("DELETE", f"/orders/regular/{broker_order_id}")
        return await self.get_order(broker_order_id)

    async def get_order(self, broker_order_id: str) -> Order:
        # Kite returns the order's full state history; last entry is current.
        history = await self._request("GET", f"/orders/{broker_order_id}")
        if not history:
            raise ZerodhaError(f"order {broker_order_id} not found")
        return self._order_from_kite(history[-1])

    async def _find_order_by_tag(self, tag: str) -> Order | None:
        """Scan today's orderbook for a non-dead order carrying ``tag``."""
        orders = await self._request("GET", "/orders") or []
        for raw in orders:
            if raw.get("tag") == tag and str(raw.get("status", "")).upper() not in _DEAD_KITE_STATUSES:
                return self._order_from_kite(raw)
        return None

    # ── Positions ────────────────────────────────────────────────────

    async def list_positions(self) -> list[Position]:
        """Net day positions + demat holdings, merged per symbol.

        Kite splits "bought today" (positions) from "settled delivery"
        (holdings); the risk engine wants the combined exposure.
        """
        merged: dict[str, Position] = {}

        holdings = await self._request("GET", "/portfolio/holdings") or []
        for h in holdings:
            pos = self._position_from_kite(h)
            if pos.qty != 0:
                merged[pos.symbol] = pos

        net = (await self._request("GET", "/portfolio/positions") or {}).get("net", [])
        for p in net:
            pos = self._position_from_kite(p)
            if pos.qty == 0:
                continue
            prior = merged.get(pos.symbol)
            if prior is None:
                merged[pos.symbol] = pos
                continue
            total_qty = prior.qty + pos.qty
            if total_qty == 0:
                del merged[pos.symbol]
                continue
            merged[pos.symbol] = Position(
                symbol=pos.symbol,
                qty=total_qty,
                avg_entry_price=(
                    prior.avg_entry_price * prior.qty + pos.avg_entry_price * pos.qty
                ) / total_qty,
                market_value=prior.market_value + pos.market_value,
                unrealized_pl=prior.unrealized_pl + pos.unrealized_pl,
                unrealized_pl_pct=0.0,  # recomputed below
                raw={"holdings": prior.raw, "positions": pos.raw},
            )

        out: list[Position] = []
        for pos in merged.values():
            cost = pos.avg_entry_price * pos.qty
            pct = (pos.unrealized_pl / abs(cost)) * 100 if cost else 0.0
            out.append(
                Position(
                    symbol=pos.symbol,
                    qty=pos.qty,
                    avg_entry_price=pos.avg_entry_price,
                    market_value=pos.market_value,
                    unrealized_pl=pos.unrealized_pl,
                    unrealized_pl_pct=pct,
                    raw=pos.raw,
                )
            )
        return out

    async def get_position(self, symbol: str) -> Position | None:
        target = join_symbol(*split_symbol(symbol))
        for pos in await self.list_positions():
            if pos.symbol == target:
                return pos
        return None

    # ── Account ──────────────────────────────────────────────────────

    async def get_account_equity(self) -> float:
        """Equity-segment net margin + market value of demat holdings (INR).

        Day positions are excluded on purpose: their cash impact is already
        inside the margin number (equity debits cash; derivatives block
        margin), so adding their market value would double-count.
        """
        margins = await self._request("GET", "/user/margins")
        net = float((margins.get("equity") or {}).get("net", 0) or 0)
        holdings = await self._request("GET", "/portfolio/holdings") or []
        holdings_value = sum(
            float(h.get("last_price", 0) or 0) * int(float(h.get("quantity", 0) or 0))
            for h in holdings
        )
        return net + holdings_value

    async def get_buying_power(self) -> float:
        """Live balance available to trade in the equity segment (INR)."""
        margins = await self._request("GET", "/user/margins")
        available = (margins.get("equity") or {}).get("available") or {}
        live = available.get("live_balance")
        if live is not None:
            return float(live)
        cash = available.get("cash")
        if cash is not None:
            return float(cash)
        return float((margins.get("equity") or {}).get("net", 0) or 0)

    # ── Mappers ──────────────────────────────────────────────────────

    def _order_from_kite(self, raw: dict[str, Any]) -> Order:
        filled_qty = int(float(raw.get("filled_quantity", 0) or 0))
        avg_price = float(raw.get("average_price", 0) or 0)
        submitted = (
            _parse_kite_ts(raw.get("order_timestamp"))
            or datetime.now(timezone.utc)
        )
        status = _status_from_kite(str(raw.get("status", "")), filled_qty)
        return Order(
            broker_order_id=str(raw.get("order_id", "")),
            client_order_id=raw.get("tag") or None,
            symbol=join_symbol(
                str(raw.get("exchange", "NSE")), str(raw.get("tradingsymbol", ""))
            ),
            side=Side(str(raw.get("transaction_type", "BUY")).upper()),
            qty=int(float(raw.get("quantity", 0) or 0)),
            filled_qty=filled_qty,
            avg_fill_price=avg_price if avg_price > 0 else None,
            status=status,
            submitted_at=submitted,
            filled_at=(
                _parse_kite_ts(raw.get("exchange_update_timestamp"))
                if status is OrderStatus.FILLED
                else None
            ),
            raw={k: str(v) for k, v in raw.items()},
        )

    def _position_from_kite(self, raw: dict[str, Any]) -> Position:
        qty = int(float(raw.get("quantity", 0) or 0))
        avg = float(raw.get("average_price", 0) or 0)
        last = float(raw.get("last_price", 0) or 0)
        pnl = float(raw.get("pnl", (last - avg) * qty) or 0)
        cost = avg * qty
        return Position(
            symbol=join_symbol(
                str(raw.get("exchange", "NSE")), str(raw.get("tradingsymbol", ""))
            ),
            qty=qty,
            avg_entry_price=avg,
            market_value=last * qty,
            unrealized_pl=pnl,
            unrealized_pl_pct=(pnl / abs(cost)) * 100 if cost else 0.0,
            raw={k: str(v) for k, v in raw.items()},
        )


# ─────────────────────────────────────────────────────────────────────
# Session-token exchange (module-level — used by the API connect flow)
# ─────────────────────────────────────────────────────────────────────


async def exchange_request_token(
    *,
    api_key: str,
    api_secret: str,
    request_token: str,
    base_url: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """POST /session/token — returns Kite's session payload.

    Keys of interest: ``access_token``, ``user_id``, ``user_name``,
    ``login_time``. ``client`` is injectable for tests (MockTransport).
    """
    base = base_url or os.environ.get("KITE_API_BASE", "").strip() or DEFAULT_API_BASE
    form = {
        "api_key": api_key,
        "request_token": request_token,
        "checksum": session_checksum(api_key, request_token, api_secret),
    }
    headers = {"X-Kite-Version": "3"}

    owned = False
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)
        owned = True
    try:
        resp = await client.post(f"{base}/session/token", data=form, headers=headers)
    except httpx.HTTPError as exc:
        raise ZerodhaError(f"network error reaching Kite: {exc}") from exc
    finally:
        if owned:
            await client.aclose()

    try:
        payload = resp.json()
    except ValueError as exc:
        raise ZerodhaError(f"Kite returned non-JSON (HTTP {resp.status_code})") from exc

    if resp.status_code >= 400 or payload.get("status") == "error":
        message = payload.get("message", f"HTTP {resp.status_code}")
        raise ZerodhaError(f"session token exchange failed: {message}")

    data = payload.get("data") or {}
    if not data.get("access_token"):
        raise ZerodhaError("Kite returned no access_token")
    return data
