"""Scanner orchestration tests — bars in, signals out, no network.

Covers the three things the engine owns: the market-hours gate, the
settled-vs-live snapshot join, and the cooldown. Providers are fakes, so
these run in MOCK mode with no keys.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from engine.features.bars import IntradayBar
from engine.features.clock import ClockProvider, MarketClock
from engine.features.technicals import DailyBar
from engine.scanner import (
    Scanner,
    ScannerConfig,
    TriggerCooldown,
    TriggerRule,
    build_snapshot,
)

OPEN_AT = datetime(2026, 6, 16, 15, 0, tzinfo=UTC)  # Tue, mid-session
CLOSED_AT = datetime(2026, 6, 16, 23, 0, tzinfo=UTC)  # after hours
WEEKEND_AT = datetime(2026, 6, 20, 15, 0, tzinfo=UTC)


def daily_series(
    n: int = 120, *, start: float = 100.0, step: float = 0.0, end_day: date | None = None
) -> list[DailyBar]:
    """``n`` settled daily bars ending the day before ``end_day``."""
    last = (end_day or OPEN_AT.date()) - timedelta(days=1)
    out: list[DailyBar] = []
    for i in range(n):
        c = start + step * i
        out.append(
            DailyBar(
                day=last - timedelta(days=n - 1 - i),
                open=c,
                high=c * 1.005,
                low=c * 0.995,
                close=c,
                volume=1_000_000.0,
            )
        )
    return out


def intraday_series(prices: list[float], *, volume: float = 100_000.0) -> list[IntradayBar]:
    base = OPEN_AT - timedelta(minutes=15 * len(prices))
    return [
        IntradayBar(
            ts=base + timedelta(minutes=15 * i),
            open=p,
            high=p * 1.002,
            low=p * 0.998,
            close=p,
            volume=volume,
        )
        for i, p in enumerate(prices)
    ]


class FakeDaily:
    name = "fake-daily"

    def __init__(self, series: dict[str, list[DailyBar]]) -> None:
        self.series = series
        self.calls = 0

    async def daily_bars(self, symbol: str, *, lookback_days: int = 320) -> list[DailyBar]:
        self.calls += 1
        if symbol not in self.series:
            raise RuntimeError(f"no fixture for {symbol}")
        return self.series[symbol]


class FakeIntraday:
    name = "fake-intraday"

    def __init__(self, series: dict[str, list[IntradayBar]], *, fail: bool = False) -> None:
        self.series = series
        self.calls = 0
        self.fail = fail

    async def intraday_bars(
        self,
        symbols: list[str],
        *,
        bar_minutes: int = 15,
        session_start: datetime | None = None,
    ) -> dict[str, list[IntradayBar]]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return {s: self.series.get(s, []) for s in symbols}


class FakeClock:
    """A ``ClockProvider`` double — returns a fixed ``MarketClock``, no
    network, so tests can assert on the injected-clock path without
    depending on ``AlpacaClock``'s real HTTP call."""

    name = "fake-clock"

    def __init__(self, value: MarketClock) -> None:
        self.value = value
        self.calls = 0

    async def now(self, *, at: datetime | None = None) -> MarketClock:
        self.calls += 1
        return self.value


def make_scanner(
    daily: dict[str, list[DailyBar]],
    intraday: dict[str, list[IntradayBar]],
    *,
    config: ScannerConfig | None = None,
    cooldown_minutes: int = 240,
    clock: ClockProvider | None = None,
) -> Scanner:
    return Scanner(
        daily_bars=FakeDaily(daily),
        intraday=FakeIntraday(intraday),
        clock=clock,
        config=config or ScannerConfig(),
        cooldown=TriggerCooldown(cooldown_minutes),
    )


# ─────────────────────────────────────────────────────────────────────
# Market-hours gate
# ─────────────────────────────────────────────────────────────────────


