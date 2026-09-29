"""Zerodha daily-reconnect reminder cron.

Kite Connect access tokens are flushed ~06:00 IST every morning and there
are no refresh tokens — the user must re-login each trading day. Nothing
in the repo currently documents that connect/token-refresh flow in detail
— the doc that once covered it was dropped in a docs consolidation and
was not replaced. This script pushes a "reconnect before market open"
notification to every user who has an active zerodha connection with an
expired token.

The council scheduler sends this reminder itself at 08:30 IST on NSE
trading days (``ZERODHA_RECONNECT_REMINDER_ENABLED``, on by default), so
this script is for a manual nudge or a smoke test (``--force``).

Idempotency: deliberately none beyond the expiry check. Whatever triggers
a run is expected to fire at most once a day in the ordinary case;
re-running by hand re-sends the reminder, which is the behavior an
operator doing a manual nudge actually wants. (A valid, unexpired token
still short-circuits to a skip unless --force.)

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

from app.services.notifications.notifications import send_zerodha_reconnect_reminders

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s — %(message)s",
)
log = logging.getLogger("api.cron.zerodha_reconnect")


async def run(*, force: bool = False, only_user_id: str | None = None) -> int:
    """Fan the reminder out to every user with an active zerodha connection
    (``send_zerodha_reconnect_reminders``). Returns pushes sent."""
    return await send_zerodha_reconnect_reminders(force=force, only_user_id=only_user_id)


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
