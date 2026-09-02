"""/api/v1/insights/scan-funnel — symbol-scan funnel route tests.

Mirrors test_scanner_route.py's structure exactly: a fresh TestClient, the
same dev-bypass login helper, and monkeypatching the singleton the service
layer reads. get_council_scheduler is patched on
app.services.council.scan_funnel_service (where that module imported it
into its own namespace at module scope, precisely so this patch works),
not on app.services.council.scheduler where it's defined.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import ClassVar, Iterator

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app  # noqa: E402
from app.services.auth.auth_store import reset_auth_store_for_tests  # noqa: E402
from app.services.broker.broker_store import reset_broker_store_for_tests  # noqa: E402
from app.services.council.store import reset_store_for_tests  # noqa: E402


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


def _login(c: TestClient, email: str = "scan-funnel-user@example.com") -> str:
    challenge = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    return c.post(
        "/api/v1/auth/verify",
        json={"email": email, "token": challenge["devToken"]},
    ).json()["accessToken"]


def test_scan_funnel_honest_defaults_when_no_scheduler(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No scheduler running (the default) -> 200 with every field at an
    honest None/empty default, never a 404 and never a fabricated zero.
    Unlike /insights/funnel, this must work with USE_POSTGRES unset."""
    from app.services.council import scan_funnel_service as mod

    monkeypatch.setattr(mod, "get_council_scheduler", lambda: None)
    monkeypatch.delenv("USE_POSTGRES", raising=False)

    access = _login(client)
    r = client.get("/api/v1/insights/scan-funnel", headers=_bearer(access))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["universe"]["eligibleCount"] is None
    assert body["universe"]["examinedCount"] is None
    assert body["universe"]["refreshedAt"] is None
    assert body["sweep"] is None
    assert body["chainPreflight"] is None
    assert "generatedAt" in body


def test_scan_funnel_reports_a_real_baseline_sweep(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.council import scan_funnel_service as mod

    when = datetime.now(UTC)

    class _FakeTally:
        watchlist_size = 110
        cleared_math = 34
        admitted_to_llm = 20
        capped_breakdown: ClassVar[dict[str, int]] = {"llm_daily_symbol_cap_reached": 14}
        generated_at = when

    class _FakeScheduler:
        last_universe_refresh_result: ClassVar[dict[str, int]] = {
            "equity": 56, "options": 12, "eligible_universe": 1024, "examined": 178,
        }
        last_universe_refresh_at = when
        last_sweep_tally = _FakeTally()
        last_sweep_kind = "baseline"
        last_sweep_tally_at = when

    monkeypatch.setattr(mod, "get_council_scheduler", lambda: _FakeScheduler())

    access = _login(client)
    r = client.get("/api/v1/insights/scan-funnel", headers=_bearer(access))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["universe"]["eligibleCount"] == 1024
    assert body["universe"]["examinedCount"] == 178
    assert body["sweep"]["kind"] == "baseline"
    assert body["sweep"]["watchlistSize"] == 110
    assert body["sweep"]["clearedMath"] == 34
    assert body["sweep"]["admittedToLlm"] == 20
    # capped_breakdown's KEYS are skip_reason data strings, not schema
    # field names -- CamelCaseModel correctly leaves them snake_case,
    # same as funnel_service's own top_rejection_reasons reason strings.
    assert body["sweep"]["cappedBreakdown"] == {"llm_daily_symbol_cap_reached": 14}
    assert body["chainPreflight"] is None


def test_scan_funnel_reports_a_triggered_sweep_distinctly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A triggered loop's tiny watchlist carries its own `kind` -- the
    frontend needs this to caption it honestly rather than showing it
    unexplained next to a much larger baseline sweep's numbers."""
    from app.services.council import scan_funnel_service as mod

    when = datetime.now(UTC)

    class _FakeTally:
        watchlist_size = 2
        cleared_math = 1
        admitted_to_llm = 1
        capped_breakdown: ClassVar[dict[str, int]] = {}
        generated_at = when

    class _FakeScheduler:
        last_universe_refresh_result = None
        last_universe_refresh_at = None
        last_sweep_tally = _FakeTally()
        last_sweep_kind = "triggered"
        last_sweep_tally_at = when

    monkeypatch.setattr(mod, "get_council_scheduler", lambda: _FakeScheduler())

    access = _login(client)
    body = client.get("/api/v1/insights/scan-funnel", headers=_bearer(access)).json()

    assert body["sweep"]["kind"] == "triggered"
    assert body["sweep"]["watchlistSize"] == 2
    assert body["universe"]["eligibleCount"] is None
