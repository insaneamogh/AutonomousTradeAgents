"""F1 regression — no endpoint may return another tenant's decisions.

Two authenticated users seed decisions with distinguishable symbols and
P&L, then each reads every aggregate endpoint. Nothing belonging to the
other user may appear in the response: not a symbol, not a bull/bear
string, not a fill price, not a realized-P&L figure, not a count.

The four in-memory-backed endpoints (/review/queue, /review/agreement,
/review/scorecard, /strategies/performance) are exercised over HTTP. The
three Postgres-only ones (/ghost/summary, /risk/vetoes,
/decisions/{id}/timeline) can't run without a database in CI, so their
scoping is pinned at the service layer instead — the query the builder
emits must carry a user_id predicate, and the biography builder must
refuse a row it doesn't own.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app
from app.services.auth_store import reset_auth_store_for_tests
from app.services.review_store import reset_review_store_for_tests
from trading_agents.memory import (
    DecisionEntry,
    get_decision_log,
    reset_memory_stores_for_tests,
)

# Alice's and Bob's marker values. Every assertion below is "the other
# user's marker must not appear anywhere in my response body".
ALICE_SYMBOL = "NVDA"
ALICE_PNL = 1234.56
ALICE_BULL = "ALICE-ONLY-BULL-CASE"

BOB_SYMBOL = "TSLA"
BOB_PNL = -987.65
BOB_BULL = "BOB-ONLY-BULL-CASE"


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_auth_store_for_tests()
    reset_review_store_for_tests()
    reset_memory_stores_for_tests()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _login(c: TestClient, email: str) -> tuple[str, str]:
    """Register + verify a real user. Returns (access_token, user_id)."""
    challenge = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    verified = c.post(
        "/api/v1/auth/verify",
        json={"email": email, "token": challenge["devToken"]},
    ).json()
    return verified["accessToken"], verified["userId"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed(user_id: str, symbol: str, pnl: float, bull: str) -> DecisionEntry:
    entry = DecisionEntry(
        user_id=user_id,
        symbol=symbol,
        horizon="short",
        triggered_at=datetime.now(UTC) - timedelta(hours=1),
        selected_strategy="momentum",
        selector_confidence=0.6,
        final_action="BUY",
        risk_approved=True,
        fill_qty=10,
        fill_avg_price=200.0,
        realized_pnl=pnl,
        raw_state={"proposal": {"qty": 10, "bull_case": bull, "bear_case": "bear"}},
    )
    return await get_decision_log().record(entry)


@pytest.fixture
def two_users(client: TestClient) -> dict[str, Any]:
    """Alice and Bob, each with one completed decision of their own."""
    import anyio

    alice_token, alice_id = _login(client, "alice-tenant@example.com")
    bob_token, bob_id = _login(client, "bob-tenant@example.com")
    assert alice_id != bob_id

    alice_decision = anyio.run(_seed, alice_id, ALICE_SYMBOL, ALICE_PNL, ALICE_BULL)
    bob_decision = anyio.run(_seed, bob_id, BOB_SYMBOL, BOB_PNL, BOB_BULL)

    return {
        "alice": {"token": alice_token, "id": alice_id, "decision": alice_decision},
        "bob": {"token": bob_token, "id": bob_id, "decision": bob_decision},
    }


# ─────────────────────────────────────────────────────────────────────
# /review/queue
# ─────────────────────────────────────────────────────────────────────


def test_review_queue_shows_only_own_decisions(
    client: TestClient, two_users: dict[str, Any]
) -> None:
    alice, bob = two_users["alice"], two_users["bob"]

    body = client.get("/api/v1/review/queue?windowDays=30", headers=_bearer(alice["token"])).json()
    ids = [i["decisionId"] for i in body["items"]]
    assert ids == [alice["decision"].id]
    assert body["totalInWindow"] == 1, "Bob's decision must not inflate Alice's counter"

    raw = client.get(
        "/api/v1/review/queue?windowDays=30", headers=_bearer(alice["token"])
    ).text
    for leak in (BOB_SYMBOL, BOB_BULL, str(BOB_PNL)):
        assert leak not in raw, f"{leak!r} leaked into Alice's queue"

    # And symmetrically for Bob.
    bob_raw = client.get(
        "/api/v1/review/queue?windowDays=30", headers=_bearer(bob["token"])
    ).text
    for leak in (ALICE_SYMBOL, ALICE_BULL, str(ALICE_PNL)):
        assert leak not in bob_raw, f"{leak!r} leaked into Bob's queue"


def test_cannot_grade_another_users_decision(
    client: TestClient, two_users: dict[str, Any]
) -> None:
    """Grading is an IDOR surface too — Bob's id must be ungradeable by Alice."""
    alice, bob = two_users["alice"], two_users["bob"]

    r = client.post(
        f"/api/v1/review/{bob['decision'].id}",
        json={"grade": "good"},
        headers=_bearer(alice["token"]),
    )
    assert r.status_code == 404

    # Alice can still grade her own.
    ok = client.post(
        f"/api/v1/review/{alice['decision'].id}",
        json={"grade": "good"},
        headers=_bearer(alice["token"]),
    )
    assert ok.status_code == 200, ok.text


# ─────────────────────────────────────────────────────────────────────
# /review/agreement + /review/scorecard
# ─────────────────────────────────────────────────────────────────────


