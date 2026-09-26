"""ZerodhaBroker unit tests — all network via httpx.MockTransport.

Covers: symbol convention, order-form mapping (MARKET/LIMIT/SL/SL-M),
product inference (CNC vs NRML), tag-emulated idempotency, status mapping,
positions merge (holdings + net), margins → equity/buying power, and the
session-token checksum exchange.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC
from typing import Any
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


@pytest.fixture(autouse=True)
def _fresh_order_pacing() -> None:
    from broker.zerodha import reset_order_rate_for_tests

    reset_order_rate_for_tests()


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
    from datetime import datetime

    # 2026-06-10 12:00 UTC = 17:30 IST → next flush 2026-06-11 06:00 IST
    now = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    expiry = next_token_expiry(now)
    assert expiry == datetime(2026, 6, 11, 0, 30, tzinfo=UTC)

    # 2026-06-10 23:00 UTC = 04:30 IST next day → flush same IST morning
    now = datetime(2026, 6, 10, 23, 0, tzinfo=UTC)
    assert next_token_expiry(now) == datetime(2026, 6, 11, 0, 30, tzinfo=UTC)


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
    # Kite rejects an API market order without it (SEBI, from 2026-04-01).
    assert seen["market_protection"] == "-1"
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
    assert "market_protection" not in seen  # MARKET and SL-M only, per Kite


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


def test_market_protection_is_validated_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KITE_MARKET_PROTECTION", "3")
    assert ZerodhaBroker("k", "t")._market_protection == "3"
    for bad in ("0", "101", "-2", "auto"):
        monkeypatch.setenv("KITE_MARKET_PROTECTION", bad)
        with pytest.raises(ValueError):
            ZerodhaBroker("k", "t")


# ── GTT: the broker-side exit for an equity entry ───────────────────


@pytest.mark.asyncio
async def test_exit_oco_is_a_two_leg_gtt_with_ascending_triggers() -> None:
    import json

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("POST", "/gtt/triggers")
        seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return _ok({"trigger_id": 123})

    gtt = await _broker(handler).place_exit_oco(
        symbol="NSE:RELIANCE", qty=5, entry_side=Side.BUY, stop=2800.0, target=3100.0,
        last_price=2900.0,
    )
    assert gtt == "gtt:123"
    assert seen["type"] == "two-leg"
    condition, orders = json.loads(seen["condition"]), json.loads(seen["orders"])
    assert condition["trigger_values"] == [2800.0, 3100.0]
    assert condition["last_price"] == 2900.0
    # Stop leg first (lower trigger), limit half a percent through it, on the tick.
    assert [(o["transaction_type"], o["order_type"], o["price"]) for o in orders] == [
        ("SELL", "LIMIT", 2786.0), ("SELL", "LIMIT", 3100.0)]
    assert {o["product"] for o in orders} == {"CNC"} and {o["quantity"] for o in orders} == {5}


@pytest.mark.asyncio
async def test_exit_oco_refuses_a_last_price_outside_its_triggers() -> None:
    with pytest.raises(ZerodhaError):
        await _broker(lambda r: _ok({"trigger_id": 1})).place_exit_oco(
            symbol="NSE:RELIANCE", qty=5, entry_side=Side.BUY, stop=2800.0, target=3100.0,
            last_price=2790.0,
        )


def _gtt(status: str, results: list[Any]) -> dict[str, Any]:
    return {
        "id": 123, "status": status, "type": "two-leg", "created_at": "2026-09-25 10:00:00",
        "condition": {"exchange": "NSE", "tradingsymbol": "RELIANCE",
                      "trigger_values": [2800.0, 3100.0]},
        "orders": [
            {"transaction_type": "SELL", "quantity": 5, "price": 2786.0, "result": results[0]},
            {"transaction_type": "SELL", "quantity": 5, "price": 3100.0, "result": results[1]},
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gtt", "status", "kind", "avg"),
    [
        (_gtt("active", [None, None]), OrderStatus.ACCEPTED, None, None),
        (_gtt("deleted", [None, None]), OrderStatus.CANCELED, None, None),
        (_gtt("triggered", [{"order_result": {"order_id": "9001"}}, None]),
         OrderStatus.FILLED, "stop", 2795.0),
        (_gtt("triggered", [None, {"order_result": {"order_id": "9001"}}]),
         OrderStatus.FILLED, "limit", 2795.0),
        (_gtt("triggered", [{"order_result": {"order_id": "", "rejection_reason": "circuit"}},
                            None]), OrderStatus.REJECTED, "stop", None),
    ],
)
async def test_a_gtt_reads_as_one_exit_order(gtt, status, kind, avg) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gtt/triggers/123":
            return _ok(gtt)
        if request.url.path == "/orders/9001":
            return _ok([_order_row(order_id="9001", transaction_type="SELL", quantity=5,
                                   filled_quantity=5, average_price=2795.0, status="COMPLETE")])
        raise AssertionError(f"unexpected {request.url.path}")

    order = await _broker(handler).get_order("gtt:123")
    assert order.broker_order_id == "gtt:123"
    assert order.status is status
    assert order.raw.get("order_type") == kind
    assert order.avg_fill_price == avg


@pytest.mark.asyncio
async def test_closing_a_symbol_also_deletes_its_active_gtts() -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/gtt/triggers":
            other = dict(_gtt("active", [None, None]), id=124)
            other["condition"] = {"exchange": "NSE", "tradingsymbol": "INFY"}
            return _ok([_gtt("active", [None, None]), other])
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return _ok({"trigger_id": 123})
        if request.url.path == "/orders":
            return _ok([])
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    assert await _broker(handler).cancel_open_orders("NSE:RELIANCE") == 1
    assert deleted == ["/gtt/triggers/123"]


@pytest.mark.asyncio
async def test_an_unregistered_ip_is_its_own_error_not_a_login_problem() -> None:
    from broker.zerodha import ZerodhaIpNotAllowedError

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _ok([])
        return httpx.Response(403, json={"status": "error", "error_type": "PermissionException",
                                         "message": "Orders from this IP are not allowed"})

    with pytest.raises(ZerodhaIpNotAllowedError):
        await _broker(handler).place_order(
            OrderRequest(symbol="NSE:RELIANCE", side=Side.BUY, qty=1, order_type=OrderType.MARKET)
        )


@pytest.mark.asyncio
async def test_order_placement_is_paced_under_kites_per_second_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import broker.zerodha as z

    z.reset_order_rate_for_tests()
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        z._order_times["testkey"].clear()  # the window passes
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    for _ in range(z._ORDER_RATE_PER_SECOND):
        await z._await_order_slot("testkey")
    assert slept == []
    await z._await_order_slot("testkey")  # the 9th inside one second waits
    assert slept and slept[0] <= 1.0
    z.reset_order_rate_for_tests()


@pytest.mark.asyncio
async def test_only_order_mutations_go_through_the_static_ip_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import broker.zerodha as z

    monkeypatch.setenv("KITE_ORDER_PROXY_URL", "http://egress.internal:3128")
    proxies: list[tuple[str, str, object]] = []
    real_client = httpx.AsyncClient

    def spy(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        proxies.append(kwargs.pop("proxy"))
        return real_client(*args, transport=httpx.MockTransport(
            lambda r: _ok({"order_id": "1"} if r.method == "POST" else [_order_row(order_id="1")])
        ), **{k: v for k, v in kwargs.items() if k != "transport"})

    monkeypatch.setattr(z.httpx, "AsyncClient", spy)
    broker = ZerodhaBroker(api_key="k", access_token="t")  # no transport: real egress path
    await broker.place_order(OrderRequest(symbol="NSE:RELIANCE", side=Side.BUY, qty=1,
                                          order_type=OrderType.MARKET))
    # POST /orders/regular via the proxy; the read-back GET /orders/1 direct.
    assert proxies == ["http://egress.internal:3128", None]


# ── Market data ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_instruments_dump_is_parsed_with_lot_and_tick_sizes() -> None:
    csv_text = (
        "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
        "tick_size,lot_size,instrument_type,segment,exchange\n"
        "738561,2885,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.05,1,EQ,NSE,NSE\n"
        "256265,1001,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/instruments/NSE"
        return httpx.Response(200, text=csv_text)

    rows = await _broker(handler).instruments("nse")
    assert [(r["tradingsymbol"], r["instrument_token"], r["lot_size"], r["tick_size"])
            for r in rows] == [("RELIANCE", 738561, 1, 0.05), ("NIFTY 50", 256265, 0, 0.0)]


@pytest.mark.asyncio
async def test_daily_history_parses_kites_iso_candles() -> None:
    from datetime import UTC, datetime

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/instruments/historical/738561/day"
        assert request.url.params["from"] == "2026-09-01 00:00:00"
        return _ok({"candles": [["2026-09-24T00:00:00+0530", 2890.0, 2915.5, 2880.0, 2905.0,
                                 1_234_567]]})

    candles = await _broker(handler).historical_daily(
        738561, start=datetime(2026, 9, 1, tzinfo=UTC), end=datetime(2026, 9, 25, tzinfo=UTC),
    )
    assert len(candles) == 1
    ts, o, h, low, c, v = candles[0]
    assert (ts.date().isoformat(), o, h, low, c, v) == ("2026-09-24", 2890.0, 2915.5, 2880.0,
                                                        2905.0, 1_234_567.0)


@pytest.mark.asyncio
async def test_quotes_are_requested_in_one_call_keyed_by_exchange_symbol() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get_list("i") == ["NSE:RELIANCE", "NSE:INFY"]
        return _ok({"NSE:RELIANCE": {"last_price": 2905.0}})

    q = await _broker(handler).quotes(["NSE:RELIANCE", "infy"])
    assert q == {"NSE:RELIANCE": {"last_price": 2905.0}}


# ── Index options (NFO) ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("side", "kite"), [(Side.BUY_TO_OPEN, "BUY"), (Side.SELL_TO_CLOSE, "SELL")])
async def test_option_sides_reach_kite_as_buy_and_sell(side: Side, kite: str) -> None:
    """Kite's transaction_type is BUY or SELL; the executor sends options as
    BUY_TO_OPEN / SELL_TO_CLOSE, which Kite would reject."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return _ok({"order_id": "1"})
        return _ok([_order_row(order_id="1", exchange="NFO",
                               tradingsymbol="NIFTY26SEP25000CE", transaction_type=kite)])

    order = await _broker(handler).place_order(OrderRequest(
        symbol="NFO:NIFTY26SEP25000CE", side=side, qty=65, order_type=OrderType.LIMIT,
        limit_price=120.0,
    ))
    assert seen["transaction_type"] == kite
    assert seen["product"] == "NRML" and seen["exchange"] == "NFO"
    assert order.symbol == "NFO:NIFTY26SEP25000CE"


