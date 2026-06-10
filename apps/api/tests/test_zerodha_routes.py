"""Zerodha connect-flow tests.

End-to-end against the FastAPI TestClient. The Kite session-token
endpoint is mocked via ``httpx.MockTransport`` so no network hits happen.

Same crypto gating as test_broker.py: the happy-path tests skip when
``cryptography`` isn't installed.
"""

from __future__ import annotations

import os
from typing import Any, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app  # noqa: E402
from app.services import crypto, zerodha_connect  # noqa: E402
from app.services.auth_store import reset_auth_store_for_tests  # noqa: E402
from app.services.broker_store import reset_broker_store_for_tests  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# Test plumbing
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_stores() -> None:
    reset_auth_store_for_tests()
    reset_broker_store_for_tests()


@pytest.fixture(autouse=True)
def _kite_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KITE_API_KEY", "testapikey")
    monkeypatch.setenv("KITE_API_SECRET", "testapisecret")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _login_and_get_access(c: TestClient, email: str = "zerodha-user@example.com") -> str:
    challenge = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    issued = c.post(
        "/api/v1/auth/verify",
        json={"email": email, "token": challenge["devToken"]},
    ).json()
    return issued["accessToken"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mock_session_endpoint(
    *,
    access_token: str = "kite-daily-token",
    kite_user_id: str = "AB1234",
    fail_status: int | None = None,
) -> Iterator[None]:
    """Patch the connect service's exchange to hit a MockTransport."""

    def handler(request: httpx.Request) -> httpx.Response:
        if fail_status is not None:
            return httpx.Response(
                fail_status,
                json={"status": "error", "message": "Token is invalid or has expired."},
            )
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "access_token": access_token,
                    "user_id": kite_user_id,
                    "user_name": "Test User",
                    "login_time": "2026-06-10 09:00:00",
                },
            },
        )

    transport = httpx.MockTransport(handler)
    real_exchange = zerodha_connect.exchange_request_token

    async def patched(*, request_token: str, client: Any = None) -> Any:
        async with httpx.AsyncClient(
            transport=transport, base_url="https://api.kite.trade"
        ) as c:
            return await real_exchange(request_token=request_token, client=c)

    zerodha_connect.exchange_request_token = patched  # type: ignore[assignment]
    try:
        yield
    finally:
        zerodha_connect.exchange_request_token = real_exchange  # type: ignore[assignment]


@pytest.fixture
def mocked_session_endpoint() -> Iterator[None]:
    yield from _mock_session_endpoint()


pytestmark_crypto = pytest.mark.skipif(
    not crypto.is_available(),
    reason="cryptography not installed — Zerodha connect round-trip skipped",
)


# ─────────────────────────────────────────────────────────────────────
# Config gating
# ─────────────────────────────────────────────────────────────────────


@pytestmark_crypto
def test_start_returns_503_when_kite_env_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KITE_API_KEY")
    access = _login_and_get_access(client)
    r = client.post("/api/v1/broker/connect/zerodha/start", headers=_bearer(access))
    assert r.status_code == 503
    assert "KITE_API_KEY" in r.json()["detail"]


# ─────────────────────────────────────────────────────────────────────
# Start
# ─────────────────────────────────────────────────────────────────────


@pytestmark_crypto
def test_start_returns_kite_login_url_with_state(client: TestClient) -> None:
    access = _login_and_get_access(client)
    r = client.post("/api/v1/broker/connect/zerodha/start", headers=_bearer(access))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["loginUrl"].startswith("https://kite.zerodha.com/connect/login?")
    assert "api_key=testapikey" in body["loginUrl"]
    assert body["state"]
    # The state must ride along on redirect_params so Zerodha echoes it.
    assert f"state%3D{body['state']}" in body["loginUrl"]


def test_start_requires_real_auth(client: TestClient) -> None:
    r = client.post("/api/v1/broker/connect/zerodha/start")
    assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────────
# Callback (authed POST)
# ─────────────────────────────────────────────────────────────────────


