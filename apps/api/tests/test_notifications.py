"""Notifications tests.

Three surfaces under test:
  1. Routes: register / list / revoke (router-level happy path + 404).
  2. Expo Push client: the hand-rolled httpx POST + per-ticket error
     handling + token-revocation on DeviceNotRegistered.
  3. Council hook: /agent/run fans a proposal-pending push to every
     active device for the calling user.
"""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DEV_AUTH_BYPASS", "1")

from app.main import app  # noqa: E402
from app.services import expo_push  # noqa: E402
from app.services.auth_store import reset_auth_store_for_tests  # noqa: E402
from app.services.notification_store import (  # noqa: E402
    reset_notification_store_for_tests,
    get_notification_store,
)


@pytest.fixture(autouse=True)
def _reset_stores() -> None:
    reset_auth_store_for_tests()
    reset_notification_store_for_tests()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _login(c: TestClient, email: str = "push-user@example.com") -> str:
    challenge = c.post("/api/v1/auth/request-login", json={"email": email}).json()
    issued = c.post(
        "/api/v1/auth/verify",
        json={"email": email, "token": challenge["devToken"]},
    ).json()
    return issued["accessToken"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────


def test_register_device_round_trips(client: TestClient) -> None:
    access = _login(client)
    r = client.post(
        "/api/v1/notifications/register-device",
        headers=_bearer(access),
        json={
            "expoPushToken": "ExponentPushToken[abc123]",
            "platform": "ios",
            "label": "Test iPhone",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["platform"] == "ios"
    assert body["label"] == "Test iPhone"
    assert body["revokedAt"] is None


def test_register_device_is_idempotent(client: TestClient) -> None:
    """Re-registering the same (user, token) returns the SAME row id —
    not a new one. Otherwise a re-launch on the same device would orphan
    the prior row.
    """
    access = _login(client)
    headers = _bearer(access)
    payload = {
        "expoPushToken": "ExponentPushToken[idem-XYZ]",
        "platform": "ios",
        "label": "iPhone",
    }
    a = client.post("/api/v1/notifications/register-device", headers=headers, json=payload).json()
    b = client.post("/api/v1/notifications/register-device", headers=headers, json=payload).json()
    assert a["id"] == b["id"]


def test_list_devices_returns_only_caller(client: TestClient) -> None:
    alice = _login(client, "alice-push@example.com")
    bob = _login(client, "bob-push@example.com")

    client.post(
        "/api/v1/notifications/register-device",
        headers=_bearer(alice),
        json={"expoPushToken": "ExponentPushToken[alice]", "platform": "ios"},
    )
    client.post(
        "/api/v1/notifications/register-device",
        headers=_bearer(bob),
        json={"expoPushToken": "ExponentPushToken[bob]", "platform": "android"},
    )

    alice_list = client.get(
        "/api/v1/notifications/devices", headers=_bearer(alice)
    ).json()["devices"]
    bob_list = client.get(
        "/api/v1/notifications/devices", headers=_bearer(bob)
    ).json()["devices"]

    assert len(alice_list) == 1
    assert len(bob_list) == 1
    assert alice_list[0]["platform"] == "ios"
    assert bob_list[0]["platform"] == "android"


def test_revoke_device_flips_revoked_at(client: TestClient) -> None:
    access = _login(client)
    headers = _bearer(access)
    dev = client.post(
        "/api/v1/notifications/register-device",
        headers=headers,
        json={"expoPushToken": "ExponentPushToken[rev]", "platform": "ios"},
    ).json()

    r = client.delete(f"/api/v1/notifications/devices/{dev['id']}", headers=headers)
    assert r.status_code == 200
    assert r.json()["revokedAt"] is not None


def test_revoke_other_users_device_is_404(client: TestClient) -> None:
    alice = _login(client, "alice-rev@example.com")
    bob = _login(client, "bob-rev@example.com")
    dev = client.post(
        "/api/v1/notifications/register-device",
        headers=_bearer(alice),
        json={"expoPushToken": "ExponentPushToken[alice-rev]", "platform": "ios"},
    ).json()
    r = client.delete(
        f"/api/v1/notifications/devices/{dev['id']}", headers=_bearer(bob)
    )
    assert r.status_code == 404


def test_register_requires_real_auth(client: TestClient) -> None:
    """DEV_AUTH_BYPASS doesn't apply to /notifications/* either."""
    r = client.post(
        "/api/v1/notifications/register-device",
        json={"expoPushToken": "ExponentPushToken[x]", "platform": "ios"},
    )
    assert r.status_code == 401


# ─────────────────────────────────────────────────────────────────────
# Expo Push client
# ─────────────────────────────────────────────────────────────────────


def test_expo_push_sends_and_handles_device_not_registered() -> None:
    """Mock Expo's response with one ok ticket + one DeviceNotRegistered.
    The client should report sent=1 + revoked=1 + invoke the callback.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, list)
        # Two messages in the batch — return one OK + one error.
        return httpx.Response(
            200,
            json={
                "data": [
                    {"status": "ok", "id": "ticket-1"},
                    {
                        "status": "error",
                        "message": "Token invalid",
                        "details": {"error": "DeviceNotRegistered"},
                    },
                ]
            },
        )

    revoked: list[str] = []

    async def _revoke(token: str) -> None:
        revoked.append(token)

    async def _run() -> expo_push.PushResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await expo_push.send_push(
                [
                    expo_push.PushMessage(to="ExponentPushToken[ok]", title="t", body="b"),
                    expo_push.PushMessage(to="ExponentPushToken[dead]", title="t", body="b"),
                ],
                client=c,
                revoke_token=_revoke,
            )

    result = asyncio.run(_run())
    assert result.sent == 1
    assert result.revoked_tokens == ["ExponentPushToken[dead]"]
    assert revoked == ["ExponentPushToken[dead]"]
    assert result.other_errors == []


def test_expo_push_swallows_5xx() -> None:
    """A 5xx from Expo must NOT raise — gets logged + recorded in
    ``other_errors`` so the council route stays clean.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream gone")

    async def _run() -> expo_push.PushResult:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await expo_push.send_push(
                [expo_push.PushMessage(to="ExponentPushToken[x]", title="t", body="b")],
                client=c,
            )

    result = asyncio.run(_run())
    assert result.sent == 0
    assert result.revoked_tokens == []
    assert any("5xx" in e for e in result.other_errors)


def test_expo_push_empty_batch_is_noop() -> None:
    async def _run() -> expo_push.PushResult:
        return await expo_push.send_push([])

    result = asyncio.run(_run())
    assert result.sent == 0


# ─────────────────────────────────────────────────────────────────────
# Council hook
# ─────────────────────────────────────────────────────────────────────


def test_agent_run_schedules_push_to_registered_devices(
    client: TestClient, monkeypatch
) -> None:
    """End-to-end: register a device, run the council, confirm the
    fan-out task calls our patched send_push with the right payload.
    """
    access = _login(client, "council-push@example.com")
    client.post(
        "/api/v1/notifications/register-device",
        headers=_bearer(access),
        json={"expoPushToken": "ExponentPushToken[run-test]", "platform": "ios"},
    )

    captured: list[list[expo_push.PushMessage]] = []

    async def fake_send_push(messages, *, client=None, revoke_token=None):  # noqa: ANN001
        captured.append(list(messages))
        return expo_push.PushResult(sent=len(messages), revoked_tokens=[], other_errors=[])

    # Patch the symbol the notifications service imported.
    from app.services import notifications as notif_mod

    monkeypatch.setattr(notif_mod, "send_push", fake_send_push)

    r = client.post(
        "/api/v1/agent/run",
        headers=_bearer(access),
        json={"symbol": "NVDA"},
    )
    assert r.status_code == 200, r.text

    # The fan-out task is scheduled via asyncio.create_task in the route.
    # TestClient pumps the loop synchronously, so by the time we get here
    # the task has already run — but if it raced, retry a few ticks.
    async def _drain_loop() -> None:
        for _ in range(10):
            if captured:
                return
            await asyncio.sleep(0.01)

    asyncio.run(_drain_loop())

    # We may not always get a proposal (mock council can HOLD); only assert
    # the push fan-out when the council actually produced one.
    if r.json().get("proposal") is not None:
        assert captured, "council produced a proposal but no push was scheduled"
        batch = captured[0]
        assert len(batch) == 1
        msg = batch[0]
        assert msg.to == "ExponentPushToken[run-test]"
        assert "NVDA" in msg.body
        assert msg.data == {"kind": "proposal_pending"}


def test_agent_run_with_no_devices_is_silent(client: TestClient, monkeypatch) -> None:
    """The push fan-out should be a no-op when the user has no devices —
    not raise, not log an error at WARNING. Validate by checking that
    send_push is never called.
    """
    access = _login(client, "no-devices@example.com")

    called = False

    async def fake_send_push(*_args, **_kwargs):  # noqa: ANN001
        nonlocal called
        called = True
        return expo_push.PushResult(sent=0, revoked_tokens=[], other_errors=[])

    from app.services import notifications as notif_mod

    monkeypatch.setattr(notif_mod, "send_push", fake_send_push)

    client.post("/api/v1/agent/run", headers=_bearer(access), json={"symbol": "NVDA"})

    async def _wait() -> None:
        await asyncio.sleep(0.05)

    asyncio.run(_wait())
    assert called is False


# ─────────────────────────────────────────────────────────────────────
# Store unit tests
# ─────────────────────────────────────────────────────────────────────


def test_store_revoke_by_token_revokes_all_matching_rows() -> None:
    """Helper for Expo's DeviceNotRegistered cleanup."""
    store = get_notification_store()

    async def _run() -> None:
        rec = await store.register_device(
            user_id="user-a",
            expo_push_token="ExponentPushToken[same]",
            platform="ios",
        )
        assert rec.revoked_at is None
        await store.revoke_by_token("ExponentPushToken[same]")
        fresh = await store.get_device(rec.id)
        assert fresh is not None
        assert fresh.revoked_at is not None

    asyncio.run(_run())