async def test_no_scan_when_the_market_is_closed() -> None:
    """The gate must fire BEFORE any provider call — a closed-market scan
    that still hits the data API is a rate-limit budget spent on nothing."""
    sc = make_scanner({"AAA": daily_series()}, {"AAA": intraday_series([110.0])})
    result = await sc.scan(["AAA"], now=CLOSED_AT)
    assert result.market_open is False
    assert result.signals == ()
    assert sc.intraday.calls == 0  # type: ignore[attr-defined]
    assert sc.daily_bars.calls == 0  # type: ignore[attr-defined]


async def test_no_scan_at_the_weekend() -> None:
    sc = make_scanner({"AAA": daily_series()}, {})
    assert (await sc.scan(["AAA"], now=WEEKEND_AT)).market_open is False


async def test_force_bypasses_the_hours_gate() -> None:
    sc = make_scanner(
        {"AAA": daily_series(step=0.0)},
        {"AAA": intraday_series([104.0])},
    )
    result = await sc.scan(["AAA"], now=CLOSED_AT, force=True)
    assert result.market_open is True
    assert sc.intraday.calls == 1  # type: ignore[attr-defined]


async def test_empty_watchlist_is_a_clean_no_op() -> None:
    sc = make_scanner({}, {})
    result = await sc.scan([], now=OPEN_AT)
    assert result.symbols_scanned == ()
    assert result.signals == ()


# ─────────────────────────────────────────────────────────────────────
# Injected clock (AlpacaClock via ClockProvider) vs. the local calendar
# ─────────────────────────────────────────────────────────────────────


async def test_scanner_uses_the_local_calendar_when_no_clock_is_injected() -> None:
    """``clock=None`` (the default — every pre-existing call site) must
    preserve exactly the old behaviour: the local holiday-table calendar
    decides, and ``market_open_source`` says so."""
    sc = make_scanner({"AAA": daily_series()}, {"AAA": intraday_series([104.0])})
    assert sc.clock is None
    result = await sc.scan(["AAA"], now=OPEN_AT)
    assert result.market_open is True
    assert result.market_open_source == "local_calendar"


async def test_scanner_reports_market_open_source() -> None:
    """An injected clock's ``source`` (e.g. Alpaca's real ``/v2/clock``)
    flows through to the ``ScanResult`` — not hardcoded to the local-
    calendar default the moment a clock is configured."""
    clock = FakeClock(MarketClock(is_open=True, source="alpaca"))
    sc = make_scanner(
        {"AAA": daily_series()}, {"AAA": intraday_series([104.0])}, clock=clock
    )
    result = await sc.scan(["AAA"], now=OPEN_AT)
    assert result.market_open is True
    assert result.market_open_source == "alpaca"
    assert clock.calls == 1


async def test_scanner_skips_scan_when_injected_clock_reports_closed() -> None:
    """The injected clock's VERDICT gates the scan, not just its label.
    ``OPEN_AT`` is a weekday session the local calendar would call open;
    the clock saying closed must still block the scan — this is the whole
    point of wiring a real clock (an unscheduled early close the static
    holiday table cannot know about)."""
    clock = FakeClock(MarketClock(is_open=False, source="alpaca"))
    sc = make_scanner(
        {"AAA": daily_series()}, {"AAA": intraday_series([104.0])}, clock=clock
    )
    result = await sc.scan(["AAA"], now=OPEN_AT)
    assert result.market_open is False
    assert result.market_open_source == "alpaca"
    assert sc.intraday.calls == 0  # type: ignore[attr-defined]
    assert sc.daily_bars.calls == 0  # type: ignore[attr-defined]


# ─────────────────────────────────────────────────────────────────────
# Snapshot construction
# ─────────────────────────────────────────────────────────────────────


def test_snapshot_joins_settled_levels_to_the_live_price() -> None:
    daily = daily_series(120, start=100.0, step=0.0)
    snap = build_snapshot("AAA", daily, intraday_series([103.0, 104.0]), observed_at=OPEN_AT)
    assert snap is not None
    assert snap.prior_close == pytest.approx(100.0)  # settled
    assert snap.last_price == pytest.approx(104.0)  # live
    assert snap.sma20 == pytest.approx(100.0)
    assert snap.session_open == pytest.approx(103.0)
    assert snap.intraday_bars == 2
    assert snap.donchian_low_20 is not None and snap.donchian_low_20 < snap.donchian_high_20  # type: ignore[operator]