def test_an_nfo_option_position_is_an_option_in_units() -> None:
    b = ZerodhaBroker(api_key="k", access_token="t")
    opt = b._position_from_kite({"exchange": "NFO", "tradingsymbol": "NIFTY26SEP25000CE",
                                 "quantity": 65, "average_price": 120.0, "last_price": 90.0})
    fut = b._position_from_kite({"exchange": "NFO", "tradingsymbol": "NIFTY26SEPFUT",
                                 "quantity": 65, "average_price": 25000.0, "last_price": 25100.0})
    eq = b._position_from_kite({"exchange": "NSE", "tradingsymbol": "RELIANCE", "quantity": 5,
                                "average_price": 2900.0, "last_price": 2950.0})
    assert (opt.is_option, opt.multiplier, opt.unrealized_pl_pct) == (True, 1, -25.0)
    assert fut.is_option is False and eq.is_option is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("exchanges", "level"), [
    (["NSE", "BSE", "NFO", "BFO"], 2),
    (["NSE", "BSE"], 0),
    ([], 0),
])
async def test_options_level_is_whether_the_account_has_fo_enabled(
    exchanges: list[str], level: int,
) -> None:
    """options_level_insufficient vetoes None, so returning None (as this
    once did) refused every NSE option entry."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/user/profile"
        return _ok({"user_id": "AB1234", "exchanges": exchanges})

    assert await _broker(handler).get_options_trading_level() == level


@pytest.mark.asyncio
@pytest.mark.parametrize(("side", "limit", "sent"), [
    (Side.BUY_TO_OPEN, 119.76, "119.8"),   # mid + 0.01 from entry_price: up, still marketable
    (Side.SELL_TO_CLOSE, 60.03, "60.0"),   # a sell rounds down
    (Side.BUY, 2900.05, "2900.05"),        # already on the tick: unchanged
])
async def test_limit_prices_are_snapped_onto_the_tick_toward_the_market(
    side: Side, limit: float, sent: str,
) -> None:
    """Kite rejects an off-tick price; the option entry limit is mid + 0.01."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/orders":
            return _ok([])
        if request.method == "POST" and request.url.path == "/orders/regular":
            seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return _ok({"order_id": "240610000001"})
        return _ok([_order_row(tag="agentexecabc123")])

    symbol = "NSE:RELIANCE" if side is Side.BUY else "NFO:NIFTY26OCT25000CE"
    await _broker(handler).place_order(OrderRequest(
        symbol=symbol, side=side, qty=65, order_type=OrderType.LIMIT, limit_price=limit,
        client_order_id="agent-exec-abc123",
    ))
    assert seen["price"] == sent
