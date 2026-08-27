"""Notification fan-out: pushes, e-mail, and the device-token store.

Re-exports the notification entry points at package level. The package
was split out of a flat ``services/`` directory, and cross-app callers —
notably ``trading_agents.jobs.daily_cron`` — import from the package name
rather than the module inside it. Without these names the daily cron's
"new proposal" push raised ImportError on every approved proposal and was
swallowed by its own best-effort ``except``, so scheduled picks landed in
the database and notified nobody.
"""

from app.services.notifications.notifications import (
    schedule_position_event_notification,
    schedule_proposal_pending_notification,
    send_zerodha_reconnect_notification,
)

__all__ = [
    "schedule_position_event_notification",
    "schedule_proposal_pending_notification",
    "send_zerodha_reconnect_notification",
]