def test_snapshot_drops_a_same_day_daily_bar() -> None:
    """A provider that leaks today's half-formed candle must not be able to
    contaminate the levels the crosses compare against."""
    daily = daily_series(120, start=100.0)
    today_bar = DailyBar(
        day=OPEN_AT.date(), open=200.0, high=200.0, low=200.0, close=200.0, volume=1.0
    )
    snap = build_snapshot("AAA", [*daily, today_bar], intraday_series([101.0]),
                          observed_at=OPEN_AT)
    assert snap is not None
    assert snap.prior_close == pytest.approx(100.0)
    assert snap.sma20 == pytest.approx(100.0)


def test_snapshot_without_intraday_falls_back_to_the_prior_close() -> None:
    snap = build_snapshot("AAA", daily_series(), [], observed_at=OPEN_AT)
    assert snap is not None
    assert snap.has_intraday is False
    assert snap.last_price == pytest.approx(snap.prior_close)


def test_snapshot_returns_none_without_settled_history() -> None:
    assert build_snapshot("AAA", [], intraday_series([100.0]), observed_at=OPEN_AT) is None


def test_live_rsi_differs_from_settled_rsi() -> None:
    """The live RSI folds today's price into the Wilder recursion — that is
    what makes an intraday band exit a real transition."""
    daily = daily_series(120, start=100.0, step=0.05)
    snap = build_snapshot("AAA", daily, intraday_series([90.0]), observed_at=OPEN_AT)
    assert snap is not None
    assert snap.rsi_prior is not None and snap.rsi_live is not None
    assert snap.rsi_live < snap.rsi_prior


# ─────────────────────────────────────────────────────────────────────
# End-to-end signal production
# ─────────────────────────────────────────────────────────────────────


async def test_breakout_produces_a_named_signal() -> None:
    daily = daily_series(120, start=100.0, step=0.0)
    sc = make_scanner({"AAA": daily}, {"AAA": intraday_series([106.0])})
    result = await sc.scan(["AAA"], now=OPEN_AT)
    fired = {s.trigger_rule for s in result.signals}
    assert TriggerRule.DONCHIAN_BREAKOUT_UP in fired
    assert result.triggered_symbols == ("AAA",)
    assert all(s.symbol == "AAA" for s in result.signals)


async def test_quiet_symbol_produces_no_signal() -> None:
    daily = daily_series(120, start=100.0, step=0.0)
    sc = make_scanner({"AAA": daily}, {"AAA": intraday_series([100.0])})
    result = await sc.scan(["AAA"], now=OPEN_AT)
    assert result.signals == ()
    assert result.triggered_symbols == ()


async def test_thin_history_is_reported_as_an_error_not_a_signal() -> None:
    sc = make_scanner({"AAA": daily_series(20)}, {"AAA": intraday_series([130.0])})
    result = await sc.scan(["AAA"], now=OPEN_AT)
    assert result.signals == ()
    assert "AAA" in result.errors


async def test_one_bad_symbol_does_not_kill_the_pass() -> None:
    """A daily-bars failure on one name must not cost the other fourteen."""
    daily = daily_series(120, start=100.0, step=0.0)
    sc = make_scanner({"AAA": daily}, {"AAA": intraday_series([106.0])})
    result = await sc.scan(["AAA", "MISSING"], now=OPEN_AT)
    assert result.triggered_symbols == ("AAA",)
    assert "MISSING" in result.errors


async def test_intraday_failure_yields_no_signals_and_records_errors() -> None:
    sc = Scanner(
        daily_bars=FakeDaily({"AAA": daily_series()}),
        intraday=FakeIntraday({}, fail=True),
        cooldown=TriggerCooldown(),
    )
    result = await sc.scan(["AAA"], now=OPEN_AT)
    assert result.signals == ()
    assert "AAA" in result.errors


