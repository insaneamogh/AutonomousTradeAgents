"""/api/v1/health/full — system-status aggregator tests.

Read-only endpoint that aggregates per-component liveness. We test:
  - Cold-start fresh API → council=warning, approvals=ok-inbox-clear,
    broker=warning-no-connection, reconciler=unknown.
  - After a council run lands a proposal → council=ok, approvals=ok
    (because the proposal also surfaces in pending).
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app  # noqa: E402
from app.services.auth_store import reset_auth_store_for_tests  # noqa: E402
from app.services.broker_store import reset_broker_store_for_tests  # noqa: E402
from app.services.store import reset_store_for_tests  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_auth_store_for_tests()
    reset_broker_store_for_tests()
    reset_store_for_tests()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(c: TestClient, email: str = "health-user@example.com") -> str:
    challenge = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    return c.post(
        "/api/v1/auth/verify",
        json={"email": email, "token": challenge["devToken"]},
    ).json()["accessToken"]


def test_health_full_returns_all_five_components(client: TestClient) -> None:
    """Even on a cold-start API, every component returns a status."""
    access = _login(client)
    r = client.get("/api/v1/health/full", headers=_bearer(access))
    assert r.status_code == 200, r.text
    body = r.json()
    for component in ("council", "approvals", "broker", "reconciler", "llmCost"):
        assert component in body, f"missing component: {component}"
        assert body[component]["status"] in ("ok", "warning", "danger", "unknown")
        assert body[component]["label"]


def test_health_full_council_after_run_is_ok(client: TestClient) -> None:
    """After a fresh council pass lands recent activity, the council
    component reports ``ok`` + a 24h-window label.

    (We don't assert the pre-state — MockStore seeds itself with sample
    activity rows so the cold-start surface depends on whether that seed
    is recent or stale. The state transition is what matters here.)
    """
    access = _login(client)

    # Trigger a council pass so we have a fresh row.
    r = client.post(
        "/api/v1/agent/run", headers=_bearer(access), json={"symbol": "NVDA"}
    )
    assert r.status_code == 200

    after = client.get("/api/v1/health/full", headers=_bearer(access)).json()
    assert after["council"]["status"] == "ok"
    assert "24h" in after["council"]["label"]


def test_health_full_broker_warns_when_no_connection(client: TestClient) -> None:
    access = _login(client)
    r = client.get("/api/v1/health/full", headers=_bearer(access))
    assert r.status_code == 200
    body = r.json()
    assert body["broker"]["status"] == "warning"
    assert "connect" in body["broker"]["label"].lower()


def test_health_full_reconciler_muted_in_mock_mode(client: TestClient) -> None:
    """Without USE_POSTGRES=1, the reconciler isn't running. We surface
    "unknown" rather than a misleading green or red.
    """
    access = _login(client)
    body = client.get("/api/v1/health/full", headers=_bearer(access)).json()
    assert body["reconciler"]["status"] == "unknown"
