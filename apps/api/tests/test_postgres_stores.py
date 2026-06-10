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
    from app.services.postgres_auth_store import PostgresAuthStore

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
    from app.services.postgres_auth_store import PostgresAuthStore

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
    from app.services.postgres_auth_store import PostgresAuthStore

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
    from app.services.postgres_auth_store import PostgresAuthStore
    from app.services.postgres_broker_store import PostgresBrokerStore

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


# ─────────────────────────────────────────────────────────────────────
# PostgresNotificationStore
# ─────────────────────────────────────────────────────────────────────


async def test_postgres_notification_store_register_idempotent_and_revoke_by_token() -> None:
    from app.services.postgres_auth_store import PostgresAuthStore
    from app.services.postgres_notification_store import PostgresNotificationStore

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
    from trading_agents.memory.decision_log import DecisionEntry
    from trading_agents.memory.postgres import (
        FIXTURE_USER_ID,
        PostgresDecisionLog,
    )

    log = PostgresDecisionLog()
    fixture_user = str(FIXTURE_USER_ID)

    # One closed, ready-for-review row.
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
    # One still-open row in the same window — should NOT come back.
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
    rec_closed = await log.record(closed)
    rec_open = await log.record(open_entry)

    pending = await log.list_pending_reflection()
    ids = [p.id for p in pending]
    assert rec_closed.id in ids
    assert rec_open.id not in ids

    await log.mark_reviewed(rec_closed.id)
    pending_after = await log.list_pending_reflection()
    assert rec_closed.id not in [p.id for p in pending_after]


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


# Keep imports alive for linters even when Postgres isn't running.
_ = (uuid, timedelta)
