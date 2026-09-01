"""Auth + production-hardening tests — the fail-closed guards from the audit.

Covers:
  - "live" counts as production (C4 root cause: the leak was an inline
    prod-check that omitted it).
  - DEV_AUTH_BYPASS is force-disabled in production regardless of the env
    var (C2).
  - production_config_problems flags default/missing secrets (C1/C5/CORS).
  - Access token stops working immediately after logout — session binding
    (H2/E6).
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.core.config import get_settings, production_config_problems  # noqa: E402
from app.main import app  # noqa: E402
from app.middleware.auth import _dev_bypass_enabled  # noqa: E402
from app.services.auth.auth_store import reset_auth_store_for_tests  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    reset_auth_store_for_tests()
    get_settings.cache_clear()
    yield
    # Restore a clean settings cache so env flips don't leak into later tests.
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ── Config guards ────────────────────────────────────────────────────


def test_live_env_counts_as_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "live")
    get_settings.cache_clear()
    assert get_settings().is_production is True


def test_dev_bypass_forced_off_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("DEV_AUTH_BYPASS", "1")  # even explicitly ON
    get_settings.cache_clear()
    assert _dev_bypass_enabled() is False


def test_dev_bypass_off_by_default_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """F3: the bypass is explicit opt-in. It used to default ON outside
    production, and "staging" is not in _PRODUCTION_ENVS — so a staging box
    resolved every unauthenticated request to the fixture user."""
    for env in ("local", "staging", "dev"):
        monkeypatch.setenv("ENV", env)
        monkeypatch.delenv("DEV_AUTH_BYPASS", raising=False)
        get_settings.cache_clear()
        assert _dev_bypass_enabled() is False, env


def test_dev_bypass_explicit_opt_in_still_works_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENV", "local")
    monkeypatch.setenv("DEV_AUTH_BYPASS", "1")
    get_settings.cache_clear()
    assert _dev_bypass_enabled() is True


def test_production_config_flags_insecure_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "change-me-locally-32-bytes-min")  # default
    monkeypatch.delenv("BROKER_TOKEN_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("CORS_ORIGINS", "*")  # wildcard → empty effective in prod
    get_settings.cache_clear()
    problems = production_config_problems(get_settings())
    assert any("JWT_SECRET" in p for p in problems)
    assert any("BROKER_TOKEN_ENCRYPTION_KEY" in p for p in problems)
    assert any("CORS_ORIGINS" in p for p in problems)


def test_production_config_clean_when_all_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setenv("BROKER_TOKEN_ENCRYPTION_KEY", "a-real-fernet-key")
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.com")
    monkeypatch.delenv("DEV_AUTH_BYPASS", raising=False)
    get_settings.cache_clear()
    assert production_config_problems(get_settings()) == []


def test_local_env_has_no_config_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "local")
    get_settings.cache_clear()
    assert production_config_problems(get_settings()) == []


# ── Session binding: logout kills the access token ───────────────────


def test_access_token_revoked_immediately_after_logout(client: TestClient) -> None:
    email = "revoke-me@example.com"
    challenge = client.post("/api/v1/auth/request-login", json={"email": email}).json()
    issued = client.post(
        "/api/v1/auth/verify", json={"email": email, "token": challenge["devToken"]}
    ).json()
    access = issued["accessToken"]
    refresh = issued["refreshToken"]
    hdr = {"Authorization": f"Bearer {access}"}

    # Access works before logout.
    assert client.get("/api/v1/auth/me", headers=hdr).status_code == 200

    # Log out (revokes the session).
    out = client.post("/api/v1/auth/logout", headers=hdr, json={"refreshToken": refresh})
    assert out.status_code == 200 and out.json()["revoked"] is True

    # SAME access token is now rejected — session-bound, not just TTL-bound.
    assert client.get("/api/v1/auth/me", headers=hdr).status_code == 401


def test_logout_ignores_foreign_refresh_token(client: TestClient) -> None:
    """User A cannot revoke via a refresh token that isn't theirs."""
    a = "user-a@example.com"
    b = "user-b@example.com"
    a_ch = client.post("/api/v1/auth/request-login", json={"email": a}).json()
    a_tok = client.post(
        "/api/v1/auth/verify", json={"email": a, "token": a_ch["devToken"]}
    ).json()
    b_ch = client.post("/api/v1/auth/request-login", json={"email": b}).json()
    b_tok = client.post(
        "/api/v1/auth/verify", json={"email": b, "token": b_ch["devToken"]}
    ).json()

    # A authenticates but submits B's refresh token → refused, B stays valid.
    out = client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {a_tok['accessToken']}"},
        json={"refreshToken": b_tok["refreshToken"]},
    )
    assert out.json()["revoked"] is False
    # B's access token still works.
    assert (
        client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {b_tok['accessToken']}"},
        ).status_code
        == 200
    )