async def test_intraday_is_fetched_in_one_batched_call() -> None:
    """Cost claim under test: request count must not scale with watchlist size."""
    daily = {s: daily_series() for s in ("AAA", "BBB", "CCC")}
    intra = {s: intraday_series([100.0]) for s in daily}
    sc = make_scanner(daily, intra)
    await sc.scan(list(daily), now=OPEN_AT)
    assert sc.intraday.calls == 1  # type: ignore[attr-defined]


async def test_relative_strength_ranks_the_scanned_universe() -> None:
    daily = {
        "UP": daily_series(120, start=100.0, step=0.5),
        "FLAT": daily_series(120, start=100.0, step=0.0),
        "DOWN": daily_series(120, start=160.0, step=-0.3),
    }
    intra = {s: intraday_series([daily[s][-1].close]) for s in daily}
    sc = make_scanner(daily, intra)
    result = await sc.scan(list(daily), now=OPEN_AT)
    rs = result.relative_strength
    assert rs["UP"] > rs["FLAT"] > rs["DOWN"]


# ─────────────────────────────────────────────────────────────────────
# Cooldown
# ─────────────────────────────────────────────────────────────────────


async def test_the_same_trigger_does_not_re_fire_inside_the_cooldown() -> None:
    """The whole cost argument: a sticky condition wakes the council once,
    not once per scan for the rest of the session."""
    daily = daily_series(120, start=100.0, step=0.0)
    sc = make_scanner({"AAA": daily}, {"AAA": intraday_series([106.0])})

    first = await sc.scan(["AAA"], now=OPEN_AT)
    second = await sc.scan(["AAA"], now=OPEN_AT + timedelta(minutes=5))
    third = await sc.scan(["AAA"], now=OPEN_AT + timedelta(minutes=10))

    assert first.signals != ()
    assert second.signals == ()
    assert third.signals == ()
    assert second.suppressed != ()  # visible, not silently dropped


async def test_the_trigger_re_fires_once_the_cooldown_elapses() -> None:
    daily = daily_series(120, start=100.0, step=0.0)
    sc = make_scanner({"AAA": daily}, {"AAA": intraday_series([106.0])}, cooldown_minutes=60)
    assert (await sc.scan(["AAA"], now=OPEN_AT)).signals != ()
    assert (await sc.scan(["AAA"], now=OPEN_AT + timedelta(minutes=30))).signals == ()
    assert (await sc.scan(["AAA"], now=OPEN_AT + timedelta(minutes=61))).signals != ()


def test_cooldown_is_scoped_per_symbol_and_rule() -> None:
    cd = TriggerCooldown(60)
    assert cd.is_cool("AAA", "dma20_cross_up", OPEN_AT) is True
    cd.mark("AAA", "dma20_cross_up", OPEN_AT)
    assert cd.is_cool("AAA", "dma20_cross_up", OPEN_AT) is False
    # A different rule on the same symbol is independent…
    assert cd.is_cool("AAA", "gap_up_2pct", OPEN_AT) is True
    # …and so is the same rule on a different symbol.
    assert cd.is_cool("BBB", "dma20_cross_up", OPEN_AT) is True


def test_cooldown_is_case_insensitive_on_the_symbol() -> None:
    cd = TriggerCooldown(60)
    cd.mark("aaa", "gap_up_2pct", OPEN_AT)
    assert cd.is_cool("AAA", "gap_up_2pct", OPEN_AT) is False


def test_cooldown_reset_clears_everything() -> None:
    cd = TriggerCooldown(60)
    cd.mark("AAA", "gap_up_2pct", OPEN_AT)
    cd.reset()
    assert cd.is_cool("AAA", "gap_up_2pct", OPEN_AT) is True


def test_zero_cooldown_never_suppresses() -> None:
    cd = TriggerCooldown(0)
    cd.mark("AAA", "gap_up_2pct", OPEN_AT)
    assert cd.is_cool("AAA", "gap_up_2pct", OPEN_AT) is True
