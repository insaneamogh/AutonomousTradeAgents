"""Zerodha daily-reconnect reminder cron.

Kite Connect access tokens are flushed ~06:00 IST every morning and there
are no refresh tokens — the user must re-login each trading day (see
apps/api/AUTH.md, "Zerodha (Kite Connect) connect flow"). This script
pushes a "reconnect before market open" notification to every user who
has an active zerodha connection with an expired token.

Schedule it ONCE per weekday at 09:00 IST (03:30 UTC) — before NSE opens
at 09:15 IST:

  GitHub Actions:  schedule: - cron: '30 3 * * 1-5'
  Fly machines:    fly machine schedule against this script

Idempotency: deliberately none beyond the expiry check. The scheduler
fires once daily; re-running by hand re-sends the reminder, which is the
behavior an operator doing a manual nudge actually wants. (A valid,
unexpired token still short-circuits to a skip unless --force.)

Usage:

    PYTHONPATH=apps/api:apps/agents:packages/engine:packages/broker \\
    USE_POSTGRES=1 \\
    python apps/api/scripts/zerodha_reconnect_cron.py [--force] [--user-id UUID]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.services.broker_store import get_broker_store
from app.services.notifications import send_zerodha_reconnect_notification

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s — %(message)s",
)
log = logging.getLogger("api.cron.zerodha_reconnect")


async def run(*, force: bool = False, only_user_id: str | None = None) -> int:
    """Fan the reminder out to every user with an active zerodha connection.

    Returns the total number of pushes sent. Per-user failures are logged
    and skipped — one broken device row must not starve other users of
    their reminder.
    """
    store = get_broker_store()
    conns = await store.list_active_connections_by_broker("zerodha")
    user_ids = sorted({c.user_id for c in conns})
    if only_user_id is not None:
        user_ids = [u for u in user_ids if u == only_user_id]

    if not user_ids:
        log.info("no active zerodha connections — nothing to do")
        return 0

    total = 0
    for user_id in user_ids:
        try:
            total += await send_zerodha_reconnect_notification(
                user_id, broker_store=store, force=force
            )
        except Exception as exc:  # noqa: BLE001 — continue past per-user failures
            log.warning("user=%s reminder failed — %s", user_id, exc)
    log.info("done — users=%d pushes_sent=%d", len(user_ids), total)
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Zerodha daily-reconnect reminder")
    parser.add_argument(
        "--force",
        action="store_true",
        help="send even when the stored token hasn't expired yet (manual smoke)",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="limit the fan-out to one user (manual nudge)",
    )
    args = parser.parse_args()
    asyncio.run(run(force=args.force, only_user_id=args.user_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
