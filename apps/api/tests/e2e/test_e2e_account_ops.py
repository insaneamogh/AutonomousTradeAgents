"""Scenarios: operating the account — the kill switch, and a lost broker ack.

Same harness as test_e2e_option_lifecycle.py: a private Postgres database,
SimBroker, the real HTTP routes and the production fleet tick.
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
    fleet_tick,
    occ_for,
    option_proposal,
    order_rows,
    patch_broker,
    seed_account,
)

from broker.types import AccountActivity

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("e2e_db")]


@pytest.fixture
def options_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_OPTIONS", "1")
    monkeypatch.delenv("TRADING_MODE", raising=False)


async def _approve(occ: str, expiry, *, exit_mode: str = "agent") -> str:
    from app.services.council.store import get_store

    await get_store().append_pending(option_proposal(
        occ=occ, underlying="NVDA", strike=250.0, expiry=expiry, limit=2.55,
    ))
    async with api_client() as api:
        pid = (await api.get("/api/v1/approvals/pending")).json()[0]["id"]
        r = await api.post(f"/api/v1/approvals/{pid}/decision",
                           json={"outcome": "approved", "exitMode": exit_mode})
        assert r.status_code == 200 and r.json()["executed"], r.text
    return pid


async def test_flatten_all_closes_everything_and_turns_auto_approve_off(
    monkeypatch: pytest.MonkeyPatch, options_on: None, outbox: list[dict],
) -> None:
    from sqlalchemy import select

    from engine.db.models import BrokerConnection
    from engine.db.session import async_session_factory

    sim = SimBroker()
    expiry = (datetime.now(UTC) + timedelta(days=30)).date()
    occ = occ_for("NVDA", expiry, "call", 250.0)
    sim.set_price(occ, 2.50)
    sim.set_price("AAPL", 200.0)
    sim.held["AAPL"] = _Held(10, 190.0)  # bought by hand at the broker: no decision
    patch_broker(monkeypatch, sim, await seed_account(auto_approve=True))
    pid = await _approve(occ, expiry)
    await fleet_tick(monkeypatch)  # fill, stop, and a snapshot that lists AAPL

    async with api_client() as api:
        listed = {p["symbol"] for p in (await api.get("/api/v1/positions")).json()}
        assert listed == {"NVDA", "AAPL"}
        sim.set_price(occ, 3.10)
        r = await api.post("/api/v1/positions/flatten-all")
        assert r.status_code == 200, r.text
        body = r.json()

    assert body["autoApproveRevoked"] == 1
    assert sorted((p["symbol"], p["closed"]) for p in body["positions"]) == [
        ("AAPL", True), ("NVDA", True)]
    assert sim.held == {}, "every position must be gone at the broker"

    await fleet_tick(monkeypatch, market_open=False)
    d = await decision_row(pid)
    assert d.close_reason == "user_kill_switch"
    assert d.realized_pnl == Decimal("55.00")  # (3.10 - 2.55) x 100
    async with async_session_factory()() as s:
        consent = (await s.execute(select(BrokerConnection.auto_approve_consent))).scalar_one()
    assert consent is False


async def test_a_lost_broker_ack_still_leaves_the_position_managed_and_closable(
    monkeypatch: pytest.MonkeyPatch, options_on: None, outbox: list[dict],
) -> None:
    """The order reached the broker but its acknowledgement was never
    stored (a DB hiccup between submit and stamp): the orders row stays
    `pending` with no broker id. Orphan adoption must still give the
    decision its fill, and once the row is past the grace, a close the user
    makes at the broker must still be detected."""
    from app.services.orders import executor, order_sync

    async def _lost(**_kw: object) -> None:
        raise RuntimeError("connection reset while stamping the ack")

    sim = SimBroker()
    expiry = (datetime.now(UTC) + timedelta(days=30)).date()
    occ = occ_for("NVDA", expiry, "call", 250.0)
    sim.set_price(occ, 2.50)
    patch_broker(monkeypatch, sim, await seed_account())
    monkeypatch.setattr(executor, "persist_order_result", _lost)
    pid = await _approve(occ, expiry)

    await fleet_tick(monkeypatch)
    d = await decision_row(pid)
    assert d.fill_qty == 1, "orphan adoption must heal the fill"
    rows = await order_rows(pid)
    assert [(r.status, r.broker_order_id) for r in rows if not
            r.client_order_id.startswith("agent-protstop-")] == [("pending", None)]

    sim.held.pop(occ)  # the user sells it in the Alpaca app
    sim.set_price(occ, 2.00)
    await fleet_tick(monkeypatch, market_open=False)
    assert (await decision_row(pid)).closed_at is None, "inside the grace: still in flight"

    # Another contract's expiry must not be read as this one's.
    sim.activities.append(AccountActivity(
        activity_id="x", activity_type="OPEXP", symbol=occ_for("NVDA", expiry, "call", 260.0),
        qty=-1.0, day=datetime.now(UTC).date(),
    ))
    monkeypatch.setattr(order_sync, "UNACKED_ORDER_GRACE", timedelta(0))
    await fleet_tick(monkeypatch, market_open=False)
    d = await decision_row(pid)
    assert d.close_reason == "external_broker"
    # From the last snapshot mark before it vanished (2.50), per contract x 100.
    assert d.realized_pnl == Decimal("-5.00")


async def test_a_failed_activities_read_still_records_the_close(
    monkeypatch: pytest.MonkeyPatch, options_on: None, outbox: list[dict],
) -> None:
    sim = SimBroker()
    expiry = (datetime.now(UTC) + timedelta(days=30)).date()
    occ = occ_for("NVDA", expiry, "call", 250.0)
    sim.set_price(occ, 2.50)
    patch_broker(monkeypatch, sim, await seed_account())
    pid = await _approve(occ, expiry, exit_mode="manual")
    await fleet_tick(monkeypatch)

    sim.expire(occ)
    sim.activities_error = RuntimeError("403 from the activities endpoint")
    await fleet_tick(monkeypatch, market_open=False)
    assert (await decision_row(pid)).close_reason == "external_broker"
