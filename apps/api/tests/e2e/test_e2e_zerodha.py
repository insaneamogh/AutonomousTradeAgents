"""Scenarios: Zerodha (Kite) alongside Alpaca. docs/PLAN_ZERODHA.md Z1.

The symbol's exchange picks the broker (NSE:/NFO: -> Zerodha), each
broker gets its own fleet pass and snapshot, and neither pass touches the
other's positions. SimBroker(kite=True) refuses what ZerodhaBroker refuses.
"""

from __future__ import annotations

import uuid
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


async def test_the_councils_sizing_equity_is_the_symbols_own_broker_account(
    monkeypatch: pytest.MonkeyPatch, live_india: None, outbox: list[dict],
) -> None:
    """A two-broker user has a USD and an INR account. The cron's equity
    resolver must hand the Drafter the INR figure for an NSE symbol."""
    from trading_agents.jobs.daily_cron import _equity_resolver

    await _both_accounts(monkeypatch)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)  # one snapshot per broker
    resolve = _equity_resolver("00000000-0000-0000-0000-000000000001")
    assert await resolve(source="alpaca") == 100_000.0
    assert await resolve(source="zerodha") == 1_000_000.0


# ── NSE index options (NFO) ──────────────────────────────────────────


def _nifty_call(expiry, *, lots: int = 1, lot_size: int = 65, premium: float = 120.0,
                broker_lot: int | None = None):
    from datetime import timedelta

    from app.schemas.approvals import ApprovalProposalDto

    now = datetime.now(UTC)
    contract = f"NFO:NIFTY{expiry:%y%b}25000CE".upper()
    return ApprovalProposalDto(
        id=f"agent-nfo-{contract[-12:].lower()}", symbol="NSE:NIFTY 50", side="BUY",
        direction="long", is_option=True, option_action="buy_to_open", occ_symbol=contract,
        strike=25000.0, expiry_date=expiry, contract_type="call",
        # Kite counts option quantity in UNITS (lots x lot size): multiplier 1.
        multiplier=1, lot_size=broker_lot, open_interest=500_000, volume=200_000,
        bid=round(premium - 0.5, 2), ask=premium, implied_volatility=0.14,
        qty=lots * lot_size, order_type="LIMIT", limit_price=premium,
        estimated_notional=lots * lot_size * premium, time_stop_days=5,
        rationale="e2e", bull_case="e2e bull", bear_case="e2e bear",
        risk_level=2, conviction_level=3, council_confidence=0.6,
        proposed_at=now, expires_at=now + timedelta(hours=6),
    ), contract


async def test_a_nifty_call_goes_to_zerodha_in_lots_and_its_premium_stop_closes_it(
    monkeypatch: pytest.MonkeyPatch, live_india: None, outbox: list[dict],
) -> None:
    us, india = await _both_accounts(monkeypatch)
    expiry = (datetime.now(UTC) + timedelta(days=25)).date()
    proposal, contract = _nifty_call(expiry)
    india.set_price(contract, 118.0)

    pid = await _approve(proposal, exit_mode="agent")
    assert us.orders == {}
    placed = [r for r in india.requests.values() if r.symbol == contract]
    assert [(r.side.value, r.qty, r.order_type.value) for r in placed] == [
        ("BUY_TO_OPEN", 65, "LIMIT")]  # ZerodhaBroker maps BUY_TO_OPEN to Kite's BUY

    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    d = await decision_row(pid)
    assert d.fill_qty == 65 and d.fill_avg_price == Decimal("120.0000")
    assert not [r for r in india.requests.values() if r.stop_price is not None], (
        "no resting stop at Kite (DAY/IOC only); the software stop covers NFO")

    india.set_price(contract, 60.0)  # -50%: at the premium stop (options_stop_loss_pct)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    d = await decision_row(pid)
    assert d.close_reason in ("option_stop_loss", "option_trail_stop")
    assert d.realized_pnl == Decimal("-3900.00")  # (60 - 120) x 65 units, in INR


