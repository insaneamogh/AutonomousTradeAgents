"""Scenarios: Zerodha (Kite) alongside Alpaca. docs/PLAN_ZERODHA.md Z1.

The symbol's exchange picks the broker (NSE:/NFO: -> Zerodha), each
broker gets its own fleet pass and snapshot, and neither pass touches the
other's positions. SimBroker(kite=True) refuses what ZerodhaBroker refuses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from e2e_harness import (
    SimBroker,
    _Held,
    api_client,
    decision_row,
    equity_proposal,
    fleet_tick,
    occ_for,
    option_proposal,
    patch_brokers,
    seed_account,
)

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("e2e_db")]

US_CLOSED_IN_OPEN = {"US": False, "IN": True}


@pytest.fixture
def live_india(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Zerodha account is always real money: the two-key live gate."""
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    monkeypatch.setenv("ALLOW_OPTIONS", "1")
    monkeypatch.delenv("TRADING_MODE", raising=False)


async def _both_accounts(monkeypatch: pytest.MonkeyPatch) -> tuple[SimBroker, SimBroker]:
    us, india = SimBroker(), SimBroker(kite=True, cash=1_000_000.0)
    patch_brokers(monkeypatch, {
        "alpaca": (us, await seed_account(broker="alpaca")),
        "zerodha": (india, await seed_account(broker="zerodha", live_consent=True)),
    })
    return us, india


async def _approve(proposal, exit_mode: str) -> str:
    from app.services.council.store import get_store

    await get_store().append_pending(proposal)
    async with api_client() as api:
        pending = (await api.get("/api/v1/approvals/pending")).json()
        pid = next(p["id"] for p in pending if p["symbol"] == proposal.symbol)
        r = await api.post(f"/api/v1/approvals/{pid}/decision",
                           json={"outcome": "approved", "exitMode": exit_mode})
        assert r.status_code == 200, r.text
        assert r.json()["executed"] is True, r.json()
    return pid


async def test_an_nse_trade_goes_to_zerodha_and_is_managed_there(
    monkeypatch: pytest.MonkeyPatch, live_india: None, outbox: list[dict],
) -> None:
    us, india = await _both_accounts(monkeypatch)
    india.set_price("NSE:RELIANCE", 2900.0)

    pid = await _approve(
        equity_proposal(symbol="NSE:RELIANCE", qty=5, last=2900.0, stop=2800.0, target=3100.0),
        exit_mode="manual",
    )
    assert us.orders == {}, "an NSE order must never reach Alpaca"
    assert india.held["NSE:RELIANCE"].qty == 5

    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    d = await decision_row(pid)
    assert d.fill_qty == 5 and d.fill_avg_price == Decimal("2900.0000")

    india.set_price("NSE:RELIANCE", 2950.0)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    async with api_client() as api:
        listed = {p["symbol"]: p for p in (await api.get("/api/v1/positions")).json()}
        assert listed["NSE:RELIANCE"]["managed"] is True
        r = await api.post(f"/api/v1/positions/{listed['NSE:RELIANCE']['decisionId']}/close")
        assert r.status_code == 200 and r.json()["closed"], r.text
    assert "NSE:RELIANCE" not in india.held and us.orders == {}

    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    d = await decision_row(pid)
    assert d.close_reason == "user_manual"
    assert d.realized_pnl == Decimal("250.00")  # (2950 - 2900) x 5, in INR


async def test_each_broker_keeps_its_own_book_and_flatten_all_closes_both(
    monkeypatch: pytest.MonkeyPatch, live_india: None, outbox: list[dict],
) -> None:
    us, india = await _both_accounts(monkeypatch)
    expiry = (datetime.now(UTC) + timedelta(days=30)).date()
    occ = occ_for("NVDA", expiry, "call", 250.0)
    us.set_price(occ, 2.50)
    india.set_price("NSE:INFY", 1500.0)
    # Bought by hand in each broker's own app: no decision behind either.
    us.set_price("AAPL", 200.0)
    us.held["AAPL"] = _Held(2, 190.0)
    india.set_price("NSE:TCS", 4000.0)
    india.held["NSE:TCS"] = _Held(3, 3900.0)

    us_pid = await _approve(option_proposal(occ=occ, underlying="NVDA", strike=250.0,
                                            expiry=expiry), exit_mode="agent")
    in_pid = await _approve(
        equity_proposal(symbol="NSE:INFY", qty=10, last=1500.0, stop=1450.0, target=1600.0),
        exit_mode="manual",
    )
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    # Neither pass mistook the other broker's position for a close.
    assert (await decision_row(us_pid)).closed_at is None
    assert (await decision_row(in_pid)).closed_at is None

    async with api_client() as api:
        listed = {(p["symbol"], p["managed"]) for p in (await api.get("/api/v1/positions")).json()}
        assert listed == {("NVDA", True), ("NSE:INFY", True), ("AAPL", False), ("NSE:TCS", False)}
        r = await api.post("/api/v1/positions/flatten-all")
        assert r.status_code == 200, r.text
        assert all(p["closed"] for p in r.json()["positions"]), r.json()

    assert us.held == {} and india.held == {}
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    assert (await decision_row(us_pid)).close_reason == "user_kill_switch"
    assert (await decision_row(in_pid)).close_reason == "user_kill_switch"


