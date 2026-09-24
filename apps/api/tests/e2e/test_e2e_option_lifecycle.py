"""Scenarios: a long option from approval to however it ends.

Each runs the production code path against a private Postgres database
and SimBroker (see e2e_harness.py): the approvals HTTP route, the
executor and risk re-check, order persistence, the reconciler fleet tick
(snapshot, order_sync, protective stop, exits), and the positions API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from e2e_harness import (
    FIXTURE_USER,
    SimBroker,
    api_client,
    decision_row,
    fleet_tick,
    occ_for,
    option_proposal,
    order_rows,
    patch_broker,
    seed_account,
)

pytestmark = pytest.mark.usefixtures("e2e_db")


@pytest.fixture
def options_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_OPTIONS", "1")
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("AGENTS_REQUIRE_REAL_LLM", raising=False)


async def _open_call(
    monkeypatch: pytest.MonkeyPatch, sim: SimBroker, *, exit_mode: str = "agent"
) -> tuple[str, str]:
    """Approve one NVDA call over HTTP, let it fill, run one fleet tick.
    Returns (decision_id, occ)."""
    from app.services.council.store import get_store

    expiry = (datetime.now(UTC) + timedelta(days=30)).date()
    occ = occ_for("NVDA", expiry, "call", 250.0)
    sim.set_price("NVDA", 248.0)
    sim.set_price(occ, 2.50)
    patch_broker(monkeypatch, sim, await seed_account())
    await get_store().append_pending(option_proposal(
        occ=occ, underlying="NVDA", strike=250.0, expiry=expiry, limit=2.55,
    ))

    async with api_client() as api:
        pending = (await api.get("/api/v1/approvals/pending")).json()
        assert [p["occSymbol"] for p in pending] == [occ]
        decision_id = pending[0]["id"]
        r = await api.post(f"/api/v1/approvals/{decision_id}/decision",
                           json={"outcome": "approved", "exitMode": exit_mode})
        assert r.status_code == 200, r.text
        assert r.json()["executed"] is True, r.json()

    await fleet_tick(monkeypatch)
    return decision_id, occ


async def test_a_resting_stop_that_fills_closes_the_decision_as_protective_stop(
    monkeypatch: pytest.MonkeyPatch, options_on: None, outbox: list[dict],
) -> None:
    sim = SimBroker()
    decision_id, occ = await _open_call(monkeypatch, sim)

    d = await decision_row(decision_id)
    assert d.fill_qty == 1 and d.fill_avg_price == Decimal("2.5500")
    stops = [o for o in sim.requests.values() if o.stop_price is not None and o.symbol == occ]
    assert len(stops) == 1, "the entry fill must place exactly one resting stop"
    stop = stops[0]
    rows = await order_rows(decision_id)
    assert any(r.client_order_id.startswith("agent-protstop-") and r.broker_order_id
               for r in rows), "the stop's own orders row must carry the broker id"

    # The mark falls through the stop, as it would overnight with the app down.
    sim.set_price(occ, stop.stop_price)
    await fleet_tick(monkeypatch, market_open=False)

    d = await decision_row(decision_id)
    assert d.closed_at is not None
    assert d.close_reason == "protective_stop"
    assert d.realized_pnl == ((Decimal(str(stop.stop_price)) - Decimal("2.55")) * 100).quantize(
        Decimal("0.01"))

    async with api_client() as api:
        closed = (await api.get("/api/v1/positions/history")).json()["positions"]
    assert [(c["occSymbol"], c["closeReason"]) for c in closed] == [(occ, "protective_stop")]


async def test_an_expired_contract_is_closed_at_zero_not_as_an_external_sale(
    monkeypatch: pytest.MonkeyPatch, options_on: None, outbox: list[dict],
) -> None:
    sim = SimBroker()
    decision_id, occ = await _open_call(monkeypatch, sim)

    sim.expire(occ)
    await fleet_tick(monkeypatch, market_open=False)

    d = await decision_row(decision_id)
    assert d.close_reason == "option_expired"
    assert d.realized_pnl == Decimal("-255.00")
    assert any(p["title"] == "Option expired" for p in outbox)


async def test_an_exercised_call_pages_the_operator_about_the_stock(
    monkeypatch: pytest.MonkeyPatch, options_on: None, outbox: list[dict],
) -> None:
    sim = SimBroker()
    decision_id, occ = await _open_call(monkeypatch, sim)
    sim.set_price(occ, 6.00)
    await fleet_tick(monkeypatch, market_open=False)  # snapshot records the mark

    sim.exercise(occ, underlying="NVDA", strike=250.0)
    await fleet_tick(monkeypatch, market_open=False)

    d = await decision_row(decision_id)
    assert d.close_reason == "option_exercised"
    assert d.realized_pnl == Decimal("345.00")  # (6.00 - 2.55) x 100, from the last mark
    pages = [p for p in outbox if p.get("data_kind") == "ops_alert"]
    assert pages and "100 NVDA shares bought" in pages[0]["body"]
    assert sim.held["NVDA"].qty == 100


async def test_eod_report_counts_the_day_from_the_real_tables(
    monkeypatch: pytest.MonkeyPatch, options_on: None, outbox: list[dict],
) -> None:
    from app.services.council import eod_report
    from engine.db.session import async_session_factory

    sim = SimBroker()
    _decision_id, occ = await _open_call(monkeypatch, sim)
    sim.expire(occ)
    await fleet_tick(monkeypatch, market_open=False)

    async def _no_ghosts(day):  # ghost marks need historical bars; not this scenario's subject
        return {"created": 0, "updated": 0, "finalized": 0}

    monkeypatch.setattr(eod_report, "_mark_ghosts", _no_ghosts)
    report = await eod_report.run_eod(
        user_id=FIXTURE_USER, session_factory=async_session_factory(),
        day=datetime.now(UTC).date(),
    )
    assert report is not None
    assert report.closes_by_reason == {"option_expired": 1}
    assert report.realized_today == -255.0
    assert report.decisions_today == 1
    assert any(p.get("data_kind") == "daily_report" and "$" not in p["body"] for p in outbox)


async def test_the_agents_own_stop_loss_close_is_recorded_as_its_own(
    monkeypatch: pytest.MonkeyPatch, options_on: None, outbox: list[dict],
) -> None:
    """The software stop fires during market hours and the close fills the
    moment it is submitted: the decision must close with the agent's
    reason and the real exit price, and keep its ENTRY price."""
    sim = SimBroker()
    decision_id, occ = await _open_call(monkeypatch, sim)
    for oid, req in list(sim.requests.items()):  # take the resting stop out of play
        if req.stop_price is not None:
            await sim.cancel_order(oid)

    sim.set_price(occ, 1.20)  # -53%, through the -40% premium stop
    await fleet_tick(monkeypatch, market_open=True)
    await fleet_tick(monkeypatch, market_open=True)

    d = await decision_row(decision_id)
    assert d.fill_avg_price == Decimal("2.5500"), "the exit fill overwrote the entry price"
    assert d.close_reason == "option_stop_loss"
    assert d.realized_pnl == Decimal("-135.00")


async def test_a_manual_exit_position_never_gets_an_agent_stop(
    monkeypatch: pytest.MonkeyPatch, options_on: None, outbox: list[dict],
) -> None:
    """The user kept the exit. Neither the entry lifecycle nor the
    filled-at-acknowledgement catch-up may place a stop, however many
    ticks run, and a -60% mark closes nothing."""
    sim = SimBroker()
    decision_id, occ = await _open_call(monkeypatch, sim, exit_mode="manual")
    sim.set_price(occ, 1.00)
    for _ in range(3):
        await fleet_tick(monkeypatch, market_open=True)

    assert not [o for o in sim.requests.values() if o.stop_price is not None]
    d = await decision_row(decision_id)
    assert d.closed_at is None and d.exit_mode == "manual"
