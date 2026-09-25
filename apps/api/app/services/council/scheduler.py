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
  UNIVERSE_REFRESH_ENABLED    1 to arm a third, much cheaper daily loop
                              (default off — explicit opt-in, same as the
                              other two): once/day, screens Alpaca's real
                              tradable universe (no LLM calls at all — see
                              trading_agents.jobs.universe_refresh) and
                              writes the survivors into user_watchlist's
                              auto-discovered tier, which the baseline/
                              trigger loops above then sweep like any
                              other watchlist row. Zero LLM cost of its
                              own; it only widens what the other two loops
                              already do.
  UNIVERSE_REFRESH_HOUR_UTC   Hour (0-23) the daily refresh fires, default
                              12 — before the first scan time and before
                              the 13:30 UTC US market open, so freshly
                              auto-discovered symbols are in the watchlist
                              before anything sweeps it that day.
  IV_SNAPSHOT_ENABLED         1 (DEFAULT ON) for the daily IV-history
                              recorder: one chain snapshot per options
                              underlying, written to iv_history. Default
                              on, unlike the loops above, because it
                              spends no LLM budget, only reads the broker,
                              and every day it does not run is a day of
                              history that can never be recovered (IV rank
                              needs 60+ days of it).
  IV_SNAPSHOT_HOUR_UTC        Hour (0-23), default 20, fired at :15, i.e.
                              after the 16:00 ET close in daylight time
                              (an hour before it in standard time: still
                              a closing-hour snapshot).
  EOD_REPORT_ENABLED          1 (DEFAULT ON) for the end-of-day job:
                              ghost P&L marking, then the daily report
                              (log + OPS_ALERT_WEBHOOK_URL + a counts-only
                              push). No LLM spend. Runs whether or not the
                              council can, which is the point: ghost
                              marking used to run only inside the council
                              cron, and stopped when the LLM key did.
  EOD_REPORT_HOUR_UTC         Hour (0-23), default 21, fired at :15, i.e.
                              after the 16:00 ET close in both daylight
                              and standard time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from engine.scanner import ScanResult
    from trading_agents.jobs.daily_cron import SweepTally

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


def _universe_refresh_enabled() -> bool:
    return _flag("UNIVERSE_REFRESH_ENABLED")


def _universe_refresh_hour() -> int:
    try:
        h = int(os.environ.get("UNIVERSE_REFRESH_HOUR_UTC", "").strip() or 12)
    except ValueError:
        logger.warning("ignoring malformed UNIVERSE_REFRESH_HOUR_UTC — using 12")
        return 12
    return h if 0 <= h <= 23 else 12


def _iv_snapshot_hour() -> int:
    try:
        h = int(os.environ.get("IV_SNAPSHOT_HOUR_UTC", "").strip() or 20)
    except ValueError:
        logger.warning("ignoring malformed IV_SNAPSHOT_HOUR_UTC — using 20")
        return 20
    return h if 0 <= h <= 23 else 20


def _eod_report_hour() -> int:
    try:
        h = int(os.environ.get("EOD_REPORT_HOUR_UTC", "").strip() or 21)
    except ValueError:
        logger.warning("ignoring malformed EOD_REPORT_HOUR_UTC — using 21")
        return 21
    return h if 0 <= h <= 23 else 21


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        logger.warning("ignoring malformed %s — using %d", name, default)
        return default


def _env_watchlist() -> list[str]:
    """Configured watchlist from env, falling back to the cron's defaults."""
    from trading_agents.jobs.daily_cron import DEFAULT_WATCHLIST

    raw = os.environ.get("AGENT_CRON_WATCHLIST", "").strip()
    return [s.strip().upper() for s in raw.split(",") if s.strip()] or list(
        DEFAULT_WATCHLIST
    )


async def _watchlist_with_instruments() -> tuple[list[str], dict[str, str]]:
    """The curated watchlist as ``(symbols, {symbol: asset_class})``.

    ``daily_cron.cli()`` has always preferred the ``user_watchlist`` table
    — "tell the agent what you're interested in, it tracks those" — but
    the scheduler read only ``AGENT_CRON_WATCHLIST``. Since the scheduler
    is the path that actually runs in production, curating a watchlist in
    the app changed nothing: the scanner and the baseline sweep kept
    working off whatever the env var said.

    The ``asset_class`` half is what makes options reachable from a
    scheduled run at all — without it every sweep is an equity sweep no
    matter what the row says. Env-derived symbols carry no asset_class, so
    they map to equity, which is the safe default.

    Falls back to env on any load failure. An unreachable table must not
    stop the sweep from running at all.
    """
    from trading_agents.jobs.daily_cron import _load_user_watchlist

    if not _flag("USE_POSTGRES"):
        return _env_watchlist(), {}
    try:
        curated = await _load_user_watchlist(_cron_user())
    except Exception:
        logger.exception("user watchlist load failed — using the env list")
        return _env_watchlist(), {}
    if not curated:
        return _env_watchlist(), {}
    return [sym for sym, _ in curated], {sym: ac for sym, ac in curated}


