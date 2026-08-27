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
        managed or [],
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
