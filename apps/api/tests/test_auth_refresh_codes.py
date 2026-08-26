"""Machine-readable ``code`` on a failed ``POST /auth/refresh``.

Part of the "I keep getting logged out" resilience fix: the mobile client
needs to tell "this credential is truly dead" (revoked / invalid /
superseded) apart from "the backend doesn't currently recognize this
session" (not found) so it doesn't wipe a refresh token that a later
backend restore could still redeem. See ``app.services.auth.auth.RefreshError``
and the ``REFRESH_CODE_*`` constants.

Every existing test_auth*.py suite already covers the plain 401 status
code for these same failure paths — this file only adds the ``code`` field
assertions on top, so nothing there needed to change.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.core.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services.auth import auth as auth_svc  # noqa: E402
from app.services.auth.auth_store import MockAuthStore, reset_auth_store_for_tests  # noqa: E402
from app.services.auth.jwt_service import hash_token, mint_access, mint_refresh  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_auth_store_for_tests()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _login(client: TestClient, email: str) -> dict[str, str]:
    token = client.post("/api/v1/auth/request-login", json={"email": email}).json()["devToken"]
    return client.post("/api/v1/auth/verify", json={"email": email, "token": token}).json()


# ─────────────────────────────────────────────────────────────────────
# HTTP-level: reachable through the public /auth/refresh surface
# ─────────────────────────────────────────────────────────────────────


def test_refresh_of_revoked_session_carries_session_revoked_code(client: TestClient) -> None:
    issued = _login(client, "revoked-code@example.com")
    hdr = {"Authorization": f"Bearer {issued['accessToken']}"}
    assert client.post("/api/v1/auth/logout", headers=hdr).json()["revoked"] is True

    r = client.post("/api/v1/auth/refresh", json={"refreshToken": issued["refreshToken"]})
    assert r.status_code == 401
    assert r.json()["code"] == "session_revoked"
    # `detail` stays a plain human string — every other auth error already
    # returns one, and the mobile client only special-cases `code`.
    assert isinstance(r.json()["detail"], str)


def test_refresh_replay_carries_superseded_code(client: TestClient) -> None:
    issued = _login(client, "superseded-code@example.com")
    client.post("/api/v1/auth/refresh", json={"refreshToken": issued["refreshToken"]})

    # Replay the OLD (now-rotated-away) refresh token.
    r = client.post("/api/v1/auth/refresh", json={"refreshToken": issued["refreshToken"]})
    assert r.status_code == 401
    assert r.json()["code"] == "superseded"


def test_refresh_wrong_token_typ_carries_token_invalid_code(client: TestClient) -> None:
    """An access-typed token presented to /refresh is a malformed credential,
    not an unrecognized session — token_invalid, not session_not_found."""
    settings = get_settings()
    access_shaped = mint_access(
        secret=settings.jwt_secret, user_id="00000000-0000-0000-0000-000000000001"
    )
    r = client.post("/api/v1/auth/refresh", json={"refreshToken": access_shaped})
    assert r.status_code == 401
    assert r.json()["code"] == "token_invalid"


def test_refresh_garbage_token_carries_token_invalid_code(client: TestClient) -> None:
    r = client.post("/api/v1/auth/refresh", json={"refreshToken": "not-a-jwt-at-all"})
    assert r.status_code == 401
    assert r.json()["code"] == "token_invalid"


def test_refresh_unknown_session_carries_session_not_found_code(client: TestClient) -> None:
    """A well-formed, correctly-signed refresh JWT whose session id was
    never created (e.g. wiped server-side data) — the credential ISN'T
    necessarily dead, the backend just doesn't currently know it."""
    settings = get_settings()
    orphaned = mint_refresh(
        secret=settings.jwt_secret,
        user_id="00000000-0000-0000-0000-000000000001",
        session_id="session-that-was-never-created",
    )
    r = client.post("/api/v1/auth/refresh", json={"refreshToken": orphaned})
    assert r.status_code == 401
    assert r.json()["code"] == "session_not_found"


# ─────────────────────────────────────────────────────────────────────
# Service-level: session_expired isn't reachable through the public HTTP
# surface within a test's lifetime (refresh sessions live 30 days), so we
# construct the expired-session state directly against the store, the same
# way test_auth_hardening.py's test_rotate_session_is_compare_and_swap does.
# ─────────────────────────────────────────────────────────────────────


async def test_refresh_of_expired_session_raises_session_expired_code() -> None:
    store = MockAuthStore()
    secret = "test-secret-that-is-long-enough-32bytes"
    user = await store.upsert_user("expired-session@example.com")

    session = await store.create_session(
        user_id=user.id,
        refresh_token_hash="placeholder",
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),  # already expired
    )
    raw_refresh = mint_refresh(secret=secret, user_id=user.id, session_id=session.id)
    await store.rotate_session(session.id, new_refresh_token_hash=hash_token(raw_refresh))

    with pytest.raises(auth_svc.RefreshError) as exc_info:
        await auth_svc.refresh(refresh_token=raw_refresh, store=store, secret=secret)
    assert exc_info.value.code == "session_expired"
