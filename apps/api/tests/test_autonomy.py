"""GET /api/v1/agent/autonomy — the readiness report."""

from __future__ import annotations

import os
from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app
from app.services.auth.auth_store import reset_auth_store_for_tests
from app.services.broker import broker_use
from app.services.council import scheduler as sch
from app.services.platform import autonomy as svc
from app.services.platform import circuit_breaker_service as cb

SECRET = "sk-super-secret-value-123"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "USE_POSTGRES", "LLM_PROVIDER", "ANTHROPIC_API_KEY", "GLM_API_KEY",
        "ZAI_API_KEY", "TYPESAFE_API_KEY", "COUNCIL_SCHEDULER_ENABLED",
        "AUTO_APPROVE_ENABLED", "LIVE_TRADING_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)
    reset_auth_store_for_tests()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _checks(body: dict) -> dict[str, dict]:
    return {c["id"]: c for c in body["checks"]}


def _healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "glm")
    monkeypatch.setenv("GLM_API_KEY", SECRET)
    monkeypatch.setenv("COUNCIL_SCHEDULER_ENABLED", "1")
    monkeypatch.setattr(
        sch, "get_council_scheduler", lambda: SimpleNamespace(last_run_at=None)
    )
    monkeypatch.setattr(svc, "_market_open", lambda now: True)

    async def _conn(user_id, **kw):
        return SimpleNamespace(broker="alpaca", access_token_expires_at=None,
                               status="active", auto_approve_consent=False)

    monkeypatch.setattr(broker_use, "get_active_broker_connection", _conn)


def test_no_llm_key_is_blocked_with_the_fix(client: TestClient) -> None:
    r = client.get("/api/v1/agent/autonomy")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "blocked"
    llm = _checks(body)["llm"]
    assert llm["ok"] is False and llm["severity"] == "blocker"
    assert llm["fix"] == "Set GLM_API_KEY and LLM_PROVIDER=glm (or ANTHROPIC_API_KEY) on the API"
    assert body["headline"].startswith("Blocked")
    assert "asOf" in body and "lastSweepAt" in body and "nextSweepAt" in body


def test_healthy_desk_is_running(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _healthy(monkeypatch)
    body = client.get("/api/v1/agent/autonomy").json()
    assert body["state"] == "running", body
    c = _checks(body)
    assert c["llm"]["ok"] and c["scheduler"]["ok"] and c["broker"]["ok"]
    assert c["auto_approve"]["ok"] is False and c["auto_approve"]["severity"] == "warning"
    assert body["nextSweepAt"] is not None


def test_closed_market_is_idle(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _healthy(monkeypatch)
    monkeypatch.setattr(svc, "_market_open", lambda now: False)
    assert client.get("/api/v1/agent/autonomy").json()["state"] == "idle"


def test_latched_breaker_blocks(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _healthy(monkeypatch)

    async def _halted(user_id):
        return cb.CircuitBreakerStatus(halted=True, reason="drawdown -3.01%")

    monkeypatch.setattr(cb, "get_status", _halted)
    body = client.get("/api/v1/agent/autonomy").json()
    assert body["state"] == "blocked"
    br = _checks(body)["breaker"]
    assert br["ok"] is False and br["fix"] == "Acknowledge the breaker"


def test_a_raising_check_does_not_fail_the_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _healthy(monkeypatch)

    async def _boom(user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(cb, "get_status", _boom)
    r = client.get("/api/v1/agent/autonomy")
    assert r.status_code == 200
    br = _checks(r.json())["breaker"]
    assert br["ok"] is False and br["severity"] == "warning"
    assert br["detail"] == "could not be read"


def test_no_key_value_in_the_json(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _healthy(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET + "-anthropic")
    text = client.get("/api/v1/agent/autonomy").text
    assert SECRET not in text
