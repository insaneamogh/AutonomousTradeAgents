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

Two loops, deliberately different in cost:

  * **Baseline sweep** — the full watchlist at fixed UTC times. Every
    symbol gets a council pass whether or not anything moved. This is
    the daily "what do we think about the book" opinion.

  * **Trigger loop** — a cheap deterministic scan every few minutes
    (``engine.scanner``, zero LLM), which wakes the council ONLY for
    symbols where a named rule fired. This is what "scans throughout the
    day" means without paying for an LLM pass per symbol per interval:
    the council costs ~$0.066/symbol, so a 15-symbol sweep every 15
    minutes would be ~$538/month re-deriving mostly-unchanged daily bars.
    Triggered runs cost the same per pass but happen only when the
    market actually did something.

Config:
  COUNCIL_SCHEDULER_ENABLED   1 to turn it on (default off — an explicit
                              opt-in, because it spends LLM budget).
  COUNCIL_SCAN_TIMES_UTC      Comma-separated HH:MM, default "14:00"
                              (10:00 ET — half an hour after the open, so
                              the opening auction has settled).
  COUNCIL_BASELINE_ENABLED    1 (default) to keep the fixed-time sweep.
  SCANNER_ENABLED             1 to arm the trigger loop (default off).
  SCANNER_INTERVAL_MINUTES    Trigger-loop cadence, default 5.
  SCANNER_MAX_COUNCIL_RUNS    Per-scan ceiling on triggered council runs,
                              default 3. A budget stop: a violent market
                              open can trip many rules at once, and this
                              caps the spend rather than trusting the
                              thresholds to stay conservative forever.
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


def _flag(name: str, *, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _enabled() -> bool:
    return _flag("COUNCIL_SCHEDULER_ENABLED")


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        logger.warning("ignoring malformed %s — using %d", name, default)
        return default


def _watchlist() -> list[str]:
    """Configured watchlist, falling back to the cron's default set."""
    from trading_agents.jobs.daily_cron import DEFAULT_WATCHLIST

    raw = os.environ.get("AGENT_CRON_WATCHLIST", "").strip()
    return [s.strip().upper() for s in raw.split(",") if s.strip()] or list(
        DEFAULT_WATCHLIST
    )


def _cron_user() -> str:
    return os.environ.get("AGENT_CRON_USER_ID", "").strip() or _FIXTURE_USER


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
        self._tasks: list[asyncio.Task[None]] = []
        self.last_run_at: datetime | None = None
        self.last_result: dict[str, int] | str | None = None
        # Trigger-loop observability, surfaced on /health/full.
        self.last_scan_at: datetime | None = None
        self.last_scan_signals: int = 0
        self.last_triggered: tuple[str, ...] = ()

    def start(self) -> None:
        if self._tasks:
            return
        if _flag("COUNCIL_BASELINE_ENABLED", default=True):
            self._tasks.append(asyncio.create_task(self._baseline_loop()))
        else:
            logger.info("baseline sweep disabled (COUNCIL_BASELINE_ENABLED=0)")
        if _flag("SCANNER_ENABLED"):
            self._tasks.append(asyncio.create_task(self._trigger_loop()))
        else:
            logger.info("trigger loop disabled (set SCANNER_ENABLED=1 to arm it)")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks = []

    async def _baseline_loop(self) -> None:
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

    # ── Trigger loop ─────────────────────────────────────────────────
    #
    # Cheap deterministic scan → council only on a named trigger. The
    # scanner does no LLM work, so this can run every few minutes; the
    # expensive part fires only when a rule actually trips.

    async def _trigger_loop(self) -> None:
        from engine.scanner import scanner_from_env

        scanner = scanner_from_env()
        if scanner is None:
            logger.warning(
                "SCANNER_ENABLED=1 but Alpaca data keys are missing — "
                "trigger loop not started"
            )
            return

        interval = _int_env("SCANNER_INTERVAL_MINUTES", 5) * 60
        max_runs = _int_env("SCANNER_MAX_COUNCIL_RUNS", 3)
        logger.info(
            "trigger loop armed — scanning every %d min, max %d council runs per scan",
            interval // 60, max_runs,
        )

        while True:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            try:
                await self._scan_once(scanner, max_runs)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scan failed — retrying next interval")

    async def _scan_once(self, scanner: object, max_runs: int) -> None:
        """One deterministic scan; council runs only for triggered symbols."""
        from trading_agents.jobs.daily_cron import SymbolScanContext
        from trading_agents.jobs.daily_cron import main as cron_main

        symbols = _watchlist()
        result = await scanner.scan(symbols)  # type: ignore[attr-defined]

        self.last_scan_at = result.scanned_at
        self.last_scan_signals = len(result.signals)

        if not result.market_open:
            logger.debug("scan skipped — market closed")
            return

        triggered = result.triggered_symbols
        self.last_triggered = triggered
        if not triggered:
            logger.debug(
                "scan clean — %d symbols, no triggers (%d suppressed by cooldown)",
                len(result.symbols_scanned), len(result.suppressed),
            )
            return

        # Budget stop. A violent open can trip many rules at once; cap the
        # spend rather than trusting the thresholds to stay conservative.
        selected = list(triggered[:max_runs])
        if len(triggered) > max_runs:
            logger.warning(
                "%d symbols triggered, running the first %d (SCANNER_MAX_COUNCIL_RUNS)",
                len(triggered), max_runs,
            )

        # Hand the council WHY it was woken, so the analysts see the named
        # rule rather than arriving with no more context than a sweep.
        scan_context = {
            sym: SymbolScanContext(
                signals=result.signals_for(sym),
                relative_strength_rank=result.relative_strength.get(sym),
            )
            for sym in selected
        }
        for sym in selected:
            rules = ", ".join(s.trigger_rule for s in result.signals_for(sym))
            logger.info("triggered council run: %s (%s)", sym, rules)

        started = datetime.now(UTC)
        # force=True: the scanner already decided this symbol deserves a
        # look. The cron's own per-user-per-symbol-per-day guard would
        # otherwise suppress a genuine intraday trigger after the
        # baseline sweep had already covered that symbol today.
        code = await cron_main(
            _cron_user(),
            selected,
            force=True,
            skip_ghost_eval=True,
            skip_reflect=True,
            scan_context=scan_context,
        )
        self.last_run_at = started
        self.last_result = {"exit_code": code, "symbols": len(selected), "triggered": 1}

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
