"""Broker OAuth tests.

End-to-end against the FastAPI TestClient. We mock the Alpaca token
endpoint via ``httpx.MockTransport`` so no network hits happen.

When ``cryptography`` isn't installed, the OAuth happy-path tests
skip — the /start route returns 503 in that case (verified by a
separate test). When cryptography IS installed, the full round-trip
runs.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app  # noqa: E402
from app.services.auth.auth_store import reset_auth_store_for_tests  # noqa: E402
from app.services.auth.jwt_service import mint_access  # noqa: E402
from app.services.broker import alpaca_oauth, crypto  # noqa: E402
from app.services.broker.broker_store import reset_broker_store_for_tests  # noqa: E402

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


# ─────────────────────────────────────────────────────────────────────
# Desktop/web browser GET redirect — mirrors test_zerodha_routes.py's
# three redirect tests, plus the ?error= denial case Zerodha's flow has
# no equivalent of.
# ─────────────────────────────────────────────────────────────────────


@pytestmark_crypto
def test_alpaca_browser_redirect_completes_connect_without_bearer(
    client: TestClient, mocked_token_endpoint: None
) -> None:
    """The desktop/web build starts OAuth with platform='web' and lands on
    this GET redirect after Alpaca's own top-level browser navigation —
    no bearer available there, unlike the native POST /callback.
    """
    access = _login_and_get_access(client)
    started = client.post(
        "/api/v1/broker/connect/alpaca/start",
        headers=_bearer(access),
        json={"isPaper": True, "platform": "web"},
    ).json()

    r = client.get(
        "/api/v1/broker/connect/alpaca/redirect",
        params={"code": "auth-code-from-alpaca", "state": started["state"]},
    )
    assert r.status_code == 200, r.text
    assert "connected" in r.text.lower()

    listed = client.get("/api/v1/broker/connections", headers=_bearer(access)).json()
    assert len(listed) == 1
    assert listed[0]["status"] == "active"
    assert listed[0]["accountNumber"] == "PA-ACCOUNT-001"


@pytestmark_crypto
def test_alpaca_browser_redirect_replay_fails(
    client: TestClient, mocked_token_endpoint: None
) -> None:
    access = _login_and_get_access(client)
    started = client.post(
        "/api/v1/broker/connect/alpaca/start",
        headers=_bearer(access),
        json={"isPaper": True, "platform": "web"},
    ).json()

    params = {"code": "auth-code-from-alpaca", "state": started["state"]}
    first = client.get("/api/v1/broker/connect/alpaca/redirect", params=params)
    assert first.status_code == 200, first.text
    replay = client.get("/api/v1/broker/connect/alpaca/redirect", params=params)
    assert replay.status_code == 400


@pytestmark_crypto
def test_alpaca_browser_redirect_missing_params_is_400(client: TestClient) -> None:
    r = client.get("/api/v1/broker/connect/alpaca/redirect")
    assert r.status_code == 400
    assert "Missing" in r.text


@pytestmark_crypto
def test_alpaca_browser_redirect_access_denied_shows_friendly_message(
    client: TestClient,
) -> None:
    """Alpaca's OAuth2-standard denial shape — the user declined consent —
    redirects back as ``?error=...&state=...`` with NO ``code``. Zerodha's
    request-token flow has no equivalent shape, so this needed an explicit
    check rather than falling out of the shared helper. Must render a
    friendly message, not a raw 400 validation error, and must not require
    a valid/consumable state to explain that.
    """
    r = client.get(
        "/api/v1/broker/connect/alpaca/redirect",
        params={"error": "access_denied", "state": "never-issued-or-missing"},
    )
    assert r.status_code == 200, r.text
    assert "not completed" in r.text.lower()
    assert "access_denied" in r.text


# ─────────────────────────────────────────────────────────────────────
# redirect_uri must match between /start's authorize_url and the
# token-exchange step — for BOTH the native default and the "web" hint.
# ─────────────────────────────────────────────────────────────────────


def _capture_redirect_uri_sent_to_token_endpoint() -> tuple[dict[str, list[str]], Iterator[None]]:
    """Patch ``exchange_code_for_tokens`` to route through a MockTransport
    that records the POSTed form body, so the test can assert on the exact
    ``redirect_uri`` Alpaca's token endpoint actually received.
    """
    received: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        from urllib.parse import parse_qs

        received.update(parse_qs(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "access_token": "tok-XXX",
                "refresh_token": "",
                "expires_in": 0,
                "token_type": "Bearer",
                "scope": "",
            },
        )

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

    def _install() -> Iterator[None]:
        alpaca_oauth.exchange_code_for_tokens = patched  # type: ignore[assignment]
        try:
            yield
        finally:
            alpaca_oauth.exchange_code_for_tokens = real_exchange  # type: ignore[assignment]

    return received, _install()


def _redirect_uri_from_authorize_url(authorize_url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(authorize_url).query)["redirect_uri"][0]


@pytestmark_crypto
def test_native_exchange_uses_default_redirect_uri_unchanged(client: TestClient) -> None:
    """No ``platform`` sent (today's only real client, the native app):
    the authorize URL AND the token exchange must both keep using the
    implicit native default — byte-for-byte unchanged from before the
    "web" redirect_uri plumbing was added.
    """
    received, install = _capture_redirect_uri_sent_to_token_endpoint()
    next(install)
    try:
        access = _login_and_get_access(client)
        started = client.post(
            "/api/v1/broker/connect/alpaca/start",
            headers=_bearer(access),
            json={"isPaper": True},
        ).json()
        authorize_redirect = _redirect_uri_from_authorize_url(started["authorizeUrl"])
        assert authorize_redirect == "autotrader://broker/callback"

        r = client.post(
            "/api/v1/broker/connect/alpaca/callback",
            headers=_bearer(access),
            json={"code": "abc", "state": started["state"]},
        )
        assert r.status_code == 200, r.text
    finally:
        try:
            next(install)
        except StopIteration:
            pass

    assert received["redirect_uri"] == [authorize_redirect]


@pytestmark_crypto
def test_web_exchange_uses_same_redirect_uri_as_authorize_url(client: TestClient) -> None:
    """``platform: "web"`` must produce an authorize_url whose redirect_uri
    EXACTLY matches what the token-exchange step sends — most OAuth2
    providers (Alpaca included) require the two to match, so a start/
    exchange mismatch here would pass every test that doesn't check this
    specifically while still failing for real against Alpaca's API.
    """
    received, install = _capture_redirect_uri_sent_to_token_endpoint()
    next(install)
    try:
        access = _login_and_get_access(client)
        started = client.post(
            "/api/v1/broker/connect/alpaca/start",
            headers=_bearer(access),
            json={"isPaper": True, "platform": "web"},
        ).json()
        authorize_redirect = _redirect_uri_from_authorize_url(started["authorizeUrl"])
        assert authorize_redirect == alpaca_oauth.default_web_redirect_uri()
        assert authorize_redirect != "autotrader://broker/callback"

        r = client.get(
            "/api/v1/broker/connect/alpaca/redirect",
            params={"code": "abc", "state": started["state"]},
        )
        assert r.status_code == 200, r.text
    finally:
        try:
            next(install)
        except StopIteration:
            pass

    assert received["redirect_uri"] == [authorize_redirect]


# ─────────────────────────────────────────────────────────────────────
# connectionSource — display-only field distinguishing env-bootstrapped
# connections from real OAuth ones (see app/services/broker/env_bootstrap.py)
# ─────────────────────────────────────────────────────────────────────


@pytestmark_crypto
def test_connections_response_flags_environment_vs_oauth_source(
    client: TestClient, mocked_token_endpoint: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.broker.env_bootstrap import ensure_env_broker_connection

    # A real OAuth connection reports "oauth".
    access = _login_and_get_access(client, "oauth-source-user@example.com")
    started = client.post(
        "/api/v1/broker/connect/alpaca/start",
        headers=_bearer(access),
        json={"isPaper": True},
    ).json()
    client.post(
        "/api/v1/broker/connect/alpaca/callback",
        headers=_bearer(access),
        json={"code": "abc", "state": started["state"]},
    )
    listed = client.get("/api/v1/broker/connections", headers=_bearer(access)).json()
    assert len(listed) == 1
    assert listed[0]["connectionSource"] == "oauth"

    # A second user, bootstrapped from process env keys, reports "environment".
    email = "env-source-user@example.com"
    challenge = client.post("/api/v1/auth/request-login", json={"email": email}).json()
    issued = client.post(
        "/api/v1/auth/verify",
        json={"email": email, "token": challenge["devToken"]},
    ).json()
    other_access, other_user_id = issued["accessToken"], issued["userId"]

    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    created = asyncio.run(ensure_env_broker_connection(other_user_id))
    assert created is True

    other_listed = client.get(
        "/api/v1/broker/connections", headers=_bearer(other_access)
    ).json()
    assert len(other_listed) == 1
    assert other_listed[0]["connectionSource"] == "environment"
