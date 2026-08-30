"""Tests for ``app.services.broker.env_bootstrap`` — previously zero
coverage despite backing the "link Alpaca paper from environment API
keys" auto-connect that every deployment relies on.

Three layers, each with its own section below:

  1. ``env_keys_present`` / live-endpoint refusal — pure predicates, no
     crypto involved.
  2. ``ensure_env_broker_connection`` — the per-user primitive. Gated on
     real ``cryptography`` being installed, same as ``test_broker.py``,
     since the happy path must actually encrypt the sentinel to succeed.
  3. ``bootstrap_env_broker_connections`` — the boot-time fan-out that
     decides WHICH users to run (1) for depending on ``use_pg``, plus a
     full ``lifespan`` boot proving the un-gating fix end-to-end.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.services.auth.auth_store import (  # noqa: E402
    FIXTURE_USER_ID,
    reset_auth_store_for_tests,
)
from app.services.broker import crypto  # noqa: E402
from app.services.broker.broker_store import (  # noqa: E402
    InMemoryBrokerStore,
    reset_broker_store_for_tests,
)
from app.services.broker.crypto import decrypt_from_storage, encrypt_for_storage  # noqa: E402
from app.services.broker.env_bootstrap import (  # noqa: E402
    ALPACA_ENV_SENTINEL,
    bootstrap_env_broker_connections,
    ensure_env_broker_connection,
    env_keys_present,
)

pytestmark_crypto = pytest.mark.skipif(
    not crypto.is_available(),
    reason="cryptography not installed — env-bootstrap round-trip skipped",
)


@pytest.fixture(autouse=True)
def _clean_alpaca_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test opts into Alpaca env keys explicitly — starting from a
    clean slate means a key leaked from the real shell environment can't
    skew a "keys absent" assertion. Also clears the allowlist vars so
    every test's use of them is explicit, not inherited from the real
    shell environment or a prior test's monkeypatch.
    """
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)
    monkeypatch.delenv("ALPACA_ENV_CONNECTION_USER_IDS", raising=False)
    monkeypatch.delenv("AGENT_CRON_USER_ID", raising=False)


# ─────────────────────────────────────────────────────────────────────
# env_keys_present / live-endpoint refusal
# ─────────────────────────────────────────────────────────────────────


def test_env_keys_present_false_when_both_unset() -> None:
    assert env_keys_present() is False


def test_env_keys_present_false_when_only_one_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    assert env_keys_present() is False


def test_env_keys_present_false_when_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace-only values don't count — mirrors the ``.strip()`` in
    the implementation guarding against an env var set to "" or " ".
    """
    monkeypatch.setenv("ALPACA_API_KEY", "   ")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    assert env_keys_present() is False


def test_env_keys_present_true_when_both_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    assert env_keys_present() is True


async def test_ensure_connection_noop_without_keys() -> None:
    store = InMemoryBrokerStore()
    assert await ensure_env_broker_connection("user-1", store=store) is False
    assert await store.list_connections("user-1") == []


async def test_ensure_connection_refuses_live_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """ALPACA_BASE_URL naming a live endpoint must never auto-connect —
    connecting real money is an explicit, human, per-connection decision.
    """
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://api.alpaca.markets")
    store = InMemoryBrokerStore()
    assert await ensure_env_broker_connection("user-1", store=store) is False
    assert await store.list_connections("user-1") == []


@pytestmark_crypto
async def test_ensure_connection_unset_base_url_defaults_to_paper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ALPACA_BASE_URL at all must default to paper, not refuse — an
    ambiguous/unset base URL must never be read as "live".
    """
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    monkeypatch.setenv("AGENT_CRON_USER_ID", "user-1")  # this test's own concern is paper-vs-live, not the allowlist
    store = InMemoryBrokerStore()
    assert await ensure_env_broker_connection("user-1", store=store) is True
    rows = await store.list_connections("user-1")
    assert rows[0].is_paper is True


# ─────────────────────────────────────────────────────────────────────
# ensure_env_broker_connection — happy path / idempotency / non-clobber
# ─────────────────────────────────────────────────────────────────────


@pytestmark_crypto
async def test_ensure_connection_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    monkeypatch.setenv("AGENT_CRON_USER_ID", "user-1")  # this test's own concern is the row shape, not the allowlist
    store = InMemoryBrokerStore()

    created = await ensure_env_broker_connection("user-1", store=store)
    assert created is True

    rows = await store.list_connections("user-1")
    assert len(rows) == 1
    row = rows[0]
    assert row.broker == "alpaca"
    assert row.is_paper is True
    assert row.live_trading_consent is False
    assert row.auto_approve_consent is False
    assert row.status == "active"
    assert row.account_number is None
    assert decrypt_from_storage(row.encrypted_access_token) == ALPACA_ENV_SENTINEL


