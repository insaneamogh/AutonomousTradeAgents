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
import json
import logging
import math
import os
import re
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta, timezone
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

# SEBI's retail algo framework (in force for API orders since 2026-04-01):
# a MARKET or SL-M order placed through the API must carry market
# protection, and Kite rejects one without it. Per Kite's order docs the
# value is ">0 and up to 100 (custom %), or -1 (auto protection)", applied
# only to MARKET and SL-M. -1 lets the exchange band decide.
_PROTECTED_ORDER_TYPES = frozenset({OrderType.MARKET, OrderType.STOP})
DEFAULT_MARKET_PROTECTION = "-1"


def _market_protection(raw: str | None) -> str:
    value = (raw or "").strip() or DEFAULT_MARKET_PROTECTION
    try:
        pct = float(value)
    except ValueError as exc:
        raise ValueError(f"KITE_MARKET_PROTECTION must be -1 or in (0, 100], got {value!r}") from exc
    if pct != -1 and not (0 < pct <= 100):
        raise ValueError(f"KITE_MARKET_PROTECTION must be -1 or in (0, 100], got {value!r}")
    return value


class ZerodhaError(Exception):
    """Kite API returned an error envelope or unexpected payload."""


class ZerodhaIpNotAllowedError(ZerodhaError):
    """Kite refused an order because it did not come from the static IP
    registered for this API key (SEBI's retail algo framework, in force for
    API orders since 2026-04-01). Distinct from an expired token: the fix
    is the egress (KITE_ORDER_PROXY_URL / the IP registered with Kite), not
    a re-login."""


# ── Order-rate guard ──────────────────────────────────────────────────
# Kite: 10 orders/s and 400/min per API key; above 10/s the strategy must
# be registered with the exchange. Stay well under both, per key, across
# every ZerodhaBroker in this process (one is built per request).
_ORDER_RATE_PER_SECOND = 8
_ORDER_RATE_PER_MINUTE = 300
_order_times: dict[str, list[float]] = {}


async def _await_order_slot(api_key: str) -> None:
    import asyncio
    import time as _time

    stamps = _order_times.setdefault(api_key, [])
    while True:
        now = _time.monotonic()
        stamps[:] = [t for t in stamps if now - t < 60.0]
        last_second = [t for t in stamps if now - t < 1.0]
        if len(last_second) < _ORDER_RATE_PER_SECOND and len(stamps) < _ORDER_RATE_PER_MINUTE:
            stamps.append(now)
            return
        oldest = last_second[0] if len(last_second) >= _ORDER_RATE_PER_SECOND else stamps[0]
        window = 1.0 if len(last_second) >= _ORDER_RATE_PER_SECOND else 60.0
        await asyncio.sleep(max(0.01, window - (now - oldest)))


def reset_order_rate_for_tests() -> None:
    _order_times.clear()


def _is_order_mutation(method: str, path: str) -> bool:
    """Calls that place, modify or cancel an order or a GTT: the ones Kite
    checks against the registered static IP."""
    return method != "GET" and (path.startswith("/orders") or path.startswith("/gtt"))


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
    now_ist = (now or datetime.now(UTC)).astimezone(IST)
    flush = now_ist.replace(
        hour=TOKEN_FLUSH_IST.hour, minute=TOKEN_FLUSH_IST.minute,
        second=0, microsecond=0,
    )
    if now_ist >= flush:
        flush += timedelta(days=1)
    return flush.astimezone(UTC)


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
    text = str(value)
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    except ValueError:
        pass
    # Historical candles carry ISO with an offset: 2017-12-15T09:15:00+0530.
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


_KITE_SIDE: dict[Side, str] = {
    Side.BUY: "BUY", Side.SELL: "SELL", Side.BUY_TO_OPEN: "BUY", Side.SELL_TO_CLOSE: "SELL",
}