@pytestmark_crypto
def test_callback_round_trips_to_connection(
    client: TestClient, mocked_session_endpoint: None
) -> None:
    access = _login_and_get_access(client)
    started = client.post(
        "/api/v1/broker/connect/zerodha/start", headers=_bearer(access)
    ).json()

    r = client.post(
        "/api/v1/broker/connect/zerodha/callback",
        headers=_bearer(access),
        json={"requestToken": "REQ123", "state": started["state"]},
    )
    assert r.status_code == 200, r.text
    conn = r.json()["connection"]
    assert conn["broker"] == "zerodha"
    assert conn["isPaper"] is False  # Kite has no paper environment
    assert conn["accountNumber"] == "AB1234"
    assert conn["status"] == "active"

    listed = client.get("/api/v1/broker/connections", headers=_bearer(access)).json()
    assert any(c["broker"] == "zerodha" for c in listed)


@pytestmark_crypto
def test_callback_unknown_state_is_400(
    client: TestClient, mocked_session_endpoint: None
) -> None:
    access = _login_and_get_access(client)
    r = client.post(
        "/api/v1/broker/connect/zerodha/callback",
        headers=_bearer(access),
        json={"requestToken": "REQ123", "state": "never-issued"},
    )
    assert r.status_code == 400


@pytestmark_crypto
def test_callback_cross_user_state_is_400(
    client: TestClient, mocked_session_endpoint: None
) -> None:
    """Alice can't complete Bob's Zerodha connect."""
    bob = _login_and_get_access(client, email="bob@example.com")
    started = client.post(
        "/api/v1/broker/connect/zerodha/start", headers=_bearer(bob)
    ).json()

    alice = _login_and_get_access(client, email="alice@example.com")
    r = client.post(
        "/api/v1/broker/connect/zerodha/callback",
        headers=_bearer(alice),
        json={"requestToken": "REQ123", "state": started["state"]},
    )
    assert r.status_code == 400
    # Non-leaking error: doesn't reveal whether the state existed.
    assert "state mismatch" in r.json()["detail"]


@pytestmark_crypto
def test_callback_exchange_failure_is_502(client: TestClient) -> None:
    access = _login_and_get_access(client)
    started = client.post(
        "/api/v1/broker/connect/zerodha/start", headers=_bearer(access)
    ).json()

    gen = _mock_session_endpoint(fail_status=403)
    next(gen)
    try:
        r = client.post(
            "/api/v1/broker/connect/zerodha/callback",
            headers=_bearer(access),
            json={"requestToken": "STALE", "state": started["state"]},
        )
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
    assert r.status_code == 502


# ─────────────────────────────────────────────────────────────────────
# Browser GET redirect
# ─────────────────────────────────────────────────────────────────────


@pytestmark_crypto
def test_browser_redirect_completes_connect_without_bearer(
    client: TestClient, mocked_session_endpoint: None
) -> None:
    """The Kite-registered redirect URL hits us as a plain browser GET —
    the single-use state from the authed /start identifies the user.
    """
    access = _login_and_get_access(client)
    started = client.post(
        "/api/v1/broker/connect/zerodha/start", headers=_bearer(access)
    ).json()

    r = client.get(
        "/api/v1/broker/connect/zerodha/redirect",
        params={"request_token": "REQ123", "state": started["state"]},
    )
    assert r.status_code == 200, r.text
    assert "Zerodha connected" in r.text

    listed = client.get("/api/v1/broker/connections", headers=_bearer(access)).json()
    zerodha = next(c for c in listed if c["broker"] == "zerodha")
    assert zerodha["status"] == "active"
    assert zerodha["accountNumber"] == "AB1234"


@pytestmark_crypto
def test_browser_redirect_replay_fails(
    client: TestClient, mocked_session_endpoint: None
) -> None:
    access = _login_and_get_access(client)
    started = client.post(
        "/api/v1/broker/connect/zerodha/start", headers=_bearer(access)
    ).json()

    params = {"request_token": "REQ123", "state": started["state"]}
    first = client.get("/api/v1/broker/connect/zerodha/redirect", params=params)
    assert first.status_code == 200
    replay = client.get("/api/v1/broker/connect/zerodha/redirect", params=params)
    assert replay.status_code == 400


@pytestmark_crypto
def test_browser_redirect_missing_params_is_400(client: TestClient) -> None:
    r = client.get("/api/v1/broker/connect/zerodha/redirect")
    assert r.status_code == 400
    assert "Missing" in r.text
