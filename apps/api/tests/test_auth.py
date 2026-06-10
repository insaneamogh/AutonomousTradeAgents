"""Auth flow tests.

End-to-end against the FastAPI TestClient with the in-memory MockAuthStore.
Covers:
  - Magic-link request → verify happy path
  - Refresh-token rotation
  - Refresh-token replay → session revoked
  - Expired access token rejected
  - DEV_AUTH_BYPASS lets the existing fixture-user paths through
  - DEV_AUTH_BYPASS=0 + no bearer → 401
  - /auth/me requires real auth (no bypass)
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

# Set the bypass default BEFORE importing app, since middleware reads env
# at request time but we want a clean default for the suite.
os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.core.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth_store import reset_auth_store_for_tests  # noqa: E402
from app.services.jwt_service import mint, mint_access  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """Each test gets a fresh in-memory auth store."""
    reset_auth_store_for_tests()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ─────────────────────────────────────────────────────────────────────
# Magic-link happy path
# ─────────────────────────────────────────────────────────────────────


def test_magic_link_request_then_verify_issues_tokens(client: TestClient) -> None:
    r = client.post("/api/v1/auth/request-login", json={"email": "alice@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert "devToken" in body and body["devToken"], "dev mode should return raw token"
    assert "expiresAt" in body

    token = body["devToken"]
    r2 = client.post(
        "/api/v1/auth/verify",
        json={"email": "alice@example.com", "token": token},
    )
    assert r2.status_code == 200, r2.text
    issued = r2.json()
    assert issued["email"] == "alice@example.com"
    assert issued["accessToken"]
    assert issued["refreshToken"]
    assert issued["accessExpiresInSeconds"] > 0
    assert issued["refreshExpiresInSeconds"] > issued["accessExpiresInSeconds"]


def test_verify_with_wrong_token_is_401(client: TestClient) -> None:
    client.post("/api/v1/auth/request-login", json={"email": "bob@example.com"})
    r = client.post(
        "/api/v1/auth/verify",
        json={"email": "bob@example.com", "token": "not-the-real-token"},
    )
    assert r.status_code == 401


def test_verify_locks_token_after_first_use(client: TestClient) -> None:
    client.post("/api/v1/auth/request-login", json={"email": "carol@example.com"})
    token = client.post(
        "/api/v1/auth/request-login", json={"email": "carol@example.com"}
    ).json()["devToken"]

    ok = client.post(
        "/api/v1/auth/verify",
        json={"email": "carol@example.com", "token": token},
    )
    assert ok.status_code == 200

    # Same token a second time — must fail.
    replay = client.post(
        "/api/v1/auth/verify",
        json={"email": "carol@example.com", "token": token},
    )
    assert replay.status_code == 401


# ─────────────────────────────────────────────────────────────────────
# Refresh rotation
# ─────────────────────────────────────────────────────────────────────


def _login(client: TestClient, email: str = "user@example.com") -> dict[str, str]:
    token = client.post("/api/v1/auth/request-login", json={"email": email}).json()["devToken"]
    return client.post(
        "/api/v1/auth/verify", json={"email": email, "token": token}
    ).json()


def test_refresh_rotates_tokens(client: TestClient) -> None:
    issued = _login(client)
    r = client.post("/api/v1/auth/refresh", json={"refreshToken": issued["refreshToken"]})
    assert r.status_code == 200
    rotated = r.json()
    assert rotated["refreshToken"] != issued["refreshToken"], "refresh must rotate"
    assert rotated["accessToken"] != issued["accessToken"]


def test_refresh_replay_revokes_session(client: TestClient) -> None:
    """Use the OLD refresh after a rotation. Server should revoke the
    session entirely — both old and new refreshes are dead.
    """
    issued = _login(client, "dana@example.com")
    rotated = client.post(
        "/api/v1/auth/refresh", json={"refreshToken": issued["refreshToken"]}
    ).json()

    # Replay the original (now-invalidated) refresh.
    replay = client.post("/api/v1/auth/refresh", json={"refreshToken": issued["refreshToken"]})
    assert replay.status_code == 401

    # And the rotated one should now also be dead.
    second_try = client.post(
        "/api/v1/auth/refresh", json={"refreshToken": rotated["refreshToken"]}
    )
    assert second_try.status_code == 401


# ─────────────────────────────────────────────────────────────────────
# Access tokens + protected routes
# ─────────────────────────────────────────────────────────────────────


def test_authed_access_token_lets_through_me(client: TestClient) -> None:
    issued = _login(client, "eve@example.com")
    r = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {issued['accessToken']}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "eve@example.com"


def test_me_refuses_dev_bypass(client: TestClient) -> None:
    """No Authorization header — DEV_AUTH_BYPASS gives the fixture user
    elsewhere, but /auth/me explicitly requires a real session.
    """
    r = client.get("/api/v1/auth/me")
    assert r.status_code == 401


def test_expired_access_token_is_401(client: TestClient) -> None:
    settings = get_settings()
    # Mint a token that's already expired (negative lifetime).
    expired = mint(
        secret=settings.jwt_secret,
        user_id="00000000-0000-0000-0000-000000000001",
        typ="access",
        lifetime=timedelta(seconds=-10),
    )
    r = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert r.status_code == 401


def test_access_token_wrong_typ_is_401(client: TestClient) -> None:
    """An access endpoint must reject a token minted with typ=refresh."""
    settings = get_settings()
    refresh_shaped = mint(
        secret=settings.jwt_secret,
        user_id="00000000-0000-0000-0000-000000000001",
        typ="refresh",
        lifetime=timedelta(minutes=15),
        session_id="abc",
    )
    r = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {refresh_shaped}"},
    )
    assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────────
# DEV_AUTH_BYPASS gate
# ─────────────────────────────────────────────────────────────────────


def test_dev_bypass_lets_account_through_without_bearer(client: TestClient) -> None:
    """With DEV_AUTH_BYPASS=1 (default in tests), legacy mobile calls still work."""
    r = client.get("/api/v1/account")
    assert r.status_code == 200


def test_dev_bypass_disabled_requires_bearer(monkeypatch) -> None:
    """Flip the bypass off → /account must 401 without a Bearer header."""
    monkeypatch.setenv("DEV_AUTH_BYPASS", "0")
    # Re-import the app so the env change takes effect? Actually the middleware
    # reads env per-request, so the existing app is fine.
    c = TestClient(app)
    r = c.get("/api/v1/account")
    assert r.status_code == 401


def test_dev_bypass_disabled_still_accepts_real_bearer(monkeypatch, client: TestClient) -> None:
    """With bypass off, a valid access token still works."""
    issued = _login(client, "frank@example.com")
    monkeypatch.setenv("DEV_AUTH_BYPASS", "0")
    c = TestClient(app)
    r = c.get(
        "/api/v1/account",
        headers={"Authorization": f"Bearer {issued['accessToken']}"},
    )
    assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────────
# JWT primitives — algorithm-confusion guard
# ─────────────────────────────────────────────────────────────────────


def test_tampered_header_is_rejected() -> None:
    """The HS256 guard refuses any header that isn't byte-equal to our HS256 declaration."""
    settings = get_settings()
    valid = mint_access(secret=settings.jwt_secret, user_id="00000000-0000-0000-0000-000000000001")
    parts = valid.split(".")
    # Substitute an "alg: none" header (the classic JWT downgrade attack).
    import base64
    import json

    none_header = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "typ": "JWT"}, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode()
    tampered = f"{none_header}.{parts[1]}.{parts[2]}"

    from app.services.jwt_service import TokenError, verify_access

    with pytest.raises(TokenError):
        verify_access(secret=settings.jwt_secret, token=tampered)
