"""Per-broker portfolio summary tests.

Fake brokers replace ``with_broker_client`` in the portfolio service's
namespace (same seam as the executor tests). Connections are seeded
through the InMemory broker store so the active-connection scan is real.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app  # noqa: E402
from app.services import portfolio_service as portfolio_mod  # noqa: E402
from app.services.auth_store import reset_auth_store_for_tests  # noqa: E402
from app.services.broker_store import (  # noqa: E402
    get_broker_store,
    reset_broker_store_for_tests,
)
from app.services.broker_use import BrokerUnavailableError  # noqa: E402
from app.services.store import reset_store_for_tests  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin live mode: these tests assert the real-broker entries only.
    # Paper-mode entries are covered in test_paper_mode.py.
    monkeypatch.setenv("TRADING_MODE", "live")
    reset_auth_store_for_tests()
    reset_broker_store_for_tests()
    reset_store_for_tests()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _login(c: TestClient, email: str = "pnl-user@example.com") -> tuple[str, str]:
    challenge = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    issued = c.post(
        "/api/v1/auth/verify",
        json={"email": email, "token": challenge["devToken"]},
    ).json()
    return issued["accessToken"], issued["userId"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_connection(user_id: str, broker: str) -> None:
    await get_broker_store().upsert_connection(
        user_id=user_id,
        broker=broker,
        is_paper=(broker == "alpaca"),
        account_number="ACCT-1" if broker == "alpaca" else "AB1234",
        encrypted_access_token="enc",
        encrypted_refresh_token=None,
        access_token_expires_at=None,
    )


@dataclass
class _FakePosition:
    symbol: str
    qty: int
    avg_entry_price: float
    market_value: float
    unrealized_pl: float
    unrealized_pl_pct: float


@dataclass
class _FakeBroker:
    equity: float = 100_000.0
    buying_power: float = 50_000.0
    positions: list[_FakePosition] = field(default_factory=list)

    async def get_account_equity(self) -> float:
        return self.equity

    async def get_buying_power(self) -> float:
        return self.buying_power

    async def list_positions(self) -> list[_FakePosition]:
        return self.positions


def _patch_clients(
    monkeypatch: pytest.MonkeyPatch,
    by_broker: dict[str, Any],
) -> None:
    """Map broker name → _FakeBroker | Exception in the service namespace."""

    @asynccontextmanager
    async def fake_cm(user_id: str, *, broker: str | None = None, store: Any = None):
        target = by_broker.get(broker or "")
        if isinstance(target, Exception):
            raise target
        conn_rows = await get_broker_store().list_connections(user_id)
        conn = next(r for r in conn_rows if r.broker == broker)
        yield target, conn

    monkeypatch.setattr(portfolio_mod, "with_broker_client", fake_cm)


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────


def test_no_connections_returns_empty_list(client: TestClient) -> None:
    access, _ = _login(client)
    r = client.get("/api/v1/portfolio/summary", headers=_bearer(access))
    assert r.status_code == 200, r.text
    assert r.json() == {"brokers": []}


def test_single_broker_math(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import anyio

    access, user_id = _login(client)
    anyio.run(_seed_connection, user_id, "alpaca")
    broker = _FakeBroker(
        positions=[
            _FakePosition("NVDA", 10, 100.0, 1_200.0, 200.0, 20.0),
            _FakePosition("AAPL", 5, 200.0, 900.0, -100.0, -10.0),
        ]
    )
    _patch_clients(monkeypatch, {"alpaca": broker})

    r = client.get("/api/v1/portfolio/summary", headers=_bearer(access))
    assert r.status_code == 200, r.text
    [entry] = r.json()["brokers"]
    assert entry["broker"] == "alpaca"
    assert entry["currency"] == "USD"
    assert entry["status"] == "ok"
    assert entry["equity"] == 100_000.0
    assert len(entry["positions"]) == 2
    assert entry["profitWindow"]["unrealizedPnl"] == 100.0  # 200 - 100
    assert entry["profitWindow"]["windowDays"] == 30
    assert entry["profitWindow"]["attribution"] == "symbol_market"


def test_expired_zerodha_degrades_while_alpaca_ok(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anyio

    access, user_id = _login(client)
    anyio.run(_seed_connection, user_id, "alpaca")
    anyio.run(_seed_connection, user_id, "zerodha")
    _patch_clients(
        monkeypatch,
        {
            "alpaca": _FakeBroker(),
            "zerodha": BrokerUnavailableError(
                "Stored zerodha access token has expired — reconnect Zerodha."
            ),
        },
    )

    r = client.get("/api/v1/portfolio/summary", headers=_bearer(access))
    assert r.status_code == 200, r.text
    entries = {e["broker"]: e for e in r.json()["brokers"]}
    assert entries["alpaca"]["status"] == "ok"
    assert entries["zerodha"]["status"] == "token_expired"
    assert entries["zerodha"]["currency"] == "INR"
    assert entries["zerodha"]["equity"] is None
    assert "reconnect" in entries["zerodha"]["detail"].lower()


def test_window_days_validation(client: TestClient) -> None:
    access, _ = _login(client)
    r = client.get(
        "/api/v1/portfolio/summary?windowDays=0", headers=_bearer(access)
    )
    assert r.status_code == 422
