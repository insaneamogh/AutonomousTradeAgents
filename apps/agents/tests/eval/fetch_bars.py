"""Fetch daily bars for the backtest universe. READ-ONLY, network + Alpaca.

One call per symbol against Alpaca's free IEX daily feed, which reaches
back to ~2020-07-27 (about 1,530 trading days). Written to a gzipped JSON
fixture so `signal_backtest` runs offline like the rest of `tests/eval`.

    AK=... SK=... python -m tests.eval.fetch_bars tests/eval/fixtures/bars.json.gz

NSE (docs/PLAN_ZERODHA.md Z4), through a Kite session: the paid Kite
Connect plan and that day's access token. Kite serves at most 2,000 days
of daily candles per request, so each symbol is fetched in chunks.

    KITE_API_KEY=... KITE_ACCESS_TOKEN=... python -m tests.eval.fetch_bars --kite
"""

from __future__ import annotations

import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request

START = "2020-01-01"
END = "2026-09-01"

# Liquid, mostly large-cap names from the live watchlist plus SPY as the
# correlation benchmark. Deliberately NOT the full 190: the free feed is
# rate-limited, and a broad-but-shallow universe would trade breadth for
# the depth of history that actually makes the result mean something.
UNIVERSE = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "AMD", "AVGO", "CRM", "ADBE", "NFLX", "INTC", "MU", "QCOM", "TXN",
    "JPM", "BAC", "WFC", "GS", "MS", "C", "AXP", "BLK",
    "XOM", "CVX", "COP", "SLB", "XLE",
    "JNJ", "UNH", "PFE", "ABBV", "MRK", "LLY", "GILD", "BMY",
    "WMT", "COST", "HD", "MCD", "NKE", "SBUX", "TGT", "LOW",
    "CAT", "BA", "GE", "HON", "UPS", "RTX",
    "XLF", "XLK", "XLV", "IWM", "DIA",
]


def fetch(symbol: str, key: str, secret: str) -> list[dict]:
    url = (
        f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
        f"?timeframe=1Day&start={START}&end={END}&limit=10000&feed=iex"
    )
    req = urllib.request.Request(
        url, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r).get("bars") or []
        except urllib.error.HTTPError as exc:
            if exc.code == 429:  # rate limited — back off rather than lose the symbol
                time.sleep(2 * (attempt + 1))
                continue
            raise
    return []


# Approximately today's NIFTY 50, plus the index as the benchmark. NOT a
# point-in-time membership list: names that left the index over the window
# are absent, which flatters any backtest on it (survivorship). NSE revises
# the index twice a year; check the current list before fetching.
UNIVERSE_NSE = [
    "NSE:NIFTY 50",
    "NSE:RELIANCE", "NSE:HDFCBANK", "NSE:ICICIBANK", "NSE:INFY", "NSE:TCS", "NSE:ITC",
    "NSE:LT", "NSE:BHARTIARTL", "NSE:SBIN", "NSE:AXISBANK", "NSE:KOTAKBANK",
    "NSE:HINDUNILVR", "NSE:BAJFINANCE", "NSE:M&M", "NSE:MARUTI", "NSE:SUNPHARMA",
    "NSE:HCLTECH", "NSE:TITAN", "NSE:NTPC", "NSE:ULTRACEMCO", "NSE:POWERGRID",
    "NSE:ASIANPAINT", "NSE:ONGC", "NSE:ADANIENT", "NSE:ADANIPORTS", "NSE:TATASTEEL",
    "NSE:COALINDIA", "NSE:BAJAJFINSV", "NSE:NESTLEIND", "NSE:JSWSTEEL", "NSE:GRASIM",
    "NSE:TECHM", "NSE:WIPRO", "NSE:HINDALCO", "NSE:CIPLA", "NSE:DRREDDY", "NSE:SBILIFE",
    "NSE:HDFCLIFE", "NSE:EICHERMOT", "NSE:TATACONSUM", "NSE:APOLLOHOSP", "NSE:BAJAJ-AUTO",
    "NSE:SHRIRAMFIN", "NSE:TRENT", "NSE:BEL", "NSE:INDUSINDBK", "NSE:HEROMOTOCO",
    "NSE:BRITANNIA", "NSE:BPCL",
]

