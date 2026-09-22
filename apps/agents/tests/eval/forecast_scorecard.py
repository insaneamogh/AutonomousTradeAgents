"""Was each view the desk formed actually right? Scored against the tape.

docs/PLAN_PLATFORM.md Phase 3, the "forecast ledger". The Refusal Ledger
scores only what was REFUSED, and P&L scores only what was TRADED, which
is 21 positions. Neither answers the question the LLM spend hangs on: does
the council's opinion carry information that `strategy_fit` alone does not?

Every decision row already holds the views it was built from, so this
needs no migration and works retroactively over every recorded decision:

  strategy_fit   `reasoning.strategy_fit.winner`: the deterministic call
  bull / bear    `reasoning.options_resolution.bull|bear`: each LLM agent
                 independently, including the passes where they DISAGREED
                 and nothing traded
  council        the resolved direction when the pass proceeded (options),
                 or a BUY/SELL final action (equity)

Each view is scored on the UNDERLYING: sign-adjusted % move from the
decision-time price to the close `h` trading days later. It judges the
direction call, not the contract. A right call can still lose on an
option (entry_quality), but a WRONG call cannot win, and that is what
this can settle.

    railway run -s AutonomousTradeAgents python -m tests.eval.forecast_scorecard
    ... --horizon 5

Read-only: one SELECT on agent_decisions, then Alpaca daily closes. Writes
nothing.

What it deliberately does NOT claim. The live window is weeks, not years,
so every view source FAILS the acceptance bar's history clause by
construction. The useful outputs are the RELATIVE ones:
  - council vs strategy_fit on the same rows
  - which agent was right when Bull and Bear disagreed
  - whether hit rate rises with stated conviction (calibration)
None of those needs years; each needs only enough rows.
"""

from __future__ import annotations

import asyncio
import os
import statistics as st
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tests.eval.acceptance import t_stat

from engine.options.rules.concentration import occ_root
from engine.prices.base import DailyClose

_ET = ZoneInfo("America/New_York")

SOURCES = ("strategy_fit", "bull", "bear", "council")

CONVICTION_BUCKETS = ((0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 1.01))
"""Edges chosen from the MEASURED council range (0.28-0.62 over 151 option
decisions), so each bucket can actually fill."""


@dataclass(frozen=True)
class View:
    source: str
    symbol: str
    day: date
    """The ET trading date the view was formed."""
    direction: str
    conviction: float | None
    entry: float | None
    """Decision-time price (`reasoning.feature_snapshot.last_price`), when
    persisted. Otherwise the scorer falls back to that day's close."""


@dataclass(frozen=True)
class Scored:
    view: View
    ret_pct: float
    """Sign-adjusted: positive means the direction call was right."""


def _float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _direction(v: Any) -> str | None:
    d = str(v or "").strip().lower()
    return d if d in ("long", "short") else None


def extract_views(row: Mapping[str, Any]) -> list[View]:
    """Every scorable view in one `agent_decisions` row. Never raises on a
    partial or pre-migration row: a missing block yields no view."""
    raw_symbol = str(row.get("symbol") or "").strip().upper()
    symbol = occ_root(raw_symbol) or raw_symbol
    triggered = row.get("triggered_at")
    if not symbol or not isinstance(triggered, datetime):
        return []
    day = triggered.astimezone(_ET).date() if triggered.tzinfo else triggered.date()
    reasoning = row.get("reasoning") or {}
    if not isinstance(reasoning, Mapping):
        return []
    snap = reasoning.get("feature_snapshot") or {}
    entry = _float(snap.get("last_price")) if isinstance(snap, Mapping) else None

    views: list[View] = []

    fit = reasoning.get("strategy_fit") or {}
    winner = fit.get("winner") if isinstance(fit, Mapping) else None
    if isinstance(winner, Mapping):
        d = _direction(winner.get("direction"))
        if d:
            views.append(View("strategy_fit", symbol, day, d,
                              _float(winner.get("conviction")), entry))

    res = reasoning.get("options_resolution") or {}
    if isinstance(res, Mapping) and res:
        for side in ("bull", "bear"):
            agent = res.get(side) or {}
            if not isinstance(agent, Mapping) or agent.get("degraded"):
                # A degraded view is a parse failure, not an opinion. Scoring
                # it would count formatting as judgement.
                continue
            d = _direction(agent.get("direction"))
            if d:
                views.append(View(side, symbol, day, d, _float(agent.get("conviction")), entry))
        if res.get("proceed"):
            d = _direction(res.get("direction"))
            if d:
                views.append(View("council", symbol, day, d, _float(res.get("conviction")), entry))
    else:
        action = str(row.get("final_action") or "").upper()
        if action in ("BUY", "SELL"):
            views.append(View("council", symbol, day,
                              "long" if action == "BUY" else "short", None, entry))
    return views


