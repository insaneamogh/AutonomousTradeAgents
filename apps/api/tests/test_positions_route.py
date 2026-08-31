"""/api/v1/positions route tests (mock-store mode).

Deep close mechanics (risk gate, bracket cancel, broker SELL) are covered
by the position_manager tests + the executor path. Here we pin the route
surface: auth, the empty-in-mock-mode list, and the close endpoint's
error mapping when there's no Postgres-backed position.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app
from app.services.auth.auth_store import reset_auth_store_for_tests
from app.services.council.store import reset_store_for_tests


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_POSTGRES", raising=False)  # mock-store mode
    reset_auth_store_for_tests()
    reset_store_for_tests()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _bearer(c: TestClient, email: str = "pos-user@example.com") -> dict[str, str]:
    challenge = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    issued = c.post(
        "/api/v1/auth/verify", json={"email": email, "token": challenge["devToken"]}
    ).json()
    return {"Authorization": f"Bearer {issued['accessToken']}"}


def test_open_positions_empty_in_mock_mode(client: TestClient) -> None:
    r = client.get("/api/v1/positions")
    assert r.status_code == 200
    assert r.json() == []


def test_close_unknown_position_404(client: TestClient) -> None:
    # Authenticated, but no Postgres-backed position exists → not_found.
    r = client.post(
        "/api/v1/positions/not-a-real-id/close", headers=_bearer(client)
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "not_found"


def test_close_requires_real_auth(client: TestClient) -> None:
    # require_real_auth must reject a missing/bad bearer.
    r = client.post("/api/v1/positions/abc/close")
    assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────────
# POST /positions/unmanaged/{symbol}/close — closing a broker position
# with NO agent decision behind it at all (see position_manager
# .close_unmanaged_position_now). Deep mechanics (risk gate, side
# derivation, unlinked persistence) are covered by the position_manager
# tests; this pins the route surface — auth, and the mock-mode mapping —
# exactly like the decision-keyed close endpoint above.
# ─────────────────────────────────────────────────────────────────────


def test_close_unmanaged_requires_real_auth(client: TestClient) -> None:
    r = client.post("/api/v1/positions/unmanaged/NVDA/close")
    assert r.status_code == 401


def test_close_unmanaged_in_mock_mode_409(client: TestClient) -> None:
    # No Postgres in this test process → close_unmanaged_position_now's own
    # gate refuses before ever touching a broker. 409 (not 404): unlike a
    # decision_id, "NVDA" isn't a resource identifier that either does or
    # doesn't exist — it's "you don't currently hold this," the same
    # category as "already closed."
    r = client.post(
        "/api/v1/positions/unmanaged/NVDA/close", headers=_bearer(client)
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "no_open_position"


def test_close_unmanaged_route_is_distinct_from_the_decision_route(
    client: TestClient,
) -> None:
    """A symbol like "NVDA" is never a valid decision_id, but the two
    routes must not be ambiguous regardless — pin that posting to the
    decision-keyed route with a bare symbol still 404s as "not_found"
    (no matching decision), not something confused with the unmanaged
    route's 409."""
    r = client.post("/api/v1/positions/NVDA/close", headers=_bearer(client))
    assert r.status_code == 404
    assert r.json()["detail"] == "not_found"


# ─────────────────────────────────────────────────────────────────────
# Unmanaged broker positions
#
# A position held at the broker with no agent decision behind it used to
# be dropped, so /account reported open positions that /positions
# rendered as an empty list. These pin the shape of the row we emit
# instead — no decision_id, managed=False, and no exit plan to promise.
# ─────────────────────────────────────────────────────────────────────


class _Snapshot:
    def __init__(self, positions: list[dict], captured_at: object) -> None:
        self.open_positions = positions
        self.captured_at = captured_at


def _unmanaged(positions: list[dict], managed: list | None = None) -> list:
    from datetime import UTC, datetime

    from app.services.orders.positions_service import _unmanaged as fn

    return fn(
        {str(p["symbol"]).upper(): p for p in positions},
        # Real callers pass the broker-side KEY set (OCC for an option,
        # plain symbol otherwise — see positions_service._broker_key_for_
        # decision); this suite only exercises plain-equity coverage, so a
        # direct .symbol set reproduces that contract without needing a
        # real AgentDecision-shaped object.
        {str(m.symbol).upper() for m in (managed or [])},
        _Snapshot(positions, datetime(2026, 8, 27, tzinfo=UTC)),
    )