async def _refused(proposal) -> dict:
    from app.services.council.store import get_store

    await get_store().append_pending(proposal)
    async with api_client() as api:
        pending = (await api.get("/api/v1/approvals/pending")).json()
        pid = next(p["id"] for p in pending if p["symbol"] == proposal.symbol)
        r = await api.post(f"/api/v1/approvals/{pid}/decision",
                           json={"outcome": "approved", "exitMode": "agent"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["executed"] is False and body["riskBlocked"] is True, body
    return body


async def test_an_account_without_fo_is_refused_the_nifty_call_by_name(
    monkeypatch: pytest.MonkeyPatch, live_india: None,
) -> None:
    """The real adapter reports F&O as the options level. Before, it
    returned None for every account and the rule refused them all."""
    _us, india = await _both_accounts(monkeypatch)
    india.fo_enabled = False
    proposal, contract = _nifty_call((datetime.now(UTC) + timedelta(days=25)).date())
    india.set_price(contract, 118.0)

    body = await _refused(proposal)
    assert body["riskVetoRule"] == "options_level_insufficient"
    assert india.requests == {}


async def test_an_off_lot_nifty_quantity_is_refused_before_it_reaches_kite(
    monkeypatch: pytest.MonkeyPatch, live_india: None,
) -> None:
    _us, india = await _both_accounts(monkeypatch)
    proposal, contract = _nifty_call((datetime.now(UTC) + timedelta(days=25)).date(),
                                     lot_size=50)  # 50 units: NIFTY trades in 65s
    india.set_price(contract, 118.0)

    body = await _refused(proposal)
    assert body["riskVetoRule"] == "lot_size_block"
    assert india.requests == {}


# ── Each book's drawdown is measured in its own currency ─────────────


async def test_a_small_inr_book_is_not_measured_against_the_usd_book(
    monkeypatch: pytest.MonkeyPatch, live_india: None,
) -> None:
    """Kite reports no prior-close equity, so the Zerodha snapshot's day
    P&L falls back to the day's first snapshot. That must be Zerodha's
    own: the Alpaca pass runs first every tick, and INR 50,000 against a
    USD 100,000 baseline read as -50% and latched the breaker."""
    from sqlalchemy import select

    from engine.db.models import CircuitBreakerState, PositionsSnapshot
    from engine.db.session import async_session_factory

    _us, india = await _both_accounts(monkeypatch)
    india.cash = 50_000.0
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    india.cash = 49_000.0  # a real -2% INR day
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    async with async_session_factory()() as s:
        snaps = (await s.execute(
            select(PositionsSnapshot).order_by(PositionsSnapshot.captured_at.desc())
        )).scalars().all()
        breaker = (await s.execute(select(CircuitBreakerState))).scalars().all()
    latest = {}
    for snap in snaps:
        latest.setdefault(snap.source, snap)
    assert float(latest["zerodha"].daily_pnl_pct) == pytest.approx(-2.0)
    assert float(latest["alpaca"].daily_pnl_pct) == pytest.approx(0.0)
    assert all(b.status != "halted" for b in breaker), [b.halt_reason for b in breaker]


async def test_the_council_risks_each_proposal_against_its_own_brokers_book(
    monkeypatch: pytest.MonkeyPatch, live_india: None,
) -> None:
    """The draft-time risk officer read the newest snapshot of any broker,
    and the Zerodha pass writes last each tick: a US proposal was judged
    against INR 1,000,000 of equity and the INR positions."""
    from unittest.mock import patch

    from e2e_harness import FIXTURE_USER

    from engine.db.session import async_session_factory
    from engine.risk.postgres_context import PostgresRiskContextProvider
    from engine.risk.types import RiskDecision
    from trading_agents.nodes.risk_officer import risk_officer_node

    us, india = await _both_accounts(monkeypatch)
    us.set_price("AAPL", 200.0)
    us.held["AAPL"] = _Held(2, 190.0)
    india.set_price("NSE:TCS", 4000.0)
    india.held["NSE:TCS"] = _Held(3, 3900.0)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    seen: dict[str, object] = {}

    def _spy(proposal, context, caps, *, specialists):
        seen[proposal.symbol] = context
        return RiskDecision(approved=True, reason="ok", checks_passed=())

    provider = PostgresRiskContextProvider(async_session_factory())
    with patch("trading_agents.nodes.risk_officer.evaluate", _spy):
        for symbol in ("AAPL", "NSE:INFY"):
            await risk_officer_node({
                "symbol": symbol, "user_id": FIXTURE_USER,
                "context": {"last_price": 100.0, "asset": {}},
                "proposal": {"side": "BUY", "qty": 1, "estimated_notional": 100.0,
                             "confidence": 0.7},
            }, context_provider=provider)

    us_ctx, in_ctx = seen["AAPL"], seen["NSE:INFY"]
    assert us_ctx.account_equity == pytest.approx(100_400.0)  # cash + 2 x 200
    assert [p.symbol for p in us_ctx.open_positions] == ["AAPL"]
    assert in_ctx.account_equity == pytest.approx(1_012_000.0)  # cash + 3 x 4000
    assert [p.symbol for p in in_ctx.open_positions] == ["NSE:TCS"]


async def test_a_revised_lot_size_is_taken_from_the_proposal_not_the_stale_table(
    monkeypatch: pytest.MonkeyPatch, live_india: None,
) -> None:
    """NSE revises lot sizes by circular. The drafter records the lot size
    from that day's instruments dump; the executor's re-check must use it,
    or the day NIFTY moves to 75 every correct order is refused as off-lot
    against the fallback table's 65."""
    _us, india = await _both_accounts(monkeypatch)
    proposal, contract = _nifty_call((datetime.now(UTC) + timedelta(days=25)).date(),
                                     lot_size=75, broker_lot=75)
    india.set_price(contract, 118.0)

    await _approve(proposal, exit_mode="agent")
    assert [r.qty for r in india.requests.values()] == [75]


# ── NSE expiry: Kite has no lifecycle feed; the contract just vanishes ──


def _just_expired():
    """An expiry whose settlement Kite's quote still shows right now: today
    after the 15:30 IST close, else the last NSE session before today."""
    from datetime import time
    from zoneinfo import ZoneInfo

    from engine.features.market_calendar import is_in_trading_day

    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    if is_in_trading_day(now.date()) and now.time() >= time(15, 30):
        return now.date()
    d = now.date() - timedelta(days=1)
    while not is_in_trading_day(d):
        d -= timedelta(days=1)
    return d


async def _held_to_expiry(monkeypatch, proposal, contract, *, entry: float):
    """Approve a manual-mode NSE option (no expiry sweep), fill it, then let
    its expiry pass."""
    from engine.db.models import AgentDecision
    from engine.db.session import async_session_factory

    _us, india = await _both_accounts(monkeypatch)
    india.set_price(contract, entry)
    pid = await _approve(proposal, exit_mode="manual")
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    d = await decision_row(pid)
    assert d.fill_qty, "the entry must fill before it can expire"
    async with async_session_factory()() as s:
        row = await s.get(AgentDecision, d.id)
        row.proposal = {**row.proposal, "expiryDate": _just_expired().isoformat()}
        await s.commit()
    return india, pid


async def test_an_in_the_money_nifty_call_is_settled_in_cash_not_sold_at_the_broker(
    monkeypatch: pytest.MonkeyPatch, live_india: None, outbox: list[dict],
) -> None:
    proposal, contract = _nifty_call((datetime.now(UTC) + timedelta(days=25)).date())
    india, pid = await _held_to_expiry(monkeypatch, proposal, contract, entry=118.0)

    india.kite_settle(contract, underlying="NSE:NIFTY 50", strike=25_000.0, kind="call",
                      close=25_300.0)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    d = await decision_row(pid)
    assert d.close_reason == "option_settled"
    assert d.realized_pnl == Decimal("11700.00")  # (300 intrinsic - 120) x 65
    assert not any("closed" in p.get("body", "").lower() and "broker" in p.get("body", "").lower()
                   for p in outbox), "no 'you closed it at the broker' push"


async def test_an_out_of_the_money_nifty_call_expires_worthless(
    monkeypatch: pytest.MonkeyPatch, live_india: None, outbox: list[dict],
) -> None:
    proposal, contract = _nifty_call((datetime.now(UTC) + timedelta(days=25)).date())
    india, pid = await _held_to_expiry(monkeypatch, proposal, contract, entry=118.0)

    india.kite_settle(contract, underlying="NSE:NIFTY 50", strike=25_000.0, kind="call",
                      close=24_800.0)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    d = await decision_row(pid)
    assert d.close_reason == "option_expired"
    assert d.realized_pnl == Decimal("-7800.00")  # the whole 120 x 65 premium
    assert any(p["title"] == "Option expired" for p in outbox)


async def test_an_in_the_money_stock_call_delivers_shares_and_pages(
    monkeypatch: pytest.MonkeyPatch, live_india: None, outbox: list[dict],
) -> None:
    """NSE stock options settle by delivery: 250 RELIANCE shares land in
    holdings with no stop. That is a page, not a quiet close."""
    from app.schemas.approvals import ApprovalProposalDto

    now = datetime.now(UTC)
    contract = "NFO:RELIANCE26OCT3000CE"
    proposal = ApprovalProposalDto(
        id="agent-nfo-reliance-call", symbol="NSE:RELIANCE", side="BUY", direction="long",
        is_option=True, option_action="buy_to_open", occ_symbol=contract, strike=3000.0,
        expiry_date=(now + timedelta(days=25)).date(), contract_type="call", multiplier=1,
        lot_size=250, open_interest=50_000, volume=20_000, bid=29.5, ask=30.0,
        implied_volatility=0.25, qty=250, order_type="LIMIT", limit_price=30.0,
        estimated_notional=7_500.0, time_stop_days=5, rationale="e2e", bull_case="e2e bull",
        bear_case="e2e bear", risk_level=2, conviction_level=3, council_confidence=0.6,
        proposed_at=now, expires_at=now + timedelta(hours=6),
    )
    india, pid = await _held_to_expiry(monkeypatch, proposal, contract, entry=29.0)

    india.kite_settle(contract, underlying="NSE:RELIANCE", strike=3000.0, kind="call",
                      close=3060.0)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    d = await decision_row(pid)
    assert d.close_reason == "option_exercised"
    assert d.realized_pnl == Decimal("7500.00")  # (60 intrinsic - 30) x 250
    assert india.held["NSE:RELIANCE"].qty == 250
    pages = [p for p in outbox if p.get("data_kind") == "ops_alert"]
    assert any("250 NSE:RELIANCE shares bought" in p["body"] for p in pages), pages


async def test_an_nfo_contract_sold_in_kite_before_expiry_is_still_an_external_close(
    monkeypatch: pytest.MonkeyPatch, live_india: None, outbox: list[dict],
) -> None:
    proposal, contract = _nifty_call((datetime.now(UTC) + timedelta(days=25)).date())
    _us, india = await _both_accounts(monkeypatch)
    india.set_price(contract, 118.0)
    pid = await _approve(proposal, exit_mode="manual")
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    india.set_price(contract, 140.0)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)  # snapshot carries the mark
    india.held.pop(contract)  # sold by hand in Kite, weeks before expiry
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    d = await decision_row(pid)
    assert d.close_reason == "external_broker"
    assert d.realized_pnl == Decimal("1300.00")  # (140 - 120) x 65, from the last mark


async def test_an_order_the_account_cannot_fund_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, live_india: None,
) -> None:
    """Rs 1,000,000 of equity, but nearly all of it in TCS shares: the
    premium caps pass and Kite would still reject the order for want of
    cash. The executor refuses it first, as insufficient_margin."""
    _us, india = await _both_accounts(monkeypatch)
    india.cash = 5_000.0
    india.set_price("NSE:TCS", 4000.0)
    india.held["NSE:TCS"] = _Held(250, 4000.0)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    proposal, contract = _nifty_call((datetime.now(UTC) + timedelta(days=25)).date())
    india.set_price(contract, 118.0)

    body = await _refused(proposal)
    assert body["riskVetoRule"] == "insufficient_margin", body
    assert "7,825.00" in body["riskReason"] and "5,000.00" in body["riskReason"]
    assert india.requests == {}


