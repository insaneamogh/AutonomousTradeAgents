"""Scenarios: the equity path end to end (PLAN_PLATFORM Phase 5's
precondition for INSTRUMENT_ROUTER_ENABLED: the account has 6 equity
fills and 0 closes in its history, so nothing had exercised the close)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from e2e_harness import (
    SimBroker,
    api_client,
    decision_row,
    equity_proposal,
    fleet_tick,
    patch_broker,
    seed_account,
)

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("e2e_db")]


async def _buy_aapl(monkeypatch: pytest.MonkeyPatch, sim: SimBroker) -> str:
    from app.services.council.store import get_store

    sim.set_price("AAPL", 200.0)
    patch_broker(monkeypatch, sim, await seed_account())
    await get_store().append_pending(
        equity_proposal(symbol="AAPL", qty=10, last=200.0, stop=190.0, target=220.0)
    )
    async with api_client() as api:
        pid = (await api.get("/api/v1/approvals/pending")).json()[0]["id"]
        r = await api.post(f"/api/v1/approvals/{pid}/decision",
                           json={"outcome": "approved", "exitMode": "agent"})
        assert r.status_code == 200, r.text
        assert r.json()["executed"] is True, r.json()
    await fleet_tick(monkeypatch)
    return pid


@pytest.mark.parametrize(
    ("move_to", "reason", "pnl"),
    [(185.0, "bracket_stop", Decimal("-150.00")), (225.0, "bracket_target", Decimal("200.00"))],
)
async def test_a_bracket_leg_that_fills_closes_the_decision_with_its_own_reason(
    monkeypatch: pytest.MonkeyPatch, outbox: list[dict], move_to: float, reason: str,
    pnl: Decimal,
) -> None:
    """The stop leg elects at 190 (target leg at 220 fills at 220); the
    exit is the broker's own child order, which has no orders row of ours.
    It must still close the decision with the leg's real fill, not be
    mistaken for the user selling at Alpaca."""
    sim = SimBroker()
    pid = await _buy_aapl(monkeypatch, sim)
    d = await decision_row(pid)
    assert d.fill_qty == 10 and d.fill_avg_price == Decimal("200.0000")

    sim.set_price("AAPL", move_to)
    assert "AAPL" not in sim.held
    await fleet_tick(monkeypatch, market_open=False)

    d = await decision_row(pid)
    assert d.close_reason == reason
    assert d.realized_pnl == pnl


async def test_stock_sold_by_hand_at_the_broker_is_an_external_close(
    monkeypatch: pytest.MonkeyPatch, outbox: list[dict],
) -> None:
    """No leg filled and no order of ours: the user sold in the Alpaca app.
    P&L comes from the last snapshot mark, x1 for stock (the multiplier
    only applies to contracts)."""
    sim = SimBroker()
    pid = await _buy_aapl(monkeypatch, sim)
    sim.set_price("AAPL", 204.0)
    await fleet_tick(monkeypatch, market_open=False)  # snapshot marks 204

    await sim.cancel_open_orders("AAPL")  # Alpaca cancels the legs on a manual sell
    sim.held.pop("AAPL")
    await fleet_tick(monkeypatch, market_open=False)

    d = await decision_row(pid)
    assert d.close_reason == "external_broker"
    assert d.realized_pnl == Decimal("40.00")  # (204 - 200) x 10 shares
    assert any(p["title"] == "Position closed at broker" for p in outbox)