def test_unmanaged_long_position_is_listed_without_a_decision_id() -> None:
    (row,) = _unmanaged(
        [{"symbol": "NVDA", "qty": 23, "market_value": 5095.42, "avg_entry_price": 212.23}]
    )
    assert row.decision_id is None
    assert row.managed is False
    assert (row.symbol, row.side, row.direction, row.qty) == ("NVDA", "BUY", "long", 23)
    assert row.last_price == pytest.approx(221.54, abs=0.01)
    # (221.54 - 212.23) * 23
    assert row.unrealized_pnl == pytest.approx(214.11, abs=0.05)
    # Nothing was promised about the exit, because nothing planned it.
    assert row.exit_mode == "manual"
    assert row.stop_loss is None and row.target_price is None


def test_unmanaged_short_position_gains_when_price_falls() -> None:
    # Alpaca reports qty AND market_value negative for a short.
    (row,) = _unmanaged(
        [{"symbol": "TSLA", "qty": -10, "market_value": -3000.0, "avg_entry_price": 320.0}]
    )
    assert (row.side, row.direction, row.qty) == ("SELL", "short", 10)
    assert row.last_price == pytest.approx(300.0)
    # Entered at 320, marked at 300 → a short is up 20/share on 10 shares.
    assert row.unrealized_pnl == pytest.approx(200.0)


def test_symbol_already_covered_by_a_decision_is_not_duplicated() -> None:
    class _Managed:
        symbol = "NVDA"

    assert (
        _unmanaged(
            [{"symbol": "NVDA", "qty": 23, "market_value": 5095.42, "avg_entry_price": 212.23}],
            [_Managed()],
        )
        == []
    )


# ─────────────────────────────────────────────────────────────────────
# Pending-fill positions
#
# An approved proposal used to disappear the instant it was decided and
# only reappear once the broker filled it — invisible in between, most
# visibly outside market hours when an order can sit accepted-but-unfilled
# for hours. These pin the DTO `_from_decision` builds for that gap.
# ─────────────────────────────────────────────────────────────────────


class _Decision:
    def __init__(self, **kw: object) -> None:
        self.id = kw.get("id", "dec-1")
        self.proposal = kw.get("proposal", {})
        self.fill_avg_price = kw.get("fill_avg_price")
        self.fill_qty = kw.get("fill_qty")
        self.symbol = kw.get("symbol", "KO")
        self.exit_mode = kw.get("exit_mode", "agent")
        self.user_responded_at = kw.get("user_responded_at")
        self.triggered_at = kw.get("triggered_at", "2026-08-27T09:21:01Z")


def test_pending_fill_reports_the_proposal_qty_with_no_entry_or_mark() -> None:
    from app.services.orders.positions_service import _from_decision

    dto = _from_decision(
        _Decision(
            proposal={"side": "BUY", "qty": 55, "stopLoss": 87.04, "targetPrice": 97.72},
        ),
        marks={"KO": 89.5},  # even a known mark must not be used pre-fill
        status="pending_fill",
    )
    assert dto.status == "pending_fill"
    assert dto.qty == 55
    assert dto.avg_entry_price is None
    assert dto.last_price is None
    assert dto.unrealized_pnl is None
    assert (dto.stop_loss, dto.target_price) == (87.04, 97.72)


def test_open_position_uses_the_live_mark() -> None:
    from app.services.orders.positions_service import _from_decision

    dto = _from_decision(
        _Decision(
            proposal={"side": "BUY", "qty": 55},
            fill_qty=55,
            fill_avg_price=87.5,
        ),
        marks={"KO": 90.0},
        status="open",
    )
    assert dto.status == "open"
    assert dto.avg_entry_price == 87.5
    assert dto.last_price == 90.0
    assert dto.unrealized_pnl == pytest.approx(137.5)  # (90 - 87.5) * 55
