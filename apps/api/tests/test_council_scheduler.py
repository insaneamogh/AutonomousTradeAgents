"""``CouncilScheduler._scan_once`` — trigger-loop unit tests.

Exercises the method directly against a FRESH ``CouncilScheduler`` instance
(never the module singleton), with a fake scanner exposing an async
``.scan()`` that returns a hand-built ``ScanResult``. No Alpaca keys, no
background task, no Postgres.

``daily_cron.main`` is imported INSIDE ``_scan_once`` (not at module scope
in ``scheduler.py``), so it must be patched on the defining module —
``trading_agents.jobs.daily_cron`` — the same technique
``test_daily_cron.py`` already uses for ``is_us_trading_day``.

The kwargs assertion in ``test_scan_once_...exact_kwargs`` is the tripwire
against ever regressing back to ``force=True`` on the triggered path,
which silently bypassed the once-per-symbol-per-day dedup guard.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from engine.scanner import ScanResult, ScanSignal


def _signal(symbol: str, rule: str = "volume_spike_2x") -> ScanSignal:
    return ScanSignal(
        symbol=symbol,
        trigger_rule=rule,
        strength=0.8,
        observed_at=datetime.now(UTC),
        direction="bullish",
        detail=f"{symbol} test signal ({rule})",
        context={},
    )


class _FakeScanner:
    """Stands in for ``engine.scanner.Scanner`` — returns a canned result
    regardless of the symbols it's asked to scan."""

    def __init__(self, result: ScanResult) -> None:
        self._result = result
        self.scan_calls: list[list[str]] = []

    async def scan(self, symbols: list[str]) -> ScanResult:
        self.scan_calls.append(list(symbols))
        return self._result


@pytest.fixture(autouse=True)
def _pinned_watchlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic watchlist so ``_watchlist()`` doesn't depend on
    ``daily_cron.DEFAULT_WATCHLIST`` staying a particular size — the fake
    scanner ignores its input anyway, this just keeps the env tidy."""
    monkeypatch.setenv("AGENT_CRON_WATCHLIST", "AAA,BBB,CCC,DDD")


async def test_scan_once_records_metadata_and_calls_cron_with_exact_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: one triggered symbol → scan metadata is recorded AND
    the council is called with exactly ``force=False,
    skip_calendar_gate=True`` (never ``force=True``)."""
    from app.services.council.scheduler import CouncilScheduler
    from trading_agents.jobs import daily_cron

    sig = _signal("AAA")
    result = ScanResult(
        scanned_at=datetime.now(UTC),
        market_open=True,
        symbols_scanned=("AAA", "BBB", "CCC", "DDD"),
        signals=(sig,),
        suppressed=(),
    )
    scanner = _FakeScanner(result)

    captured: dict = {}

    async def fake_cron_main(user_id, symbols, **kwargs):
        captured["user_id"] = user_id
        captured["symbols"] = list(symbols)
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(daily_cron, "main", fake_cron_main)

    scheduler = CouncilScheduler()
    await scheduler._scan_once(scanner, max_runs=3)

    # Scan metadata recorded.
    assert scheduler.last_scan_at == result.scanned_at
    assert scheduler.last_scan_signals == 1
    assert scheduler.last_scan_result is result
    assert scheduler.last_triggered == ("AAA",)
    assert scheduler.last_council_run_symbols == ("AAA",)
    assert scheduler.last_run_at is not None
    assert scheduler.last_result == {"exit_code": 0, "symbols": 1, "triggered": 1}

    # The tripwire.
    assert captured["symbols"] == ["AAA"]
    assert captured["kwargs"]["force"] is False
    assert captured["kwargs"]["skip_calendar_gate"] is True
    assert captured["kwargs"]["skip_ghost_eval"] is True
    assert captured["kwargs"]["skip_reflect"] is True
    assert callable(captured["kwargs"]["on_sweep_scored"])