@pytestmark_crypto
async def test_ensure_connection_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    monkeypatch.setenv("AGENT_CRON_USER_ID", "user-1")  # this test's own concern is idempotency, not the allowlist
    store = InMemoryBrokerStore()

    first = await ensure_env_broker_connection("user-1", store=store)
    second = await ensure_env_broker_connection("user-1", store=store)
    assert first is True
    assert second is False  # already active — no duplicate row

    rows = await store.list_connections("user-1")
    assert len(rows) == 1


@pytestmark_crypto
async def test_ensure_connection_does_not_clobber_existing_oauth_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The most important regression guard in this file: the bootstrap now
    runs UNCONDITIONALLY on every boot (that's the whole point of this
    change), so it must never overwrite a real OAuth-established
    connection with the env sentinel.
    """
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    monkeypatch.setenv("AGENT_CRON_USER_ID", "user-1")  # this test's own concern is non-clobbering, not the allowlist
    store = InMemoryBrokerStore()

    real_token = encrypt_for_storage("real-oauth-access-token")
    pre_existing = await store.upsert_connection(
        user_id="user-1",
        broker="alpaca",
        is_paper=True,
        account_number="PA-REAL-001",
        encrypted_access_token=real_token,
        encrypted_refresh_token=None,
        access_token_expires_at=None,
    )

    result = await ensure_env_broker_connection("user-1", store=store)
    assert result is False

    rows = await store.list_connections("user-1")
    assert len(rows) == 1
    assert rows[0].id == pre_existing.id
    assert rows[0].account_number == "PA-REAL-001"
    assert decrypt_from_storage(rows[0].encrypted_access_token) == "real-oauth-access-token"


# ─────────────────────────────────────────────────────────────────────
# bootstrap_env_broker_connections — the boot-time fan-out
# ─────────────────────────────────────────────────────────────────────


async def test_bootstrap_noop_without_keys() -> None:
    store = InMemoryBrokerStore()
    created, considered = await bootstrap_env_broker_connections(use_pg=False, store=store)
    assert (created, considered) == (0, 0)
    created, considered = await bootstrap_env_broker_connections(use_pg=True, store=store)
    assert (created, considered) == (0, 0)


@pytestmark_crypto
async def test_bootstrap_mock_mode_targets_only_fixture_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``use_pg=False`` can't enumerate MockAuthStore's users (no public
    "list all" accessor — ``_users`` is a private dict) — it must target
    exactly ``FIXTURE_USER_ID``, and nothing else.
    """
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    # Matches the real .env.example convention: AGENT_CRON_USER_ID ==
    # FIXTURE_USER_ID, so a correctly-configured single-tenant deployment
    # keeps un-gating the mock-mode fixture user unchanged.
    monkeypatch.setenv("AGENT_CRON_USER_ID", FIXTURE_USER_ID)
    store = InMemoryBrokerStore()

    created, considered = await bootstrap_env_broker_connections(use_pg=False, store=store)
    assert (created, considered) == (1, 1)

    fixture_rows = await store.list_connections(FIXTURE_USER_ID)
    assert len(fixture_rows) == 1
    assert fixture_rows[0].broker == "alpaca"

    # A stand-in "someone else" proves the fan-out touched only the fixture id.
    assert await store.list_connections("some-other-real-user-id") == []


@pytestmark_crypto
async def test_bootstrap_mock_mode_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    monkeypatch.setenv("AGENT_CRON_USER_ID", FIXTURE_USER_ID)
    store = InMemoryBrokerStore()

    first = await bootstrap_env_broker_connections(use_pg=False, store=store)
    second = await bootstrap_env_broker_connections(use_pg=False, store=store)
    assert first == (1, 1)
    assert second == (0, 1)  # still considers the one fixture user; creates nothing new


# ─────────────────────────────────────────────────────────────────────
# Full lifespan boot — proves the un-gating fix end-to-end under MockStore
# ─────────────────────────────────────────────────────────────────────


