"""/api/v1/circuit-breaker route — status + acknowledge surface.

Mock-store mode is never halted (no Postgres breaker table), so status is
False and acknowledge is a safe no-op; the real halt→banner→resume path is
Postgres-marked. Here we pin the route contract + auth.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app  # noqa: E402
from app.services.auth.auth_store import reset_auth_store_for_tests  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_POSTGRES", raising=False)
    reset_auth_store_for_tests()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _bearer(c: TestClient, email: str = "cb@example.com") -> dict[str, str]:
    ch = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    issued = c.post(
        "/api/v1/auth/verify", json={"email": email, "token": ch["devToken"]}
    ).json()
    return {"Authorization": f"Bearer {issued['accessToken']}"}


def test_status_not_halted_in_mock_mode(client: TestClient) -> None:
    r = client.get("/api/v1/circuit-breaker")
    assert r.status_code == 200
    assert r.json()["halted"] is False


def test_acknowledge_requires_real_auth(client: TestClient) -> None:
    r = client.post("/api/v1/circuit-breaker/acknowledge")
    assert r.status_code == 401  # require_real_auth, no bypass


def test_acknowledge_is_safe_noop_when_not_halted(client: TestClient) -> None:
    r = client.post("/api/v1/circuit-breaker/acknowledge", headers=_bearer(client))
    assert r.status_code == 200
    assert r.json()["halted"] is False
