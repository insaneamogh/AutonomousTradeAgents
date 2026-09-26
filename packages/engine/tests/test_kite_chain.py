"""NSE option chain from Kite (docs/PLAN_ZERODHA.md Z2): the instruments
dump plus one quote call becomes ContractQuote candidates in lots."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from engine.options import pricing
from engine.options.kite_chain import (
    CARRY_RATE,
    MAX_QUOTED_CONTRACTS,
    fetch_kite_option_candidates,
    option_name_for,
    reset_cache_for_tests,
)

NOW = datetime(2026, 9, 28, 5, 0, tzinfo=UTC)  # 10:30 IST, a Monday
SPOT = 25_000.0
VOL = 0.14


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    reset_cache_for_tests()


def _row(ts: str, name: str, expiry: date, strike: float, kind: str, lot: int = 65) -> dict:
    return {"tradingsymbol": ts, "name": name, "expiry": expiry.isoformat(), "strike": strike,
            "instrument_type": kind, "lot_size": lot, "segment": "NFO-OPT"}


class _FakeKite:
    """Quotes every NFO contract at its Black-Scholes value (vol 14%) with a
    one-rupee spread, so the IV the adapter backs out is checkable."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.quote_calls: list[list[str]] = []
        self.dump_calls = 0

    async def instruments(self, exchange: str) -> list[dict[str, Any]]:
        assert exchange == "NFO"
        self.dump_calls += 1
        return self.rows

    async def quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        self.quote_calls.append(list(symbols))
        by_ts = {f"NFO:{r['tradingsymbol']}": r for r in self.rows}
        out: dict[str, dict[str, Any]] = {}
        for s in symbols:
            if s == "NSE:NIFTY 50":
                out[s] = {"last_price": SPOT}
                continue
            r = by_ts[s]
            expiry = date.fromisoformat(r["expiry"])
            t = (datetime(expiry.year, expiry.month, expiry.day, 10, 0, tzinfo=UTC)
                 - NOW).total_seconds() / (365 * 86400)
            kind = "call" if r["instrument_type"] == "CE" else "put"
            mid = pricing.price(SPOT, r["strike"], t, VOL, kind=kind, rate=CARRY_RATE)
            out[s] = {
                "last_price": mid, "oi": 650_000, "volume": 130_000,
                "timestamp": "2026-09-28 10:29:58",
                "depth": {"buy": [{"price": mid - 0.5, "quantity": 650}],
                          "sell": [{"price": mid + 0.5, "quantity": 650}]},
            }
        return out


def _chain() -> list[dict]:
    monthly = NOW.date() + timedelta(days=29)
    rows = []
    for strike in range(23_000, 27_001, 100):
        for kind in ("CE", "PE"):
            rows.append(_row(f"NIFTY26OCT{strike}{kind}", "NIFTY", monthly, strike, kind))
    rows.append(_row("NIFTY26SEP25000CE", "NIFTY", NOW.date() + timedelta(days=1), 25_000, "CE"))
    rows.append(_row("BANKNIFTY26OCT55000CE", "BANKNIFTY", monthly, 55_000, "CE", lot=30))
    rows.append({**_row("NIFTY26OCTFUT", "NIFTY", monthly, 0, "FUT")})
    return rows


def test_index_underlyings_map_to_their_nfo_names() -> None:
    assert option_name_for("NSE:NIFTY 50") == "NIFTY"
    assert option_name_for("NSE:NIFTY BANK") == "BANKNIFTY"
    assert option_name_for("NSE:RELIANCE") == "RELIANCE"


async def test_candidates_are_in_lots_with_iv_and_delta_from_the_quote() -> None:
    kite = _FakeKite(_chain())
    got = await fetch_kite_option_candidates(
        "NSE:NIFTY 50", client=kite, now=NOW, contract_type="call", spot_hint=SPOT,
    )

    # Calls only, inside the DTE window (not the 1-day weekly), within 6%
    # of spot (about 23,500-26,500), NIFTY only.
    assert {q.contract_type for q in got} == {"call"}
    assert {q.occ_symbol[:9] for q in got} == {"NFO:NIFTY"}
    strikes = sorted(q.strike for q in got)
    assert 23_500 <= strikes[0] <= 23_600 and 26_400 <= strikes[-1] <= 26_500
    assert len(kite.quote_calls) == 1, "underlying and contracts go in ONE quote call"
    assert kite.quote_calls[0][0] == "NSE:NIFTY 50"

    atm = next(q for q in got if q.strike == 25_000)
    assert atm.multiplier == 65, "the lot size is the multiplier, so the sizer counts lots"
    assert (atm.open_interest, atm.volume) == (10_000, 2_000), "units / 65 = lots"
    assert atm.implied_volatility == pytest.approx(VOL, abs=0.01)
    assert atm.delta == pytest.approx(0.55, abs=0.05)
    assert atm.quote_ts == datetime(2026, 9, 28, 4, 59, 58, tzinfo=UTC)  # IST stamp

    await fetch_kite_option_candidates("NSE:NIFTY 50", client=kite, now=NOW, spot_hint=SPOT)
    assert kite.dump_calls == 1, "the NFO dump is fetched once per IST day"


async def test_without_a_spot_hint_the_spot_is_quoted_and_unknown_names_are_empty() -> None:
    kite = _FakeKite(_chain())
    got = await fetch_kite_option_candidates("NSE:NIFTY 50", client=kite, now=NOW)
    assert kite.quote_calls[0] == ["NSE:NIFTY 50"] and got
    assert {q.contract_type for q in got} == {"call", "put"}
    assert await fetch_kite_option_candidates("NSE:NOPE", client=kite, now=NOW) == ()


async def test_a_wide_chain_is_cut_to_one_quote_call_nearest_the_money_first() -> None:
    monthly = NOW.date() + timedelta(days=29)
    rows = [_row(f"NIFTY26OCT{25_000 + i}CE", "NIFTY", monthly, 25_000 + i, "CE")
            for i in range(-1_000, 1_000)]
    kite = _FakeKite(rows)
    got = await fetch_kite_option_candidates(
        "NSE:NIFTY 50", client=kite, now=NOW, contract_type="call", spot_hint=SPOT,
    )
    assert len(kite.quote_calls[0]) == MAX_QUOTED_CONTRACTS + 1
    assert max(abs(q.strike - SPOT) for q in got) <= 250
