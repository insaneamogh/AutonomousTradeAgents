"""Broker OAuth tests.

End-to-end against the FastAPI TestClient. We mock the Alpaca token
endpoint via ``httpx.MockTransport`` so no network hits happen.

When ``cryptography`` isn't installed, the OAuth happy-path tests
skip — the /start route returns 503 in that case (verified by a
separate test). When cryptography IS installed, the full round-trip
runs.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app  # noqa: E402
from app.services import alpaca_oauth, crypto  # noqa: E402
from app.services.auth_store import reset_auth_store_for_tests  # noqa: E402
from app.services.broker_store import reset_broker_store_for_tests  # noqa: E402
from app.services.jwt_service import mint_access  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# Test plumbing
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_stores() -> None:
    reset_auth_store_for_tests()
    reset_broker_store_for_tests()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _login_and_get_access(c: TestClient, email: str = "alpaca-user@example.com") -> str:
    """Mint a real magic-link → verify round trip + return the access token.
    Reuses the auth flow from test_auth.py.
    """
    challenge = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    issued = c.post(
        "/api/v1/auth/verify",
        json={"email": email, "token": challenge["devToken"]},
    ).json()
    return issued["accessToken"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────────
# 503 fallback when crypto isn't installed
# ─────────────────────────────────────────────────────────────────────


def test_start_returns_503_when_crypto_unavailable(client: TestClient, monkeypatch) -> None:
    """If the operator hasn't run `uv sync` for cryptography, the OAuth
    routes should surface a clear 503 — not a 500 ImportError trace.
    """
    monkeypatch.setattr(crypto, "_CRYPTO_AVAILABLE", False)
    access = _login_and_get_access(client)
    r = client.post(
        "/api/v1/broker/connect/alpaca/start",
        headers=_bearer(access),
        json={"isPaper": True},
    )
    assert r.status_code == 503
    assert "uv sync" in r.json()["detail"]


# ─────────────────────────────────────────────────────────────────────
# Happy-path round trip (gated on cryptography)
# ─────────────────────────────────────────────────────────────────────


pytestmark_crypto = pytest.mark.skipif(
    not crypto.is_available(),
    reason="cryptography not installed — full OAuth round-trip skipped",
)


def _mock_token_endpoint(
    *,
    access_token: str = "alpaca-access-XXX",
    refresh_token: str = "alpaca-refresh-YYY",
    account_number: str = "PA-ACCOUNT-001",
    expires_in: int = 86_400,
    raise_status: int | None = None,
) -> Iterator[None]:
    """Patch the module-level ``token_endpoint`` resolver to point at a
    MockTransport so the real Alpaca host is never touched."""

    def handler(request: httpx.Request) -> httpx.Response:
        if raise_status is not None:
            return httpx.Response(raise_status, json={"error": "invalid_grant"})
        # Confirm the client supplied the PKCE verifier — sanity check on
        # the helper, not Alpaca's behavior.
        from urllib.parse import parse_qs
        form = parse_qs(request.content.decode("utf-8"))
        assert "code_verifier" in form, "PKCE verifier missing"
        return httpx.Response(
            200,
            json={
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_in": expires_in,
                "token_type": "Bearer",
                "scope": "account:write trading",
                "account_number": account_number,
            },
        )

    # Patch exchange_code_for_tokens to use a client with our mock transport.
    transport = httpx.MockTransport(handler)
    real_exchange = alpaca_oauth.exchange_code_for_tokens

    async def patched(*, code, code_verifier, redirect_uri=None, client=None):
        async with httpx.AsyncClient(transport=transport) as c:
            return await real_exchange(
                code=code,
                code_verifier=code_verifier,
                redirect_uri=redirect_uri,
                client=c,
            )

    alpaca_oauth.exchange_code_for_tokens = patched  # type: ignore[assignment]
    try:
        yield
    finally:
        alpaca_oauth.exchange_code_for_tokens = real_exchange  # type: ignore[assignment]


@pytest.fixture
def mocked_token_endpoint() -> Iterator[None]:
    yield from _mock_token_endpoint()


@pytestmark_crypto
def test_start_returns_pkce_authorize_url(client: TestClient) -> None:
    access = _login_and_get_access(client)
    r = client.post(
        "/api/v1/broker/connect/alpaca/start",
        headers=_bearer(access),
        json={"isPaper": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["authorizeUrl"].startswith("https://app.alpaca.markets/oauth/authorize?")
    assert "code_challenge=" in body["authorizeUrl"]
    assert "code_challenge_method=S256" in body["authorizeUrl"]
    assert body["state"]
    # Dev-key warning surfaces because we haven't overridden the env.
    assert body["devWarning"] and "dev fallback" in body["devWarning"]


@pytestmark_crypto
def test_callback_round_trips_to_connection(
    client: TestClient, mocked_token_endpoint: None
) -> None:
    access = _login_and_get_access(client)
    started = client.post(
        "/api/v1/broker/connect/alpaca/start",
        headers=_bearer(access),
        json={"isPaper": True},
    ).json()

    r = client.post(
        "/api/v1/broker/connect/alpaca/callback",
        headers=_bearer(access),
        json={"code": "auth-code-from-alpaca", "state": started["state"]},
    )
    assert r.status_code == 200, r.text
    conn = r.json()["connection"]
    assert conn["broker"] == "alpaca"
    assert conn["isPaper"] is True
    assert conn["accountNumber"] == "PA-ACCOUNT-001"
    assert conn["status"] == "active"

    # List reflects the new connection.
    listed = client.get(
        "/api/v1/broker/connections", headers=_bearer(access),
    ).json()
    assert len(listed) == 1
    assert listed[0]["id"] == conn["id"]


@pytestmark_crypto
def test_callback_with_wrong_state_is_400(
    client: TestClient, mocked_token_endpoint: None
) -> None:
    access = _login_and_get_access(client)
    client.post(
        "/api/v1/broker/connect/alpaca/start",
        headers=_bearer(access),
        json={"isPaper": True},
    )
    r = client.post(
        "/api/v1/broker/connect/alpaca/callback",
        headers=_bearer(access),
        json={"code": "abc", "state": "not-the-real-state"},
    )
    assert r.status_code == 400


@pytestmark_crypto
def test_callback_state_belongs_to_other_user_is_400(
    client: TestClient, mocked_token_endpoint: None
) -> None:
    """Alice starts an OAuth flow; Bob tries to redeem her state. Must refuse."""
    alice = _login_and_get_access(client, "alice@example.com")
    bob = _login_and_get_access(client, "bob@example.com")

    started = client.post(
        "/api/v1/broker/connect/alpaca/start",
        headers=_bearer(alice),
        json={"isPaper": True},
    ).json()

    r = client.post(
        "/api/v1/broker/connect/alpaca/callback",
        headers=_bearer(bob),
        json={"code": "abc", "state": started["state"]},
    )
    assert r.status_code == 400


@pytestmark_crypto
def test_callback_state_is_single_use(
    client: TestClient, mocked_token_endpoint: None
) -> None:
    """Once a state is consumed in /callback, replaying it must fail."""
    access = _login_and_get_access(client)
    started = client.post(
        "/api/v1/broker/connect/alpaca/start",
        headers=_bearer(access),
        json={"isPaper": True},
    ).json()

    ok = client.post(
        "/api/v1/broker/connect/alpaca/callback",
        headers=_bearer(access),
        json={"code": "abc", "state": started["state"]},
    )
    assert ok.status_code == 200

    replay = client.post(
        "/api/v1/broker/connect/alpaca/callback",
        headers=_bearer(access),
        json={"code": "abc", "state": started["state"]},
    )
    assert replay.status_code == 400


@pytestmark_crypto
def test_revoke_marks_connection_revoked(
    client: TestClient, mocked_token_endpoint: None
) -> None:
    access = _login_and_get_access(client)
    started = client.post(
        "/api/v1/broker/connect/alpaca/start",
        headers=_bearer(access),
        json={"isPaper": True},
    ).json()
    conn = client.post(
        "/api/v1/broker/connect/alpaca/callback",
        headers=_bearer(access),
        json={"code": "abc", "state": started["state"]},
    ).json()["connection"]

    r = client.delete(
        f"/api/v1/broker/connections/{conn['id']}",
        headers=_bearer(access),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "revoked"


@pytestmark_crypto
def test_revoke_other_users_connection_is_404(
    client: TestClient, mocked_token_endpoint: None
) -> None:
    alice = _login_and_get_access(client, "alice2@example.com")
    bob = _login_and_get_access(client, "bob2@example.com")

    started = client.post(
        "/api/v1/broker/connect/alpaca/start",
        headers=_bearer(alice),
        json={"isPaper": True},
    ).json()
    conn = client.post(
        "/api/v1/broker/connect/alpaca/callback",
        headers=_bearer(alice),
        json={"code": "abc", "state": started["state"]},
    ).json()["connection"]

    # Bob tries to revoke Alice's connection.
    r = client.delete(
        f"/api/v1/broker/connections/{conn['id']}",
        headers=_bearer(bob),
    )
    assert r.status_code == 404


@pytestmark_crypto
def test_token_exchange_failure_is_502(client: TestClient) -> None:
    """If Alpaca returns 4xx on the token exchange, the callback surfaces 502."""
    access = _login_and_get_access(client)
    started = client.post(
        "/api/v1/broker/connect/alpaca/start",
        headers=_bearer(access),
        json={"isPaper": True},
    ).json()

    # Patch the exchange to return a non-200.
    real_exchange = alpaca_oauth.exchange_code_for_tokens

    async def failing_exchange(**kwargs):
        raise alpaca_oauth.TokenExchangeError("simulated Alpaca 400")

    alpaca_oauth.exchange_code_for_tokens = failing_exchange  # type: ignore[assignment]
    try:
        r = client.post(
            "/api/v1/broker/connect/alpaca/callback",
            headers=_bearer(access),
            json={"code": "abc", "state": started["state"]},
        )
    finally:
        alpaca_oauth.exchange_code_for_tokens = real_exchange  # type: ignore[assignment]
    assert r.status_code == 502


@pytestmark_crypto
def test_start_requires_real_auth(client: TestClient) -> None:
    """DEV_AUTH_BYPASS doesn't apply to broker routes — they MUST have a real session."""
    r = client.post(
        "/api/v1/broker/connect/alpaca/start",
        json={"isPaper": True},
    )
    assert r.status_code == 401


@pytestmark_crypto
def test_unused_imports_keep_alive_for_pyflakes() -> None:
    """Mint helper is imported above for forward use in a planned token-
    expiry test; keep the reference live so pyflakes doesn't complain.
    """
    _ = mint_access
    _ = timedelta
