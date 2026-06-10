"""Council ↔ push fan-out hook.

Called from the /agent/run route AFTER a proposal lands in the pending
queue. Sends a "New proposal" push to every active device for the user.

Architectural notes:
  - Fire-and-forget via ``asyncio.create_task``. The council route MUST
    NOT wait on Expo Push — degrades P95 latency + blocks on network
    flakiness.
  - Notification bodies are LOCK-SCREEN-SAFE. Per AGENTV1.md DO NOT:
    no broker tokens, no proposal IDs, no PII. Just side + qty + symbol.
  - We don't include the proposal_id as payload data here. Mobile reads
    /approvals/pending on tap; the id-from-payload optimization lands
    in a follow-on once we have an ack endpoint.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from app.services.expo_push import PushMessage, send_push
from app.services.notification_store import (
    NotificationStore,
    get_notification_store,
)

logger = logging.getLogger("api.notifications")


class BrokerStoreLike(Protocol):
    """The one BrokerStore method the reconnect reminder needs — keeps this
    module decoupled from the full store protocol."""

    async def list_connections(self, user_id: str) -> list[Any]: ...


def schedule_proposal_pending_notification(
    *,
    user_id: str,
    proposal: dict[str, Any],
    store: NotificationStore | None = None,
) -> asyncio.Task[None]:
    """Schedule a fan-out push. Returns the Task so tests can ``await`` it;
    production callers fire-and-forget.
    """
    s = store or get_notification_store()
    return asyncio.create_task(_fan_out(user_id=user_id, proposal=proposal, store=s))


async def _fan_out(
    *,
    user_id: str,
    proposal: dict[str, Any],
    store: NotificationStore,
) -> None:
    try:
        devices = await store.list_active_devices(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("notifications: device lookup failed for %s — %s", user_id, exc)
        return

    if not devices:
        logger.debug("notifications: no active devices for user=%s", user_id)
        return

    side = str(proposal.get("side", "?")).upper()
    symbol = str(proposal.get("symbol", "?"))
    qty = proposal.get("qty")
    title = "New trade proposal"
    body = f"{side} {qty} {symbol} — tap to review" if qty is not None else f"{side} {symbol}"

    msgs = [
        PushMessage(
            to=d.expo_push_token,
            title=title,
            body=body,
            data={"kind": "proposal_pending"},
        )
        for d in devices
    ]

    async def _revoke_token(token: str) -> None:
        await store.revoke_by_token(token)

    try:
        result = await send_push(msgs, revoke_token=_revoke_token)
    except Exception as exc:  # noqa: BLE001
        logger.warning("notifications: send_push raised unexpectedly — %s", exc)
        return

    logger.info(
        "notifications: fanned out proposal — user=%s sent=%d revoked=%d errors=%d",
        user_id, result.sent, len(result.revoked_tokens), len(result.other_errors),
    )


# ─────────────────────────────────────────────────────────────────────
# Zerodha daily-reconnect reminder
# ─────────────────────────────────────────────────────────────────────


async def send_zerodha_reconnect_notification(
    user_id: str,
    *,
    broker_store: BrokerStoreLike | None = None,
    store: NotificationStore | None = None,
    force: bool = False,
) -> int:
    """Push "reconnect Zerodha" to the user's devices when their Kite token
    is expired (it flushes ~06:00 IST daily). Returns the number of pushes
    sent — 0 means skipped (no zerodha connection / token still valid / no
    devices). ``force=True`` skips the expiry check (manual smoke).

    Called by ``apps/api/scripts/zerodha_reconnect_cron.py`` at 09:00 IST,
    before NSE opens at 09:15.
    """
    from app.services.broker_store import get_broker_store

    bs = broker_store or get_broker_store()
    ns = store or get_notification_store()

    rows = await bs.list_connections(user_id)
    conn = next(
        (r for r in rows if r.broker == "zerodha" and r.status == "active"), None
    )
    if conn is None:
        logger.debug("zerodha-reconnect: user=%s has no active zerodha — skip", user_id)
        return 0

    if not force and conn.access_token_expires_at is not None:
        expires = conn.access_token_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires > datetime.now(timezone.utc):
            logger.debug(
                "zerodha-reconnect: user=%s token valid until %s — skip",
                user_id, expires,
            )
            return 0

    devices = await ns.list_active_devices(user_id)
    if not devices:
        logger.debug("zerodha-reconnect: user=%s has no active devices — skip", user_id)
        return 0

    # Lock-screen-safe: no account numbers, no tokens.
    msgs = [
        PushMessage(
            to=d.expo_push_token,
            title="Zerodha: reconnect before market open",
            body="Kite tokens expire daily — log in again to trade today. Tap to open Settings.",
            data={"kind": "zerodha_reconnect"},
        )
        for d in devices
    ]

    async def _revoke_token(token: str) -> None:
        await ns.revoke_by_token(token)

    try:
        result = await send_push(msgs, revoke_token=_revoke_token)
    except Exception as exc:  # noqa: BLE001 — a reminder must never crash the cron
        logger.warning("zerodha-reconnect: send_push raised — %s", exc)
        return 0

    logger.info(
        "zerodha-reconnect: user=%s sent=%d revoked=%d errors=%d",
        user_id, result.sent, len(result.revoked_tokens), len(result.other_errors),
    )
    return result.sent
