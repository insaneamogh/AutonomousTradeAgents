"""Refusal Ledger market split: "US" (USD) vs "IN" (INR), never summed.

Same convention as test_veto_ledger_aggregation.py: no live database, the
session is faked and the emitted SQL is inspected. The WHERE clause is
compiled with literal binds so the assertions read the real predicate.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.middleware.auth import AuthedUser, get_current_user
from app.routers import insights
from app.services.council import ghost_service

USER_ID = "11111111-1111-1111-1111-111111111111"


class _Result:
    def all(self) -> list[Any]:
        return []

    def scalars(self) -> _Result:
        return self


class _CapturingSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def __aenter__(self) -> _CapturingSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        return _Result()


def _sql(session: _CapturingSession) -> list[str]:
    return [
        str(s.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for s in session.statements
    ]


def _where(sql: str) -> str:
    return sql.split("WHERE", 1)[1]


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> _CapturingSession:
    s = _CapturingSession()
    monkeypatch.setattr(ghost_service, "async_session_factory", lambda: lambda: s)
    return s


def _assert_us_filter(where: str) -> None:
    assert "NOT (agent_decisions.symbol LIKE" in where and "NSE:%" in where


def _assert_in_filter(where: str) -> None:
    assert "agent_decisions.symbol LIKE" in where and "NSE:%" in where
    assert "NOT (agent_decisions.symbol" not in where


# ── filter selection ────────────────────────────────────────────────


def test_default_market_excludes_nse(session: _CapturingSession) -> None:
    anyio.run(lambda: ghost_service.build_ghost_summary(30, user_id=USER_ID))
    anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID))
    anyio.run(lambda: ghost_service.build_veto_exemplar("r", user_id=USER_ID))
    sqls = _sql(session)
    assert len(sqls) == 4  # summary, veto, trim, exemplar
    for sql in sqls:
        _assert_us_filter(_where(sql))


def test_in_market_selects_only_nse(session: _CapturingSession) -> None:
    anyio.run(lambda: ghost_service.build_ghost_summary(30, user_id=USER_ID, market="IN"))
    anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID, market="IN"))
    anyio.run(lambda: ghost_service.build_veto_exemplar("r", user_id=USER_ID, market="IN"))
    sqls = _sql(session)
    assert len(sqls) == 4
    for sql in sqls:
        _assert_in_filter(_where(sql))
        assert "agent_decisions.user_id" in sql  # tenant scoping still applies


def test_all_users_sentinel_still_market_filtered() -> None:
    us = ghost_service._tenant_filters(ghost_service.ALL_USERS, "US")
    inr = ghost_service._tenant_filters(ghost_service.ALL_USERS, "IN")
    assert len(us) == len(inr) == 1
    assert str(us[0].compile(dialect=postgresql.dialect())) != str(
        inr[0].compile(dialect=postgresql.dialect())
    )


# ── currency / market on the result ─────────────────────────────────


@pytest.mark.parametrize(("market", "currency"), [("US", "USD"), ("IN", "INR")])
def test_currency_and_market_on_results(
    session: _CapturingSession, market: str, currency: str
) -> None:
    summary = anyio.run(lambda: ghost_service.build_ghost_summary(30, user_id=USER_ID, market=market))
    ledger = anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID, market=market))
    assert (summary.market, summary.currency) == (market, currency)
    assert (ledger.market, ledger.currency) == (market, currency)


@pytest.mark.parametrize(("market", "currency"), [("US", "USD"), ("IN", "INR")])
def test_unknown_tenant_still_carries_currency(market: str, currency: str) -> None:
    summary = anyio.run(
        lambda: ghost_service.build_ghost_summary(30, user_id="not-a-uuid", market=market)
    )
    ledger = anyio.run(
        lambda: ghost_service.build_veto_ledger(30, user_id="not-a-uuid", market=market)
    )
    assert (summary.market, summary.currency) == (market, currency)
    assert (ledger.market, ledger.currency) == (market, currency)


def test_default_is_us_usd(session: _CapturingSession) -> None:
    summary = anyio.run(lambda: ghost_service.build_ghost_summary(30, user_id=USER_ID))
    assert (summary.market, summary.currency) == ("US", "USD")


# ── validation ──────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["EU", "us", "", "NSE"])
def test_bad_market_raises_value_error(session: _CapturingSession, bad: str) -> None:
    with pytest.raises(ValueError):
        anyio.run(lambda: ghost_service.build_ghost_summary(30, user_id=USER_ID, market=bad))
    with pytest.raises(ValueError):
        anyio.run(lambda: ghost_service.build_veto_ledger(30, user_id=USER_ID, market=bad))
    with pytest.raises(ValueError):
        anyio.run(lambda: ghost_service.build_veto_exemplar("r", user_id=USER_ID, market=bad))
    with pytest.raises(ValueError):
        # even for a malformed tenant: a bad market is never silently swallowed
        anyio.run(lambda: ghost_service.build_ghost_summary(30, user_id="nope", market=bad))
    assert session.statements == []


# ── HTTP layer ──────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(insights.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: AuthedUser(
        id=USER_ID, email="t@example.com", auth_method="test"
    )
    monkeypatch.setattr(insights, "_require_postgres", lambda: None)
    return TestClient(app)


def test_http_market_param_threads_through(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    real_summary = ghost_service.build_ghost_summary
    real_ledger = ghost_service.build_veto_ledger

    async def summary(window_days: int = 30, *, user_id: str, market: str = "US") -> Any:
        seen.append(market)
        return await real_summary(window_days, user_id="not-a-uuid", market=market)

    async def ledger(window_days: int = 30, *, user_id: str, market: str = "US") -> Any:
        seen.append(market)
        return await real_ledger(window_days, user_id="not-a-uuid", market=market)

    monkeypatch.setattr(insights, "build_ghost_summary", summary)
    monkeypatch.setattr(insights, "build_veto_ledger", ledger)

    for path in ("/api/v1/ghost/summary", "/api/v1/risk/vetoes"):
        d = client.get(path).json()
        assert (d["market"], d["currency"]) == ("US", "USD")
        d = client.get(path, params={"market": "IN"}).json()
        assert (d["market"], d["currency"]) == ("IN", "INR")
    assert seen == ["US", "IN", "US", "IN"]


@pytest.mark.parametrize(
    "path",
    ["/api/v1/ghost/summary", "/api/v1/risk/vetoes", "/api/v1/risk/vetoes/some_rule/exemplar"],
)
def test_http_bad_market_is_422(client: TestClient, path: str) -> None:
    assert client.get(path, params={"market": "EU"}).status_code == 422
    assert client.get(path, params={"market": "in"}).status_code == 422