async def _watchlist() -> list[str]:
    """Symbols only — for callers that don't care about the instrument."""
    symbols, _ = await _watchlist_with_instruments()
    return symbols


async def configured_watchlist() -> list[str]:
    """Public wrapper around ``_watchlist`` for callers outside this module
    (``scanner_status.py`` reports its size) — avoids reaching into an
    underscore-prefixed name from another module. Not named ``watchlist``
    because ``_run_once`` already has a same-named local variable."""
    return await _watchlist()


def scanner_enabled() -> bool:
    """Public wrapper — is ``SCANNER_ENABLED`` set, regardless of whether
    the trigger loop actually managed to arm (see ``trigger_loop_armed``)."""
    return _flag("SCANNER_ENABLED")


def _cron_user() -> str:
    return os.environ.get("AGENT_CRON_USER_ID", "").strip() or _FIXTURE_USER


def _scan_times(
    env: str = "COUNCIL_SCAN_TIMES_UTC", default: str = DEFAULT_SCAN_TIMES
) -> list[tuple[int, int]]:
    """Parse a scan-times env var into sorted (hour, minute) pairs."""
    raw = os.environ.get(env, "").strip() or default
    out: list[tuple[int, int]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            hh, mm = chunk.split(":")
            h, m = int(hh), int(mm)
        except ValueError:
            logger.warning("ignoring malformed %s entry %r", env, chunk)
            continue
        if 0 <= h <= 23 and 0 <= m <= 59:
            out.append((h, m))
        else:
            logger.warning("ignoring out-of-range scan time %r", chunk)
    if out:
        return sorted(set(out))
    h, m = default.split(",")[0].split(":")
    return [(int(h), int(m))]


IN_DEFAULT_SCAN_TIMES = "04:30"
"""10:00 IST: after the 09:15 open and its first-quarter-hour noise, with
the whole session left for the entry to fill (Kite orders are DAY)."""


def _for_market(symbols: list[str], market: str) -> list[str]:
    from engine.risk.markets import market_of

    return [s for s in symbols if market_of(s) == market]


def _seconds_until_next(now: datetime, times: list[tuple[int, int]]) -> float:
    """Seconds from ``now`` to the next scheduled scan (today or tomorrow)."""
    candidates = [
        now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in times
    ]
    future = [c for c in candidates if c > now]
    nxt = future[0] if future else candidates[0] + timedelta(days=1)
    return max(1.0, (nxt - now).total_seconds())


def _missed_a_scan_this_session(now: datetime, times: list[tuple[int, int]]) -> bool:
    """True when a scan time already passed TODAY and the market is open
    right now, i.e. a restart landed after today's sweep and there is still
    a session to trade in. Outside market hours there is nothing to catch
    up: the next scheduled time will do."""
    from engine.features import is_us_market_open

    passed = any(now.replace(hour=h, minute=m, second=0, microsecond=0) <= now for h, m in times)
    if not passed:
        return False
    try:
        return bool(is_us_market_open(now))
    except Exception:
        return False


class CouncilScheduler:
    """Fires the daily council pass at configured UTC times."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []
        self.last_run_at: datetime | None = None
        self.last_result: dict[str, int] | str | None = None
        # Trigger-loop observability, surfaced on /health/full and, in
        # full detail, on /api/v1/scanner/status.
        self.last_scan_at: datetime | None = None
        self.last_scan_signals: int = 0
        self.last_triggered: tuple[str, ...] = ()
        self.last_scan_result: ScanResult | None = None
        """The full ``ScanResult`` from the last trigger-loop pass — signals,
        suppressed count, market_open — not just the summary counters above.
        ``scanner_status.py`` reads this rather than duplicating a second
        set of fields."""
        self.last_council_run_symbols: tuple[str, ...] = ()
        """Symbols passed to the last triggered council run specifically
        (as opposed to ``last_result``, which both loops update)."""
        self.trigger_loop_armed: bool = False
        """True once ``_trigger_loop`` has a live ``Scanner`` — i.e.
        SCANNER_ENABLED=1 AND Alpaca data keys are present. False while
        SCANNER_ENABLED=0, and also false when it's 1 but the scanner
        couldn't be constructed — those are different states and the
        scanner-status endpoint tells them apart via ``scanner_enabled()``
        vs this flag."""
        self.scanner_interval_minutes: int | None = None
        self.scanner_max_council_runs: int | None = None
        self.last_universe_refresh_at: datetime | None = None
        self.last_universe_refresh_result: dict[str, int] | str | None = None
        self.last_iv_snapshot_at: datetime | None = None
        self.last_iv_snapshot_result: dict[str, int] | str | None = None
        self.last_eod_at: datetime | None = None
        self.last_eod_result: str | None = None
        # Tier 1/2 of the Insights "symbol scan funnel" — fed by
        # daily_cron.main's optional on_sweep_scored recorder, one slot
        # shared by both loops rather than two separate ones. A triggered
        # sweep's watchlist is tiny (1-3 symbols, only what a technical
        # rule flagged) — showing it next to a ~100-symbol baseline sweep
        # with no context would read as a broken funnel, so `kind` lets
        # the frontend caption the difference honestly instead of this
        # scheduler silently preferring one loop's data over the other's.
        self.last_sweep_tally: SweepTally | None = None
        self.last_sweep_kind: Literal["baseline", "triggered"] | None = None
        self.last_sweep_tally_at: datetime | None = None

    def _record_sweep_tally(
        self, tally: SweepTally, *, kind: Literal["baseline", "triggered"]
    ) -> None:
        self.last_sweep_tally = tally
        self.last_sweep_kind = kind
        self.last_sweep_tally_at = tally.generated_at

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
        if _universe_refresh_enabled():
            self._tasks.append(asyncio.create_task(self._universe_refresh_loop()))
        else:
            logger.info("universe refresh disabled (set UNIVERSE_REFRESH_ENABLED=1 to arm it)")
        if _flag("IV_SNAPSHOT_ENABLED", default=True):
            self._tasks.append(asyncio.create_task(self._iv_snapshot_loop()))
        else:
            logger.info("IV snapshot disabled (IV_SNAPSHOT_ENABLED=0)")
        if _flag("IN_SWEEP_ENABLED"):
            self._tasks.append(asyncio.create_task(self._india_loop()))
        else:
            logger.info("NSE sweep disabled (IN_SWEEP_ENABLED=0; Zerodha trades real money)")
        if _flag("EOD_REPORT_ENABLED", default=True):
            self._tasks.append(asyncio.create_task(self._eod_loop()))
        else:
            logger.info("EOD report disabled (EOD_REPORT_ENABLED=0)")

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
        if _missed_a_scan_this_session(datetime.now(UTC), times):
            # A deploy or crash-restart after today's scan time used to
            # skip the day's sweep entirely: the loop only ever schedules
            # FUTURE times. Catch up once, now. Idempotent by construction:
            # daily_cron's dedup skips symbols already decided today (or
            # still inside the options cooldown), and the per-day/hour LLM
            # caps still bound spend.
            logger.info("council scheduler: missed today's scan while down — catching up now")
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("catch-up council scan failed — waiting for the next window")
                self.last_result = "failed"
                from app.services.notifications.ops_alerts import raise_ops_alert

                raise_ops_alert(
                    "sweep_failed",
                    user_id=_cron_user(),
                    title="Catch-up sweep failed",
                    body=f"The restart catch-up sweep raised {type(exc).__name__}. "
                    "It will retry at the next scan time.",
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
            except Exception as exc:
                logger.exception("scheduled council scan failed — will retry next window")
                self.last_result = "failed"
                from app.services.notifications.ops_alerts import raise_ops_alert

                raise_ops_alert(
                    "sweep_failed",
                    user_id=_cron_user(),
                    title="Scheduled sweep failed",
                    body=f"The baseline council sweep raised {type(exc).__name__}. "
                    "It will retry at the next scan time.",
                )
            # Guard against a scan finishing inside the same minute it
            # started, which would otherwise re-fire immediately.
            await asyncio.sleep(61)

    async def _india_loop(self) -> None:
        """The NSE sweep: IN_SCAN_TIMES_UTC (default 04:30 = 10:00 IST),
        NSE symbols only, gated on the NSE calendar inside the cron."""
        times = _scan_times("IN_SCAN_TIMES_UTC", IN_DEFAULT_SCAN_TIMES)
        logger.info("NSE sweep armed — scan times (UTC): %s",
                    ", ".join(f"{h:02d}:{m:02d}" for h, m in times))
        while True:
            try:
                await asyncio.sleep(_seconds_until_next(datetime.now(UTC), times))
            except asyncio.CancelledError:
                raise
            try:
                await self._run_once(market="IN")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("NSE sweep failed — will retry next window")
                from app.services.notifications.ops_alerts import raise_ops_alert

                raise_ops_alert(
                    "sweep_failed", user_id=_cron_user(), key="IN",
                    title="NSE sweep failed",
                    body=f"The NSE council sweep raised {type(exc).__name__}. "
                    "It will retry at the next scan time.",
                )
            await asyncio.sleep(61)

    # ── Universe refresh loop ────────────────────────────────────────
    #
    # Zero LLM cost — a real broker screen (list_most_active_symbols +
    # list_tradable_assets), not a council pass. Once/day is enough: the
    # tradable/fractionable/has_options facts this screens on change on
    # the order of days, not minutes (see
    # trading_agents.jobs.universe_refresh's own module docstring).

    async def _universe_refresh_loop(self) -> None:
        hour = _universe_refresh_hour()
        logger.info("universe refresh armed — fires daily at %02d:00 UTC", hour)
        while True:
            delay = _seconds_until_next(datetime.now(UTC), [(hour, 0)])
            logger.info("next universe refresh in %.0f min", delay / 60)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            try:
                await self._run_universe_refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("universe refresh failed — will retry next window")
                self.last_universe_refresh_result = "failed"
            # Same same-minute re-fire guard as the baseline loop.
            await asyncio.sleep(61)

    async def _run_universe_refresh_once(self) -> None:
        api_key = os.environ.get("ALPACA_API_KEY", "").strip()
        secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
        if not api_key or not secret_key:
            logger.warning("universe refresh skipped — Alpaca keys not set")
            self.last_universe_refresh_result = "skipped_no_keys"
            return

        from trading_agents.jobs.universe_refresh import refresh_watchlist

        result = await refresh_watchlist(_cron_user(), api_key=api_key, secret_key=secret_key)
        self.last_universe_refresh_at = datetime.now(UTC)
        self.last_universe_refresh_result = result
        logger.info("universe refresh done: %s", result)

    # ── IV history recorder ──────────────────────────────────────────
    #
    # Zero LLM cost, one chain request per options underlying, once a
    # trading day. See trading_agents.jobs.iv_snapshot.

    async def _iv_snapshot_loop(self) -> None:
        hour = _iv_snapshot_hour()
        logger.info("IV snapshot armed — fires daily at %02d:15 UTC", hour)
        while True:
            delay = _seconds_until_next(datetime.now(UTC), [(hour, 15)])
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            try:
                await self._run_iv_snapshot_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("IV snapshot failed — will retry next window")
                self.last_iv_snapshot_result = "failed"
            await asyncio.sleep(61)

    async def _run_iv_snapshot_once(self) -> None:
        from engine.features import is_us_trading_day

        today = datetime.now(UTC).date()
        if not is_us_trading_day(today):
            self.last_iv_snapshot_result = "skipped_market_holiday"
            return
        if not _flag("USE_POSTGRES"):
            self.last_iv_snapshot_result = "skipped_no_postgres"
            return
        api_key = os.environ.get("ALPACA_API_KEY", "").strip()
        secret_key = os.environ.get("ALPACA_SECRET_KEY", "").strip()
        if not api_key or not secret_key:
            self.last_iv_snapshot_result = "skipped_no_keys"
            return

        symbols, instruments = await _watchlist_with_instruments()
        underlyings = [s for s in symbols if instruments.get(s) == "option"]
        if not underlyings:
            self.last_iv_snapshot_result = "skipped_no_option_underlyings"
            return

        from trading_agents.jobs.iv_snapshot import (
            alpaca_chain_fetcher,
            postgres_writer,
            snapshot,
        )

        feed = "opra" if os.environ.get("ALPACA_OPTIONS_FEED", "").strip().lower() == "opra" else "indicative"
        result = await snapshot(
            underlyings, today,
            fetch_chain=alpaca_chain_fetcher(api_key, secret_key),
            write_rows=postgres_writer,
            feed=feed,
        )
        self.last_iv_snapshot_at = datetime.now(UTC)
        self.last_iv_snapshot_result = result
        logger.info("IV snapshot done: %s", result)

    async def _eod_loop(self) -> None:
        hour = _eod_report_hour()
        logger.info("EOD report armed — fires daily at %02d:15 UTC", hour)
        while True:
            delay = _seconds_until_next(datetime.now(UTC), [(hour, 15)])
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            try:
                await self._run_eod_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("EOD report failed — will retry next window")
                self.last_eod_result = "failed"
            await asyncio.sleep(61)

    async def _run_eod_once(self) -> None:
        from engine.features import is_us_trading_day

        today = datetime.now(UTC).date()
        if not is_us_trading_day(today):
            self.last_eod_result = "skipped_market_holiday"
            return
        if not _flag("USE_POSTGRES"):
            self.last_eod_result = "skipped_no_postgres"
            return

        from app.services.council.eod_report import run_eod
        from engine.db import async_session_factory

        report = await run_eod(
            user_id=_cron_user(), session_factory=async_session_factory(), day=today
        )
        self.last_eod_at = datetime.now(UTC)
        self.last_eod_result = "sent" if report is not None else "report_failed"

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

        interval_minutes = _int_env("SCANNER_INTERVAL_MINUTES", 5)
        max_runs = _int_env("SCANNER_MAX_COUNCIL_RUNS", 3)
        interval = interval_minutes * 60
        # Only flip to armed once the scanner actually exists — a missing
        # Alpaca key must report armed=False on /scanner/status, not a
        # false "yes" based on the env flag alone.
        self.trigger_loop_armed = True
        self.scanner_interval_minutes = interval_minutes
        self.scanner_max_council_runs = max_runs
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

        symbols, instruments = await _watchlist_with_instruments()
        result = await scanner.scan(symbols)  # type: ignore[attr-defined]

        self.last_scan_at = result.scanned_at
        self.last_scan_signals = len(result.signals)
        self.last_scan_result = result

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
        #
        # OPTIONS FIRST. Before this, `triggered[:max_runs]` handed the
        # budget out in scan order, so a handful of option underlyings
        # competed with ~37 equities for 3 slots and usually lost. Options
        # are the timing-sensitive instrument AND the one the contest
        # requires, so they get the budget first; equities take what is
        # left. Stable ordering within each group keeps scan-to-scan
        # behaviour comparable.
        opts = [s for s in triggered if instruments.get(s) == "option"]
        eqs = [s for s in triggered if instruments.get(s) != "option"]
        selected = (opts + eqs)[:max_runs]
        if len(triggered) > max_runs:
            logger.warning(
                "%d symbols triggered (%d options), running %d (SCANNER_MAX_COUNCIL_RUNS): %s",
                len(triggered), len(opts), max_runs, ", ".join(selected),
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

        self.last_council_run_symbols = tuple(selected)
        started = datetime.now(UTC)
        # force is the operator's "run it anyway" — it would skip BOTH the
        # calendar gate AND the once-per-(user, symbol, day) dedup check in
        # daily_cron, which would let a triggered run double-spend LLM cost
        # on a symbol the baseline sweep (or an earlier trigger) already
        # decided today. That's not what we want here.
        #
        # skip_calendar_gate skips ONLY the calendar check: ``result``
        # above already came from a market-hours-gated scan (market_open
        # was just checked), so that gate is redundant for this call — but
        # the dedup guard must still run, because it's what keeps the
        # once-per-symbol-per-day cap uniform whether a symbol was decided
        # by the baseline sweep or by an earlier trigger this same day.
        code = await cron_main(
            _cron_user(),
            selected,
            force=False,
            skip_calendar_gate=True,
            skip_ghost_eval=True,
            skip_reflect=True,
            scan_context=scan_context,
            instrument_by_symbol=instruments,
            on_sweep_scored=lambda t: self._record_sweep_tally(t, kind="triggered"),
        )
        self.last_run_at = started
        self.last_result = {"exit_code": code, "symbols": len(selected), "triggered": 1}

    async def _run_once(self, market: str = "US") -> None:
        """One full watchlist pass over ``market``'s symbols. Delegates to the
        existing cron entry point.

        ``daily_cron.main`` owns the market-calendar gate, the per-symbol
        idempotency check, the push notification, and the ghost/reflection
        follow-ups — this only decides *when*.
        """
        from trading_agents.jobs.daily_cron import main as cron_main

        user_id = _cron_user()
        watchlist, instruments = await _watchlist_with_instruments()
        # Each market is swept in its own session on its own calendar: an
        # NSE symbol swept at the US scan time (19:30 IST) would draft an
        # order NSE cannot fill until tomorrow, on today's stale close.
        watchlist = _for_market(watchlist, market)
        if not watchlist:
            logger.info("council scan (%s): no symbols for this market", market)
            return

        logger.info("council scan (%s) starting — %d symbols", market, len(watchlist))
        started = datetime.now(UTC)
        code = await cron_main(
            user_id, watchlist, force=False, instrument_by_symbol=instruments,
            on_sweep_scored=lambda t: self._record_sweep_tally(t, kind="baseline"),
            market=market,
        )
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