def score(view: View, closes: list[DailyClose], horizon: int) -> Scored | None:
    """Underlying move from entry to the close `horizon` trading days after
    the view's day. None when the tape does not reach that far yet."""
    series = sorted(closes, key=lambda c: c.day)
    idx = next((i for i, c in enumerate(series) if c.day >= view.day), None)
    if idx is None or idx + horizon >= len(series):
        return None
    entry = view.entry if view.entry and view.entry > 0 else series[idx].close
    exit_ = series[idx + horizon].close
    if entry <= 0:
        return None
    sign = 1.0 if view.direction == "long" else -1.0
    return Scored(view, sign * (exit_ - entry) / entry * 100.0)


def independent(scored: list[Scored], horizon: int) -> list[Scored]:
    """One observation per (source, symbol) per forward window. The live
    desk re-looked at the same names every few minutes; without this, one
    move gets counted dozens of times."""
    last: dict[tuple[str, str], date] = {}
    kept: list[Scored] = []
    for s in sorted(scored, key=lambda x: (x.view.source, x.view.symbol, x.view.day)):
        key = (s.view.source, s.view.symbol)
        prev = last.get(key)
        if prev is None or (s.view.day - prev).days >= horizon * 1.5:
            kept.append(s)
            last[key] = s.view.day
    return kept


@dataclass(frozen=True)
class Line:
    n: int
    hit: float
    mean: float
    t: float


def summarize(rows: list[Scored]) -> Line | None:
    if not rows:
        return None
    vals = [r.ret_pct for r in rows]
    return Line(
        n=len(vals),
        hit=sum(1 for v in vals if v > 0) / len(vals),
        mean=st.mean(vals),
        t=t_stat(vals),
    )


def by_source(scored: list[Scored]) -> dict[str, Line | None]:
    groups: dict[str, list[Scored]] = defaultdict(list)
    for s in scored:
        groups[s.view.source].append(s)
    return {src: summarize(groups.get(src, [])) for src in SOURCES}


def calibration(scored: list[Scored], source: str) -> list[tuple[str, Line | None]]:
    """Hit rate by stated conviction. A calibrated source's hit rate RISES
    across the buckets; a flat or falling one is conviction that means
    nothing, which is what sizing currently multiplies by."""
    out: list[tuple[str, Line | None]] = []
    for lo, hi in CONVICTION_BUCKETS:
        rows = [
            s for s in scored
            if s.view.source == source
            and s.view.conviction is not None
            and lo <= s.view.conviction < hi
        ]
        out.append((f"{lo:.1f}-{min(hi, 1.0):.1f}", summarize(rows)))
    return out


def disagreements(scored: list[Scored]) -> tuple[int, int, int]:
    """(bull right, bear right, n) on the passes where Bull and Bear pointed
    opposite ways. The resolver HOLDs every one of these, so this is the
    only place their information shows up at all."""
    by_key: dict[tuple[str, date], dict[str, Scored]] = defaultdict(dict)
    for s in scored:
        if s.view.source in ("bull", "bear"):
            by_key[(s.view.symbol, s.view.day)][s.view.source] = s
    bull_right = bear_right = n = 0
    for pair in by_key.values():
        b, r = pair.get("bull"), pair.get("bear")
        if not b or not r or b.view.direction == r.view.direction:
            continue
        n += 1
        if b.ret_pct > 0:
            bull_right += 1
        elif r.ret_pct > 0:
            bear_right += 1
    return bull_right, bear_right, n


