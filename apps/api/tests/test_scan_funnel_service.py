"""Symbol-scan funnel aggregation — apps/api/app/services/council/scan_funnel_service.py.

Pure-function tests, no DB, no scheduler — mirrors test_funnel_service.py's
style. These pin the "absent, never fabricated" tolerance rule for the
universe half (the string sentinels universe_refresh can leave behind,
and a stale pre-migration 2-key result dict) and the SweepTally-shaped
sweep half.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.services.council.scan_funnel_service import build_scan_funnel_report

_WHEN = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _FakeTally:
    """Stands in for daily_cron.SweepTally — the service is deliberately
    typed against duck-typed attributes, not the real dataclass, so this
    file stays import-free of trading_agents."""

    watchlist_size: int
    cleared_math: int
    admitted_to_llm: int
    capped_breakdown: dict[str, int]
    generated_at: datetime


def test_no_universe_refresh_and_no_sweep_yet_is_all_none() -> None:
    """The honest cold-start case: COUNCIL_SCHEDULER_ENABLED=0 or neither
    loop has fired yet. Every count is None, never a fabricated zero."""
    report = build_scan_funnel_report(
        universe_refresh_result=None,
        universe_refresh_at=None,
        sweep_tally=None,
        sweep_kind=None,
        sweep_tally_at=None,
        generated_at=_WHEN,
    )
    assert report.universe.eligible_count is None
    assert report.universe.examined_count is None
    assert report.universe.refreshed_at is None
    assert report.sweep is None
    assert report.generated_at == _WHEN


def test_universe_string_sentinels_read_as_absent_not_zero() -> None:
    """universe_refresh's own result field can hold a bare string
    ("skipped_no_keys", "failed") instead of a dict — must not crash and
    must not read as "0 eligible", which would look like a real, empty
    universe rather than "we don't know"."""
    for sentinel in ("skipped_no_keys", "failed"):
        report = build_scan_funnel_report(
            universe_refresh_result=sentinel,
            universe_refresh_at=_WHEN,
            sweep_tally=None,
            sweep_kind=None,
            sweep_tally_at=None,
            generated_at=_WHEN,
        )
        assert report.universe.eligible_count is None
        assert report.universe.examined_count is None
        # refreshed_at is ALSO suppressed -- a timestamp with no real
        # counts behind it would misleadingly imply a successful refresh.
        assert report.universe.refreshed_at is None


def test_a_stale_pre_migration_universe_dict_reads_as_absent() -> None:
    """A result dict written before the eligible_universe/examined keys
    existed ({"equity": N, "options": M} only) must not be misread as
    "eligible_count=0" -- the keys are simply not there yet."""
    report = build_scan_funnel_report(
        universe_refresh_result={"equity": 56, "options": 12},
        universe_refresh_at=_WHEN,
        sweep_tally=None,
        sweep_kind=None,
        sweep_tally_at=None,
        generated_at=_WHEN,
    )
    assert report.universe.eligible_count is None
    assert report.universe.examined_count is None


def test_a_populated_universe_result_reports_real_counts() -> None:
    report = build_scan_funnel_report(
        universe_refresh_result={
            "equity": 56, "options": 12, "eligible_universe": 1024, "examined": 178,
        },
        universe_refresh_at=_WHEN,
        sweep_tally=None,
        sweep_kind=None,
        sweep_tally_at=None,
        generated_at=_WHEN,
    )
    assert report.universe.eligible_count == 1024
    assert report.universe.examined_count == 178
    assert report.universe.refreshed_at == _WHEN


def test_a_populated_sweep_tally_reports_through_with_its_kind() -> None:
    tally = _FakeTally(
        watchlist_size=110, cleared_math=34, admitted_to_llm=20,
        capped_breakdown={"llm_daily_symbol_cap_reached": 14}, generated_at=_WHEN,
    )
    report = build_scan_funnel_report(
        universe_refresh_result=None,
        universe_refresh_at=None,
        sweep_tally=tally,
        sweep_kind="baseline",
        sweep_tally_at=_WHEN,
        generated_at=_WHEN,
    )
    assert report.sweep is not None
    assert report.sweep.kind == "baseline"
    assert report.sweep.watchlist_size == 110
    assert report.sweep.cleared_math == 34
    assert report.sweep.admitted_to_llm == 20
    assert report.sweep.capped_breakdown == {"llm_daily_symbol_cap_reached": 14}


def test_a_triggered_sweep_carries_its_own_kind_distinctly() -> None:
    """A triggered loop's tiny watchlist must not be mistaken for a
    broken baseline sweep -- the kind tag is what tells the two apart."""
    tally = _FakeTally(
        watchlist_size=2, cleared_math=1, admitted_to_llm=1,
        capped_breakdown={}, generated_at=_WHEN,
    )
    report = build_scan_funnel_report(
        universe_refresh_result=None,
        universe_refresh_at=None,
        sweep_tally=tally,
        sweep_kind="triggered",
        sweep_tally_at=_WHEN,
        generated_at=_WHEN,
    )
    assert report.sweep is not None
    assert report.sweep.kind == "triggered"
    assert report.sweep.watchlist_size == 2


def test_a_malformed_sweep_tally_degrades_to_no_sweep_rather_than_raising() -> None:
    """Some object missing the expected attributes must never take the
    whole report down -- degrades to `sweep=None`, the same honest
    "we don't know" the cold-start case reports."""
    report = build_scan_funnel_report(
        universe_refresh_result=None,
        universe_refresh_at=None,
        sweep_tally=object(),
        sweep_kind="baseline",
        sweep_tally_at=_WHEN,
        generated_at=_WHEN,
    )
    assert report.sweep is None
