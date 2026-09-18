"""Fetch daily bars for the backtest universe. READ-ONLY, network + Alpaca.

One call per symbol against Alpaca's free IEX daily feed, which reaches
back to ~2020-07-27 (about 1,530 trading days). Written to a gzipped JSON
fixture so `signal_backtest` runs offline like the rest of `tests/eval`.

    AK=... SK=... python -m tests.eval.fetch_bars tests/eval/fixtures/bars.json.gz
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


def main() -> int:
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