# ── One daily report per market, in its own currency ─────────────────


async def _no_ghosts(day, **_kw):
    return {"created": 0, "updated": 0, "finalized": 0}


async def test_each_market_gets_its_own_daily_report_in_its_own_currency(
    monkeypatch: pytest.MonkeyPatch, live_india: None, outbox: list[dict],
) -> None:
    """The report read the newest snapshot of either broker and summed
    realized P&L across both: an INR book printed in dollars, and dollars
    added to rupees."""
    from e2e_harness import FIXTURE_USER

    from app.services.council import eod_report
    from engine.db.session import async_session_factory

    us, india = await _both_accounts(monkeypatch)
    expiry = (datetime.now(UTC) + timedelta(days=30)).date()
    occ = occ_for("NVDA", expiry, "call", 250.0)
    us.set_price(occ, 2.50)
    await _approve(option_proposal(occ=occ, underlying="NVDA", strike=250.0, expiry=expiry),
                   exit_mode="manual")
    proposal, contract = _nifty_call((datetime.now(UTC) + timedelta(days=25)).date())
    india.set_price(contract, 118.0)
    await _approve(proposal, exit_mode="agent")
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    us.expire(occ)                    # US: -255 USD
    india.set_price(contract, 60.0)   # NSE: premium stop, -3,900 INR
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    monkeypatch.setattr(eod_report, "_mark_ghosts", _no_ghosts)
    kw = {"user_id": FIXTURE_USER, "session_factory": async_session_factory()}
    us_r = await eod_report.run_eod(**kw, market="US")
    in_r = await eod_report.run_eod(**kw, market="IN", skip_if_empty=True)

    assert us_r.closes_by_reason == {"option_expired": 1} and us_r.realized_today == -255.0
    assert us_r.equity == pytest.approx(99_745.0)
    assert sum(in_r.closes_by_reason.values()) == 1 and in_r.realized_today == -3900.0
    assert in_r.equity == pytest.approx(996_100.0)
    _title, body = eod_report.render_report(in_r)
    assert "Equity ₹996,100.00" in body and "realized -₹3,900.00" in body
    titles = [p["title"] for p in outbox if p.get("data_kind") == "daily_report"]
    assert titles == ["Trading day closed", "NSE trading day closed"]


