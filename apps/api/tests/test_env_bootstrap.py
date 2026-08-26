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
    skew a "keys absent" assertion.
    """
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)


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
    store = InMemoryBrokerStore()

    created = await ensure_env_broker_connection("user-1", store=store)
    assert created is True

    rows = await store.list_connections("user-1")
    assert len(rows) == 1
    row = rows[0]
    assert row.broker == "alpaca"
    assert row.is_paper is True
    assert row.live_trading_consent is False
    assert row.status == "active"
    assert row.account_number is None
    assert decrypt_from_storage(row.encrypted_access_token) == ALPACA_ENV_SENTINEL


@pytestmark_crypto
async def test_ensure_connection_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
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
