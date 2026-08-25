"""P3 security work-package tests — auth surface.

One test per audit finding so a regression names the finding it broke:

  F8   /auth/verify is rate-limited, and the scrypt candidate loop runs
       off the event loop (unauthenticated CPU-amplification DoS).
  F2   logout with NO body revokes the caller's session via the access
       token's ``sid`` — it used to return revoked=False and do nothing.
  F5   JWT_SECRET_PREVIOUS verifies (never signs), so rotating the signing
       key doesn't log every live session out.
  F14/15/16  transport headers: CSP / Permissions-Policy / COOP on every
       response, no-store on token-bearing paths, non-credentialed CORS.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from datetime import UTC

from app.core.config import get_settings
from app.main import app
from app.services.auth.auth_store import reset_auth_store_for_tests
from app.services.auth.jwt_service import mint_access, verify_access
from app.services.auth.rate_limit import (
    VERIFY_EMAIL_LIMIT,
    reset_rate_limit_for_tests,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_auth_store_for_tests()
    reset_rate_limit_for_tests()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _login(client: TestClient, email: str) -> dict[str, str]:
    token = client.post(
        "/api/v1/auth/request-login", json={"email": email}
    ).json()["devToken"]
    return client.post(
        "/api/v1/auth/verify", json={"email": email, "token": token}
    ).json()


# ─────────────────────────────────────────────────────────────────────
# F8 — /auth/verify throttling
# ─────────────────────────────────────────────────────────────────────


def test_verify_is_rate_limited(client: TestClient) -> None:
    """Each verify costs one scrypt per outstanding link for that email.
    Unthrottled, that is a CPU amplifier for an unauthenticated caller."""
    email = "verify-flood@example.com"
    client.post("/api/v1/auth/request-login", json={"email": email})

    for _ in range(VERIFY_EMAIL_LIMIT):
        r = client.post(
            "/api/v1/auth/verify", json={"email": email, "token": "wrong-token"}
        )
        assert r.status_code == 401, r.text

    blocked = client.post(
        "/api/v1/auth/verify", json={"email": email, "token": "wrong-token"}
    )
    assert blocked.status_code == 429


async def test_magic_link_hashing_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``verify_magic_link`` must hand the scrypt comparisons to a worker
    thread — 50ms of memory-hard hashing per candidate on the event loop
    stalls every other in-flight request."""
    import threading
    from datetime import datetime, timedelta

    from app.services.auth import auth as auth_svc
    from app.services.auth.auth_store import MockAuthStore
    from app.services.auth.jwt_service import hash_token, new_opaque_token

    loop_thread = threading.get_ident()
    hashing_threads: list[int] = []
    real_verify = auth_svc.verify_token_hash

    def spy(token: str, *, stored: str) -> bool:
        hashing_threads.append(threading.get_ident())
        return real_verify(token, stored=stored)

    monkeypatch.setattr(auth_svc, "verify_token_hash", spy)

    store = MockAuthStore()
    raw = new_opaque_token()
    await store.create_magic_link(
        email="thread@example.com",
        token_hash=hash_token(raw),
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )

    issued = await auth_svc.verify_magic_link(
        email="thread@example.com",
        token=raw,
        store=store,
        secret="test-secret-that-is-long-enough-32bytes",
    )

    assert issued.access_token
    assert hashing_threads, "no hashing happened — the test is not exercising it"
    assert all(t != loop_thread for t in hashing_threads)


# ─────────────────────────────────────────────────────────────────────
# F2 — logout without a body actually revokes
# ─────────────────────────────────────────────────────────────────────


def test_logout_without_body_revokes_the_access_token_session(
    client: TestClient,
) -> None:
    issued = _login(client, "bodyless-logout@example.com")
    hdr = {"Authorization": f"Bearer {issued['accessToken']}"}
    assert client.get("/api/v1/auth/me", headers=hdr).status_code == 200

    out = client.post("/api/v1/auth/logout", headers=hdr)
    assert out.status_code == 200
    assert out.json()["revoked"] is True, "logout with no body must not be a no-op"

    # Session-bound: the same access token is dead immediately.
    assert client.get("/api/v1/auth/me", headers=hdr).status_code == 401


def test_logout_then_refresh_reuse_is_401(client: TestClient) -> None:
    """Logout by ``sid`` kills the refresh token too — the session row is
    what both tokens hang off."""
    issued = _login(client, "logout-refresh@example.com")
    hdr = {"Authorization": f"Bearer {issued['accessToken']}"}

    assert client.post("/api/v1/auth/logout", headers=hdr).json()["revoked"] is True

    reused = client.post(
        "/api/v1/auth/refresh", json={"refreshToken": issued["refreshToken"]}
    )
    assert reused.status_code == 401


# ─────────────────────────────────────────────────────────────────────
# F5 — verify-only key rotation
# ─────────────────────────────────────────────────────────────────────


def test_previous_secret_verifies_but_never_signs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = "old-signing-secret-at-least-32-chars!"
    new = "new-signing-secret-at-least-32-chars!"
    token = mint_access(secret=old, user_id="u-1", session_id="s-1")

    # Without the rotation env, the old token is dead.
    monkeypatch.delenv("JWT_SECRET_PREVIOUS", raising=False)
    from app.services.auth.jwt_service import TokenError

    with pytest.raises(TokenError):
        verify_access(secret=new, token=token)

    # With it, the pre-rotation session keeps working.
    monkeypatch.setenv("JWT_SECRET_PREVIOUS", old)
    claims = verify_access(secret=new, token=token)
    assert claims.sub == "u-1" and claims.sid == "s-1"

    # Newly minted tokens use the CURRENT secret only — a holder of the old
    # key cannot verify them.
    fresh = mint_access(secret=new, user_id="u-1", session_id="s-1")
    with pytest.raises(TokenError):
        verify_access(secret=old, token=fresh)


def test_rotation_still_rejects_garbage_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JWT_SECRET_PREVIOUS", "old-secret,older-secret")
    from app.services.auth.jwt_service import TokenError

    good = mint_access(secret="current-secret", user_id="u-2")
    tampered = good.rsplit(".", 1)[0] + ".AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    with pytest.raises(TokenError):
        verify_access(secret="current-secret", token=tampered)


def test_production_config_rejects_useless_previous_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import production_config_problems

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("JWT_SECRET_PREVIOUS", "x" * 40)  # same as current
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", "a-real-fernet-key")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    get_settings.cache_clear()
    problems = production_config_problems(get_settings())
    assert any("JWT_SECRET_PREVIOUS" in p for p in problems)


# ─────────────────────────────────────────────────────────────────────
# F14/F15/F16 — transport headers
# ─────────────────────────────────────────────────────────────────────


def test_security_headers_snapshot(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    h = r.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer"
    assert h["cross-origin-opener-policy"] == "same-origin"
    assert "default-src 'none'" in h["content-security-policy"]
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert "camera=()" in h["permissions-policy"]


def test_token_bearing_paths_are_no_store(client: TestClient) -> None:
    r = client.post(
        "/api/v1/auth/request-login", json={"email": "cache-me-not@example.com"}
    )
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"

    # Non-token paths are left alone (no needless cache-busting).
    assert client.get("/health").headers.get("cache-control") is None


def test_cors_is_not_credentialed(client: TestClient) -> None:
    """Auth is Bearer-only. A credentialed CORS policy would let a hostile
    page ride an ambient session it should never have."""
    r = client.options(
        "/api/v1/auth/request-login",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.headers.get("access-control-allow-credentials") is None
