"""The daily IV recorder: one row per underlying, failures isolated."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from trading_agents.jobs.iv_snapshot import IvRow, snapshot

TODAY = date(2026, 9, 23)


@dataclass(frozen=True)
class Q:
    contract_type: str
    expiry: date
    delta: float | None
    implied_volatility: float | None


def _chain(iv: float) -> list[Q]:
    out = []
    for days in (21, 35, 56, 70):
        exp = TODAY + timedelta(days=days)
        out += [Q("call", exp, 0.5, iv), Q("put", exp, -0.5, iv)]
    return out


async def test_one_row_per_underlying_with_both_maturities() -> None:
    written: list[IvRow] = []

    async def fetch(sym, today):
        return _chain(0.30 if sym == "NVDA" else 0.20)

    async def write(rows):
        written.extend(rows)

    result = await snapshot(["nvda", "SPY", "NVDA"], TODAY, fetch_chain=fetch,
                            write_rows=write, feed="indicative")
    assert result == {"recorded": 2, "no_atm_iv": 0, "failed": 0}
    by = {r.symbol: r for r in written}
    assert by["NVDA"].atm_iv_30d == pytest.approx(0.30)
    assert by["NVDA"].atm_iv_60d == pytest.approx(0.30)
    assert by["SPY"].n_expiries == 4 and by["SPY"].feed == "indicative"


async def test_one_bad_chain_does_not_cost_the_rest_of_the_day() -> None:
    written: list[IvRow] = []

    async def fetch(sym, today):
        if sym == "BAD":
            raise RuntimeError("chain 500")
        return _chain(0.25)

    async def write(rows):
        written.extend(rows)

    result = await snapshot(["BAD", "SPY"], TODAY, fetch_chain=fetch,
                            write_rows=write, feed="opra")
    assert result["failed"] == 1 and result["recorded"] == 1
    assert [r.symbol for r in written] == ["SPY"]


async def test_no_atm_market_is_recorded_as_a_fact_not_skipped() -> None:
    written: list[IvRow] = []

    async def fetch(sym, today):
        return []

    async def write(rows):
        written.extend(rows)

    result = await snapshot(["THIN"], TODAY, fetch_chain=fetch, write_rows=write, feed="indicative")
    assert result == {"recorded": 1, "no_atm_iv": 1, "failed": 0}
    assert written[0].atm_iv_30d is None
