"""ZerodhaBroker unit tests — all network via httpx.MockTransport.

Covers: symbol convention, order-form mapping (MARKET/LIMIT/SL/SL-M),
product inference (CNC vs NRML), tag-emulated idempotency, status mapping,
positions merge (holdings + net), margins → equity/buying power, and the
session-token checksum exchange.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable
from urllib.parse import parse_qs

import httpx
import pytest

from broker.types import OrderRequest, OrderStatus, OrderType, Side, TimeInForce
from broker.zerodha import (
    ZerodhaBroker,
    ZerodhaError,
    exchange_request_token,
    login_url,
    next_token_expiry,
    session_checksum,
    split_symbol,
)


def _broker(handler: Callable[[httpx.Request], httpx.Response]) -> ZerodhaBroker:
    return ZerodhaBroker(
        api_key="testkey",
        access_token="testtoken",
        transport=httpx.MockTransport(handler),
    )


def _ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data})


def _order_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "order_id": "240610000001",
        "exchange": "NSE",
        "tradingsymbol": "RELIANCE",
        "transaction_type": "BUY",
        "quantity": 10,
        "filled_quantity": 0,
        "average_price": 0,
        "status": "OPEN",
        "tag": None,
        "order_timestamp": "2026-06-10 09:21:03",
    }
    row.update(overrides)
    return row


# ── Symbols ──────────────────────────────────────────────────────────


def test_split_symbol_defaults_to_nse() -> None:
    assert split_symbol("reliance") == ("NSE", "RELIANCE")
    assert split_symbol("NFO:NIFTY24DECFUT") == ("NFO", "NIFTY24DECFUT")
    assert split_symbol("nfo:nifty2461923500ce") == ("NFO", "NIFTY2461923500CE")


def test_login_url_carries_state_via_redirect_params() -> None:
    url = login_url("mykey", redirect_params="state=abc123")
    assert url.startswith("https://kite.zerodha.com/connect/login?")
    assert "api_key=mykey" in url
    assert "v=3" in url
    assert "redirect_params=state%3Dabc123" in url


def test_session_checksum_is_sha256_of_concat() -> None:
    expected = hashlib.sha256(b"keyTOKENsecret").hexdigest()
    assert session_checksum("key", "TOKEN", "secret") == expected


def test_next_token_expiry_is_six_am_ist() -> None:
    from datetime import datetime, timezone

    # 2026-06-10 12:00 UTC = 17:30 IST → next flush 2026-06-11 06:00 IST
    now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    expiry = next_token_expiry(now)
    assert expiry == datetime(2026, 6, 11, 0, 30, tzinfo=timezone.utc)

    # 2026-06-10 23:00 UTC = 04:30 IST next day → flush same IST morning
    now = datetime(2026, 6, 10, 23, 0, tzinfo=timezone.utc)
    assert next_token_expiry(now) == datetime(2026, 6, 11, 0, 30, tzinfo=timezone.utc)


# ── Orders ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_place_market_order_maps_form_fields() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/orders":
            return _ok([])  # tag-idempotency pre-check finds nothing
        if request.method == "POST" and request.url.path == "/orders/regular":
            seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return _ok({"order_id": "240610000001"})
        if request.url.path == "/orders/240610000001":
            return _ok([_order_row(tag="agentexecabc123")])
        raise AssertionError(f"unexpected call {request.method} {request.url.path}")

    order = await _broker(handler).place_order(
        OrderRequest(
            symbol="NSE:RELIANCE",
            side=Side.BUY,
            qty=10,
            order_type=OrderType.MARKET,
            client_order_id="agent-exec-abc123",
        )
    )
    assert seen["exchange"] == "NSE"
    assert seen["tradingsymbol"] == "RELIANCE"
    assert seen["transaction_type"] == "BUY"
    assert seen["order_type"] == "MARKET"
    assert seen["quantity"] == "10"
    assert seen["product"] == "CNC"
    assert seen["validity"] == "DAY"
    assert seen["tag"] == "agentexecabc123"
    assert order.broker_order_id == "240610000001"
    assert order.status is OrderStatus.ACCEPTED
    assert order.symbol == "NSE:RELIANCE"


@pytest.mark.asyncio
async def test_derivatives_default_to_nrml_product() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/orders" and request.method == "GET":
            return _ok([])
        if request.url.path == "/orders/regular":
            seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return _ok({"order_id": "1"})
        return _ok([_order_row(order_id="1", exchange="NFO", tradingsymbol="NIFTY24DECFUT")])

    await _broker(handler).place_order(
        OrderRequest(symbol="NFO:NIFTY24DECFUT", side=Side.SELL, qty=50)
    )
    assert seen["product"] == "NRML"
    assert seen["exchange"] == "NFO"


@pytest.mark.asyncio
async def test_stop_limit_maps_to_sl_with_both_prices() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/orders" and request.method == "GET":
            return _ok([])
        if request.url.path == "/orders/regular":
            seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return _ok({"order_id": "1"})
        return _ok([_order_row(order_id="1")])

    await _broker(handler).place_order(
        OrderRequest(
            symbol="INFY",
            side=Side.SELL,
            qty=5,
            order_type=OrderType.STOP_LIMIT,
            limit_price=1490.0,
            stop_price=1500.0,
        )
    )
    assert seen["order_type"] == "SL"
    assert seen["price"] == "1490.0"
    assert seen["trigger_price"] == "1500.0"


@pytest.mark.asyncio
async def test_gtc_is_rejected_loudly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok([])

    with pytest.raises(ValueError, match="DAY/IOC"):
        await _broker(handler).place_order(
            OrderRequest(
                symbol="INFY", side=Side.BUY, qty=1, time_in_force=TimeInForce.GTC,
            )
        )


@pytest.mark.asyncio
async def test_retry_with_same_client_order_id_returns_existing_order() -> None:
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "GET" and request.url.path == "/orders":
            return _ok([_order_row(tag="agentexecprop1", status="OPEN")])
        if request.method == "POST":
            posts += 1
            return _ok({"order_id": "999"})
        return _ok([_order_row()])

    order = await _broker(handler).place_order(
        OrderRequest(symbol="NSE:RELIANCE", side=Side.BUY, qty=10,
                     client_order_id="agent-exec-prop1")
    )
    assert posts == 0, "must NOT re-submit when a live order carries the tag"
    assert order.broker_order_id == "240610000001"


@pytest.mark.asyncio
async def test_rejected_order_with_same_tag_does_not_block_resubmit() -> None:
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "GET" and request.url.path == "/orders":
            return _ok([_order_row(tag="agentexecprop1", status="REJECTED")])
        if request.method == "POST":
            posts += 1
            return _ok({"order_id": "1000"})
        return _ok([_order_row(order_id="1000", tag="agentexecprop1")])

    await _broker(handler).place_order(
        OrderRequest(symbol="NSE:RELIANCE", side=Side.BUY, qty=10,
                     client_order_id="agent-exec-prop1")
    )
    assert posts == 1, "a dead order must not satisfy the idempotency check"


@pytest.mark.asyncio
async def test_status_mapping_complete_and_partial() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok([
            _order_row(status="COMPLETE", filled_quantity=10, average_price=2900.5),
        ])

    order = await _broker(handler).get_order("240610000001")
    assert order.status is OrderStatus.FILLED
    assert order.filled_qty == 10
    assert order.avg_fill_price == 2900.5
    assert order.filled_at is not None or order.submitted_at is not None

    def handler_partial(request: httpx.Request) -> httpx.Response:
        return _ok([_order_row(status="OPEN", filled_quantity=4)])

    order = await _broker(handler_partial).get_order("240610000001")
    assert order.status is OrderStatus.PARTIALLY_FILLED


@pytest.mark.asyncio
async def test_kite_error_envelope_raises_zerodha_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "status": "error",
                "message": "Incorrect `api_key` or `access_token`.",
                "error_type": "TokenException",
            },
        )

    with pytest.raises(ZerodhaError, match="TokenException"):
        await _broker(handler).get_order("1")


@pytest.mark.asyncio
async def test_cancel_order_deletes_then_refetches() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "DELETE":
            return _ok({"order_id": "240610000001"})
        return _ok([_order_row(status="CANCELLED")])

    order = await _broker(handler).cancel_order("240610000001")
    assert calls[0] == "DELETE /orders/regular/240610000001"
    assert order.status is OrderStatus.CANCELED


# ── Positions + account ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_positions_merges_holdings_and_net() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/portfolio/holdings":
            return _ok([
                {"exchange": "NSE", "tradingsymbol": "INFY", "quantity": 10,
                 "average_price": 1400.0, "last_price": 1500.0, "pnl": 1000.0},
            ])
        if request.url.path == "/portfolio/positions":
            return _ok({"net": [
                # Same symbol bought again today → must merge with holding.
                {"exchange": "NSE", "tradingsymbol": "INFY", "quantity": 5,
                 "average_price": 1480.0, "last_price": 1500.0, "pnl": 100.0},
                # Derivative day position → separate row.
                {"exchange": "NFO", "tradingsymbol": "NIFTY24DECFUT", "quantity": 50,
                 "average_price": 24000.0, "last_price": 24100.0, "pnl": 5000.0},
                # Flat position → dropped.
                {"exchange": "NSE", "tradingsymbol": "TCS", "quantity": 0,
                 "average_price": 0, "last_price": 4000.0, "pnl": 0},
            ]})
        raise AssertionError(request.url.path)

    positions = {p.symbol: p for p in await _broker(handler).list_positions()}
    assert set(positions) == {"NSE:INFY", "NFO:NIFTY24DECFUT"}
    infy = positions["NSE:INFY"]
    assert infy.qty == 15
    assert infy.unrealized_pl == pytest.approx(1100.0)
    # Blended cost basis: (1400*10 + 1480*5) / 15
    assert infy.avg_entry_price == pytest.approx((1400 * 10 + 1480 * 5) / 15)


@pytest.mark.asyncio
async def test_get_position_normalizes_bare_symbol() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/portfolio/holdings":
            return _ok([
                {"exchange": "NSE", "tradingsymbol": "INFY", "quantity": 10,
                 "average_price": 1400.0, "last_price": 1500.0, "pnl": 1000.0},
            ])
        return _ok({"net": []})

    broker = _broker(handler)
    assert (await broker.get_position("infy")) is not None
    assert (await broker.get_position("NSE:INFY")) is not None
    assert (await broker.get_position("TCS")) is None


@pytest.mark.asyncio
async def test_equity_and_buying_power_from_margins() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user/margins":
            return _ok({
                "equity": {
                    "net": 50_000.0,
                    "available": {"cash": 48_000.0, "live_balance": 47_500.0},
                },
            })
        if request.url.path == "/portfolio/holdings":
            return _ok([
                {"exchange": "NSE", "tradingsymbol": "INFY", "quantity": 10,
                 "average_price": 1400.0, "last_price": 1500.0, "pnl": 1000.0},
            ])
        return _ok({"net": []})

    broker = _broker(handler)
    assert await broker.get_account_equity() == pytest.approx(50_000 + 15_000)
    assert await broker.get_buying_power() == pytest.approx(47_500.0)


# ── Session-token exchange ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_exchange_request_token_sends_checksum() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/session/token"
        seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return _ok({"access_token": "daily-token", "user_id": "AB1234",
                    "user_name": "Test User", "login_time": "2026-06-10 09:00:00"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.kite.trade"
    ) as client:
        data = await exchange_request_token(
            api_key="key", api_secret="secret", request_token="REQ",
            base_url="https://api.kite.trade", client=client,
        )
    assert seen["checksum"] == session_checksum("key", "REQ", "secret")
    assert data["access_token"] == "daily-token"
    assert data["user_id"] == "AB1234"


@pytest.mark.asyncio
async def test_exchange_request_token_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"status": "error", "message": "Token is invalid or has expired."},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.kite.trade"
    ) as client:
        with pytest.raises(ZerodhaError, match="invalid or has expired"):
            await exchange_request_token(
                api_key="key", api_secret="secret", request_token="STALE",
                base_url="https://api.kite.trade", client=client,
            )
