"""/api/v1/scanner/status — scanner/trigger-loop observability tests.

Mirrors ``test_health_route.py``'s structure: a fresh ``TestClient``, the
same dev-bypass login helper, and monkeypatching the singleton the service
layer reads. ``get_council_scheduler`` is patched on
``app.services.council.scanner_status`` (where ``scanner_status.py``
imported it into its own namespace), not on ``app.services.council.
scheduler`` where it's defined — patch at the point of use.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app  # noqa: E402
from app.services.auth.auth_store import reset_auth_store_for_tests  # noqa: E402
from app.services.broker.broker_store import reset_broker_store_for_tests  # noqa: E402
from app.services.council.store import reset_store_for_tests  # noqa: E402
from engine.scanner import ScanResult, ScanSignal  # noqa: E402


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


def _login(c: TestClient, email: str = "scanner-user@example.com") -> str:
    challenge = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    return c.post(
        "/api/v1/auth/verify",
        json={"email": email, "token": challenge["devToken"]},
    ).json()["accessToken"]


def test_scanner_status_honest_defaults_when_no_scheduler(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No scheduler running (the default — COUNCIL_SCHEDULER_ENABLED=0 or
    USE_POSTGRES=0) → 200 with every field at an honest default, never an
    error."""
    from app.services.council import scanner_status as scanner_status_mod

    monkeypatch.setattr(scanner_status_mod, "get_council_scheduler", lambda: None)
    monkeypatch.delenv("SCANNER_ENABLED", raising=False)
    monkeypatch.setenv("AGENT_CRON_WATCHLIST", "AAA,BBB,CCC")

    access = _login(client)
    r = client.get("/api/v1/scanner/status", headers=_bearer(access))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["schedulerEnabled"] is False
    assert body["scannerEnabledFlag"] is False
    assert body["triggerLoopArmed"] is False
    assert body["marketOpen"] is None
    assert body["marketOpenSource"] is None
    assert body["lastScanAt"] is None
    assert body["scanIntervalMinutes"] is None
    assert body["maxCouncilRunsPerScan"] is None
    assert body["watchlistSize"] == 3
    assert body["signals"] == []
    assert body["triggeredSymbols"] == []
    assert body["suppressedCount"] == 0
    assert body["lastCouncilRunAt"] is None
    assert body["lastCouncilRunSymbols"] == []
    assert "generatedAt" in body


def test_scanner_status_flags_unavailable_when_armed_but_not_running(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCANNER_ENABLED=1 but no scheduler running yet → the client can
    still tell "flag set" apart from "actually armed" (the missing-Alpaca-
    keys state), because the two booleans are independent."""
    from app.services.council import scanner_status as scanner_status_mod

    monkeypatch.setattr(scanner_status_mod, "get_council_scheduler", lambda: None)
    monkeypatch.setenv("SCANNER_ENABLED", "1")

    access = _login(client)
    body = client.get("/api/v1/scanner/status", headers=_bearer(access)).json()

    assert body["schedulerEnabled"] is False
    assert body["scannerEnabledFlag"] is True
    assert body["triggerLoopArmed"] is False


def test_scanner_status_populated_when_scheduler_has_data(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wired-in fake scheduler with scan data → the response carries the
    signal detail, camelCased, straight through from the scanner package."""
    from app.services.council import scanner_status as scanner_status_mod

    signal = ScanSignal(
        symbol="NVDA",
        trigger_rule="dma20_cross_up",
        strength=0.75,
        observed_at=datetime.now(UTC),
        direction="bullish",
        detail="NVDA crossed above its 20-DMA",
        context={"sma20": 120.5},
    )
    result = ScanResult(
        scanned_at=datetime.now(UTC),
        market_open=True,
        symbols_scanned=("NVDA", "AAPL"),
        signals=(signal,),
        suppressed=(),
    )

    class _FakeScheduler:
        trigger_loop_armed = True
        last_scan_at = result.scanned_at
        last_scan_result = result
        scanner_interval_minutes = 5
        scanner_max_council_runs = 3
        last_run_at = result.scanned_at
        last_council_run_symbols = ("NVDA",)

    monkeypatch.setattr(
        scanner_status_mod, "get_council_scheduler", lambda: _FakeScheduler()
    )
    monkeypatch.setenv("SCANNER_ENABLED", "1")
    monkeypatch.setenv("AGENT_CRON_WATCHLIST", "NVDA,AAPL")

    access = _login(client)
    r = client.get("/api/v1/scanner/status", headers=_bearer(access))
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["schedulerEnabled"] is True
    assert body["scannerEnabledFlag"] is True
    assert body["triggerLoopArmed"] is True
    assert body["marketOpen"] is True
    assert body["marketOpenSource"] == "local_calendar"
    assert body["watchlistSize"] == 2
    assert body["scanIntervalMinutes"] == 5
    assert body["maxCouncilRunsPerScan"] == 3
    assert body["triggeredSymbols"] == ["NVDA"]
    assert body["lastCouncilRunSymbols"] == ["NVDA"]
    assert body["suppressedCount"] == 0

    assert len(body["signals"]) == 1
    sig = body["signals"][0]
    assert sig["symbol"] == "NVDA"
    assert sig["rule"] == "dma20_cross_up"
    assert sig["direction"] == "bullish"
    assert sig["strength"] == 0.75
    assert sig["detail"] == "NVDA crossed above its 20-DMA"
    assert sig["context"] == {"sma20": 120.5}