@pytestmark_crypto
def test_lifespan_boot_links_fixture_user_under_mockstore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``USE_POSTGRES`` unset (MockStore — the shipped default) plus Alpaca
    env keys present: the fixture user must end up with an active Alpaca
    connection after boot.

    This is the actual regression the fix closes. Before it, the only call
    site lived inside ``if use_pg and enable_reconciler:`` in
    ``app.main.lifespan`` — under MockStore that branch never runs, so the
    bootstrap silently never fired here despite the keys being configured.

    Uses ``with TestClient(app) as c:`` deliberately (not a bare
    ``TestClient(app)``, which every other test in this suite uses) — only
    the context-manager form actually drives FastAPI's real
    startup/shutdown lifespan events.
    """
    monkeypatch.delenv("USE_POSTGRES", raising=False)
    monkeypatch.delenv("RECONCILER_ENABLED", raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "lifespan-test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "lifespan-test-secret")
    monkeypatch.setenv("AGENT_CRON_USER_ID", FIXTURE_USER_ID)

    reset_auth_store_for_tests()
    reset_broker_store_for_tests()

    from app.main import app
    from app.services.broker.broker_store import get_broker_store

    with TestClient(app):
        pass  # entering/exiting the context is what runs startup + shutdown

    store = get_broker_store()
    rows = asyncio.run(store.list_connections(FIXTURE_USER_ID))
    assert len(rows) == 1
    assert rows[0].broker == "alpaca"
    assert rows[0].status == "active"
    assert rows[0].is_paper is True

    reset_auth_store_for_tests()
    reset_broker_store_for_tests()


# ─────────────────────────────────────────────────────────────────────
# Per-login catch-up — a user who arrives AFTER boot must not be skipped,
# but ONLY when they are on the owner allowlist. This is the exact
# docs/PLAN_MULTI_TENANT.md §1 fix: before it, EVERY new signup got the
# operator's own write-capable Alpaca connection, on their first login.
# ─────────────────────────────────────────────────────────────────────


@pytestmark_crypto
def test_new_signup_does_not_get_the_env_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE critical regression test for docs/PLAN_MULTI_TENANT.md §1.

    Before the allowlist gate, a brand-new signup — no exotic path, just
    the ordinary magic-link flow — was silently handed a live connection
    to the OPERATOR's own Alpaca account (the one being traded/scored):
    they could approve a pending proposal, close a position, revoke the
    connection, or arm auto-approve, all on someone else's book. Break:
    remove the allowlist check from ``ensure_env_broker_connection`` and
    this signup gets a connection again.
    """
    monkeypatch.setenv("ALPACA_API_KEY", "post-boot-test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "post-boot-test-secret")
    monkeypatch.delenv("USE_POSTGRES", raising=False)
    # Deliberately NOT setting ALPACA_ENV_CONNECTION_USER_IDS or
    # AGENT_CRON_USER_ID — this is what "a deployment with the keys set
    # but no allowlist configured yet" looks like, and it must fail
    # closed (nobody gets the connection), not fail open.

    reset_auth_store_for_tests()
    reset_broker_store_for_tests()

    from app.main import app
    from app.services.broker.broker_store import get_broker_store

    with TestClient(app) as c:
        # Boot already ran (with zero non-fixture users) by the time this
        # request fires — exactly the "empty database at boot" scenario
        # that made a real deployment hit this on its very first real user.
        r = c.post("/api/v1/auth/request-login", json={"email": "judge@example.com"})
        assert r.status_code == 200
        token = r.json()["devToken"]
        r2 = c.post(
            "/api/v1/auth/verify",
            json={"email": "judge@example.com", "token": token},
        )
        assert r2.status_code == 200, r2.text
        user_id = r2.json()["userId"]

    store = get_broker_store()
    rows = asyncio.run(store.list_connections(user_id))
    assert rows == []

    reset_auth_store_for_tests()
    reset_broker_store_for_tests()


@pytestmark_crypto
def test_login_catchup_still_fires_for_an_allowlisted_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catch-up mechanism itself (not just the allowlist gate) must
    still work — this is ``test_owner_still_gets_the_env_connection`` from
    docs/PLAN_MULTI_TENANT.md §5, the one that catches over-narrowing the
    fix into breaking the operator's own login. Exercises the exact
    function ``routers/auth.py``'s verify/Google handlers call, with a
    user id that IS on the allowlist (mirroring the real
    AGENT_CRON_USER_ID == FIXTURE_USER_ID convention), rather than the
    full random-UUID signup flow used above — the point here is proving
    the call site still works, not re-deriving a fresh id to allowlist
    mid-request.
    """
    monkeypatch.setenv("ALPACA_API_KEY", "post-boot-test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "post-boot-test-secret")
    monkeypatch.setenv("AGENT_CRON_USER_ID", FIXTURE_USER_ID)

    reset_broker_store_for_tests()

    from app.routers.auth import _bootstrap_broker_for_new_login
    from app.services.broker.broker_store import get_broker_store

    asyncio.run(_bootstrap_broker_for_new_login(FIXTURE_USER_ID))

    store = get_broker_store()
    rows = asyncio.run(store.list_connections(FIXTURE_USER_ID))
    assert len(rows) == 1
    assert rows[0].broker == "alpaca"
    assert rows[0].status == "active"

    reset_broker_store_for_tests()


def test_refresh_does_not_trigger_broker_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/refresh`` must NOT call the bootstrap — an existing session
    already had its chance at login time, and paying this check on every
    ~15-minute token refresh for every active user would be pure waste."""
    from app.main import app

    reset_auth_store_for_tests()
    reset_broker_store_for_tests()
    monkeypatch.setenv("ALPACA_API_KEY", "refresh-test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "refresh-test-secret")

    calls: list[str] = []

    async def _spy(user_id: str, **_kw: object) -> bool:
        calls.append(user_id)
        return False

    monkeypatch.setattr(
        "app.services.broker.env_bootstrap.ensure_env_broker_connection", _spy
    )

    client = TestClient(app)
    r = client.post("/api/v1/auth/request-login", json={"email": "refresher@example.com"})
    token = r.json()["devToken"]
    verified = client.post(
        "/api/v1/auth/verify",
        json={"email": "refresher@example.com", "token": token},
    ).json()
    calls.clear()  # only care about what /refresh does, not /verify

    r2 = client.post(
        "/api/v1/auth/refresh", json={"refreshToken": verified["refreshToken"]}
    )
    assert r2.status_code == 200, r2.text
    assert calls == []

    reset_auth_store_for_tests()
    reset_broker_store_for_tests()


# ─────────────────────────────────────────────────────────────────────
# _env_connection_allowlist — the resolution logic in isolation
# ─────────────────────────────────────────────────────────────────────


def test_allowlist_empty_when_neither_var_set() -> None:
    """Fail closed: no explicit list AND no AGENT_CRON_USER_ID fallback
    means nobody gets the env connection — an operator who forgets to set
    either var gets a locked-down deployment, never an accidentally-open
    one."""
    from app.services.broker.env_bootstrap import _env_connection_allowlist

    assert _env_connection_allowlist() == set()


def test_allowlist_falls_back_to_agent_cron_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CRON_USER_ID", "cron-user")
    from app.services.broker.env_bootstrap import _env_connection_allowlist

    assert _env_connection_allowlist() == {"cron-user"}


def test_allowlist_explicit_var_wins_over_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CRON_USER_ID", "cron-user")
    monkeypatch.setenv("ALPACA_ENV_CONNECTION_USER_IDS", "owner-1, owner-2")
    from app.services.broker.env_bootstrap import _env_connection_allowlist

    assert _env_connection_allowlist() == {"owner-1", "owner-2"}


def test_allowlist_ignores_blank_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_ENV_CONNECTION_USER_IDS", "owner-1,, ,owner-2,")
    from app.services.broker.env_bootstrap import _env_connection_allowlist

    assert _env_connection_allowlist() == {"owner-1", "owner-2"}


# ─────────────────────────────────────────────────────────────────────
# bootstrap_env_broker_connections(use_pg=True) — the real multi-user
# fan-out. Requires a real Postgres (RUN_POSTGRES_TESTS=1) because this
# path enumerates real `User` rows — a mocked session would only prove
# the mock was called correctly, not that the real SELECT + allowlist
# combination behaves as intended.
# ─────────────────────────────────────────────────────────────────────


def _postgres_available() -> bool:
    if os.environ.get("RUN_POSTGRES_TESTS", "").strip().lower() not in ("1", "true", "yes"):
        return False
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("DATABASE_URL", "").strip())


@pytest.mark.skipif(
    not _postgres_available(),
    reason="Postgres tests opt-in via RUN_POSTGRES_TESTS=1 + DATABASE_URL set.",
)
@pytestmark_crypto
async def test_boot_sweep_only_attaches_allowlisted_users_among_several(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/PLAN_MULTI_TENANT.md §5's ``test_boot_sweep_respects_the_allowlist``.

    Three real ``User`` rows exist at boot; only one is allowlisted. Break
    by dropping the allowlist check from ``ensure_env_broker_connection``
    (or by adding a SEPARATE, un-synchronised check directly inside
    ``bootstrap_env_broker_connections`` that later drifts from it) and
    all three get connections instead of one.
    """
    import secrets

    from app.services.auth.postgres_auth_store import PostgresAuthStore

    monkeypatch.setenv("ALPACA_API_KEY", "boot-sweep-test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "boot-sweep-test-secret")

    auth_store = PostgresAuthStore()
    owner = await auth_store.upsert_user(f"owner-{secrets.token_hex(4)}@example.com")
    stranger_a = await auth_store.upsert_user(f"stranger-a-{secrets.token_hex(4)}@example.com")
    stranger_b = await auth_store.upsert_user(f"stranger-b-{secrets.token_hex(4)}@example.com")

    monkeypatch.setenv("ALPACA_ENV_CONNECTION_USER_IDS", owner.id)

    conn_store = InMemoryBrokerStore()
    created, considered = await bootstrap_env_broker_connections(use_pg=True, store=conn_store)

    assert created == 1
    assert considered >= 3  # at least our three; other tests' rows may also exist

    assert len(await conn_store.list_connections(owner.id)) == 1
    assert await conn_store.list_connections(stranger_a.id) == []
    assert await conn_store.list_connections(stranger_b.id) == []