def test_agreement_and_scorecard_count_only_own_reviews(
    client: TestClient, two_users: dict[str, Any]
) -> None:
    alice, bob = two_users["alice"], two_users["bob"]

    client.post(
        f"/api/v1/review/{alice['decision'].id}",
        json={"grade": "good"},
        headers=_bearer(alice["token"]),
    )
    client.post(
        f"/api/v1/review/{bob['decision'].id}",
        json={"grade": "bad"},
        headers=_bearer(bob["token"]),
    )

    alice_agreement = client.get(
        "/api/v1/review/agreement?windowDays=30", headers=_bearer(alice["token"])
    ).json()
    assert alice_agreement["totalReviewed"] == 1

    alice_scorecard = client.get(
        "/api/v1/review/scorecard?windowDays=180", headers=_bearer(alice["token"])
    ).json()
    assert sum(m["totalReviewed"] for m in alice_scorecard["months"]) == 1

    bob_scorecard = client.get(
        "/api/v1/review/scorecard?windowDays=180", headers=_bearer(bob["token"])
    ).json()
    assert sum(m["totalReviewed"] for m in bob_scorecard["months"]) == 1


# ─────────────────────────────────────────────────────────────────────
# /strategies/performance
# ─────────────────────────────────────────────────────────────────────


def test_strategy_performance_aggregates_only_own_trades(
    client: TestClient, two_users: dict[str, Any]
) -> None:
    alice, bob = two_users["alice"], two_users["bob"]

    def momentum_for(token: str) -> dict[str, Any]:
        body = client.get(
            "/api/v1/strategies/performance?windowDays=30", headers=_bearer(token)
        ).json()
        return next(s for s in body["strategies"] if s["strategyId"] == "momentum")

    a = momentum_for(alice["token"])
    assert a["decisionsInWindow"] == 1
    assert a["realizedPnl"] == pytest.approx(ALICE_PNL)
    assert a["wins"] == 1 and a["losses"] == 0

    b = momentum_for(bob["token"])
    assert b["decisionsInWindow"] == 1
    assert b["realizedPnl"] == pytest.approx(BOB_PNL)
    assert b["wins"] == 0 and b["losses"] == 1


def test_a_third_user_with_no_history_sees_nothing(client: TestClient) -> None:
    """Cold-start must not be seeded from other tenants' rows."""
    import anyio

    _, alice_id = _login(client, "alice-solo@example.com")
    anyio.run(_seed, alice_id, ALICE_SYMBOL, ALICE_PNL, ALICE_BULL)

    carol_token, _ = _login(client, "carol-solo@example.com")

    queue = client.get("/api/v1/review/queue", headers=_bearer(carol_token)).json()
    assert queue["items"] == []
    assert queue["totalInWindow"] == 0

    perf = client.get("/api/v1/strategies/performance", headers=_bearer(carol_token)).json()
    assert all(s["decisionsInWindow"] == 0 for s in perf["strategies"])
    assert all(s["realizedPnl"] == 0.0 for s in perf["strategies"])
    assert ALICE_SYMBOL not in client.get(
        "/api/v1/review/queue", headers=_bearer(carol_token)
    ).text


# ─────────────────────────────────────────────────────────────────────
# Postgres-only endpoints — scoping pinned at the service layer
# ─────────────────────────────────────────────────────────────────────


class _CapturingSession:
    """Records every statement the builder executes; returns no rows."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def __aenter__(self) -> _CapturingSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, stmt: Any) -> Any:
        self.statements.append(stmt)

        class _Result:
            def all(self) -> list[Any]:
                return []

            def scalars(self) -> Any:
                return self

        return _Result()


def _compiled(session: _CapturingSession) -> str:
    return " ".join(str(s) for s in session.statements)


@pytest.mark.parametrize("builder_name", ["build_ghost_summary", "build_veto_ledger"])
def test_ghost_builders_filter_on_user_id(
    monkeypatch: pytest.MonkeyPatch, builder_name: str
) -> None:
    """The emitted SQL must constrain agent_decisions.user_id."""
    import anyio
    from app.services import ghost_service

    session = _CapturingSession()
    monkeypatch.setattr(ghost_service, "async_session_factory", lambda: lambda: session)

    builder = getattr(ghost_service, builder_name)
    anyio.run(lambda: builder(30, user_id="11111111-1111-1111-1111-111111111111"))

    sql = _compiled(session)
    assert "agent_decisions.user_id" in sql, f"{builder_name} emitted an unscoped query"


def test_ghost_builders_return_empty_for_unknown_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed user id must degrade to "no rows", never to "every row"."""
    import anyio
    from app.services import ghost_service

    session = _CapturingSession()
    monkeypatch.setattr(ghost_service, "async_session_factory", lambda: lambda: session)

    summary = anyio.run(lambda: ghost_service.build_ghost_summary(30, user_id="not-a-uuid"))
    ledger = anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id="not-a-uuid"))

    assert summary.vetoed.count == 0 and summary.saved_usd == 0.0
    assert ledger.total_vetoes == 0 and ledger.rules == []
    assert session.statements == [], "an unknown tenant must not reach the database"


def test_biography_refuses_a_row_owned_by_another_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /decisions/{id}/timeline IDOR: ownership is checked first."""
    import uuid as _uuid

    import anyio
    from app.services import biography_service

    owner = _uuid.UUID("11111111-1111-1111-1111-111111111111")
    intruder = "22222222-2222-2222-2222-222222222222"
    decision_id = str(_uuid.uuid4())

    class _Row:
        user_id = owner

    class _Session(_CapturingSession):
        async def get(self, model: Any, pk: Any) -> Any:
            return _Row()

    monkeypatch.setattr(
        biography_service, "async_session_factory", lambda: lambda: _Session()
    )

    bio = anyio.run(
        lambda: biography_service.build_biography(decision_id, user_id=intruder)
    )
    assert bio is None, "another user's decision timeline must not be readable"


def test_biography_rejects_malformed_decision_id() -> None:
    import anyio
    from app.services.biography_service import build_biography

    assert anyio.run(lambda: build_biography("../../etc/passwd", user_id="whoever")) is None
