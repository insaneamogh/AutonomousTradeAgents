"""Symbol-scan funnel aggregation — /api/v1/insights/scan-funnel.

A DIFFERENT funnel from ``funnel_service.py``'s contract funnel. That one
answers "for one symbol that already reached a paid pass, which contract
did select_contract choose". This one answers the question one level up
— "how many symbols does the scanner even look at, and how many of those
ever reach a paid pass at all" — the tiered shape
``docs/PLAN_1000_SYMBOL_SCAN.md`` specifies (eligible universe -> active
this sweep -> cleared the deterministic math -> admitted to the LLM).

Sibling to ``scanner_status.py`` on purpose, same reasoning: there is no
Postgres table for this, ``CouncilScheduler`` is the only place it lives
(``last_universe_refresh_result`` from the once-daily universe screen,
``last_sweep_tally`` from whichever of the baseline/trigger loops most
recently called ``daily_cron.main``), and the one thing to guard against
is the scheduler not existing at all (``COUNCIL_SCHEDULER_ENABLED=0``),
not per-field I/O — every read here is an in-memory attribute access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.time import utc_now
from app.services.council.scheduler import get_council_scheduler


@dataclass(frozen=True)
class ScanFunnelUniverse:
    """Tier 0 (eligible universe) + Tier 1 (examined this refresh) — from
    the once-daily universe screen, NOT recomputed per sweep. All fields
    ``None`` until ``UNIVERSE_REFRESH_ENABLED=1`` has fired at least once,
    or if the last refresh was skipped/failed — never a fabricated zero.
    """

    eligible_count: int | None = None
    examined_count: int | None = None
    refreshed_at: datetime | None = None


@dataclass(frozen=True)
class ScanFunnelSweep:
    """Tier 2 (+ Tier 4's input count) — from whichever loop (baseline or
    triggered) most recently called ``daily_cron.main``. ``kind`` lets a
    reader tell a full baseline sweep's numbers apart from a triggered
    loop's much smaller ones — collapsing the two would make a real,
    narrow triggered sweep look like a broken baseline one."""

    kind: str | None = None
    watchlist_size: int = 0
    cleared_math: int = 0
    admitted_to_llm: int = 0
    capped_breakdown: dict[str, int] = field(default_factory=dict)
    generated_at: datetime | None = None


@dataclass(frozen=True)
class ScanFunnelReport:
    universe: ScanFunnelUniverse
    sweep: ScanFunnelSweep | None
    generated_at: datetime


def _universe_counts(result: object) -> tuple[int | None, int | None]:
    """``(eligible_count, examined_count)`` from a raw
    ``last_universe_refresh_result`` value — ``(None, None)`` for anything
    that isn't the current, populated dict shape: the string sentinels
    (``"skipped_no_keys"``, ``"failed"``), ``None`` (never ran), or a
    stale pre-migration 2-key dict (``{"equity": N, "options": M}``, no
    ``eligible_universe``/``examined`` keys at all). Matches
    ``funnel_service._stage_counts``'s own rule: an absent/malformed
    value reads as ABSENT, never coerced to a fabricated zero.
    """
    if not isinstance(result, dict):
        return None, None

    def _int_or_none(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return _int_or_none(result.get("eligible_universe")), _int_or_none(result.get("examined"))


def build_scan_funnel_report(
    *,
    universe_refresh_result: object,
    universe_refresh_at: datetime | None,
    sweep_tally: object,
    sweep_kind: str | None,
    sweep_tally_at: datetime | None,
    generated_at: datetime,
) -> ScanFunnelReport:
    """Pure aggregation, no I/O — split out from
    ``build_scan_funnel_report_for_scheduler`` so it is testable without a
    running scheduler, mirroring ``funnel_service.build_funnel_report_from_rows``.

    ``sweep_tally`` is typed ``object`` (not ``SweepTally``) so this stays
    import-free of ``trading_agents`` — the caller (the scheduler-reading
    wrapper below) passes a real ``SweepTally`` or ``None``; anything
    missing the four expected attributes degrades to the empty sweep
    rather than raising, matching this module's "absent, never fabricated"
    rule for the universe half above.
    """
    eligible_count, examined_count = _universe_counts(universe_refresh_result)
    universe = ScanFunnelUniverse(
        eligible_count=eligible_count,
        examined_count=examined_count,
        refreshed_at=universe_refresh_at if eligible_count is not None else None,
    )

    sweep: ScanFunnelSweep | None = None
    if sweep_tally is not None:
        try:
            sweep = ScanFunnelSweep(
                kind=sweep_kind,
                watchlist_size=int(sweep_tally.watchlist_size),  # type: ignore[attr-defined]
                cleared_math=int(sweep_tally.cleared_math),  # type: ignore[attr-defined]
                admitted_to_llm=int(sweep_tally.admitted_to_llm),  # type: ignore[attr-defined]
                capped_breakdown=dict(sweep_tally.capped_breakdown),  # type: ignore[attr-defined]
                generated_at=sweep_tally_at,
            )
        except AttributeError:
            sweep = None

    return ScanFunnelReport(universe=universe, sweep=sweep, generated_at=generated_at)


async def build_scan_funnel_report_for_scheduler() -> ScanFunnelReport:
    """Reads the live ``CouncilScheduler`` singleton. Never raises — an
    absent scheduler (``COUNCIL_SCHEDULER_ENABLED=0``, the common case on
    a fresh box) reports the same honest empty shape
    ``build_scan_funnel_report`` already produces for no data, not a 500.
    """
    now = utc_now()
    scheduler = get_council_scheduler()
    if scheduler is None:
        return build_scan_funnel_report(
            universe_refresh_result=None,
            universe_refresh_at=None,
            sweep_tally=None,
            sweep_kind=None,
            sweep_tally_at=None,
            generated_at=now,
        )

    return build_scan_funnel_report(
        universe_refresh_result=scheduler.last_universe_refresh_result,
        universe_refresh_at=scheduler.last_universe_refresh_at,
        sweep_tally=scheduler.last_sweep_tally,
        sweep_kind=scheduler.last_sweep_kind,
        sweep_tally_at=scheduler.last_sweep_tally_at,
        generated_at=now,
    )