# ── Concurrency: refresh CAS + magic-link single-use claim (H1/M1) ────


async def test_rotate_session_is_compare_and_swap() -> None:
    from datetime import datetime, timedelta, timezone

    from app.services.auth.auth_store import MockAuthStore

    store = MockAuthStore()
    sess = await store.create_session(
        user_id="u1",
        refresh_token_hash="H0",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    # First rotation with the correct expected hash wins.
    won = await store.rotate_session(
        sess.id, new_refresh_token_hash="H1", expected_current_hash="H0"
    )
    assert won is not None and won.refresh_token_hash == "H1"
    # A second rotation still expecting H0 misses (a concurrent refresh
    # already moved it) → None.
    missed = await store.rotate_session(
        sess.id, new_refresh_token_hash="H2", expected_current_hash="H0"
    )
    assert missed is None
    # Bootstrap form (no expected) is unconditional.
    boot = await store.rotate_session(sess.id, new_refresh_token_hash="H3")
    assert boot is not None and boot.refresh_token_hash == "H3"


async def test_concurrent_refresh_with_same_token_ends_with_one_winner() -> None:
    """H1, service-level: two callers racing the SAME refresh token through
    the full ``auth_svc.refresh()`` — not just the store's CAS in isolation
    (see ``test_rotate_session_is_compare_and_swap`` above).

    ``MockAuthStore.get_session`` used to hand back the live, shared
    ``SessionRecord`` instance. ``auth.refresh()`` holds that reference in a
    local var and reads ``.refresh_token_hash`` twice — once to validate the
    presented token, once (after a real suspend: ``asyncio.to_thread``'s
    scrypt verify) as the CAS's ``expected_current_hash``. A concurrent
    winner's rotation lands on that SAME object in between, so the loser's
    second read silently picked up the NEW hash and the CAS spuriously
    matched a moving target — both callers walked away with a valid token
    pair instead of exactly one.
    """
    from datetime import datetime, timedelta, timezone

    from app.services.auth import auth as auth_svc
    from app.services.auth.auth_store import MockAuthStore
    from app.services.auth.jwt_service import hash_token, mint_refresh

    store = MockAuthStore()
    secret = "test-secret-that-is-long-enough-32bytes"
    user = await store.upsert_user("racing-refresh@example.com")
    session = await store.create_session(
        user_id=user.id,
        refresh_token_hash="placeholder",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )
    raw_refresh = mint_refresh(secret=secret, user_id=user.id, session_id=session.id)
    await store.rotate_session(session.id, new_refresh_token_hash=hash_token(raw_refresh))

    first, second = await asyncio.gather(
        auth_svc.refresh(refresh_token=raw_refresh, store=store, secret=secret),
        auth_svc.refresh(refresh_token=raw_refresh, store=store, secret=secret),
        return_exceptions=True,
    )

    winners = [r for r in (first, second) if not isinstance(r, Exception)]
    losers = [r for r in (first, second) if isinstance(r, Exception)]

    assert len(winners) == 1, (first, second)
    assert len(losers) == 1, (first, second)
    assert isinstance(losers[0], auth_svc.RefreshError)
    assert losers[0].code == "superseded"

    # Whichever won, a detected race revokes the session outright.
    final = await store.get_session(session.id)
    assert final is not None and final.revoked_at is not None


async def test_mark_magic_link_used_claims_once() -> None:
    from datetime import datetime, timedelta, timezone

    from app.services.auth.auth_store import MockAuthStore

    store = MockAuthStore()
    link = await store.create_magic_link(
        email="c@example.com",
        token_hash="h",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    assert await store.mark_magic_link_used(link.id) is True  # winner
    assert await store.mark_magic_link_used(link.id) is False  # already claimed


def test_second_verify_of_same_link_is_rejected(client: TestClient) -> None:
    email = "single-use@example.com"
    ch = client.post("/api/v1/auth/request-login", json={"email": email}).json()
    token = ch["devToken"]
    first = client.post("/api/v1/auth/verify", json={"email": email, "token": token})
    assert first.status_code == 200
    # Replaying the exact same link → 401 (claimed once).
    second = client.post("/api/v1/auth/verify", json={"email": email, "token": token})
    assert second.status_code == 401
