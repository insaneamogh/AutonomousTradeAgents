"""Paper trading mode tests.

TRADING_MODE defaults to ``paper``: executions run the real risk chain
then fill against the in-memory paper book — no broker connection, no
crypto, no order endpoint. The two-key flip (TRADING_MODE=live +
LIVE_TRADING_ENABLED=1) is what eventually reaches a real broker.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterator

import anyio
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app  # noqa: E402
from app.schemas.approvals import ApprovalProposalDto  # noqa: E402
from app.services.auth_store import reset_auth_store_for_tests  # noqa: E402
from app.services.broker_store import reset_broker_store_for_tests  # noqa: E402
from app.services.executor import execute_proposal  # noqa: E402
from app.services.paper_broker import (  # noqa: E402
    get_paper_store,
    reset_paper_store_for_tests,
    trading_mode,
)
from app.services.store import get_store, reset_store_for_tests  # noqa: E402

USER = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADING_MODE", raising=False)
    reset_auth_store_for_tests()
    reset_broker_store_for_tests()
    reset_store_for_tests()
    reset_paper_store_for_tests()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _login(c: TestClient, email: str = "paper-user@example.com") -> str:
    challenge = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    issued = c.post(
        "/api/v1/auth/verify",
        json={"email": email, "token": challenge["devToken"]},
    ).json()
    return issued["accessToken"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _proposal(
    *,
    proposal_id: str = "agent-paper-1",
    symbol: str = "NVDA",
    side: str = "BUY",
    qty: int = 10,
    last_price: float = 100.0,
) -> ApprovalProposalDto:
    return ApprovalProposalDto(
        id=proposal_id,
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        qty=qty,
        order_type="MARKET",
        limit_price=None,
        estimated_notional=qty * last_price,
        rationale="test",
        bull_case="up",
        bear_case="down",
        risk_level=2,  # type: ignore[arg-type]
        conviction_level=4,  # type: ignore[arg-type]
        proposed_at=datetime.now(timezone.utc),
    )


async def _seed_pending(proposal: ApprovalProposalDto) -> None:
    """Push a handcrafted proposal into the mock store's pending queue."""
    store = get_store()
    await store.append_pending(proposal)


# ─────────────────────────────────────────────────────────────────────
# Mode default
# ─────────────────────────────────────────────────────────────────────


def test_default_mode_is_paper() -> None:
    assert trading_mode() == "paper"


def test_garbage_mode_falls_back_to_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "yolo")
    assert trading_mode() == "paper"


# ─────────────────────────────────────────────────────────────────────
# Execution — no broker connection needed
# ─────────────────────────────────────────────────────────────────────


def test_paper_execute_via_route_without_broker_connection(client: TestClient) -> None:
    """The full mobile flow: run council → approve → simulated fill.
    No broker connected, no cryptography required, nothing leaves the app.
    """
    access = _login(client)
    r = client.post("/api/v1/agent/run", headers=_bearer(access), json={"symbol": "NVDA"})
    assert r.status_code == 200, r.text
    proposal = r.json().get("proposal")
    if proposal is None:
        pytest.skip("mock council HOLD'd")

    r = client.post(
        f"/api/v1/orders/execute/{proposal['id']}", headers=_bearer(access)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["riskBlocked"] is False
    assert body["order"]["isPaper"] is True
    assert body["order"]["status"] == "filled"
    assert body["order"]["brokerOrderId"].startswith("paper-")
    assert "paper_mode" in body["informationalFlags"]


async def test_paper_buy_books_position_and_debits_cash() -> None:
    await _seed_pending(_proposal(qty=10, last_price=100.0))
    resp = await execute_proposal(user_id=USER, proposal_id="agent-paper-1")
    assert resp.order is not None and resp.order.is_paper

    pf = get_paper_store().portfolio(USER, "US")
    assert pf.cash == 100_000.0 - 1_000.0
    holding = pf.holdings["NVDA"]
    assert holding.qty == resp.order.qty
    assert holding.avg_entry_price == 100.0


async def test_paper_sell_realizes_pnl() -> None:
    pf = get_paper_store().portfolio(USER, "US")
    pf.fill(symbol="NVDA", side="BUY", qty=10, price=100.0,
            proposal_id=None, client_order_id=None)

    await _seed_pending(
        _proposal(proposal_id="agent-paper-2", side="SELL", qty=10, last_price=120.0)
    )
    resp = await execute_proposal(user_id=USER, proposal_id="agent-paper-2")
    assert resp.order is not None

    sells = [f for f in pf.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].realized_pnl == pytest.approx((120.0 - 100.0) * 10)
    assert "NVDA" not in pf.holdings


async def test_paper_naked_sell_is_vetoed_by_risk_chain() -> None:
    await _seed_pending(
        _proposal(proposal_id="agent-paper-3", side="SELL", qty=5, last_price=100.0)
    )
    resp = await execute_proposal(user_id=USER, proposal_id="agent-paper-3")
    assert resp.risk_blocked is True
    assert resp.risk_veto_rule == "forbid_short_phase_0"
    assert get_paper_store().portfolio(USER, "US").fills == []


async def test_paper_fill_is_idempotent_on_client_order_id() -> None:
    pf = get_paper_store().portfolio(USER, "US")
    a = pf.fill(symbol="NVDA", side="BUY", qty=10, price=100.0,
                proposal_id="p1", client_order_id="agent-exec-p1")
    b = pf.fill(symbol="NVDA", side="BUY", qty=10, price=100.0,
                proposal_id="p1", client_order_id="agent-exec-p1")
    assert a.id == b.id
    assert pf.holdings["NVDA"].qty == 10  # not 20
    assert pf.cash == 100_000.0 - 1_000.0


async def test_india_symbols_book_in_inr_market() -> None:
    await _seed_pending(
        _proposal(proposal_id="agent-paper-4", symbol="NSE:RELIANCE",
                  qty=10, last_price=2_900.0)
    )
    resp = await execute_proposal(user_id=USER, proposal_id="agent-paper-4")
    assert resp.order is not None

    store = get_paper_store()
    assert store.has_book(USER, "IN")
    pf = store.portfolio(USER, "IN")
    assert pf.cash == 1_000_000.0 - 29_000.0
    assert not store.portfolio(USER, "US").holdings


# ─────────────────────────────────────────────────────────────────────
# Portfolio summary surfaces the paper book
# ─────────────────────────────────────────────────────────────────────


def test_portfolio_summary_shows_paper_book(client: TestClient) -> None:
    access = _login(client)
    anyio.run(_seed_pending, _proposal(qty=10, last_price=100.0))
    r = client.post("/api/v1/orders/execute/agent-paper-1", headers=_bearer(access))
    assert r.status_code == 200, r.text

    r = client.get("/api/v1/portfolio/summary", headers=_bearer(access))
    assert r.status_code == 200, r.text
    entries = {e["accountNumber"]: e for e in r.json()["brokers"]}
    paper_us = entries["PAPER-US"]
    assert paper_us["broker"] == "paper"
    assert paper_us["isPaper"] is True
    assert paper_us["currency"] == "USD"
    assert paper_us["buyingPower"] == 100_000.0 - 1_000.0
    assert paper_us["positions"][0]["symbol"] == "NVDA"
    assert paper_us["profitWindow"]["attribution"] == "paper_engine"