KITE_MAX_DAYS = 2000


async def fetch_kite(
    client, symbols: list[str], *, start: str = START, end: str = END, pause_s: float = 0.4,
) -> dict[str, list[list]]:
    """Daily candles per symbol as [date, o, h, l, c, v] rows (IST dates).
    A symbol missing from today's NSE dump is reported and skipped."""
    import asyncio
    from datetime import date, datetime, timedelta
    from zoneinfo import ZoneInfo

    from engine.risk.markets import tradingsymbol_of

    ist = ZoneInfo("Asia/Kolkata")
    dump = {str(r["tradingsymbol"]).upper(): r for r in await client.instruments("NSE")}
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    out: dict[str, list[list]] = {}
    for i, sym in enumerate(symbols, 1):
        inst = dump.get(tradingsymbol_of(sym).upper())
        if inst is None:
            print(f"  {i:>3}/{len(symbols)} {sym:18} not in today's NSE dump — skipped")
            continue
        rows: dict[str, list] = {}
        lo = first
        while lo <= last:
            hi = min(last, lo + timedelta(days=KITE_MAX_DAYS - 1))
            candles = await client.historical_daily(
                int(inst["instrument_token"]),
                start=datetime.combine(lo, datetime.min.time(), tzinfo=ist),
                end=datetime.combine(hi, datetime.max.time(), tzinfo=ist),
            )
            for ts, o, h, lo_, c, v in candles:
                rows[ts.astimezone(ist).date().isoformat()] = [
                    ts.astimezone(ist).date().isoformat(), o, h, lo_, c, v]
            lo = hi + timedelta(days=1)
            if pause_s:
                await asyncio.sleep(pause_s)  # Kite historical: about 3 requests a second
        out[sym] = [rows[d] for d in sorted(rows)]
        print(f"  {i:>3}/{len(symbols)} {sym:18} {len(out[sym]):>5} bars", flush=True)
    return out


def _main_kite() -> int:
    import asyncio

    from broker.zerodha import ZerodhaBroker

    key = os.environ.get("KITE_API_KEY", "").strip()
    token = os.environ.get("KITE_ACCESS_TOKEN", "").strip()
    if not key or not token:
        print("KITE_API_KEY and KITE_ACCESS_TOKEN required (today's Kite session)",
              file=sys.stderr)
        return 1
    args = [a for a in sys.argv[1:] if a != "--kite"]
    out = args[0] if args else "tests/eval/fixtures/bars_nse.json.gz"
    data = asyncio.run(fetch_kite(ZerodhaBroker(api_key=key, access_token=token), UNIVERSE_NSE))
    with gzip.open(out, "wt") as fh:
        json.dump({"start": START, "end": END, "source": "kite", "bars": data}, fh)
    total = sum(len(v) for v in data.values())
    print(f"\n{len(data)} symbols, {total:,} bars -> {out}")
    return 0


def main() -> int:
    if "--kite" in sys.argv[1:]:
        return _main_kite()
    key, secret = os.environ.get("AK", ""), os.environ.get("SK", "")
    if not key or not secret:
        print("AK/SK required", file=sys.stderr)
        return 1
    out = sys.argv[1] if len(sys.argv) > 1 else "tests/eval/fixtures/bars.json.gz"

    data: dict[str, list[list]] = {}
    for i, sym in enumerate(UNIVERSE, 1):
        bars = fetch(sym, key, secret)
        # [date, open, high, low, close, volume] — positional keeps the
        # fixture a third the size of keyed objects across ~85k bars.
        data[sym] = [
            [b["t"][:10], b["o"], b["h"], b["l"], b["c"], b["v"]] for b in bars
        ]
        print(f"  {i:>3}/{len(UNIVERSE)} {sym:6} {len(bars):>5} bars", flush=True)
        time.sleep(0.25)

    with gzip.open(out, "wt") as fh:
        json.dump({"start": START, "end": END, "bars": data}, fh)
    total = sum(len(v) for v in data.values())
    print(f"\n{len(data)} symbols, {total:,} bars -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
