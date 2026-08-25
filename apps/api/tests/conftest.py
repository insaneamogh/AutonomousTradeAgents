"""Shared test fixtures for the API suite.

The login rate limiter (5/hour/email + 30/hour/IP) is in-process and global.
Every TestClient request originates from the same IP, so without a reset the
IP window would trip partway through a full run and 429 unrelated tests.
Reset it before each test so the limiter is exercised in isolation where a
test wants to, and invisible everywhere else.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_login_rate_limit() -> None:
    from app.services.auth.rate_limit import reset_rate_limit_for_tests

    reset_rate_limit_for_tests()
    yield
    reset_rate_limit_for_tests()
