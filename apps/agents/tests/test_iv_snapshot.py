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


async def test_the_kite_chain_becomes_an_nse_iv_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the real kite_chain adapter: calls and puts at two
    monthly expiries, priced by Black-Scholes at 14%, must come back as a
    30-day ATM IV of about 14%, feed 'kite'."""
    import contextlib
    from datetime import UTC, datetime, timedelta

    from engine.options import pricing
    from engine.options.kite_chain import CARRY_RATE, reset_cache_for_tests
    from trading_agents.jobs import iv_snapshot

    reset_cache_for_tests()
    monkeypatch.setattr(iv_snapshot, "KITE_QUOTE_SPACING_S", 0.0)
    spot, vol = 25_000.0, 0.14
    today = datetime.now(UTC).date()
    expiries = [today + timedelta(days=d) for d in (22, 50)]
    rows = [
        {"tradingsymbol": f"NIFTY{e:%y%m%d}{k}{t}", "name": "NIFTY", "expiry": e.isoformat(),
         "strike": float(k), "instrument_type": t, "lot_size": 65}
        for e in expiries for k in range(24_000, 26_001, 100) for t in ("CE", "PE")
    ]
    by_symbol = {f"NFO:{r['tradingsymbol']}": r for r in rows}

    class _Kite:
        async def instruments(self, exchange):
            return rows

        async def quotes(self, symbols):
            out = {}
            for s in symbols:
                if s == "NSE:NIFTY 50":
                    out[s] = {"last_price": spot}
                    continue
                r = by_symbol[s]
                t = ((datetime.fromisoformat(r["expiry"] + "T10:00:00+00:00")
                      - datetime.now(UTC)).total_seconds() / (365 * 86400))
                kind = "call" if r["instrument_type"] == "CE" else "put"
                mid = pricing.price(spot, r["strike"], t, vol, kind=kind, rate=CARRY_RATE)
                out[s] = {"oi": 650_000, "volume": 65_000,
                          "depth": {"buy": [{"price": mid - 0.25}], "sell": [{"price": mid + 0.25}]}}
            return out

    @contextlib.asynccontextmanager
    async def _open():
        yield _Kite()

    written: list = []

    async def _write(r):
        written.extend(r)

    result = await iv_snapshot.snapshot(
        ["NSE:NIFTY 50"], today, fetch_chain=iv_snapshot.kite_chain_fetcher(_open),
        write_rows=_write, feed="kite",
    )
    assert result == {"recorded": 1, "no_atm_iv": 0, "failed": 0}
    (row,) = written
    assert row.symbol == "NSE:NIFTY 50" and row.feed == "kite" and row.n_expiries == 2
    assert row.atm_iv_30d == pytest.approx(vol, abs=0.01)
