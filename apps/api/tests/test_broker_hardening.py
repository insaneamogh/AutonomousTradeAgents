"""Broker router hardening: landing-page escaping, connection health,
read-only verify, and the broker catalog."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app
from app.services.auth.auth_store import reset_auth_store_for_tests
from app.services.broker import broker_use, crypto
from app.services.broker.broker_store import (
    get_broker_store,
    reset_broker_store_for_tests,
)

SECRET = "SUPERSECRETTOKEN123"
XSS = "<script>alert(1)</script>"


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_auth_store_for_tests()
    reset_broker_store_for_tests()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _login(c: TestClient, email: str) -> tuple[str, str]:
    ch = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    issued = c.post(
        "/api/v1/auth/verify", json={"email": email, "token": ch["devToken"]}
    ).json()
    return issued["accessToken"], issued["userId"]


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed(user_id: str, broker: str, expires: datetime | None) -> str:
    rec = asyncio.run(
        get_broker_store().upsert_connection(
            user_id=user_id, broker=broker, is_paper=True, account_number="ACC1",
            encrypted_access_token=SECRET, encrypted_refresh_token=None,
            access_token_expires_at=expires,
        )
    )
    return rec.id


def test_redirect_error_param_is_escaped(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(crypto, "_CRYPTO_AVAILABLE", True)
    r = client.get("/api/v1/broker/connect/alpaca/redirect", params={"error": XSS})
    assert r.status_code == 200
    assert XSS not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert "autotrader://settings" in r.text


def test_return_link_uses_app_web_url_escaped(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(crypto, "_CRYPTO_AVAILABLE", True)
    monkeypatch.setenv("APP_WEB_URL", 'https://app.example.com/"><script>x</script>')
    r = client.get("/api/v1/broker/connect/alpaca/redirect", params={"error": "denied"})
    assert "<script>x</script>" not in r.text
    assert "Return to the app" in r.text
    assert "autotrader://settings" in r.text


def test_needs_reconnect_expired_vs_fresh(client: TestClient) -> None:
    access, uid = _login(client, "health@example.com")
    now = datetime.now(UTC)
    expired = _seed(uid, "zerodha", now - timedelta(hours=1))
    naive_expired = _seed(uid, "alpaca", (now - timedelta(hours=1)).replace(tzinfo=None))
    rows = {
        c["id"]: c
        for c in client.get("/api/v1/broker/connections", headers=_h(access)).json()
    }
    assert rows[expired]["status"] == "active"
    assert rows[expired]["needsReconnect"] is True
    assert rows[expired]["market"] == "IN" and rows[expired]["currency"] == "INR"
    assert rows[expired]["tokenExpiresAt"] is not None
    assert rows[naive_expired]["needsReconnect"] is True

    reset_broker_store_for_tests()
    access, uid = _login(client, "health2@example.com")
    fresh = _seed(uid, "zerodha", now + timedelta(hours=5))
    none_exp = _seed(uid, "alpaca", None)
    rows = {
        c["id"]: c
        for c in client.get("/api/v1/broker/connections", headers=_h(access)).json()
    }
    assert rows[fresh]["needsReconnect"] is False
    assert rows[none_exp]["needsReconnect"] is False
    assert rows[none_exp]["market"] == "US" and rows[none_exp]["currency"] == "USD"


def test_verify_other_users_connection_is_404(client: TestClient) -> None:
    _a, uid_a = _login(client, "owner@example.com")
    cid = _seed(uid_a, "alpaca", None)
    b_access, _ = _login(client, "intruder@example.com")
    r = client.post(f"/api/v1/broker/connections/{cid}/verify", headers=_h(b_access))
    assert r.status_code == 404
    r = client.post("/api/v1/broker/connections/nope/verify", headers=_h(b_access))
    assert r.status_code == 404


class _OkBroker:
    async def get_account_equity(self) -> float:
        return 1234.5

    async def get_buying_power(self) -> float:
        return 99.0


def test_verify_ok(client: TestClient, monkeypatch) -> None:
    access, uid = _login(client, "ok@example.com")
    cid = _seed(uid, "alpaca", None)
    conn = asyncio.run(get_broker_store().get_connection(cid))

    @asynccontextmanager
    async def fake(user_id, *, broker=None, store=None):
        yield _OkBroker(), conn

    monkeypatch.setattr(broker_use, "with_broker_client", fake)
    r = client.post(f"/api/v1/broker/connections/{cid}/verify", headers=_h(access))
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True
    assert body["equity"] == 1234.5 and body["buyingPower"] == 99.0
    assert body["currency"] == "USD" and body["error"] is None


@pytest.mark.parametrize("exc", [RuntimeError(f"boom token={SECRET}"), None])
def test_verify_failure_is_ok_false_without_secret(
    client: TestClient, monkeypatch, exc
) -> None:
    access, uid = _login(client, "bad@example.com")
    cid = _seed(uid, "alpaca", None)

    @asynccontextmanager
    async def fake(user_id, *, broker=None, store=None):
        if exc is None:
            raise broker_use.BrokerUnavailableError("Stored alpaca access token has expired")
        raise exc
        yield  # pragma: no cover

    monkeypatch.setattr(broker_use, "with_broker_client", fake)
    r = client.post(f"/api/v1/broker/connections/{cid}/verify", headers=_h(access))
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is False and body["error"]
    assert SECRET not in r.text
    assert "Traceback" not in r.text


def test_catalog_lists_four_and_reports_missing_names_only(
    client: TestClient, monkeypatch
) -> None:
    for k in (
        "ALPACA_OAUTH_CLIENT_ID", "ALPACA_OAUTH_CLIENT_SECRET",
        "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "KITE_API_KEY", "KITE_API_SECRET",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KITE_API_KEY", "kitekeyvalue")
    access, _ = _login(client, "cat@example.com")
    r = client.get("/api/v1/broker/catalog", headers=_h(access))
    assert r.status_code == 200
    items = {i["id"]: i for i in r.json()}
    assert set(items) == {"alpaca", "zerodha", "groww", "indmoney"}
    assert items["alpaca"]["status"] == "needs_server_config"
    assert items["alpaca"]["missingConfig"] == [
        "ALPACA_OAUTH_CLIENT_ID", "ALPACA_OAUTH_CLIENT_SECRET",
    ]
    assert items["zerodha"]["status"] == "needs_server_config"
    assert items["zerodha"]["missingConfig"] == ["KITE_API_SECRET"]
    assert items["groww"]["status"] == items["indmoney"]["status"] == "planned"
    assert "kitekeyvalue" not in r.text

    monkeypatch.setenv("KITE_API_SECRET", "s")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    items = {i["id"]: i for i in client.get("/api/v1/broker/catalog", headers=_h(access)).json()}
    assert items["alpaca"]["status"] == items["zerodha"]["status"] == "available"
    assert items["alpaca"]["missingConfig"] == []
