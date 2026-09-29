"""The Phase 3 harness on NSE (docs/PLAN_ZERODHA.md Z4): same code, NSE
bars through Kite, NIFTY 50 as the benchmark, NSE delivery costs, long
only. The real run needs a Kite session; these pin the plumbing."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.eval.signal_backtest import market, run

from engine.backtester.costs_india import round_trip_pct
from engine.features.technicals import DailyBar


def _walk(drift: float, n: int = 700, start: float = 100.0) -> list[DailyBar]:
    out, px, d = [], start, date(2021, 1, 4)
    while len(out) < n:
        if d.weekday() < 5:
            px *= 1 + drift + 0.01 * math.sin(len(out) / 7.0)
            out.append(DailyBar(day=d, open=px, high=px * 1.01, low=px * 0.99, close=px,
                                volume=1e6))
        d += timedelta(days=1)
    return out


def _data(benchmark: str) -> dict[str, list[DailyBar]]:
    return {benchmark: _walk(0.0004), "NSE:UP": _walk(0.003), "NSE:DOWN": _walk(-0.003)}


def test_nse_runs_long_only_against_nifty_and_pays_the_nse_round_trip() -> None:
    # A candidate model, not the default: strategy_fit is built without
    # shorts for IN anyway, so only a model that calls shorts itself proves
    # the harness drops them.
    from tests.eval.candidates import Momentum12_1

    nse = run(step=5, market_name="IN", data=_data("NSE:NIFTY 50"), model=Momentum12_1())
    us = run(step=5, market_name="US", data=_data("SPY"), model=Momentum12_1())

    assert nse, "the harness produced no NSE signals at all"
    assert {s.direction for s in nse} == {"long"}, "NSE delivery cannot be held short"
    assert "short" in {s.direction for s in us}, "the same data does make short calls"
    assert "NSE:NIFTY 50" not in {s.symbol for s in nse}, "the benchmark is not traded"

    cost = market("IN").cost_pct
    assert cost == round_trip_pct("equity_delivery", buy_value=1e5, sell_value=1e5)
    assert cost > 2 * market("US").cost_pct, "STT both ways: dearer than 10 bps"


def test_candidates_on_nse_judge_the_same_rules_with_no_option_line() -> None:
    from tests.eval.candidates import verdicts

    lines = verdicts("IN", data=_data("NSE:NIFTY 50"))
    assert any(line.lstrip().startswith("short_term_reversal  equity") for line in lines)
    assert any(line.lstrip().startswith("momentum_12_1") for line in lines)
    assert not any("  option  " in line for line in lines), "no NSE option model yet"
    assert "survivorship" in lines[-1]


async def test_kite_history_is_fetched_in_2000_day_chunks_and_missing_names_skipped() -> None:
    from tests.eval.fetch_bars import fetch_kite

    calls: list[tuple[int, date, date]] = []

    class _Kite:
        async def instruments(self, exchange):
            assert exchange == "NSE"
            return [{"tradingsymbol": "NIFTY 50", "instrument_token": 256265},
                    {"tradingsymbol": "RELIANCE", "instrument_token": 738561}]

        async def historical_daily(self, token, *, start, end):
            calls.append((token, start.date(), end.date()))
            day = datetime.combine(start.date(), datetime.min.time(), tzinfo=UTC)
            # 18:30 UTC the day before is 00:00 IST on `day`
            return [(day - timedelta(hours=5, minutes=30), 1, 2, 0.5, 1.5, 100)]

    got = await fetch_kite(_Kite(), ["NSE:NIFTY 50", "NSE:RELIANCE", "NSE:GONE"],
                           start="2020-01-01", end="2026-09-01", pause_s=0.0)

    assert set(got) == {"NSE:NIFTY 50", "NSE:RELIANCE"}
    reliance = [c for c in calls if c[0] == 738561]
    assert [(lo.isoformat(), hi.isoformat()) for _t, lo, hi in reliance] == [
        ("2020-01-01", "2025-06-22"), ("2025-06-23", "2026-09-01")]
    assert got["NSE:RELIANCE"][0] == ["2020-01-01", 1, 2, 0.5, 1.5, 100], "IST date"


@pytest.fixture(autouse=True)
def _no_live_kite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KITE_ACCESS_TOKEN", raising=False)
