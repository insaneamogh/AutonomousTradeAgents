"""Postgres-store integration tests — opt-in via ``RUN_POSTGRES_TESTS=1``.

These exercise the round-trip from Protocol method calls down to real
SQL against a running Postgres + the migrations applied. They are gated
exactly like ``packages/engine/tests/test_reconciler.py``'s scaffold so
the default ``pytest`` run on a fresh laptop does not require docker.

To run:

    make infra-up && make migrate
    RUN_POSTGRES_TESTS=1 DATABASE_URL=postgresql+asyncpg://...@localhost:5432/autotrader \\
        pytest apps/api/tests/test_postgres_stores.py -v

Each test uses a unique email / token / device id so back-to-back runs
without a DB reset still pass. We don't tear down rows — Phase 4
hardening adds per-test rollback via a savepoint fixture.
"""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest


def _postgres_available() -> bool:
    if os.environ.get("RUN_POSTGRES_TESTS", "").strip().lower() not in ("1", "true", "yes"):
        return False
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return False
    if not os.environ.get("DATABASE_URL", "").strip():
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _postgres_available(),
    reason="Postgres tests opt-in via RUN_POSTGRES_TESTS=1 + DATABASE_URL set.",
)


# ─────────────────────────────────────────────────────────────────────
# PostgresAuthStore
# ─────────────────────────────────────────────────────────────────────


async def test_postgres_auth_store_upsert_and_get() -> None:
    from app.services.auth.postgres_auth_store import PostgresAuthStore

    store = PostgresAuthStore()
    email = f"auth-{secrets.token_hex(4)}@example.com"

    a = await store.upsert_user(email)
    assert a.email == email
    # Second upsert returns the SAME row id — idempotent.
    b = await store.upsert_user(email)
    assert b.id == a.id

    # get_by_email + get_by_id round-trip.
    by_email = await store.get_user_by_email(email)
    assert by_email is not None and by_email.id == a.id

    by_id = await store.get_user_by_id(a.id)
    assert by_id is not None and by_id.email == email


async def test_postgres_auth_store_session_rotate_then_revoke() -> None:
    from app.services.auth.postgres_auth_store import PostgresAuthStore

    store = PostgresAuthStore()
    user = await store.upsert_user(f"session-{secrets.token_hex(4)}@example.com")

    expires = datetime.now(timezone.utc) + timedelta(days=30)
    sess = await store.create_session(
        user_id=user.id,
        refresh_token_hash="scrypt$saltA$hashA",
        expires_at=expires,
        device_id="dev-1",
        device_label="Test device",
    )
    assert sess.refresh_token_hash == "scrypt$saltA$hashA"

    rotated = await store.rotate_session(
        sess.id, new_refresh_token_hash="scrypt$saltB$hashB"
    )
    assert rotated.refresh_token_hash == "scrypt$saltB$hashB"
    assert rotated.last_seen_at >= sess.created_at

    await store.revoke_session(sess.id)
    refreshed = await store.get_session(sess.id)
    assert refreshed is not None and refreshed.revoked_at is not None


async def test_postgres_auth_store_magic_link_single_use() -> None:
    from app.services.auth.postgres_auth_store import PostgresAuthStore

    store = PostgresAuthStore()
    email = f"magic-{secrets.token_hex(4)}@example.com"

    expires = datetime.now(timezone.utc) + timedelta(minutes=15)
    rec = await store.create_magic_link(
        email=email, token_hash="scrypt$s$h", expires_at=expires
    )
    pending = await store.find_unused_magic_link(email=email)
    assert any(p.id == rec.id for p in pending)

    await store.mark_magic_link_used(rec.id)
    after = await store.find_unused_magic_link(email=email)
    assert all(p.id != rec.id for p in after)


# ─────────────────────────────────────────────────────────────────────
# PostgresBrokerStore
# ─────────────────────────────────────────────────────────────────────