async def test_no_nse_report_for_a_user_without_indian_activity(
    monkeypatch: pytest.MonkeyPatch, outbox: list[dict],
) -> None:
    from e2e_harness import FIXTURE_USER

    from app.services.council import eod_report
    from engine.db.session import async_session_factory

    patch_brokers(monkeypatch, {"alpaca": (SimBroker(), await seed_account(broker="alpaca"))})
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    r = await eod_report.run_eod(user_id=FIXTURE_USER, session_factory=async_session_factory(),
                                 market="IN", skip_if_empty=True)
    assert r is not None and r.delivered is False
    assert not [p for p in outbox if p.get("data_kind") == "daily_report"]


# ── Kite's daily token flush ─────────────────────────────────────────


async def _set_kite_expiry(delta: timedelta) -> None:
    from sqlalchemy import update

    from engine.db.models import BrokerConnection
    from engine.db.session import async_session_factory

    async with async_session_factory()() as s:
        await s.execute(update(BrokerConnection).where(BrokerConnection.broker == "zerodha")
                        .values(access_token_expires_at=datetime.now(UTC) + delta))
        await s.commit()


async def test_an_expired_kite_session_skips_the_pass_and_pages_once_while_positions_are_open(
    monkeypatch: pytest.MonkeyPatch, live_india: None, outbox: list[dict],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Before: every call in the Zerodha pass raised on the dead token,
    about six tracebacks every 30 s all day, and nobody was told that the
    open NSE option could no longer be stopped out."""
    import logging

    proposal, contract = _nifty_call((datetime.now(UTC) + timedelta(days=25)).date())
    _us, india = await _both_accounts(monkeypatch)
    india.set_price(contract, 118.0)
    pid = await _approve(proposal, exit_mode="agent")
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    assert (await decision_row(pid)).fill_qty == 65

    await _set_kite_expiry(-timedelta(minutes=1))  # 06:00 IST: Kite flushed the token
    india.set_price(contract, 60.0)                # through the premium stop
    caplog.set_level(logging.WARNING, logger="api.reconciler_fleet")
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    assert not [r for r in india.requests.values() if r.side.value == "SELL_TO_CLOSE"]
    pages = [p for p in outbox if p.get("data_kind") == "ops_alert"
             and "session expired" in p["title"]]
    assert len(pages) == 1, pages
    assert "1 open IN position" in pages[0]["body"]
    assert not [r for r in caplog.records if r.exc_info], "no traceback per tick"

    await _set_kite_expiry(timedelta(hours=12))   # the user logged in again
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    assert (await decision_row(pid)).close_reason in ("option_stop_loss", "option_trail_stop")


# ── The Refusal Ledger for NSE refusals ──────────────────────────────


async def _refusal(symbol: str, limit: float, *, days_ago: int = 8):
    from e2e_harness import FIXTURE_USER

    from engine.db.models import AgentDecision
    from engine.db.session import async_session_factory

    row = AgentDecision(
        user_id=uuid.UUID(FIXTURE_USER), symbol=symbol, horizon="short", final_action="VETOED",
        risk_approved=False, risk_veto_rule="min_council_confidence",
        proposal={"side": "BUY", "qty": 10, "limitPrice": limit, "symbol": symbol},
        triggered_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    async with async_session_factory()() as s:
        s.add(row)
        await s.commit()
    return row.id


async def test_nse_refusals_are_priced_from_kite_and_kept_out_of_the_dollar_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alpaca raises on an NSE symbol, and that one raise used to abort the
    whole marking pass. Without a Kite session an NSE refusal is skipped by
    name (no invented synthetic rupees); with one it is priced from Kite.
    Either way the dollar ledger does not add rupees to dollars."""
    import contextlib

    from e2e_harness import FIXTURE_USER
    from sqlalchemy import select

    from app.services.council.ghost_service import build_ghost_summary
    from engine.db.models import GhostOutcome
    from engine.db.session import async_session_factory
    from engine.prices.base import DailyClose
    from trading_agents.jobs import ghost_eval

    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    await seed_account(broker="alpaca")
    msft = await _refusal("MSFT", 400.0)
    aapl = await _refusal("AAPL", 200.0)
    reliance = await _refusal("NSE:RELIANCE", 2900.0)

    class _UsCloses:
        name = "fake_us"

        async def daily_closes(self, symbol, start, end):
            if symbol == "AAPL":
                raise RuntimeError("alpaca: 422")
            return [DailyClose(day=start + timedelta(days=i), close=390.0) for i in range(1, 8)]

    monkeypatch.setattr(ghost_eval, "get_price_provider", lambda **_k: _UsCloses())

    first = await ghost_eval.evaluate_ghosts()
    assert first["skip_reasons"] == {"price_fetch_failed": 1, "no_price_source": 1}

    class _Kite:
        async def instruments(self, exchange):
            return [{"tradingsymbol": "RELIANCE", "instrument_token": 738561, "lot_size": 1}]

        async def historical_daily(self, token, *, start, end):
            base = datetime.now(UTC) - timedelta(days=9)
            return [(base + timedelta(days=i), 0, 0, 0, 3000.0, 1e6) for i in range(10)]

    @contextlib.asynccontextmanager
    async def _open():
        yield _Kite()

    await ghost_eval.evaluate_ghosts(market="IN", kite_client_factory=_open)

    async with async_session_factory()() as s:
        ghosts = {g.decision_id: g for g in (await s.execute(select(GhostOutcome))).scalars()}
    assert ghosts[aapl].status == "pending" and ghosts[aapl].ghost_pnl is None, (
        "a failed fetch leaves the row for the next pass; the pass went on")
    assert ghosts[msft].price_source == "fake_us" and float(ghosts[msft].ghost_pnl) == -100.0
    assert ghosts[reliance].price_source == "kite"
    assert float(ghosts[reliance].ghost_pnl) == 1000.0  # (3000 - 2900) x 10, in rupees

    summary = await build_ghost_summary(user_id=FIXTURE_USER)
    # MSFT and the still-pending AAPL; the RELIANCE rupees are not summed in.
    assert (summary.vetoed.count, summary.vetoed.marked_pnl) == (2, -100.0)


# ── India paper trading: the persisted book against Kite's real quotes ──


class _KiteQuotes:
    """The only Kite calls a paper book makes: market data. A one-rupee
    spread around each last price."""

    def __init__(self) -> None:
        self.last: dict[str, float] = {}
        self.orders_sent = 0

    async def quotes(self, symbols):
        return {
            s.upper(): {"last_price": self.last[s],
                        "depth": {"buy": [{"price": self.last[s] - 0.5}],
                                  "sell": [{"price": self.last[s] + 0.5}]}}
            for s in symbols if s in self.last
        }

    async def order_margin(self, request):
        return request.qty * (request.limit_price or self.last[request.symbol] + 0.5)

    async def get_options_trading_level(self):
        return 2

    async def place_order(self, request):  # pragma: no cover - must never run
        self.orders_sent += 1
        raise AssertionError("a paper book must never send an order to Kite")


@pytest.fixture
def paper_india(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paper needs no live-trading keys: nothing reaches Kite."""
    monkeypatch.setenv("ALLOW_OPTIONS", "1")
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("TRADING_MODE", raising=False)


def _in_session() -> datetime:
    """11:00 IST today: inside the NSE session whenever the test runs."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(hour=11, minute=0)


async def _paper_account(monkeypatch: pytest.MonkeyPatch, **kw):
    from dataclasses import replace

    from e2e_harness import FIXTURE_USER

    from app.services.orders.kite_paper import KitePaperBroker
    from engine.db.session import async_session_factory

    md = _KiteQuotes()
    conn = await seed_account(broker="zerodha")
    kw.setdefault("clock", _in_session)
    paper = KitePaperBroker(market_data=md, user_id=FIXTURE_USER,
                            session_factory=async_session_factory(),
                            market_open=lambda _now: True, **kw)
    patch_brokers(monkeypatch, {"zerodha": (paper, replace(conn, is_paper=True))})
    return md, paper


async def test_a_paper_nifty_call_fills_on_kites_book_exits_on_its_stop_and_survives_a_restart(
    monkeypatch: pytest.MonkeyPatch, paper_india: None, outbox: list[dict],
) -> None:
    from e2e_harness import FIXTURE_USER

    from app.services.orders.kite_paper import KitePaperBroker
    from engine.db.session import async_session_factory

    md, paper = await _paper_account(monkeypatch)
    proposal, contract = _nifty_call((datetime.now(UTC) + timedelta(days=25)).date())
    md.last[contract] = 118.0                       # ask 118.5, under the 120 limit
    pid = await _approve(proposal, exit_mode="agent")
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    d = await decision_row(pid)
    assert (d.fill_qty, d.fill_avg_price) == (65, Decimal("118.5000")), "filled at the ask"
    assert [p.symbol for p in await paper.list_positions()] == [contract]

    md.last[contract] = 55.0                        # -54%: through the premium stop
    for _ in range(3):
        await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    d = await decision_row(pid)
    assert d.close_reason in ("option_stop_loss", "option_trail_stop")
    # The close is a LIMIT at the 55 mark, and the bid is 54.50: it rests,
    # as it would on Kite's book, until the bid reaches it.
    assert d.realized_pnl is None
    md.last[contract] = 56.0                        # bid 55.50: the resting sell fills
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    d = await decision_row(pid)
    assert d.realized_pnl == Decimal("-4095.00")    # (55.5 bid - 118.5) x 65
    assert md.orders_sent == 0

    restarted = KitePaperBroker(market_data=md, user_id=FIXTURE_USER,
                                session_factory=async_session_factory(),
                                market_open=lambda _now: True, clock=_in_session)
    assert await restarted.list_positions() == []
    cash = await restarted.get_buying_power()
    assert 995_800 < cash < 995_905, "1,000,000 - 4,095 less Indian charges on both fills"


async def test_a_paper_nse_equity_exits_on_its_paper_gtt_stop(
    monkeypatch: pytest.MonkeyPatch, paper_india: None, outbox: list[dict],
) -> None:
    md, paper = await _paper_account(monkeypatch)
    md.last["NSE:RELIANCE"] = 2899.0
    pid = await _approve(
        equity_proposal(symbol="NSE:RELIANCE", qty=5, last=2900.0, stop=2800.0, target=3100.0),
        exit_mode="agent",
    )
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)   # GTT placed after the fill
    assert (await decision_row(pid)).fill_avg_price == Decimal("2899.5000")

    md.last["NSE:RELIANCE"] = 2795.0                # below the 2800 trigger
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)
    d = await decision_row(pid)
    assert d.close_reason == "bracket_stop"
    assert d.realized_pnl == Decimal("-525.00")     # (2794.5 - 2899.5) x 5
    assert await paper.list_positions() == []


