"""Expo Push API client — hand-rolled httpx POST.

Sends fan-out push notifications via https://exp.host/--/api/v2/push/send.
We deliberately don't pull ``expo-server-sdk-python`` — the published
API is one well-documented POST endpoint, and a dep we'd need to track
for security advisories on top of the broker/auth stack we already own.

Behavior:
  - Chunks of 100 messages per request (the Expo Push limit).
  - **Fail-soft**: when Expo returns a per-ticket error like
    ``DeviceNotRegistered`` we mark the token revoked locally via the
    ``revoke_token`` callback. We DON'T raise — the council route that
    called us shouldn't 5xx because one stale device is dead.
  - Errors that don't carry a token-specific code (network blip, 5xx
    from Expo, malformed batch) are logged + swallowed. Per
    AGENTV1.md's "don't block the council route on push fan-out."
  - The receipt-ID poll loop (Expo's "did it land?") is out of scope for
    Phase 3 — mobile UX cares whether the OS got the push, not whether
    APNs/FCM accepted it later. Phase 4 hardening adds it.

Test seam: the module exposes ``send_push`` which takes an injectable
``httpx.AsyncClient``. Test_notifications passes a MockTransport.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

import httpx

logger = logging.getLogger("api.expo_push")


EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
MAX_PER_BATCH = 100
"""Expo Push API's documented limit per POST."""


@dataclass(frozen=True)
class PushMessage:
    to: str
    """Expo push token, e.g. ``ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]``."""
    title: str
    body: str
    data: dict[str, str] | None = None
    """Custom data — read by the mobile when the user taps the push.
    Keep it small + side-channel-safe (no broker tokens, no PII)."""
    sound: str = "default"
    priority: str = "high"


@dataclass
class PushResult:
    sent: int
    revoked_tokens: list[str]
    """Tokens Expo flagged as DeviceNotRegistered. The store handles revoke."""
    other_errors: list[str]


async def send_push(
    messages: list[PushMessage],
    *,
    client: httpx.AsyncClient | None = None,
    revoke_token: Callable[[str], Awaitable[None]] | None = None,
) -> PushResult:
    """Send a batch of pushes. Returns a result summary; never raises."""
    if not messages:
        return PushResult(sent=0, revoked_tokens=[], other_errors=[])

    owned = False
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)
        owned = True

    revoked: list[str] = []
    other: list[str] = []
    sent = 0

    try:
        for chunk in _chunked(messages, MAX_PER_BATCH):
            payload = [_to_payload(m) for m in chunk]
            try:
                resp = await client.post(
                    EXPO_PUSH_URL,
                    json=payload,
                    headers={"accept": "application/json", "content-type": "application/json"},
                )
            except httpx.HTTPError as exc:
                other.append(f"network: {exc}")
                logger.warning("expo push: network error sending chunk: %s", exc)
                continue

            if resp.status_code >= 500:
                other.append(f"5xx from Expo: {resp.status_code}")
                logger.warning("expo push: 5xx from Expo (%s)", resp.status_code)
                continue
            if resp.status_code >= 400:
                other.append(f"4xx from Expo: {resp.status_code} {resp.text[:200]}")
                logger.warning("expo push: 4xx from Expo (%s) — %s", resp.status_code, resp.text[:200])
                continue

            data = resp.json().get("data", [])
            for msg, ticket in zip(chunk, data):
                if not isinstance(ticket, dict):
                    other.append(f"malformed ticket: {ticket!r}")
                    continue
                status = ticket.get("status")
                if status == "ok":
                    sent += 1
                    continue
                # status == "error" — look at details.error for the code.
                details = ticket.get("details") or {}
                err_code = details.get("error")
                if err_code == "DeviceNotRegistered":
                    revoked.append(msg.to)
                    if revoke_token is not None:
                        try:
                            await revoke_token(msg.to)
                        except Exception as rev_exc:  # noqa: BLE001
                            logger.warning(
                                "expo push: revoke callback failed for %s — %s",
                                msg.to, rev_exc,
                            )
                else:
                    other.append(f"{err_code or 'unknown'}: {ticket.get('message', '')}")
    finally:
        if owned:
            await client.aclose()

    return PushResult(sent=sent, revoked_tokens=revoked, other_errors=other)


def _chunked(seq: list[PushMessage], n: int) -> Iterable[list[PushMessage]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _to_payload(m: PushMessage) -> dict[str, object]:
    p: dict[str, object] = {
        "to": m.to,
        "title": m.title,
        "body": m.body,
        "sound": m.sound,
        "priority": m.priority,
    }
    if m.data:
        p["data"] = m.data
    return p
