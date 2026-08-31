"""Review-route tests.

Six failure modes + a happy path:
  1. Queue excludes still-open decisions (no realized_pnl).
  2. Queue excludes already-graded decisions.
  3. Grade upsert is idempotent on (decision_id, operator) — re-POSTing
     overwrites notes/grade, doesn't 4xx.
  4. Grade on unknown decision → 404.
  5. Grade on still-open decision → 404.
  6. Agreement stat: good ↔ positive direction = agreement; skip
     excluded from the denominator.
  7. GET /review/* is gated by DEV_AUTH_BYPASS-or-real-bearer (uses
     get_current_user — so the operator sees an empty queue under bypass
     instead of a 401). POST /review/{id} (grading) is DIFFERENT: it uses
     require_real_auth (docs/IMPL_DEMO_SESSION.md §3 — grading pollutes
     reflection/calibration data, so a read-only demo session must not
     reach it), so every test that grades a decision authenticates with a
     real magic-link login instead of relying on the bypass fixture user.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app  # noqa: E402
from app.services.auth.auth_store import (  # noqa: E402
    FIXTURE_USER_ID,
    reset_auth_store_for_tests,
)
from app.services.council.review_store import reset_review_store_for_tests  # noqa: E402
from trading_agents.memory import (  # noqa: E402
    DecisionEntry,
    get_confidence_store,
    get_decision_log,
    reset_memory_stores_for_tests,
)

# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_auth_store_for_tests()
    reset_review_store_for_tests()
    reset_memory_stores_for_tests()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _login(client: TestClient, email: str) -> tuple[str, str]:
    """Register + verify a real user (magic-link). Returns (access_token,
    user_id) — needed for POST /review/{id}, which requires real auth."""
    challenge = client.post("/api/v1/auth/request-login", json={"email": email}).json()
    verified = client.post(
        "/api/v1/auth/verify", json={"email": email, "token": challenge["devToken"]}
    ).json()
    return verified["accessToken"], verified["userId"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_completed(
    symbol: str, strategy: str, pnl: float, *, user_id: str = FIXTURE_USER_ID
) -> DecisionEntry:
    """Record one completed decision into the in-memory log.

    Decisions are tenant-scoped, so a seed needs an owner — default is the
    DEV_AUTH_BYPASS fixture user, i.e. whoever these tests authenticate as.
    """
    log = get_decision_log()
    entry = DecisionEntry(
        user_id=user_id,
        symbol=symbol,
        horizon="short",
        triggered_at=datetime.now(timezone.utc),
        selected_strategy=strategy,
        selector_confidence=0.6,
        final_action="BUY",
        risk_approved=True,
        fill_qty=10,
        fill_avg_price=200.0,
        realized_pnl=pnl,
        raw_state={
            "proposal": {
                "qty": 10,
                "bull_case": f"{symbol} momentum strong",
                "bear_case": "Risk-off compresses fast",
            }
        },
    )
    asyncio.run(log.record(entry))
    return entry


def _seed_open(symbol: str, *, user_id: str = FIXTURE_USER_ID) -> DecisionEntry:
    """Record one open decision (no realized_pnl)."""
    log = get_decision_log()
    entry = DecisionEntry(
        user_id=user_id,
        symbol=symbol,
        horizon="short",
        triggered_at=datetime.now(timezone.utc),
        selected_strategy="momentum",
        selector_confidence=0.6,
        final_action="BUY",
        risk_approved=True,
    )
    asyncio.run(log.record(entry))
    return entry


# ─────────────────────────────────────────────────────────────────────
# Queue
# ─────────────────────────────────────────────────────────────────────


def test_queue_excludes_open_decisions(client: TestClient) -> None:
    closed = _seed_completed("NVDA", "momentum", 120.0)
    _seed_open("AAPL")  # still open — should NOT appear in queue

    r = client.get("/api/v1/review/queue?windowDays=30")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [i["decisionId"] for i in body["items"]]
    assert closed.id in ids
    # Open decision is excluded.
    assert len(body["items"]) == 1


def test_queue_excludes_already_graded(client: TestClient) -> None:
    token, uid = _login(client, "queue-graded@example.com")
    headers = _bearer(token)
    a = _seed_completed("NVDA", "momentum", 100.0, user_id=uid)
    b = _seed_completed("AAPL", "momentum", -50.0, user_id=uid)

    # Grade `a` → should drop from queue.
    g = client.post(f"/api/v1/review/{a.id}", json={"grade": "good"}, headers=headers)
    assert g.status_code == 200, g.text

    r = client.get("/api/v1/review/queue?windowDays=30", headers=headers)
    ids = [i["decisionId"] for i in r.json()["items"]]
    assert a.id not in ids
    assert b.id in ids


def test_queue_progress_counter(client: TestClient) -> None:
    """Header shows graded N of total M progress."""
    token, uid = _login(client, "queue-progress@example.com")
    headers = _bearer(token)
    a = _seed_completed("NVDA", "momentum", 100.0, user_id=uid)
    _seed_completed("AAPL", "momentum", -50.0, user_id=uid)
    client.post(f"/api/v1/review/{a.id}", json={"grade": "good"}, headers=headers)

    body = client.get("/api/v1/review/queue", headers=headers).json()
    assert body["totalInWindow"] == 2
    assert body["gradedInWindow"] == 1


# ─────────────────────────────────────────────────────────────────────
# Grade upsert
# ─────────────────────────────────────────────────────────────────────


def test_grade_upsert_overwrites(client: TestClient) -> None:
    """Second POST for same (decision, operator) updates the grade +
    notes instead of 4xx-ing.
    """
    token, uid = _login(client, "grade-upsert@example.com")
    headers = _bearer(token)
    a = _seed_completed("NVDA", "momentum", 100.0, user_id=uid)
    first = client.post(
        f"/api/v1/review/{a.id}", json={"grade": "good", "notes": "trend held"}, headers=headers
    ).json()
    second = client.post(
        f"/api/v1/review/{a.id}",
        json={"grade": "bad", "notes": "actually weak entry"},
        headers=headers,
    ).json()

    assert first["id"] == second["id"]
    assert second["grade"] == "bad"
    assert "weak entry" in (second["notes"] or "")


def test_grade_unknown_decision_is_404(client: TestClient) -> None:
    token, _uid = _login(client, "grade-unknown@example.com")
    r = client.post(
        "/api/v1/review/dec-does-not-exist", json={"grade": "good"}, headers=_bearer(token)
    )
    assert r.status_code == 404


def test_grade_open_decision_is_404(client: TestClient) -> None:
    """Can't grade a trade that hasn't closed yet."""
    token, uid = _login(client, "grade-open@example.com")
    open_d = _seed_open("META", user_id=uid)
    r = client.post(
        f"/api/v1/review/{open_d.id}", json={"grade": "good"}, headers=_bearer(token)
    )
    assert r.status_code == 404
    assert "realized_pnl" in r.json()["detail"]


def test_grade_validates_enum(client: TestClient) -> None:
    token, uid = _login(client, "grade-enum@example.com")
    a = _seed_completed("NVDA", "momentum", 50.0, user_id=uid)
    r = client.post(f"/api/v1/review/{a.id}", json={"grade": "maybe"}, headers=_bearer(token))
    assert r.status_code == 422  # Pydantic Literal validation


# ─────────────────────────────────────────────────────────────────────
# Agreement stat
# ─────────────────────────────────────────────────────────────────────


def test_agreement_stat_counts_only_non_skip(client: TestClient) -> None:
    """``skip`` is excluded from the agreement denominator."""
    # Nudge momentum's confidence positive so the direction is 'positive'.
    confidence_store = get_confidence_store()
    asyncio.run(confidence_store.apply_delta("momentum", confidence_delta=0.10))

    token, uid = _login(client, "agreement-skip@example.com")
    headers = _bearer(token)
    good_match = _seed_completed("NVDA", "momentum", 100.0, user_id=uid)
    bad_disagree = _seed_completed("AAPL", "momentum", -100.0, user_id=uid)
    skipped = _seed_completed("TSLA", "momentum", -10.0, user_id=uid)

    client.post(f"/api/v1/review/{good_match.id}", json={"grade": "good"}, headers=headers)
    client.post(f"/api/v1/review/{bad_disagree.id}", json={"grade": "bad"}, headers=headers)
    client.post(f"/api/v1/review/{skipped.id}", json={"grade": "skip"}, headers=headers)

    body = client.get("/api/v1/review/agreement?windowDays=30", headers=headers).json()
    # Reviewed 3, but skip is excluded from agreement.
    # good+positive (agreement: 1) ; bad+positive (disagreement: 0)
    # → 1 / 2 = 50%.
    assert body["totalReviewed"] == 3
    assert body["agreementPct"] == pytest.approx(50.0, abs=0.01)


def test_agreement_empty_when_no_reviews(client: TestClient) -> None:
    body = client.get("/api/v1/review/agreement?windowDays=30").json()
    assert body["totalReviewed"] == 0
    assert body["agreementPct"] == 0.0
    assert body["buckets"] == []


# ─────────────────────────────────────────────────────────────────────
# Auth surface
# ─────────────────────────────────────────────────────────────────────


def test_review_routes_use_dev_bypass(client: TestClient) -> None:
    """get_current_user → DEV_AUTH_BYPASS=1 should let the fixture user
    hit the queue without a Bearer header. (Sensitive routes like
    /orders/execute use require_real_auth instead.)
    """
    r = client.get("/api/v1/review/queue")
    assert r.status_code == 200