async def test_the_paper_book_expires_day_orders_and_refuses_what_kite_would(
    monkeypatch: pytest.MonkeyPatch, paper_india: None,
) -> None:
    from zoneinfo import ZoneInfo

    from broker.types import OrderRequest, OrderStatus, OrderType, Side

    ist = ZoneInfo("Asia/Kolkata")
    now = {"t": datetime.now(ist).replace(hour=11, minute=0)}
    md, paper = await _paper_account(monkeypatch, clock=lambda: now["t"])
    md.last["NSE:INFY"] = 1500.0

    def _req(side, qty, limit=None, kind=OrderType.LIMIT):
        return OrderRequest(symbol="NSE:INFY", side=side, qty=qty, order_type=kind,
                            limit_price=limit, client_order_id=None)

    resting = await paper.place_order(_req(Side.BUY, 10, limit=1400.0))
    assert resting.status is OrderStatus.ACCEPTED
    now["t"] = now["t"].replace(hour=15, minute=31)          # after the NSE close
    assert (await paper.get_order(resting.broker_order_id)).status is OrderStatus.EXPIRED

    now["t"] = now["t"].replace(hour=11) + timedelta(days=1)
    sell = await paper.place_order(_req(Side.SELL, 5, kind=OrderType.MARKET))
    assert sell.status is OrderStatus.REJECTED, "no short sale: nothing is held"
    huge = await paper.place_order(_req(Side.BUY, 1_000, kind=OrderType.MARKET))
    assert huge.status is OrderStatus.REJECTED, "1,500,500 of stock on 1,000,000 of cash"
    assert await paper.get_buying_power() == 1_000_000.0


async def test_the_account_tiles_show_each_brokers_own_book_in_its_currency(
    monkeypatch: pytest.MonkeyPatch, live_india: None,
) -> None:
    """/api/v1/account returned the newest snapshot of either broker, and
    the Zerodha pass writes last: the dollar Equity tile showed the rupee
    book."""
    us, india = await _both_accounts(monkeypatch)
    await fleet_tick(monkeypatch, market_open=US_CLOSED_IN_OPEN)

    async with api_client() as api:
        default = (await api.get("/api/v1/account")).json()
        nse = (await api.get("/api/v1/account", params={"broker": "zerodha"})).json()
        bad = await api.get("/api/v1/account", params={"broker": "nope"})

    assert (default["brokerName"], default["currency"], default["equity"]) == (
        "Alpaca", "USD", 100_000.0)
    assert (nse["brokerName"], nse["currency"], nse["equity"]) == ("Zerodha", "INR", 1_000_000.0)
    assert nse["isPaper"] is False, "a Zerodha account is real money unless ZERODHA_PAPER"
    assert bad.status_code == 422
