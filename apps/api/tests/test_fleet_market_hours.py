"""The exit ladder submits only while the market is open.

Outside hours, option marks are stale and absurdly wide (Saturday spreads
of ~30% were ~$720 of the -$2.3k shown on Sep 6). A stop evaluated then
fires on quote noise and queues a sell priced by it. The broker-side
resting stop covers off-hours; that is what it is for.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.orders.account_switch as account_switch_mod
import app.services.orders.auto_approver as auto_approver_mod
import app.services.orders.order_sync as order_sync_mod
import app.services.orders.position_manager as position_manager_mod
import app.services.orders.reconciler_fleet as fleet_mod
import app.services.orders.stale_entries as stale_entries_mod

UID = "43221580-69bc-4134-8e1e-5af75499d874"


class _Ctx:
    async def __aenter__(self) -> tuple[Any, Any]:
        return SimpleNamespace(get_account_number=AsyncMock(return_value="A")), SimpleNamespace(
            id=str(uuid.uuid4())
        )

    async def __aexit__(self, *a: Any) -> bool:
        return False


async def _tick(exits_open: bool) -> tuple[AsyncMock, AsyncMock]:
    fleet = fleet_mod.ReconcilerFleet(
        session_factory=lambda: None,
        broker_store=SimpleNamespace(
            list_active_connections_by_broker=AsyncMock(
                return_value=[SimpleNamespace(user_id=UID, id=str(uuid.uuid4()), is_paper=True)]
            )
        ),
    )
    fleet._reconciler_for = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            tick=AsyncMock(return_value=SimpleNamespace(transition=SimpleNamespace(tripped=False)))
        )
    )
    manage = AsyncMock(return_value=0)
    expiry = AsyncMock(return_value=0)
    with (
        patch.object(fleet_mod, "with_broker_client", lambda *a, **k: _Ctx()),
        patch.object(fleet_mod, "_exits_allowed_now", lambda: exits_open),
        patch.object(account_switch_mod, "reconcile_account_identity", AsyncMock(return_value=False)),
        patch.object(order_sync_mod, "sync_user_orders_and_positions", AsyncMock(return_value=None)),
        patch.object(stale_entries_mod, "sweep_stale_entry_orders_for_user", AsyncMock(return_value=0)),
        patch.object(position_manager_mod, "manage_positions_for_user", manage),
        patch.object(position_manager_mod, "sweep_expiring_options_for_user", expiry),
        patch.object(auto_approver_mod, "auto_approve_for_user", AsyncMock(return_value=0)),
    ):
        await fleet.tick()
    return manage, expiry


async def test_no_exit_is_submitted_while_the_market_is_closed() -> None:
    manage, expiry = await _tick(exits_open=False)
    manage.assert_not_awaited()
    expiry.assert_not_awaited()


async def test_both_exit_paths_run_while_the_market_is_open() -> None:
    manage, expiry = await _tick(exits_open=True)
    manage.assert_awaited_once()
    expiry.assert_awaited_once()


def test_a_calendar_failure_allows_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails OPEN: a calendar error must never be why a position can't exit."""
    import engine.features

    def boom(_now: Any) -> bool:
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(engine.features, "is_us_market_open", boom)
    assert fleet_mod._exits_allowed_now() is True
