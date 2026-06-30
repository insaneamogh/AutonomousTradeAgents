"""/api/v1/positions route tests (mock-store mode).

Deep close mechanics (risk gate, bracket cancel, broker SELL) are covered
by the position_manager tests + the executor path. Here we pin the route
surface: auth, the empty-in-mock-mode list, and the close endpoint's
error mapping when there's no Postgres-backed position.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app  # noqa: E402
from app.services.auth_store import reset_auth_store_for_tests  # noqa: E402
from app.services.store import reset_store_for_tests  # noqa: E402


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_POSTGRES", raising=False)  # mock-store mode
    reset_auth_store_for_tests()
    reset_store_for_tests()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _bearer(c: TestClient, email: str = "pos-user@example.com") -> dict[str, str]:
    challenge = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    issued = c.post(
        "/api/v1/auth/verify", json={"email": email, "token": challenge["devToken"]}
    ).json()
    return {"Authorization": f"Bearer {issued['accessToken']}"}


def test_open_positions_empty_in_mock_mode(client: TestClient) -> None:
    r = client.get("/api/v1/positions")
    assert r.status_code == 200
    assert r.json() == []


def test_close_unknown_position_404(client: TestClient) -> None:
    # Authenticated, but no Postgres-backed position exists → not_found.
    r = client.post(
        "/api/v1/positions/not-a-real-id/close", headers=_bearer(client)
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "not_found"


def test_close_requires_real_auth(client: TestClient) -> None:
    # require_real_auth must reject a missing/bad bearer.
    r = client.post("/api/v1/positions/abc/close")
    assert r.status_code == 401
