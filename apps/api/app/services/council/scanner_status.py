"""Scanner-status aggregator — read-only view of the trigger loop's state.

Sibling to ``scheduler.py`` on purpose: ``CouncilScheduler`` is the only
place this data lives (there is no Postgres table for it — this is
in-process observability for a feature that is off by default, same as
``last_scan_at``/``last_triggered`` were before this module existed).

Mirrors ``app.services.platform.health.build_health_report``'s contract:
read-only, never raises, always returns a fully-populated response so the
route can always answer 200. Unlike the health aggregator this has no
per-component I/O to guard with individual try/excepts — every read here
is an in-memory attribute access on the scheduler singleton — so the one
thing this function guards against is the scheduler not existing at all
(``COUNCIL_SCHEDULER_ENABLED=0``, or ``USE_POSTGRES=0`` so it was never
started), which is the common case on a fresh dev box.
"""

from __future__ import annotations

import logging

from app.core.time import utc_now
from app.schemas.scanner import (
    AlpacaCliHealthDto,
    ScannerStatusResponse,
    ScanSignalDto,
)
from app.services.council.scheduler import (
    configured_watchlist,
    get_council_scheduler,
    scanner_enabled,
)
from engine.scanner import ScanSignal

logger = logging.getLogger("api.scanner_status")


def _signal_dto(signal: ScanSignal) -> ScanSignalDto:
    """``ScanSignal`` → wire DTO. Built from the dataclass's own fields
    (not its ``as_dict()``) so Pydantic — not a manual isoformat/round —
    owns the JSON coercion."""
    return ScanSignalDto(
        symbol=signal.symbol,
        rule=signal.trigger_rule,
        direction=signal.direction,
        strength=signal.strength,
        detail=signal.detail,
        observed_at=signal.observed_at,
        context=dict(signal.context),
    )


async def build_scanner_status_report() -> ScannerStatusResponse:
    """Aggregate the running scheduler's scanner state. Never raises."""
    try:
        watchlist_size = len(await configured_watchlist())
    except Exception as exc:
        logger.warning("scanner watchlist read failed — %s", exc)
        watchlist_size = 0

    # One subprocess round trip (`alpaca version`) per status read. Cheap,
    # and it is the one place the hackathon's CLI requirement is provable
    # on demand rather than only as a side effect of a scan having run.
    # Failure here must never blank the rest of the report — a health
    # probe that takes down the panel it reports into is worse than no
    # probe, so this degrades to None and the field simply goes absent.
    cli_health = None
    try:
        from engine.features.alpaca_cli import cli_health as _cli_health

        cli_health = AlpacaCliHealthDto(**await _cli_health())  # type: ignore[arg-type]
    except Exception as exc:
        logger.info("alpaca CLI health probe failed — %s", exc)

    try:
        report = _build(watchlist_size)
    except Exception as exc:
        logger.warning("scanner status read failed — %s", exc)
        report = _empty_report(watchlist_size)
    return report.model_copy(update={"alpaca_cli": cli_health})


def _empty_report(watchlist_size: int = 0) -> ScannerStatusResponse:
    """Honest all-default response — used both when the scheduler was
    never started and as the fallback if something above still manages
    to raise."""
    return ScannerStatusResponse(
        scheduler_enabled=False,
        scanner_enabled_flag=scanner_enabled(),
        trigger_loop_armed=False,
        market_open=None,
        market_open_source=None,
        last_scan_at=None,
        scan_interval_minutes=None,
        max_council_runs_per_scan=None,
        watchlist_size=watchlist_size,
        signals=[],
        triggered_symbols=[],
        suppressed_count=0,
        last_council_run_at=None,
        last_council_run_symbols=[],
        generated_at=utc_now(),
    )


def _build(watchlist_size: int) -> ScannerStatusResponse:
    scheduler = get_council_scheduler()
    if scheduler is None:
        return _empty_report(watchlist_size)

    result = scheduler.last_scan_result
    signals = [_signal_dto(s) for s in result.signals] if result is not None else []
    triggered_symbols = list(result.triggered_symbols) if result is not None else []
    suppressed_count = len(result.suppressed) if result is not None else 0
    market_open = result.market_open if result is not None else None
    market_open_source = result.market_open_source if result is not None else None

    return ScannerStatusResponse(
        scheduler_enabled=True,
        scanner_enabled_flag=scanner_enabled(),
        trigger_loop_armed=scheduler.trigger_loop_armed,
        market_open=market_open,
        market_open_source=market_open_source,
        last_scan_at=scheduler.last_scan_at,
        scan_interval_minutes=scheduler.scanner_interval_minutes,
        max_council_runs_per_scan=scheduler.scanner_max_council_runs,
        watchlist_size=watchlist_size,
        signals=signals,
        triggered_symbols=triggered_symbols,
        suppressed_count=suppressed_count,
        last_council_run_at=scheduler.last_run_at,
        last_council_run_symbols=list(scheduler.last_council_run_symbols),
        generated_at=utc_now(),
    )
