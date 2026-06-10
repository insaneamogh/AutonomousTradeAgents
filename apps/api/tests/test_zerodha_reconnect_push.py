"""Zerodha daily-reconnect reminder tests.

Direct service-level tests around ``send_zerodha_reconnect_notification``
plus the cron fan-out. ``send_push`` is patched in the notifications
module's namespace (same seam as test_notifications.py).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.services import expo_push  # noqa: E402
from app.services import notifications as notif_mod  # noqa: E402
from app.services.broker_store import (  # noqa: E402
    get_broker_store,
    reset_broker_store_for_tests,
)
from app.services.notification_store import (  # noqa: E402
    get_notification_store,
    reset_notification_store_for_tests,
)
from app.services.notifications import send_zerodha_reconnect_notification  # noqa: E402

USER = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_broker_store_for_tests()
    reset_notification_store_for_tests()


@pytest.fixture
def captured_pushes(monkeypatch: pytest.MonkeyPatch) -> list[expo_push.PushMessage]:
    captured: list[expo_push.PushMessage] = []

    async def fake_send_push(messages, *, client=None, revoke_token=None):  # noqa: ANN001
        captured.extend(messages)
        return expo_push.PushResult(
            sent=len(messages), revoked_tokens=[], other_errors=[]
        )

    monkeypatch.setattr(notif_mod, "send_push", fake_send_push)
    return captured


async def _seed_zerodha(*, expired: bool, user_id: str = USER) -> None:
    delta = timedelta(hours=-2) if expired else timedelta(hours=8)
    await get_broker_store().upsert_connection(
        user_id=user_id,
        broker="zerodha",
        is_paper=False,
        account_number="AB1234",
        encrypted_access_token="enc",
        encrypted_refresh_token=None,
        access_token_expires_at=datetime.now(timezone.utc) + delta,
    )


async def _seed_device(user_id: str = USER, token: str = "ExponentPushToken[z1]") -> None:
    await get_notification_store().register_device(
        user_id=user_id, expo_push_token=token, platform="ios", label=None
    )


# ─────────────────────────────────────────────────────────────────────
# Service behavior
# ─────────────────────────────────────────────────────────────────────


async def test_expired_token_sends_push(captured_pushes: list) -> None:
    await _seed_zerodha(expired=True)
    await _seed_device()

    sent = await send_zerodha_reconnect_notification(USER)
    assert sent == 1
    [msg] = captured_pushes
    assert msg.data == {"kind": "zerodha_reconnect"}
    assert "reconnect" in msg.title.lower()
    # Lock-screen-safe: no account number, no token material.
    assert "AB1234" not in msg.body and "AB1234" not in msg.title


async def test_valid_token_skips(captured_pushes: list) -> None:
    await _seed_zerodha(expired=False)
    await _seed_device()

    sent = await send_zerodha_reconnect_notification(USER)
    assert sent == 0
    assert captured_pushes == []


async def test_force_overrides_validity(captured_pushes: list) -> None:
    await _seed_zerodha(expired=False)
    await _seed_device()

    sent = await send_zerodha_reconnect_notification(USER, force=True)
    assert sent == 1


async def test_no_zerodha_connection_is_noop(captured_pushes: list) -> None:
    await _seed_device()
    sent = await send_zerodha_reconnect_notification(USER)
    assert sent == 0
    assert captured_pushes == []


async def test_no_devices_is_noop(captured_pushes: list) -> None:
    await _seed_zerodha(expired=True)
    sent = await send_zerodha_reconnect_notification(USER)
    assert sent == 0


# ─────────────────────────────────────────────────────────────────────
# Cron fan-out
# ─────────────────────────────────────────────────────────────────────


async def test_cron_fans_out_to_all_zerodha_users(captured_pushes: list) -> None:
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "zerodha_reconnect_cron",
        Path(__file__).resolve().parents[1] / "scripts" / "zerodha_reconnect_cron.py",
    )
    assert spec and spec.loader
    cron = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cron)

    user_b = "00000000-0000-0000-0000-000000000002"
    await _seed_zerodha(expired=True)
    await _seed_zerodha(expired=True, user_id=user_b)
    await _seed_device()
    await _seed_device(user_id=user_b, token="ExponentPushToken[z2]")

    total = await cron.run()
    assert total == 2
    assert {m.to for m in captured_pushes} == {
        "ExponentPushToken[z1]",
        "ExponentPushToken[z2]",
    }


async def test_cron_user_filter(captured_pushes: list) -> None:
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "zerodha_reconnect_cron2",
        Path(__file__).resolve().parents[1] / "scripts" / "zerodha_reconnect_cron.py",
    )
    assert spec and spec.loader
    cron = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cron)

    user_b = "00000000-0000-0000-0000-000000000002"
    await _seed_zerodha(expired=True)
    await _seed_zerodha(expired=True, user_id=user_b)
    await _seed_device()
    await _seed_device(user_id=user_b, token="ExponentPushToken[z2]")

    total = await cron.run(only_user_id=user_b)
    assert total == 1
    assert captured_pushes[0].to == "ExponentPushToken[z2]"
