"""Instrument the long-vs-short `technical_score` gap over real history.

Implements `docs/PLAN_SHORTS.md` §5.2. The plan's own n=3 sample
(2026-09-01: short avg 34.7 vs long avg 58.5) was flagged as evidence,
not proof, of anything -- too small to tell a genuinely bullish tape
apart from real model asymmetry. This script is the reusable tool the
plan asked for so a REAL multi-day sample can answer that question
later, once one exists -- it is read-only against the production DB
and changes no trading behavior.

Usage (needs DATABASE_URL in the environment -- see apps/api/.env):
    uv run --package api python ../../scripts/score_gap_by_direction.py
    uv run --package api python ../../scripts/score_gap_by_direction.py --days 14
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "engine"))

from sqlalchemy import text

from engine.db.session import async_session_factory

# `reasoning->'strategy_fit'->'ranked'->0->>'direction'` is the WINNING
# strategy's direction for that pass -- the same field PLAN_SHORTS.md's
# own live query read. `technical_score` is a top-level column, the
# specialist score the 40-point floor (`RiskCaps.min_specialist_avg_score`)
# compares against. Equity-only (`proposal->>'isOption' IS DISTINCT FROM
# 'true' OR proposal IS NULL`) since an options pass's `direction=short`
# means "buy a put," a completely different risk profile -- see
# PLAN_SHORTS.md §0. Restricted to passes where a direction was actually
# selected (`selected_strategy IS NOT NULL`) -- a HOLD-before-any-LLM-call
# pass has no technical_score to compare.
QUERY = """
SELECT
  reasoning->'strategy_fit'->'ranked'->0->>'direction' AS direction,
  COUNT(*) AS n,
  AVG(technical_score) AS avg_score,
  MIN(technical_score) AS min_score,
  MAX(technical_score) AS max_score,
  STDDEV(technical_score) AS stddev_score
FROM agent_decisions
WHERE technical_score IS NOT NULL
  AND selected_strategy IS NOT NULL
  AND triggered_at > now() - make_interval(days => :days)
  AND (proposal IS NULL OR proposal->>'isOption' IS DISTINCT FROM 'true')
GROUP BY direction
ORDER BY direction
"""


async def main(days: int) -> None:
    session_factory = async_session_factory()
    async with session_factory() as session:
        rows = (await session.execute(text(QUERY), {"days": days})).mappings().all()

    if not rows:
        print(f"No equity decisions with a technical_score in the last {days} day(s).")
        return

    print(f"Equity technical_score by winning strategy_fit direction, last {days} day(s):\n")
    print(f"{'direction':<10} {'n':>4} {'avg':>8} {'min':>8} {'max':>8} {'stddev':>8}")
    by_direction: dict[str, float] = {}
    for r in rows:
        d = dict(r)
        avg = float(d["avg_score"]) if d["avg_score"] is not None else 0.0
        by_direction[d["direction"] or "?"] = avg
        print(
            f"{(d['direction'] or '?'):<10} {d['n']:>4} {avg:>8.1f} "
            f"{float(d['min_score']):>8.1f} {float(d['max_score']):>8.1f} "
            f"{(float(d['stddev_score']) if d['stddev_score'] is not None else 0.0):>8.1f}"
        )

    total_n = sum(dict(r)["n"] for r in rows)
    if total_n < 20:
        print(
            f"\nn={total_n} total -- too small to conclude anything (PLAN_SHORTS.md §2/§7: "
            "n=3 was already flagged as not proof of a structural bias). Re-run this over a "
            "wider window once more history exists; do not act on a gap this small."
        )
    elif "long" in by_direction and "short" in by_direction:
        gap = by_direction["long"] - by_direction["short"]
        print(f"\nlong − short avg gap: {gap:+.1f} points (n={total_n} total)")
        if abs(gap) > 15:
            print(
                "Gap exceeds the 15pt threshold PLAN_SHORTS.md §5.2 named as worth treating "
                "as evidence of real model asymmetry. That is a prompt/model question "
                "(§5.4), not a reason to move `min_specialist_avg_score` -- surface this, "
                "do not silently act on it."
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=30, help="Lookback window in days (default: 30)"
    )
    args = parser.parse_args()
    asyncio.run(main(args.days))