async def test_scan_once_market_closed_updates_metadata_but_skips_cron(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Market closed → scan metadata still updates (so /scanner/status can
    report ``market_open=False``), but the council is never called."""
    from app.services.council.scheduler import CouncilScheduler
    from trading_agents.jobs import daily_cron

    result = ScanResult(
        scanned_at=datetime.now(UTC),
        market_open=False,
        symbols_scanned=(),
        signals=(),
        suppressed=(),
    )
    scanner = _FakeScanner(result)

    called = 0

    async def fake_cron_main(*args, **kwargs):
        nonlocal called
        called += 1
        return 0

    monkeypatch.setattr(daily_cron, "main", fake_cron_main)

    scheduler = CouncilScheduler()
    await scheduler._scan_once(scanner, max_runs=3)

    assert scheduler.last_scan_at == result.scanned_at
    assert scheduler.last_scan_result is result
    assert scheduler.last_scan_signals == 0
    assert called == 0
    # Untouched — no triggered run happened.
    assert scheduler.last_council_run_symbols == ()
    assert scheduler.last_run_at is None


async def test_scan_once_no_signals_skips_cron(monkeypatch: pytest.MonkeyPatch) -> None:
    """Market open but a clean scan (no triggers) → still no council call."""
    from app.services.council.scheduler import CouncilScheduler
    from trading_agents.jobs import daily_cron

    result = ScanResult(
        scanned_at=datetime.now(UTC),
        market_open=True,
        symbols_scanned=("AAA", "BBB"),
        signals=(),
        suppressed=(),
    )
    scanner = _FakeScanner(result)

    called = 0

    async def fake_cron_main(*args, **kwargs):
        nonlocal called
        called += 1
        return 0

    monkeypatch.setattr(daily_cron, "main", fake_cron_main)

    scheduler = CouncilScheduler()
    await scheduler._scan_once(scanner, max_runs=3)

    assert called == 0
    assert scheduler.last_triggered == ()
    assert scheduler.last_council_run_symbols == ()


async def test_scan_once_caps_selected_symbols_at_max_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More triggered symbols than SCANNER_MAX_COUNCIL_RUNS → only the
    first N (in first-fired order) are passed to the council."""
    from app.services.council.scheduler import CouncilScheduler
    from trading_agents.jobs import daily_cron

    signals = tuple(_signal(sym) for sym in ("AAA", "BBB", "CCC", "DDD"))
    result = ScanResult(
        scanned_at=datetime.now(UTC),
        market_open=True,
        symbols_scanned=("AAA", "BBB", "CCC", "DDD"),
        signals=signals,
        suppressed=(),
    )
    scanner = _FakeScanner(result)

    captured: dict = {}

    async def fake_cron_main(user_id, symbols, **kwargs):
        captured["symbols"] = list(symbols)
        return 0

    monkeypatch.setattr(daily_cron, "main", fake_cron_main)

    scheduler = CouncilScheduler()
    await scheduler._scan_once(scanner, max_runs=2)

    assert captured["symbols"] == ["AAA", "BBB"]
    assert scheduler.last_council_run_symbols == ("AAA", "BBB")
    assert scheduler.last_triggered == ("AAA", "BBB", "CCC", "DDD")


# ── _run_once — the baseline sweep ───────────────────────────────────


async def test_run_once_calls_cron_with_a_callable_recorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same tripwire shape as the trigger loop's exact-kwargs test above,
    for the baseline sweep's own call site."""
    from app.services.council.scheduler import CouncilScheduler
    from trading_agents.jobs import daily_cron

    captured: dict = {}

    async def fake_cron_main(user_id, symbols, **kwargs):
        captured["user_id"] = user_id
        captured["symbols"] = list(symbols)
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr(daily_cron, "main", fake_cron_main)

    scheduler = CouncilScheduler()
    await scheduler._run_once()

    assert captured["symbols"] == ["AAA", "BBB", "CCC", "DDD"]
    assert captured["kwargs"]["force"] is False
    assert callable(captured["kwargs"]["on_sweep_scored"])


async def test_run_once_end_to_end_populates_last_sweep_tally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real wiring, not a mocked daily_cron.main: fakes only scoring and
    run_council (same technique test_llm_budget_gate.py uses), so this
    proves _run_once -> daily_cron.main -> on_sweep_scored ->
    CouncilScheduler._record_sweep_tally is actually connected end to end,
    not just that the right kwarg gets passed."""
    from types import SimpleNamespace

    import engine.features
    from app.services.council.scheduler import CouncilScheduler
    from trading_agents.jobs import daily_cron

    monkeypatch.setattr(engine.features, "is_us_trading_day", lambda _d: True)

    scores = {"AAA": 0.9, "BBB": None, "CCC": 0.8, "DDD": 0.7}

    def _fake_provider(symbol: str, horizon: str = "short"):
        return {"symbol": symbol}

    def _fake_best_strategy(features, *, priors=None, allow_shorts=False):
        score = scores.get(features["symbol"])
        if score is None:
            return None, []
        winner = SimpleNamespace(score=score, strategy_id="fake", direction="long")
        return winner, [winner]

    import trading_agents.strategies as strategies_mod

    monkeypatch.setattr(strategies_mod, "best_strategy", _fake_best_strategy)
    monkeypatch.setattr(daily_cron, "resolve_feature_provider", lambda **_kw: _fake_provider)
    monkeypatch.setenv("MAX_LLM_SYMBOLS_PER_SWEEP", "2")

    async def fake_run_council(**kwargs):
        return {
            "final_action": "HOLD", "selected_strategy": None,
            "selector_confidence": 0.0, "decision_id": f"dec-{kwargs['symbol']}",
        }

    monkeypatch.setattr(daily_cron, "run_council", fake_run_council)

    scheduler = CouncilScheduler()
    await scheduler._run_once()

    assert scheduler.last_sweep_kind == "baseline"
    assert scheduler.last_sweep_tally_at is not None
    tally = scheduler.last_sweep_tally
    assert tally is not None
    assert tally.watchlist_size == 4
    assert tally.cleared_math == 3, "BBB (score=None) never clears the math"
    assert tally.admitted_to_llm == 2, "MAX_LLM_SYMBOLS_PER_SWEEP=2 admits the top 2 by score"
    assert tally.capped_breakdown == {"llm_symbol_cap_reached": 1}


# ── Universe refresh loop ────────────────────────────────────────────
#
# ``_run_universe_refresh_once`` — zero-LLM-cost daily screen, see
# trading_agents.jobs.universe_refresh. ``refresh_watchlist`` is imported
# INSIDE the method (same lazy-import convention as daily_cron.main
# above), so it must be patched on its defining module.


async def test_universe_refresh_skips_and_records_when_no_alpaca_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.council.scheduler import CouncilScheduler

    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    scheduler = CouncilScheduler()
    await scheduler._run_universe_refresh_once()

    assert scheduler.last_universe_refresh_result == "skipped_no_keys"
    assert scheduler.last_universe_refresh_at is None


async def test_universe_refresh_calls_refresh_watchlist_and_records_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.council.scheduler import CouncilScheduler
    from trading_agents.jobs import universe_refresh

    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setenv("AGENT_CRON_USER_ID", "fixture-user")

    captured: dict = {}

    async def fake_refresh_watchlist(user_id, *, api_key, secret_key):
        captured["user_id"] = user_id
        captured["api_key"] = api_key
        captured["secret_key"] = secret_key
        return {"equity": 56, "options": 12}

    monkeypatch.setattr(universe_refresh, "refresh_watchlist", fake_refresh_watchlist)

    scheduler = CouncilScheduler()
    await scheduler._run_universe_refresh_once()

    assert captured == {"user_id": "fixture-user", "api_key": "k", "secret_key": "s"}
    assert scheduler.last_universe_refresh_result == {"equity": 56, "options": 12}
    assert scheduler.last_universe_refresh_at is not None


def test_universe_refresh_hour_defaults_to_12_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.council.scheduler import _universe_refresh_hour

    monkeypatch.delenv("UNIVERSE_REFRESH_HOUR_UTC", raising=False)
    assert _universe_refresh_hour() == 12


def test_universe_refresh_hour_reads_env_and_falls_back_on_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.council.scheduler import _universe_refresh_hour

    monkeypatch.setenv("UNIVERSE_REFRESH_HOUR_UTC", "9")
    assert _universe_refresh_hour() == 9

    monkeypatch.setenv("UNIVERSE_REFRESH_HOUR_UTC", "not-a-number")
    assert _universe_refresh_hour() == 12

    monkeypatch.setenv("UNIVERSE_REFRESH_HOUR_UTC", "99")
    assert _universe_refresh_hour() == 12


# ── IV history recorder ─────────────────────────────────────────────


async def test_iv_snapshot_records_only_option_underlyings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import engine.features
    from app.services.council import scheduler as sched
    from app.services.council.scheduler import CouncilScheduler
    from trading_agents.jobs import iv_snapshot

    monkeypatch.setattr(engine.features, "is_us_trading_day", lambda _d: True)
    monkeypatch.setenv("USE_POSTGRES", "1")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")

    async def fake_watchlist():
        return ["NVDA", "AAPL", "SPY"], {"NVDA": "option", "AAPL": "equity", "SPY": "option"}

    captured: dict = {}

    async def fake_snapshot(symbols, today, *, fetch_chain, write_rows, feed):
        captured["symbols"] = list(symbols)
        captured["feed"] = feed
        return {"recorded": 2, "no_atm_iv": 0, "failed": 0}

    monkeypatch.setattr(sched, "_watchlist_with_instruments", fake_watchlist)
    monkeypatch.setattr(iv_snapshot, "snapshot", fake_snapshot)
    monkeypatch.delenv("ALPACA_OPTIONS_FEED", raising=False)

    scheduler = CouncilScheduler()
    await scheduler._run_iv_snapshot_once()
    assert captured == {"symbols": ["NVDA", "SPY"], "feed": "indicative"}
    assert scheduler.last_iv_snapshot_result == {"recorded": 2, "no_atm_iv": 0, "failed": 0}


async def test_iv_snapshot_skips_holidays_and_missing_prerequisites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import engine.features
    from app.services.council.scheduler import CouncilScheduler

    monkeypatch.setattr(engine.features, "is_us_trading_day", lambda _d: False)
    s = CouncilScheduler()
    await s._run_iv_snapshot_once()
    assert s.last_iv_snapshot_result == "skipped_market_holiday"

    monkeypatch.setattr(engine.features, "is_us_trading_day", lambda _d: True)
    monkeypatch.delenv("USE_POSTGRES", raising=False)
    await s._run_iv_snapshot_once()
    assert s.last_iv_snapshot_result == "skipped_no_postgres"


def test_iv_snapshot_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every day it does not run is history that can never be recovered."""
    from app.services.council.scheduler import _flag

    monkeypatch.delenv("IV_SNAPSHOT_ENABLED", raising=False)
    assert _flag("IV_SNAPSHOT_ENABLED", default=True) is True


# ── restart catch-up ────────────────────────────────────────────────


def test_a_restart_after_the_scan_time_during_the_session_catches_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    import engine.features
    from app.services.council.scheduler import _missed_a_scan_this_session

    monkeypatch.setattr(engine.features, "is_us_market_open", lambda _now: True)
    after = datetime(2026, 9, 23, 15, 30, tzinfo=UTC)
    before = datetime(2026, 9, 23, 13, 45, tzinfo=UTC)
    assert _missed_a_scan_this_session(after, [(14, 0)]) is True
    assert _missed_a_scan_this_session(before, [(14, 0)]) is False

    monkeypatch.setattr(engine.features, "is_us_market_open", lambda _now: False)
    assert _missed_a_scan_this_session(after, [(14, 0)]) is False, "after the close: wait"


async def test_the_baseline_loop_runs_the_missed_sweep_before_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from app.services.council import scheduler as sched

    monkeypatch.setattr(sched, "_missed_a_scan_this_session", lambda _now, _times: True)
    s = sched.CouncilScheduler()
    order: list[str] = []

    async def fake_run_once() -> None:
        order.append("run")

    async def fake_sleep(_seconds: float) -> None:
        order.append("sleep")
        raise asyncio.CancelledError

    monkeypatch.setattr(s, "_run_once", AsyncMock(side_effect=fake_run_once))
    monkeypatch.setattr(sched.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await s._baseline_loop()
    assert order == ["run", "sleep"]


# ── End-of-day job ─────────────────────────────────────────────────


async def test_eod_runs_on_a_trading_day_without_the_council(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The EOD job must not depend on the LLM: it calls run_eod directly,
    never daily_cron.main (which exits 2 when the LLM key is missing)."""
    import engine.db
    import engine.features
    from app.services.council import eod_report
    from app.services.council.scheduler import CouncilScheduler
    from trading_agents.jobs import daily_cron

    monkeypatch.setattr(engine.features, "is_us_trading_day", lambda _d: True)
    monkeypatch.setattr(engine.db, "async_session_factory", lambda: "factory")
    monkeypatch.setenv("USE_POSTGRES", "1")

    async def _no_cron(*_a, **_k):
        raise AssertionError("the EOD job must not go through the council cron")

    monkeypatch.setattr(daily_cron, "main", _no_cron)
    calls: list[dict] = []

    async def fake_run_eod(**kw):
        calls.append(kw)
        return object()

    monkeypatch.setattr(eod_report, "run_eod", fake_run_eod)

    s = CouncilScheduler()
    await s._run_eod_once()
    assert s.last_eod_result == "sent"
    assert calls and calls[0]["session_factory"] == "factory"


async def test_eod_skips_holidays_and_no_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    import engine.features
    from app.services.council.scheduler import CouncilScheduler

    monkeypatch.setattr(engine.features, "is_us_trading_day", lambda _d: False)
    s = CouncilScheduler()
    await s._run_eod_once()
    assert s.last_eod_result == "skipped_market_holiday"

    monkeypatch.setattr(engine.features, "is_us_trading_day", lambda _d: True)
    monkeypatch.delenv("USE_POSTGRES", raising=False)
    await s._run_eod_once()
    assert s.last_eod_result == "skipped_no_postgres"


def test_eod_report_hour_defaults_after_the_close_year_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.council.scheduler import _eod_report_hour

    monkeypatch.delenv("EOD_REPORT_HOUR_UTC", raising=False)
    assert _eod_report_hour() == 21  # 21:15 UTC is after 16:00 ET in EST and EDT
    monkeypatch.setenv("EOD_REPORT_HOUR_UTC", "nope")
    assert _eod_report_hour() == 21
