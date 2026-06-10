"""/api/v1/strategies/performance — per-strategy aggregator tests.

Covers:
  - Cold start (no decisions): one row per STRATEGY_REGISTRY id at
    confidence=0.5, decisions_in_window=0, wins=losses=0.
  - After a few completed trades land in the DecisionLog, the bucket
    aggregates correctly + the response is sorted by confidence desc.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app  # noqa: E402
from app.services.auth_store import reset_auth_store_for_tests  # noqa: E402
from trading_agents.memory import (  # noqa: E402
    DecisionEntry,
    get_confidence_store,
    get_decision_log,
    reset_memory_stores_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_auth_store_for_tests()
    reset_memory_stores_for_tests()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(c: TestClient, email: str = "strat-user@example.com") -> str:
    challenge = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    return c.post(
        "/api/v1/auth/verify",
        json={"email": email, "token": challenge["devToken"]},
    ).json()["accessToken"]


def test_cold_start_returns_one_row_per_registry_id(client: TestClient) -> None:
    access = _login(client)
    r = client.get("/api/v1/strategies/performance", headers=_bearer(access))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["windowDays"] == 30
    ids = {s["strategyId"] for s in body["strategies"]}
    assert ids == {
        "sma_crossover",
        "rsi_mean_reversion",
        "momentum",
        "breakout",
        "vol_regime_switch",
    }
    for s in body["strategies"]:
        assert s["confidence"] == 0.5
        assert s["decisionsInWindow"] == 0
        assert s["wins"] == 0
        assert s["losses"] == 0


@pytest.mark.asyncio
async def test_aggregates_completed_trades_and_sorts_by_confidence() -> None:
    """Seed decisions directly via the in-memory log; confirm the
    aggregator picks them up + sorts by confidence desc.
    """
    log = get_decision_log()
    store = get_confidence_store()
    now = datetime.now(timezone.utc)

    # Two completed momentum trades — one win, one loss.
    await log.record(
        DecisionEntry(
            symbol="NVDA",
            triggered_at=now - timedelta(days=2),
            selected_strategy="momentum",
            selector_confidence=0.6,
            final_action="BUY",
            risk_approved=True,
            fill_qty=10,
            fill_avg_price=100.0,
            realized_pnl=120.0,
        )
    )
    await log.record(
        DecisionEntry(
            symbol="META",
            triggered_at=now - timedelta(days=1),
            selected_strategy="momentum",
            selector_confidence=0.55,
            final_action="BUY",
            risk_approved=True,
            fill_qty=5,
            fill_avg_price=200.0,
            realized_pnl=-80.0,
        )
    )
    # An open (no PnL) breakout decision — counts toward decisions_in_window
    # but not wins/losses.
    await log.record(
        DecisionEntry(
            symbol="TSLA",
            triggered_at=now - timedelta(hours=6),
            selected_strategy="breakout",
            selector_confidence=0.5,
            final_action="BUY",
            risk_approved=True,
        )
    )
    # Nudge sma_crossover above 0.5 so the sort ordering is testable.
    await store.apply_delta("sma_crossover", confidence_delta=0.08)

    # Call the service directly to avoid TestClient + a separate auth path.
    from app.services.strategies_perf import build_strategies_performance

    resp = await build_strategies_performance()

    # Sort = confidence DESC. sma_crossover was nudged to 0.58.
    assert resp.strategies[0].strategy_id == "sma_crossover"
    assert resp.strategies[0].confidence == pytest.approx(0.58, abs=1e-6)

    momentum = next(s for s in resp.strategies if s.strategy_id == "momentum")
    assert momentum.decisions_in_window == 2
    assert momentum.wins == 1
    assert momentum.losses == 1
    # Net PnL: +120 - 80 = +40.
    assert momentum.realized_pnl == pytest.approx(40.0, abs=1e-6)
    # Averages exist.
    assert momentum.avg_winner_pct is not None
    assert momentum.avg_loser_pct is not None

    breakout = next(s for s in resp.strategies if s.strategy_id == "breakout")
    assert breakout.decisions_in_window == 1
    assert breakout.wins == 0
    assert breakout.losses == 0
    assert breakout.realized_pnl == 0.0


@pytest.mark.asyncio
async def test_window_days_filters_older_decisions() -> None:
    """A decision older than the window must NOT show in the count."""
    log = get_decision_log()
    await log.record(
        DecisionEntry(
            symbol="OLD",
            triggered_at=datetime.now(timezone.utc) - timedelta(days=400),
            selected_strategy="momentum",
            selector_confidence=0.6,
            final_action="BUY",
            risk_approved=True,
            fill_qty=10,
            fill_avg_price=100.0,
            realized_pnl=50.0,
        )
    )

    from app.services.strategies_perf import build_strategies_performance

    resp = await build_strategies_performance(window_days=30)
    momentum = next(s for s in resp.strategies if s.strategy_id == "momentum")
    assert momentum.decisions_in_window == 0