async def test_postgres_broker_store_upsert_list_revoke() -> None:
    from app.services.auth.postgres_auth_store import PostgresAuthStore
    from app.services.broker.postgres_broker_store import PostgresBrokerStore

    auth = PostgresAuthStore()
    user = await auth.upsert_user(f"broker-{secrets.token_hex(4)}@example.com")

    store = PostgresBrokerStore()
    rec = await store.upsert_connection(
        user_id=user.id,
        broker="alpaca",
        is_paper=True,
        account_number="PA-XYZ",
        encrypted_access_token="enc-access",
        encrypted_refresh_token="enc-refresh",
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert rec.status == "active"

    # Idempotent upsert on (user, broker, is_paper).
    rec2 = await store.upsert_connection(
        user_id=user.id,
        broker="alpaca",
        is_paper=True,
        account_number="PA-XYZ",
        encrypted_access_token="enc-access-rotated",
        encrypted_refresh_token="enc-refresh",
        access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert rec2.id == rec.id
    assert rec2.encrypted_access_token == "enc-access-rotated"

    listed = await store.list_connections(user.id)
    assert any(r.id == rec.id for r in listed)

    assert await store.revoke_connection(rec.id) is True
    fresh = await store.get_connection(rec.id)
    assert fresh is not None and fresh.status == "revoked"
    assert fresh.encrypted_access_token == ""


async def test_postgres_broker_store_auto_approve_consent_round_trip() -> None:
    """Mirrors ``live_trading_consent``'s own round trip: defaults False,
    the setter flips it, and it is independent of the live-trading flag."""
    from app.services.auth.postgres_auth_store import PostgresAuthStore
    from app.services.broker.postgres_broker_store import PostgresBrokerStore

    auth = PostgresAuthStore()
    user = await auth.upsert_user(f"auto-approve-{secrets.token_hex(4)}@example.com")

    store = PostgresBrokerStore()
    rec = await store.upsert_connection(
        user_id=user.id,
        broker="alpaca",
        is_paper=True,
        account_number="PA-AUTO",
        encrypted_access_token="enc-access",
        encrypted_refresh_token=None,
        access_token_expires_at=None,
    )
    assert rec.auto_approve_consent is False
    assert rec.live_trading_consent is False

    assert await store.set_auto_approve_consent(rec.id, enabled=True) is True
    fresh = await store.get_connection(rec.id)
    assert fresh is not None
    assert fresh.auto_approve_consent is True
    # Independent of the live-trading key — granting one must not grant the other.
    assert fresh.live_trading_consent is False

    assert await store.set_auto_approve_consent(rec.id, enabled=False) is True
    fresh2 = await store.get_connection(rec.id)
    assert fresh2 is not None and fresh2.auto_approve_consent is False

    # A revoked connection refuses the setter, same as set_live_consent.
    assert await store.revoke_connection(rec.id) is True
    assert await store.set_auto_approve_consent(rec.id, enabled=True) is False


# ─────────────────────────────────────────────────────────────────────
# PostgresNotificationStore
# ─────────────────────────────────────────────────────────────────────


async def test_postgres_notification_store_register_idempotent_and_revoke_by_token() -> None:
    from app.services.auth.postgres_auth_store import PostgresAuthStore
    from app.services.notifications.postgres_notification_store import PostgresNotificationStore

    auth = PostgresAuthStore()
    user = await auth.upsert_user(f"push-{secrets.token_hex(4)}@example.com")

    store = PostgresNotificationStore()
    token = f"ExponentPushToken[{secrets.token_hex(8)}]"

    a = await store.register_device(
        user_id=user.id, expo_push_token=token, platform="ios", label="iPhone"
    )
    b = await store.register_device(
        user_id=user.id, expo_push_token=token, platform="ios", label="iPhone 14 Pro"
    )
    # Same row, but label rotated.
    assert a.id == b.id
    assert b.label == "iPhone 14 Pro"

    actives = await store.list_active_devices(user.id)
    assert any(r.id == a.id for r in actives)

    await store.revoke_by_token(token)
    actives_after = await store.list_active_devices(user.id)
    assert not any(r.id == a.id for r in actives_after)


# ─────────────────────────────────────────────────────────────────────
# PostgresDecisionLog + PostgresStrategyConfidenceStore
# ─────────────────────────────────────────────────────────────────────


async def test_postgres_decision_log_pending_reflection_window() -> None:
    """``list_pending_reflection`` must gate on ``closed_at``, not
    ``triggered_at``.

    Regression coverage added alongside the 2026-09-01 fix: verified live
    against production that 6/6 real closed decisions (``triggered_at``
    ~117h in the past, ``closed_at`` ~48h in the past) had never once been
    picked up by Reflection, because the query filtered on ``triggered_at``
    — a column that only ever gets OLDER relative to "now", so once a
    decision has been open longer than the reflection job's ``since``
    window, filtering on ``triggered_at`` makes it unreachable FOREVER, not
    just late for one cycle. ``stale_trigger_recent_close`` below
    reproduces exactly that shape.

    (This test previously called ``list_pending_reflection()`` with no
    arguments at all, which raises ``TypeError`` — ``user_id`` became a
    required keyword-only argument in a later refactor and this
    ``RUN_POSTGRES_TESTS``-gated file was never run against that change.
    Fixed here too.)
    """
    from sqlalchemy import update

    from engine.db.models import AgentDecision
    from engine.db.session import async_session_factory
    from trading_agents.memory.decision_log import DecisionEntry
    from trading_agents.memory.postgres import (
        FIXTURE_USER_ID,
        PostgresDecisionLog,
    )

    log = PostgresDecisionLog()
    fixture_user = str(FIXTURE_USER_ID)
    now = datetime.now(timezone.utc)

    # Closed a moment ago, triggered a moment ago — the ordinary case.
    closed = DecisionEntry(
        user_id=fixture_user,
        symbol=f"PX{secrets.token_hex(3)}",
        horizon="short",
        regime="bull",
        selected_strategy="momentum",
        selector_confidence=0.6,
        selector_rationale="seed",
        final_action="BUY",
        risk_approved=True,
        technical_score=65.0,
        fundamental_score=58.0,
        macro_score=60.0,
        fill_qty=10,
        fill_avg_price=200.0,
        realized_pnl=120.0,
    )
    # Still open (no realized_pnl) — should NOT come back regardless.
    open_entry = DecisionEntry(
        user_id=fixture_user,
        symbol=f"OP{secrets.token_hex(3)}",
        horizon="short",
        regime="bull",
        selected_strategy="momentum",
        selector_confidence=0.6,
        selector_rationale="seed",
        final_action="BUY",
        risk_approved=True,
    )
    # THE regression shape: triggered long before the window, closed just
    # inside it. Must be included — this is what the old triggered_at-gated
    # query could never do once triggered_at aged past `since`.
    stale_trigger_recent_close = DecisionEntry(
        user_id=fixture_user,
        symbol=f"ST{secrets.token_hex(3)}",
        horizon="short",
        triggered_at=now - timedelta(hours=117),
        regime="bull",
        selected_strategy="momentum",
        selector_confidence=0.6,
        selector_rationale="seed",
        final_action="BUY",
        risk_approved=True,
        fill_qty=10,
        fill_avg_price=200.0,
        realized_pnl=55.79,
    )
    # Closed outside the window too — must stay excluded either way (not
    # just "closed_at unfiltered").
    long_closed = DecisionEntry(
        user_id=fixture_user,
        symbol=f"LC{secrets.token_hex(3)}",
        horizon="short",
        triggered_at=now - timedelta(days=10),
        regime="bull",
        selected_strategy="momentum",
        selector_confidence=0.6,
        selector_rationale="seed",
        final_action="BUY",
        risk_approved=True,
        fill_qty=10,
        fill_avg_price=200.0,
        realized_pnl=10.0,
    )

    rec_closed = await log.record(closed)
    rec_open = await log.record(open_entry)
    rec_stale = await log.record(stale_trigger_recent_close)
    rec_long_closed = await log.record(long_closed)

    # record() has no closed_at parameter (DecisionEntry doesn't carry
    # it — production's only writer, order_sync.py, stamps closed_at via
    # a raw UPDATE too). Same pattern here.
    async with async_session_factory()() as session:
        await session.execute(
            update(AgentDecision)
            .where(AgentDecision.id == uuid.UUID(rec_closed.id))
            .values(closed_at=now - timedelta(hours=1))
        )
        await session.execute(
            update(AgentDecision)
            .where(AgentDecision.id == uuid.UUID(rec_stale.id))
            .values(closed_at=now - timedelta(hours=2))
        )
        await session.execute(
            update(AgentDecision)
            .where(AgentDecision.id == uuid.UUID(rec_long_closed.id))
            .values(closed_at=now - timedelta(days=9))
        )
        await session.commit()

    pending = await log.list_pending_reflection(user_id=fixture_user)
    ids = [p.id for p in pending]
    assert rec_closed.id in ids
    assert rec_stale.id in ids, (
        "a decision triggered 117h ago but CLOSED 2h ago must still be "
        "reflected on — this is the exact production bug this test pins"
    )
    assert rec_open.id not in ids, "still-open (no realized_pnl) must be excluded"
    assert rec_long_closed.id not in ids, "closed outside the window must stay excluded"

    await log.mark_reviewed(rec_closed.id)
    await log.mark_reviewed(rec_stale.id)
    pending_after = await log.list_pending_reflection(user_id=fixture_user)
    remaining_ids = [p.id for p in pending_after]
    assert rec_closed.id not in remaining_ids
    assert rec_stale.id not in remaining_ids


async def test_postgres_confidence_store_clamps_delta() -> None:
    from trading_agents.memory.postgres import PostgresStrategyConfidenceStore
    from trading_agents.memory.strategy_confidence import (
        MAX_CONFIDENCE,
        MAX_CONFIDENCE_DELTA_PER_CYCLE,
    )

    store = PostgresStrategyConfidenceStore()
    # Use a non-PLAN id to keep test runs from drifting the seeded priors.
    test_id = f"_test_{secrets.token_hex(4)}"

    before = await store.get(test_id)
    after = await store.apply_delta(test_id, confidence_delta=1.0)
    # Delta is clamped to ±MAX_CONFIDENCE_DELTA_PER_CYCLE.
    assert (after.confidence - before.confidence) == pytest.approx(
        MAX_CONFIDENCE_DELTA_PER_CYCLE
    )

    # Saturate at MAX_CONFIDENCE on repeated nudges.
    for _ in range(20):
        await store.apply_delta(test_id, confidence_delta=0.10)
    final = await store.get(test_id)
    assert final.confidence == pytest.approx(MAX_CONFIDENCE)


# ─────────────────────────────────────────────────────────────────────
# Full lifespan boot — env-key Alpaca bootstrap under real Postgres
# ─────────────────────────────────────────────────────────────────────


async def test_env_broker_bootstrap_fires_with_reconciler_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard for the second hidden gate this fix closes.

    Before it, the env-key Alpaca bootstrap lived entirely inside
    ``if use_pg and enable_reconciler:`` in ``app.main.lifespan`` — so an
    operator running real Postgres with ``RECONCILER_ENABLED=0`` got the
    exact same silent no-bootstrap as MockStore despite having a real
    database and real Alpaca keys configured. It must now fire regardless,
    gated only by the keys being present.
    """
    from app.services.auth.auth_store import reset_auth_store_for_tests
    from app.services.auth.postgres_auth_store import PostgresAuthStore
    from app.services.broker.broker_store import reset_broker_store_for_tests
    from app.services.broker.postgres_broker_store import PostgresBrokerStore

    auth = PostgresAuthStore()
    user = await auth.upsert_user(f"env-bootstrap-{secrets.token_hex(4)}@example.com")

    monkeypatch.setenv("USE_POSTGRES", "1")
    monkeypatch.setenv("RECONCILER_ENABLED", "0")
    monkeypatch.setenv("ALPACA_API_KEY", "pg-lifespan-test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "pg-lifespan-test-secret")
    monkeypatch.delenv("ALPACA_BASE_URL", raising=False)

    # The auth/broker store singletons pick Mock vs. Postgres once, on
    # first use, from USE_POSTGRES — reset so they re-resolve against the
    # env just set, instead of reusing whatever the rest of this pytest
    # session already cached (almost certainly MockStore).
    reset_auth_store_for_tests()
    reset_broker_store_for_tests()
    try:
        from fastapi.testclient import TestClient

        from app.main import app

        # `with` form: forces the real startup/shutdown lifespan to run,
        # unlike a bare TestClient(app).
        with TestClient(app):
            pass
    finally:
        reset_auth_store_for_tests()
        reset_broker_store_for_tests()

    store = PostgresBrokerStore()
    rows = await store.list_connections(user.id)
    alpaca_rows = [r for r in rows if r.broker == "alpaca" and r.status == "active"]
    assert len(alpaca_rows) == 1
    assert alpaca_rows[0].is_paper is True
    assert alpaca_rows[0].live_trading_consent is False


# Keep imports alive for linters even when Postgres isn't running.
_ = (uuid, timedelta)
