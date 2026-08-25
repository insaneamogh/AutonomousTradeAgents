"""Scheduled council runs — the thing that makes picks appear on their own.

Everything needed for an autonomous scan already existed: ``daily_cron``
walks a watchlist, runs the council per symbol, writes the decision row,
and pushes a "new proposal" notification. Nothing ever *invoked* it —
there was no Railway cron service and no GitHub Action — so picks only
appeared when a human tapped Run on one ticker at a time.

This runs that same pass on a schedule inside the API process, next to
the reconciler, which is already an in-process background task. One
deploy, no second service to keep in sync, and it inherits the API's
env and DB pool.

Single-instance assumption: two API replicas would each fire the pass.
The idempotency guard in ``daily_cron`` (one decision per user+symbol+UTC
day) makes a double-fire harmless rather than a double-trade, but the
scheduler should move to a dedicated Railway cron service before the
API is ever scaled out. ``UVICORN_WORKERS`` is pinned to 1 today, which
is what keeps this correct.

Config:
  COUNCIL_SCHEDULER_ENABLED   1 to turn it on (default off — an explicit
                              opt-in, because it spends LLM budget).
  COUNCIL_SCAN_TIMES_UTC      Comma-separated HH:MM, default "14:00"
                              (10:00 ET — half an hour after the open, so
                              the opening auction has settled).
  AGENT_CRON_USER_ID          Whose watchlist/decisions. Defaults to the
                              fixture user.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime, timedelta

logger = logging.getLogger("api.services.council.scheduler")

DEFAULT_SCAN_TIMES = "14:00"
_FIXTURE_USER = "00000000-0000-0000-0000-000000000001"


def _enabled() -> bool:
    v = os.environ.get("COUNCIL_SCHEDULER_ENABLED", "")
    return v.strip().lower() in ("1", "true", "yes", "on")


def _scan_times() -> list[tuple[int, int]]:
    """Parse COUNCIL_SCAN_TIMES_UTC into sorted (hour, minute) pairs."""
    raw = os.environ.get("COUNCIL_SCAN_TIMES_UTC", "").strip() or DEFAULT_SCAN_TIMES
    out: list[tuple[int, int]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            hh, mm = chunk.split(":")
            h, m = int(hh), int(mm)
        except ValueError:
            logger.warning("ignoring malformed COUNCIL_SCAN_TIMES_UTC entry %r", chunk)
            continue
        if 0 <= h <= 23 and 0 <= m <= 59:
            out.append((h, m))
        else:
            logger.warning("ignoring out-of-range scan time %r", chunk)
    return sorted(set(out)) or [(14, 0)]


def _seconds_until_next(now: datetime, times: list[tuple[int, int]]) -> float:
    """Seconds from ``now`` to the next scheduled scan (today or tomorrow)."""
    candidates = [
        now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in times
    ]
    future = [c for c in candidates if c > now]
    nxt = future[0] if future else candidates[0] + timedelta(days=1)
    return max(1.0, (nxt - now).total_seconds())


class CouncilScheduler:
    """Fires the daily council pass at configured UTC times."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self.last_run_at: datetime | None = None
        self.last_result: dict[str, int] | str | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        times = _scan_times()
        logger.info(
            "council scheduler armed — scan times (UTC): %s",
            ", ".join(f"{h:02d}:{m:02d}" for h, m in times),
        )
        while True:
            delay = _seconds_until_next(datetime.now(UTC), times)
            logger.info("next council scan in %.0f min", delay / 60)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduled council scan failed — will retry next window")
                self.last_result = "failed"
            # Guard against a scan finishing inside the same minute it
            # started, which would otherwise re-fire immediately.
            await asyncio.sleep(61)

    async def _run_once(self) -> None:
        """One full watchlist pass. Delegates to the existing cron entry point.

        ``daily_cron.main`` owns the market-calendar gate, the per-symbol
        idempotency check, the push notification, and the ghost/reflection
        follow-ups — this only decides *when*.
        """
        from trading_agents.jobs.daily_cron import DEFAULT_WATCHLIST
        from trading_agents.jobs.daily_cron import main as cron_main

        user_id = os.environ.get("AGENT_CRON_USER_ID", "").strip() or _FIXTURE_USER
        raw = os.environ.get("AGENT_CRON_WATCHLIST", "").strip()
        watchlist = [s.strip().upper() for s in raw.split(",") if s.strip()] or list(
            DEFAULT_WATCHLIST
        )

        logger.info("council scan starting — %d symbols", len(watchlist))
        started = datetime.now(UTC)
        code = await cron_main(user_id, watchlist, force=False)
        self.last_run_at = started
        self.last_result = {"exit_code": code, "symbols": len(watchlist)}
        logger.info("council scan finished — exit=%s", code)


_scheduler: CouncilScheduler | None = None


def get_council_scheduler() -> CouncilScheduler | None:
    """The running scheduler, or None when it was never started."""
    return _scheduler


def start_council_scheduler() -> CouncilScheduler | None:
    """Start the scheduler when enabled. Returns it, or None when off."""
    global _scheduler
    if not _enabled():
        logger.info(
            "council scheduler disabled (set COUNCIL_SCHEDULER_ENABLED=1 to arm it)"
        )
        return None
    if _scheduler is None:
        _scheduler = CouncilScheduler()
        _scheduler.start()
    return _scheduler


async def stop_council_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        await _scheduler.stop()
        _scheduler = None
