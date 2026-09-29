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

    india.set_price(contract, 60.0)  # -50%, through the -40% premium stop
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


async def _no_ghosts(day):
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
