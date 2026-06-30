"""Login rate-limit tests — the sliding window + the 429 on /request-login."""

from __future__ import annotations

import os
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app  # noqa: E402
from app.services.auth_store import reset_auth_store_for_tests  # noqa: E402
from app.services.rate_limit import (  # noqa: E402
    EMAIL_LIMIT,
    SlidingWindowLimiter,
    reset_rate_limit_for_tests,
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_auth_store_for_tests()
    reset_rate_limit_for_tests()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_sliding_window_allows_then_blocks() -> None:
    lim = SlidingWindowLimiter()
    for _ in range(3):
        assert lim.allow("k", limit=3, window=3600) is True
    assert lim.allow("k", limit=3, window=3600) is False  # 4th over the cap


def test_request_login_429_after_email_limit(client: TestClient) -> None:
    email = "rl-user@example.com"
    # First EMAIL_LIMIT are allowed.
    for _ in range(EMAIL_LIMIT):
        r = client.post("/api/v1/auth/request-login", json={"email": email})
        assert r.status_code == 200, r.text
    # The next one trips the per-email window.
    blocked = client.post("/api/v1/auth/request-login", json={"email": email})
    assert blocked.status_code == 429
