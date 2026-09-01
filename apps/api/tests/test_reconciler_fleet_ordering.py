"""The account-switch check must run BEFORE anything else touches
position state in a fleet tick — not merely be commented as first.

On 2026-09-01 it was placed after `_reconciler_for(uid).tick()` and
`sync_user_orders_and_positions` in the source while a comment above it
claimed "FIRST". On the first tick against freshly-swapped Alpaca keys,
`_detect_external_closes` read every position on the OLD account as
having vanished from the broker and closed all 7 of them with a
fabricated realized P&L under `close_reason='external_broker'` — before
`reconcile_account_identity` ever got a chance to retire that state
cleanly. Real production rows were corrupted by this.

This test does not care how the ordering bug happened; it pins the
observable behaviour: `reconcile_account_identity` must be awaited before
`Reconciler.tick()` and before `sync_user_orders_and_positions`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import app.services.orders.account_switch as account_switch_mod
import app.services.orders.auto_approver as auto_approver_mod
import app.services.orders.order_sync as order_sync_mod
import app.services.orders.position_manager as position_manager_mod
import app.services.orders.reconciler_fleet as fleet_mod
import app.services.orders.stale_entries as stale_entries_mod

USER_ID = "43221580-69bc-4134-8e1e-5af75499d874"


def _connection(user_id: str) -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, id=str(uuid.uuid4()), is_paper=True)


class _NullCtx:
    """A no-op async context manager standing in for `with_broker_client`."""

    async def __aenter__(self) -> tuple[Any, Any]:
        return SimpleNamespace(get_account_number=AsyncMock(return_value="ACCT")), SimpleNamespace(
            id=str(uuid.uuid4())
        )

    async def __aexit__(self, *a: Any) -> bool:
        return False


async def test_account_switch_is_awaited_before_the_reconciler_tick_and_order_sync() -> None:
    calls: list[str] = []

    fleet = fleet_mod.ReconcilerFleet(
        session_factory=lambda: _FakeSession(),
        broker_store=SimpleNamespace(
            list_active_connections_by_broker=AsyncMock(return_value=[_connection(USER_ID)])
        ),
    )

    fake_reconciler = SimpleNamespace(
        tick=_recording(calls, "reconciler_tick", SimpleNamespace(transition=SimpleNamespace(tripped=False)))
    )
    fleet._reconciler_for = MagicMock(return_value=fake_reconciler)  # type: ignore[method-assign]

    with (
        patch.object(fleet_mod, "with_broker_client", lambda *a, **k: _NullCtx()),
        patch.object(
            account_switch_mod, "reconcile_account_identity",
            _recording(calls, "account_switch", False),
        ),
        patch.object(
            order_sync_mod, "sync_user_orders_and_positions", _recording(calls, "order_sync", None),
        ),
        patch.object(
            stale_entries_mod, "sweep_stale_entry_orders_for_user", _recording(calls, "stale_entries", 0),
        ),
        patch.object(
            position_manager_mod, "manage_positions_for_user", _recording(calls, "position_manager", 0),
        ),
        patch.object(
            position_manager_mod, "sweep_expiring_options_for_user", _recording(calls, "expiry_sweep", 0),
        ),
        patch.object(
            auto_approver_mod, "auto_approve_for_user", _recording(calls, "auto_approve", 0),
        ),
    ):
        await fleet.tick()

    assert calls[0] == "account_switch", (
        f"account_switch must be the FIRST thing a fleet tick awaits for a user; got order {calls}. "
        "This is the exact regression that corrupted 7 production decision rows on 2026-09-01 — "
        "the reconciler tick and order sync both ran against a freshly-swapped account before "
        "reconcile_account_identity got a chance to retire the old account's state."
    )
    assert calls.index("account_switch") < calls.index("reconciler_tick")
    assert calls.index("account_switch") < calls.index("order_sync")


def _recording(calls: list[str], name: str, return_value: Any) -> AsyncMock:
    async def _fn(*a: Any, **k: Any) -> Any:
        calls.append(name)
        return return_value
    return AsyncMock(side_effect=_fn)


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def commit(self) -> None:
        return None
