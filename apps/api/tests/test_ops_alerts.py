"""Ops alerts: the silent failures that used to be one log line.

Each hook here fired in production at least once and was noticed days
later by a human reading logs (see ops_alerts.py's module docstring).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.notifications.ops_alerts as ops_mod
from app.services.notifications.ops_alerts import raise_ops_alert, reset_ops_alerts_for_tests


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_ops_alerts_for_tests()


# ── the alert itself ─────────────────────────────────────────────────


def test_a_repeat_inside_the_window_is_suppressed(caplog: pytest.LogCaptureFixture) -> None:
    """A breaker checked on a 30s tick must page once, not 720 times a day."""
    with caplog.at_level(logging.ERROR, logger="api.ops_alerts"):
        assert raise_ops_alert("breaker_tripped", key="u1", title="t", body="b") is True
        assert raise_ops_alert("breaker_tripped", key="u1", title="t", body="b") is False
        assert raise_ops_alert("breaker_tripped", key="u2", title="t", body="b") is True
    assert sum("OPS ALERT" in r.getMessage() for r in caplog.records) == 2


async def test_it_pushes_to_the_user_with_an_ops_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.notifications.notifications as notif

    sent: dict[str, Any] = {}

    def fake_schedule(**kw: Any) -> None:
        sent.update(kw)

    monkeypatch.setattr(notif, "schedule_position_event_notification", fake_schedule)
    monkeypatch.delenv("OPS_ALERT_WEBHOOK_URL", raising=False)
    raise_ops_alert("sweep_failed", user_id="u1", title="Sweep failed", body="x")
    assert sent["user_id"] == "u1" and sent["data_kind"] == "ops_alert"


async def test_the_webhook_url_is_never_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A Slack webhook URL is its own credential."""
    secret = "https://hooks.example.invalid/services/SECRET-TOKEN"

    class _Boom:
        def __init__(self, **_: Any) -> None: ...
        async def __aenter__(self) -> _Boom:
            return self
        async def __aexit__(self, *_: Any) -> None:
            return None
        async def post(self, url: str, json: Any) -> Any:
            raise RuntimeError(f"connect failed for {url}")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    with caplog.at_level(logging.WARNING, logger="api.ops_alerts"):
        await ops_mod._post_webhook(secret, "hello")
    assert "SECRET-TOKEN" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_it_never_raises_into_its_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("logging exploded")

    monkeypatch.setattr(ops_mod.logger, "error", boom)
    assert raise_ops_alert("x", title="t", body="b") is False


# ── the hooks ────────────────────────────────────────────────────────


class _NullCtx:
    async def __aenter__(self) -> tuple[Any, Any]:
        return SimpleNamespace(get_account_number=AsyncMock(return_value="ACCT")), SimpleNamespace(
            id=str(uuid.uuid4())
        )

    async def __aexit__(self, *a: Any) -> bool:
        return False


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False


async def test_a_breaker_trip_pages_the_user() -> None:
    import app.services.orders.account_switch as account_switch_mod
    import app.services.orders.auto_approver as auto_approver_mod
    import app.services.orders.order_sync as order_sync_mod
    import app.services.orders.position_manager as position_manager_mod
    import app.services.orders.reconciler_fleet as fleet_mod
    import app.services.orders.stale_entries as stale_entries_mod

    uid = "43221580-69bc-4134-8e1e-5af75499d874"
    fleet = fleet_mod.ReconcilerFleet(
        session_factory=lambda: _FakeSession(),
        broker_store=SimpleNamespace(
            list_active_connections_by_broker=AsyncMock(
                return_value=[SimpleNamespace(user_id=uid, id=str(uuid.uuid4()), is_paper=True)]
            )
        ),
    )
    tripped = SimpleNamespace(transition=SimpleNamespace(tripped=True, reason="-3.01%"))
    fleet._reconciler_for = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(tick=AsyncMock(return_value=tripped))
    )
    alerts: list[tuple[str, dict]] = []

    with (
        patch.object(fleet_mod, "with_broker_client", lambda *a, **k: _NullCtx()),
        patch.object(account_switch_mod, "reconcile_account_identity", AsyncMock(return_value=False)),
        patch.object(order_sync_mod, "sync_user_orders_and_positions", AsyncMock(return_value=None)),
        patch.object(stale_entries_mod, "sweep_stale_entry_orders_for_user", AsyncMock(return_value=0)),
        patch.object(position_manager_mod, "manage_positions_for_user", AsyncMock(return_value=0)),
        patch.object(position_manager_mod, "sweep_expiring_options_for_user", AsyncMock(return_value=0)),
        patch.object(auto_approver_mod, "auto_approve_for_user", AsyncMock(return_value=0)),
        patch.object(ops_mod, "raise_ops_alert", lambda kind, **kw: alerts.append((kind, kw))),
    ):
        await fleet.tick()

    assert [k for k, _ in alerts] == ["breaker_tripped"]
    assert alerts[0][1]["user_id"] == uid


async def test_a_failed_baseline_sweep_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.council import scheduler as sched

    s = sched.CouncilScheduler()
    monkeypatch.setattr(s, "_run_once", AsyncMock(side_effect=RuntimeError("db down")))
    sleeps = {"n": 0}

    async def fake_sleep(_seconds: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] > 1:  # the post-run 61s guard: stop the loop here
            raise asyncio.CancelledError

    monkeypatch.setattr(sched.asyncio, "sleep", fake_sleep)
    alerts: list[str] = []
    monkeypatch.setattr(ops_mod, "raise_ops_alert", lambda kind, **kw: alerts.append(kind))

    with pytest.raises(asyncio.CancelledError):
        await s._baseline_loop()
    assert alerts == ["sweep_failed"]
    assert s.last_result == "failed"


async def test_a_protective_stop_that_fails_to_place_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-09-02: every resting stop failed from the first live fill and
    nothing said so. The position silently fell back to the in-process
    monitor, which is gone the moment the process is."""
    import app.services.orders.option_stops as stops
    from engine.options.protective_stop import ProtectiveStopLevels

    class _Broker:
        async def place_order(self, _req: Any) -> Any:
            raise RuntimeError("422 stop_price invalid")

    class _Ctx:
        async def __aenter__(self) -> tuple[Any, Any]:
            return _Broker(), SimpleNamespace(id=str(uuid.uuid4()), is_paper=True)

        async def __aexit__(self, *a: Any) -> bool:
            return False

    monkeypatch.setattr(stops, "with_broker_client", lambda *a, **k: _Ctx())
    alerts: list[tuple[str, dict]] = []
    monkeypatch.setattr(ops_mod, "raise_ops_alert", lambda kind, **kw: alerts.append((kind, kw)))

    result = await stops._place(
        None,  # type: ignore[arg-type]
        user_id="u1",
        decision=SimpleNamespace(id=uuid.uuid4()),
        occ="NVDA261016C00190000",
        qty=2,
        levels=ProtectiveStopLevels(
            stop_price=2.0, limit_price=1.76, basis_pl_pct=-40.0, from_trail=False
        ),
        seq=0,
        replace_existing=False,
    )
    assert result is None
    assert [k for k, _ in alerts] == ["protective_stop_failed"]
    assert alerts[0][1]["key"] == "NVDA261016C00190000"