# ── Agent-managed NSE equity: the GTT OCO is the broker-side exit ─────


async def _agent_mode_reliance(monkeypatch: pytest.MonkeyPatch) -> tuple[SimBroker, str]:
    _us, india = await _both_accounts(monkeypatch)
    india.set_price("NSE:RELIANCE", 2900.0)
    pid = await _approve(
        equity_proposal(symbol="NSE:RELIANCE", qty=5, last=2900.0, stop=2800.0, target=3100.0),
        exit_mode="agent",
    )
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    return india, pid


async def test_an_agent_mode_nse_entry_gets_a_gtt_and_its_stop_leg_closes_it(
    monkeypatch: pytest.MonkeyPatch, live_india: None, outbox: list[dict],
) -> None:
    india, pid = await _agent_mode_reliance(monkeypatch)
    assert len(india.gtts) == 1, "the fill must place exactly one GTT OCO"
    legs = [india.requests[c] for c in india.legs_of[next(iter(india.gtts))]]
    assert sorted((leg.order_type.value, leg.stop_price or leg.limit_price) for leg in legs) == [
        ("LIMIT", 3100.0), ("STOP", 2800.0)]

    # A second tick must not place another one.
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    assert len(india.gtts) == 1

    india.set_price("NSE:RELIANCE", 2790.0)  # overnight, with nothing of ours running
    await fleet_tick(monkeypatch, market_open={"US": False, "IN": False})
    d = await decision_row(pid)
    assert d.close_reason == "bracket_stop"
    assert d.realized_pnl == Decimal("-550.00")  # (2790 - 2900) x 5


async def test_a_failed_gtt_pages_and_the_software_stop_covers_it_in_session_only(
    monkeypatch: pytest.MonkeyPatch, live_india: None, outbox: list[dict],
) -> None:
    _us, india = await _both_accounts(monkeypatch)
    india.exit_oco_error = RuntimeError("InputException: trigger too close to LTP")
    india.set_price("NSE:RELIANCE", 2900.0)
    pid = await _approve(
        equity_proposal(symbol="NSE:RELIANCE", qty=5, last=2900.0, stop=2800.0, target=3100.0),
        exit_mode="agent",
    )
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    assert not india.gtts
    assert any(p.get("data_kind") == "ops_alert" and "GTT" in p["body"] for p in outbox)

    india.set_price("NSE:RELIANCE", 2795.0)
    await fleet_tick(monkeypatch, market_open={"US": True, "IN": False})  # NSE shut: hold
    assert (await decision_row(pid)).closed_at is None
    assert "NSE:RELIANCE" in india.held

    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)  # NSE open: sell
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    d = await decision_row(pid)
    assert d.close_reason == "agent_stop"
    assert d.realized_pnl == Decimal("-525.00")  # (2795 - 2900) x 5


async def test_a_hand_sale_in_kite_is_detected_and_the_leftover_gtt_cancelled(
    monkeypatch: pytest.MonkeyPatch, live_india: None, outbox: list[dict],
) -> None:
    india, pid = await _agent_mode_reliance(monkeypatch)
    gtt = next(iter(india.gtts))
    india.set_price("NSE:RELIANCE", 2950.0)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)  # snapshot marks 2950

    india.held.pop("NSE:RELIANCE")  # sold in the Kite app; Kite leaves the GTT active
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    d = await decision_row(pid)
    assert d.close_reason == "external_broker"
    assert d.realized_pnl == Decimal("250.00")
    assert (await india.get_order(gtt)).status.value == "canceled", "a GTT left behind could sell later"


async def test_the_software_stop_never_sells_while_a_gtt_is_working(
    monkeypatch: pytest.MonkeyPatch, live_india: None, outbox: list[dict],
) -> None:
    """In the seconds between the price crossing and Kite firing the GTT,
    the software stop must hold: two exits for one position would sell
    twice."""
    india, pid = await _agent_mode_reliance(monkeypatch)
    india.hold_gtts = True
    india.set_price("NSE:RELIANCE", 2795.0)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    assert (await decision_row(pid)).closed_at is None
    assert india.held["NSE:RELIANCE"].qty == 5, "nothing may sell while the GTT works"