def _is_kite_option(exchange: str, tradingsymbol: str) -> bool:
    """An exchange-traded option on a derivatives segment: the symbol ends
    CE (call) or PE (put). Futures end FUT."""
    return exchange.upper() in _DERIVATIVE_EXCHANGES and tradingsymbol.upper().endswith(("CE", "PE"))


GTT_PREFIX = "gtt:"


def _gtt_id(broker_order_id: str) -> str:
    return broker_order_id[len(GTT_PREFIX):]


def _tick(price: float, tick: float = 0.05) -> float:
    """NSE equity tick size is 0.05; Kite rejects prices off the tick."""
    return round(round(price / tick) * tick, 2)


def _tick_toward(price: float, *, up: bool, tick: float = 0.05) -> float:
    """A limit snapped onto the tick without making it less marketable: a
    buy rounds up, a sell down. Kite rejects an off-tick price outright,
    and the option entry limit is mid + 0.01 (engine.options.entry_price),
    which is off the 0.05 tick about four times in five. A 0.05 grid is
    also on the 0.01 grid of NSE's low-priced stocks."""
    steps = round(price / tick, 6)
    steps = math.ceil(steps) if up else math.floor(steps)
    return round(steps * tick, 2)


class ZerodhaBroker(BrokerInterface):
    """Zerodha Kite Connect trading client (live only — Kite has no paper env).

    Construct with the app's ``api_key`` + the user's daily ``access_token``.
    ``transport`` is injectable so tests run against ``httpx.MockTransport``
    without network.
    """

    name = "zerodha"
    supports_brackets = False
    """Kite has no bracket for an API equity entry. The executor places a
    plain entry and order_sync puts a GTT OCO on it once it fills."""
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
        # SEBI: API orders must leave from the static IP registered with
        # Kite. When set, every order/GTT mutation goes out through this
        # proxy (the dedicated egress); reads stay direct.
        self._order_proxy = os.environ.get("KITE_ORDER_PROXY_URL", "").strip() or None
        # Validated at construction: a bad value must fail the connection,
        # not every order at submit time.
        self._market_protection = _market_protection(os.environ.get("KITE_MARKET_PROTECTION"))

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
        mutation = _is_order_mutation(method, path)
        if mutation and method == "POST":
            await _await_order_slot(self._api_key)
        proxy = self._order_proxy if mutation and self._transport is None else None
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=self._timeout,
            proxy=proxy,
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
            if mutation and " ip" in f" {message}".lower():
                logger.error(
                    "zerodha: Kite refused an order from an unregistered IP. Register this "
                    "server's egress IP with Kite, or set KITE_ORDER_PROXY_URL (%s)", error_type,
                )
                raise ZerodhaIpNotAllowedError(f"{error_type}: {message}")
            raise ZerodhaError(f"{error_type}: {message}")
        return payload.get("data")

    # ── Orders ───────────────────────────────────────────────────────

    def _product_for(self, exchange: str) -> str:
        if exchange in _DERIVATIVE_EXCHANGES:
            return "NRML" if self._default_product == "CNC" else self._default_product
        return self._default_product

    async def place_order(self, request: OrderRequest) -> Order:
        if request.take_profit_price is not None or request.stop_loss_price is not None:
            # Never silently drop the protective legs the user approved.
            raise ZerodhaError(
                "bracket exit legs are not supported on Zerodha in v1 — "
                "Kite GTT-based exits land with the India phase"
            )
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
            # Kite knows BUY and SELL only; an option order arrives here as
            # BUY_TO_OPEN / SELL_TO_CLOSE (the Alpaca-shaped intent).
            "transaction_type": _KITE_SIDE[request.side],
            "order_type": _TYPE_TO_KITE[request.order_type],
            "quantity": request.qty,
            "product": self._product_for(exchange),
            "validity": _TIF_TO_KITE[request.time_in_force],
        }
        if request.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            if request.limit_price is None:
                raise ValueError(f"{request.order_type.value} order requires limit_price")
            buying = form["transaction_type"] == "BUY"
            form["price"] = _tick_toward(request.limit_price, up=buying)
        if request.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            if request.stop_price is None:
                raise ValueError(f"{request.order_type.value} order requires stop_price")
            form["trigger_price"] = _tick(request.stop_price)
        if request.order_type in _PROTECTED_ORDER_TYPES:
            form["market_protection"] = self._market_protection
        if tag is not None:
            form["tag"] = tag

        data = await self._request("POST", "/orders/regular", data=form)
        order_id = str(data["order_id"])
        return await self.get_order(order_id)

    async def cancel_order(self, broker_order_id: str) -> Order:
        if broker_order_id.startswith(GTT_PREFIX):
            await self._request("DELETE", f"/gtt/triggers/{_gtt_id(broker_order_id)}")
            return await self.get_order(broker_order_id)
        await self._request("DELETE", f"/orders/regular/{broker_order_id}")
        return await self.get_order(broker_order_id)

    async def cancel_open_orders(self, symbol: str) -> int:
        """Cancel today's open orders on a symbol, AND delete its active
        GTTs. Called before every agent or user close: a protective GTT
        left behind after the position is gone would later fire a sell of
        shares no longer held."""
        wanted = symbol.upper()
        canceled = 0
        try:
            triggers = await self._request("GET", "/gtt/triggers") or []
        except Exception as exc:
            logger.warning("cancel_open_orders: could not list GTTs — %s", exc)
            triggers = []
        for t in triggers:
            cond = t.get("condition") or {}
            t_symbol = f"{cond.get('exchange', '')}:{cond.get('tradingsymbol', '')}".upper()
            if str(t.get("status", "")).lower() != "active" or t_symbol != wanted:
                continue
            try:
                await self._request("DELETE", f"/gtt/triggers/{t.get('id')}")
                canceled += 1
            except Exception as exc:
                logger.warning("cancel_open_orders: GTT %s delete failed — %s", t.get("id"), exc)
        orders = await self._request("GET", "/orders") or []
        for raw in orders:
            status = str(raw.get("status", "")).upper()
            raw_symbol = f"{raw.get('exchange', '')}:{raw.get('tradingsymbol', '')}".upper()
            if status in _DEAD_KITE_STATUSES or raw_symbol != wanted:
                continue
            order_id = str(raw.get("order_id", ""))
            if not order_id:
                continue
            try:
                await self._request("DELETE", f"/orders/regular/{order_id}")
                canceled += 1
            except Exception as exc:
                logger.warning("cancel_open_orders: %s failed — %s", order_id, exc)
        return canceled

    async def get_order(self, broker_order_id: str) -> Order:
        if broker_order_id.startswith(GTT_PREFIX):
            return await self._gtt_as_order(broker_order_id)
        # Kite returns the order's full state history; last entry is current.
        history = await self._request("GET", f"/orders/{broker_order_id}")
        if not history:
            raise ZerodhaError(f"order {broker_order_id} not found")
        return self._order_from_kite(history[-1])

    # ── Market data (the same access token; Kite Connect's paid plan) ─

    async def _get_text(self, path: str) -> str:
        """A non-JSON GET (the instruments dump is CSV, gzip-encoded on the
        wire; httpx decodes Content-Encoding itself)."""
        async with httpx.AsyncClient(
            base_url=self._base_url, transport=self._transport, timeout=max(self._timeout, 30.0),
        ) as client:
            try:
                resp = await client.get(path, headers=self._headers())
            except httpx.HTTPError as exc:
                raise ZerodhaError(f"network error reaching Kite: {exc}") from exc
        if resp.status_code >= 400:
            raise ZerodhaError(f"instruments dump: HTTP {resp.status_code}")
        return resp.text

    async def instruments(self, exchange: str) -> list[dict[str, Any]]:
        """Kite's daily instruments dump for one exchange: instrument_token,
        tradingsymbol, name, expiry, strike, tick_size, lot_size,
        instrument_type, segment, exchange. Regenerated once a day by Kite;
        callers cache it for the day."""
        import csv
        import io

        text = await self._get_text(f"/instruments/{exchange.upper()}")
        rows = []
        for r in csv.DictReader(io.StringIO(text)):
            rows.append({
                **r,
                "instrument_token": int(r.get("instrument_token") or 0),
                "lot_size": int(float(r.get("lot_size") or 1)),
                "tick_size": float(r.get("tick_size") or 0.05),
                "strike": float(r.get("strike") or 0),
            })
        return rows

    async def historical_daily(
        self, instrument_token: int, *, start: datetime, end: datetime
    ) -> list[tuple[datetime, float, float, float, float, float]]:
        """Daily candles (timestamp, open, high, low, close, volume) from
        GET /instruments/historical/:token/day."""
        fmt = "%Y-%m-%d %H:%M:%S"
        query = urlencode({"from": start.strftime(fmt), "to": end.strftime(fmt)})
        data = await self._request(
            "GET", f"/instruments/historical/{int(instrument_token)}/day?{query}"
        )
        out = []
        for c in (data or {}).get("candles", []):
            ts = _parse_kite_ts(c[0])
            if ts is None:
                continue
            out.append((ts, float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])))
        return out

    async def quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """Full quotes (last_price, ohlc, volume, depth, oi, ...) keyed by
        EXCHANGE:TRADINGSYMBOL, at most 500 per Kite call. A symbol Kite has
        no data for is absent, not an error."""
        out: dict[str, dict[str, Any]] = {}
        wanted = [s.upper() if ":" in s else f"NSE:{s.upper()}" for s in symbols]
        for i in range(0, len(wanted), 500):
            query = urlencode([("i", s) for s in wanted[i:i + 500]])
            out.update(await self._request("GET", f"/quote?{query}") or {})
        return out

    # ── GTT: the broker-side exit for an equity entry ─────────────────

    async def place_exit_oco(
        self,
        *,
        symbol: str,
        qty: int,
        entry_side: Side,
        stop: float,
        target: float,
        last_price: float,
    ) -> str:
        """A two-leg GTT that exits ``qty`` at ``stop`` or ``target``,
        whichever the price reaches first; Kite deletes the other leg.

        Kite has no bracket order for an API equity entry, so this is the
        broker-side exit that survives our process being down. Per the GTT
        docs: ``trigger_values`` ascending, ``orders`` in the same order,
        LIMIT orders only. The stop leg's limit sits half a percent through
        the trigger so a fast move still fills; the target leg's limit is
        the target. Returns ``gtt:<trigger_id>``, the id order_sync polls.
        """
        exchange, tradingsymbol = split_symbol(symbol)
        closing = "SELL" if entry_side in (Side.BUY, Side.BUY_TO_OPEN) else "BUY"
        slip = -0.005 if closing == "SELL" else 0.005
        legs = sorted(
            [(stop, _tick(stop * (1 + slip))), (target, _tick(target))], key=lambda leg: leg[0]
        )
        if not (legs[0][0] < last_price < legs[1][0]):
            raise ZerodhaError(
                f"GTT OCO needs last price {last_price} strictly between {legs[0][0]} "
                f"and {legs[1][0]}"
            )
        condition = {
            "exchange": exchange, "tradingsymbol": tradingsymbol,
            "trigger_values": [legs[0][0], legs[1][0]], "last_price": last_price,
        }
        orders = [
            {"exchange": exchange, "tradingsymbol": tradingsymbol,
             "transaction_type": closing, "quantity": int(qty), "order_type": "LIMIT",
             "product": self._product_for(exchange), "price": price}
            for _trigger, price in legs
        ]
        data = await self._request("POST", "/gtt/triggers", data={
            "type": "two-leg",
            "condition": json.dumps(condition),
            "orders": json.dumps(orders),
        })
        return f"{GTT_PREFIX}{data['trigger_id']}"

    async def _gtt_as_order(self, broker_order_id: str) -> Order:
        """A GTT read as one exit order. Active -> accepted; triggered ->
        the order its leg placed (fill price and status from /orders), with
        ``raw['order_type']`` 'stop' or 'limit' naming the leg; deleted,
        cancelled or expired -> canceled; a leg Kite failed to place ->
        rejected with Kite's reason."""
        t = await self._request("GET", f"/gtt/triggers/{_gtt_id(broker_order_id)}") or {}
        cond = t.get("condition") or {}
        symbol = join_symbol(str(cond.get("exchange", "NSE")), str(cond.get("tradingsymbol", "")))
        legs = t.get("orders") or []
        side = Side(str(legs[0].get("transaction_type", "SELL")).upper()) if legs else Side.SELL
        qty = int(legs[0].get("quantity", 0) or 0) if legs else 0
        status = str(t.get("status", "")).lower()
        base = Order(
            broker_order_id=broker_order_id, client_order_id=None, symbol=symbol, side=side,
            qty=qty, filled_qty=0, avg_fill_price=None, status=OrderStatus.ACCEPTED,
            submitted_at=_parse_kite_ts(t.get("created_at")) or datetime.now(UTC),
            raw={"gtt_status": status},
        )
        if status == "active":
            return base
        if status != "triggered":
            final = OrderStatus.EXPIRED if status == "expired" else OrderStatus.CANCELED
            return replace(base, status=final)
        for idx, leg in enumerate(legs):
            result = leg.get("result") or {}
            if not result:
                continue
            order_id = (result.get("order_result") or {}).get("order_id") or result.get("order_id")
            # For a SELL-to-close the lower trigger is the stop; for a
            # BUY-to-cover the upper one is.
            kind = "stop" if (idx == 0) == (side == Side.SELL) else "limit"
            if not order_id:
                reason = (result.get("order_result") or {}).get("rejection_reason") or result.get(
                    "rejection_reason", "GTT leg was not placed")
                return replace(base, status=OrderStatus.REJECTED,
                               raw={"gtt_status": status, "order_type": kind,
                                    "rejection_reason": reason})
            placed = await self.get_order(str(order_id))
            return replace(placed, broker_order_id=broker_order_id,
                           raw={**placed.raw, "gtt_status": status, "order_type": kind})
        return base

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

    async def get_options_trading_level(self) -> int | None:
        """2 when the account can trade F&O, else 0.

        Kite has no options tier. What gates buying an NSE option is the
        F&O segment being enabled on the account, which GET /user/profile
        reports as "NFO" in ``exchanges`` ("exchanges enabled for trading on
        the user's account", per Kite's docs). 2 is the long-call/put tier
        options_level_insufficient requires. This used to return None
        always, and the rule vetoes None, so every NSE option entry would
        have been refused; the docstring claimed the rule never ran for
        Indian symbols, which was not true."""
        profile = await self._request("GET", "/user/profile") or {}
        exchanges = {str(e).upper() for e in profile.get("exchanges", [])}
        return 2 if "NFO" in exchanges else 0

    # ── Mappers ──────────────────────────────────────────────────────

    def _order_from_kite(self, raw: dict[str, Any]) -> Order:
        filled_qty = int(float(raw.get("filled_quantity", 0) or 0))
        avg_price = float(raw.get("average_price", 0) or 0)
        submitted = (
            _parse_kite_ts(raw.get("order_timestamp"))
            or datetime.now(UTC)
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
        exchange = str(raw.get("exchange", "NSE"))
        tradingsymbol = str(raw.get("tradingsymbol", ""))
        return Position(
            symbol=join_symbol(exchange, tradingsymbol),
            qty=qty,
            avg_entry_price=avg,
            market_value=last * qty,
            unrealized_pl=pnl,
            unrealized_pl_pct=(pnl / abs(cost)) * 100 if cost else 0.0,
            # Kite counts an option position in UNITS (lots x lot size), so
            # the multiplier is 1: value = price x qty, unlike an OCC
            # contract's x100. is_option is what lets the premium stop and
            # the total-premium cap see it at all.
            multiplier=1,
            is_option=_is_kite_option(exchange, tradingsymbol),
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