def render(scored: list[Scored], horizon: int) -> str:
    lines = [f"\nforecast scorecard: {horizon}-trading-day horizon, underlying moves\n"]
    lines.append(f"  {'source':<13}{'n':>6}{'hit':>8}{'mean':>9}{'t':>7}")
    lines.append("  " + "-" * 43)
    for src, line in by_source(scored).items():
        if line is None:
            lines.append(f"  {src:<13}{'-':>6}")
            continue
        lines.append(f"  {src:<13}{line.n:>6}{line.hit:>8.1%}{line.mean:>+8.2f}%{line.t:>+7.2f}")
    for src in ("bull", "bear", "council"):
        cal = calibration(scored, src)
        if not any(c for _, c in cal):
            continue
        lines.append(f"\n  calibration: {src} (hit rate should RISE with conviction)")
        for label, c in cal:
            if c is None:
                lines.append(f"    {label:<9}{'-':>6}")
            else:
                lines.append(f"    {label:<9}{c.n:>6}{c.hit:>8.1%}{c.mean:>+8.2f}%")
    b, r, n = disagreements(scored)
    if n:
        lines.append(f"\n  Bull vs Bear disagreed on {n} passes (all HOLD): "
                     f"bull right {b}, bear right {r}, flat {n - b - r}")
    lines.append(
        "\n  n counts INDEPENDENT views (one per source per symbol per window).\n"
        "  Weeks of data: read RELATIVE rows (council vs strategy_fit, bull vs\n"
        "  bear, calibration), not absolute significance."
    )
    return "\n".join(lines)


def _db_url() -> str | None:
    url = os.environ.get("DATABASE_URL", "").strip()
    public = os.environ.get("DATABASE_PUBLIC_URL", "").strip()
    if ".railway.internal" in url and public:
        url = public  # the internal hostname does not resolve off-network
    if not url:
        return None
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


async def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizon", type=int, default=5)
    args = ap.parse_args()

    url = _db_url()
    if not url:
        print("DATABASE_URL unset. Run under `railway run -s AutonomousTradeAgents`.")
        return 2
    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        print("ALPACA_API_KEY/ALPACA_SECRET_KEY unset; closes are needed to score views.")
        return 2

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, echo=False)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(text(
                    "SELECT symbol, triggered_at, reasoning, final_action "
                    "FROM agent_decisions WHERE reasoning IS NOT NULL "
                    "ORDER BY triggered_at"
                ))
            ).mappings().all()
    finally:
        await engine.dispose()

    views = [v for r in rows for v in extract_views(r)]
    if not views:
        print(f"{len(rows)} decisions, none with a scorable view.")
        return 1
    print(f"{len(rows)} decisions -> {len(views)} views over "
          f"{len({v.symbol for v in views})} symbols")

    from engine.prices.alpaca import AlpacaPriceProvider

    provider = AlpacaPriceProvider(key, secret)
    start = min(v.day for v in views) - timedelta(days=7)
    end = max(v.day for v in views) + timedelta(days=args.horizon * 2 + 10)
    closes: dict[str, list[DailyClose]] = {}
    for sym in sorted({v.symbol for v in views}):
        try:
            closes[sym] = await provider.daily_closes(sym, start, end)
        except Exception as exc:  # one bad symbol must not sink the report
            print(f"  closes unavailable for {sym}: {type(exc).__name__}")

    scored = [
        s for v in views
        if (s := score(v, closes.get(v.symbol, []), args.horizon)) is not None
    ]
    print(f"{len(scored)} views scorable (the rest have not reached +{args.horizon}d yet)")
    print(render(independent(scored, args.horizon), args.horizon))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
