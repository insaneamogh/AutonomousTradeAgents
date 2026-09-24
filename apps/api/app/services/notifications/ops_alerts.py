"""Operator alerts for the failures that used to be a single log line.

docs/PLAN_PLATFORM.md Phase 6. Every one of these happened here, and each
was found days later by a human reading logs, not by the system:

  breaker_tripped         2026-09-08: the -3% halt latched and the desk sat
                          halted until someone noticed.
  protective_stop_failed  2026-09-02: every resting stop failed from its first
                          live fill (a bad column name). Nothing alerted.
  llm_auth_failed         2026-09-02: the key was rejected (401) and every pass
                          failed while the scheduler kept running.
  council_refused_start   AGENTS_REQUIRE_REAL_LLM tripped. The cron exits 2
                          and nothing else happens.
  sweep_failed            A scheduled sweep raised. Logged, then it waits for
                          tomorrow.
  option_exercised        An option left the account as stock (exercise or
  option_assigned         assignment): 100 shares per contract that no
                          decision manages, with no stop.

One call, three channels, each best-effort and independent:
  1. `logger.error`. Sentry captures ERROR-level logs when SENTRY_DSN is set.
  2. A push to the user's devices, when a user is known.
  3. OPS_ALERT_WEBHOOK_URL, when set: a Slack-compatible `{"text": ...}`
     POST, for an operator who is not watching the app. The URL is a secret
     (a Slack webhook URL IS its own credential), so it is never logged.

De-duplicated per (kind, key) for `min_interval_s`. A breaker tripped on a
30s reconciler tick must page once, not 720 times a day. The window is
per process, so a redeploy can repeat an alert once, which is acceptable
for a pager.

Never raises. An alerting failure must not take down the path that was
trying to report a failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

logger = logging.getLogger("api.ops_alerts")

DEFAULT_MIN_INTERVAL_S = 6 * 3600.0

_last_sent: dict[tuple[str, str], float] = {}


def reset_ops_alerts_for_tests() -> None:
    _last_sent.clear()


def raise_ops_alert(
    kind: str,
    *,
    title: str,
    body: str,
    user_id: str | None = None,
    key: str = "",
    min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
) -> bool:
    """Dispatch an alert. True if sent, False if de-duplicated. Push and
    webhook are scheduled on the running loop, fire-and-forget."""
    try:
        now = time.monotonic()
        dedupe = (kind, key)
        last = _last_sent.get(dedupe)
        if last is not None and now - last < min_interval_s:
            return False
        _last_sent[dedupe] = now

        logger.error("OPS ALERT [%s] %s: %s", kind, title, body)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            if user_id:
                from app.services.notifications.notifications import (
                    schedule_position_event_notification,
                )

                schedule_position_event_notification(
                    user_id=user_id, title=title, body=body, data_kind="ops_alert"
                )
            url = os.environ.get("OPS_ALERT_WEBHOOK_URL", "").strip()
            if url:
                loop.create_task(_post_webhook(url, f"[{kind}] {title}: {body}"))
        return True
    except Exception:
        # Logging can be the thing that failed, and logger.exception goes
        # through the same handlers, so even reporting the failure is guarded.
        with contextlib.suppress(Exception):
            logger.exception("ops alert %s could not be dispatched", kind)
        return False


async def _post_webhook(url: str, text: str) -> None:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            resp = await client.post(url, json={"text": text})
            if resp.status_code >= 400:
                logger.warning("ops alert webhook returned HTTP %d", resp.status_code)
    except Exception as exc:
        # Type only: httpx error messages embed the URL, and the URL is a secret.
        logger.warning("ops alert webhook failed: %s", type(exc).__name__)
