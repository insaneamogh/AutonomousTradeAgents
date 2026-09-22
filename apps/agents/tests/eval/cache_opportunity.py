"""Is an LLM result cache worth building? Measure before answering.

The claim to test (PLAN §2.2): we run ~5.9 council decisions per symbol,
our features come from DAILY bars, so most of those decisions are asking an
identical question and paying for it every time. The reference integration's
contract is "an unchanged snapshot never pays for a second LLM call" — we
only have prompt-PREFIX caching, which discounts the system block and still
pays for the user content and all output.

That claim is plausible, which is exactly why it needs measuring. Six
hypotheses on this branch have already been refuted by measurement, and
"obviously redundant calls" is the kind of claim that does not survive it.

**Read-only.** SELECT only. Writes nothing, creates nothing, needs no
migration: `agent_decisions` already persists the feature dicts as JSONB
(`technical`, `fundamental`, `macro`), so the hashes can be computed
retrospectively over history that already exists. A forward-looking hash
column would have collected nothing anyway — the desk has been halted since
2026-09-08.

    railway run -s AutonomousTradeAgents \
        python -m tests.eval.cache_opportunity

Reports TWO numbers, because they answer different questions:

  whole-snapshot   what a naive "hash everything" cache would catch. Expect
                   this to be low: `technical` carries `last_price`, which
                   moves on every intraday pass, so a whole-snapshot cache
                   misses even when nothing that matters changed.

  slow-fields      what a cache scoped to the daily-bar-derived fields would
                   catch. This is the number that decides whether 2.2 is
                   worth building.

If the two are far apart, the win is real but only with a field-scoped key.
If both are low, the calls are not redundant and this should be dropped.

RESULT (2026-09-21, 1903 decisions, 221 symbols, 2026-09-01..09-21):
**Refuted, and then unmeasurable.** 1675 of 1903 rows carry an EMPTY
snapshot; they hash alike by absence, which alone reports 48.6% redundancy
that is pure artefact. Excluding them leaves 228 rows at **1.00 calls per
symbol-day and 0% redundancy** — no same-day repeat to cache at all. The
"5.9 decisions per symbol" that motivated this counted a symbol across the
whole period, not within a day.

And even that 0% answers the wrong question: `technical` stores the
analyst's OUTPUT, not the input features. Do not build the cache.

**Correction, 2026-09-23.** The original line here said "nothing persists
the snapshot that produced a decision". That was wrong. Since migration
0012, `reasoning.feature_snapshot` has persisted the input blocks
(technicals, quant, patterns, news, events, liquidity, asset, last_price).
This script hashed the analyst-output columns instead. What really was
missing was `macro`, `options_context` and `fundamentals`. Those are now
persisted too, together with `reasoning.input_hash`, a canonical digest of
the whole snapshot. A re-run should hash `reasoning->>'input_hash'`, not
the output columns.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections import Counter, defaultdict
from typing import Any


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash(obj: Any) -> str:
    return hashlib.sha256(_canon(obj).encode()).hexdigest()[:16]


# Fields that move intraday even when the thesis has not. Excluded from the
# slow-fields hash. Discovered from the data (the script prints the real key
# sets), not assumed — an assumed list is how a cache starts serving stale
# answers for a field somebody added last month.
_FAST_FIELDS = {
    "last_price", "price", "close", "current_price", "mark", "quote",
    "bid", "ask", "mid", "spread", "volume", "as_of", "timestamp", "ts",
}


def _slow(d: dict | None) -> dict:
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in sorted(d.items()) if k.lower() not in _FAST_FIELDS}


async def main() -> int:
    url = os.environ.get("DATABASE_URL", "").strip()
    public = os.environ.get("DATABASE_PUBLIC_URL", "").strip()
    if ".railway.internal" in url and public:
        # `railway run` injects the service's own env, and DATABASE_URL there
        # is the INTERNAL hostname, which resolves only inside Railway's
        # network. From a laptop that is a bare DNS failure with a stack
        # trace that says nothing about the real cause.
        url = public
    if not url:
        print("DATABASE_URL unset. Run under `railway run -s AutonomousTradeAgents`.")
        return 2
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, echo=False)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT user_id, symbol, triggered_at,
                               technical, fundamental, macro,
                               final_action, selected_strategy
                        FROM agent_decisions
                        ORDER BY triggered_at
                        """
                    )
                )
            ).mappings().all()
    finally:
        await engine.dispose()

    if not rows:
        print("no decisions recorded — nothing to measure.")
        return 1

    print(f"decisions                {len(rows)}")
    print(f"symbols                  {len({r['symbol'] for r in rows})}")
    print(f"span                     {rows[0]['triggered_at']:%Y-%m-%d} "
          f"-> {rows[-1]['triggered_at']:%Y-%m-%d}")

    # What the feature dicts actually contain, so _FAST_FIELDS can be checked
    # against reality rather than trusted.
    for col in ("technical", "fundamental", "macro"):
        keys: Counter[str] = Counter()
        for r in rows:
            if isinstance(r[col], dict):
                keys.update(r[col].keys())
        present = sum(1 for r in rows if isinstance(r[col], dict) and r[col])
        print(f"\n{col:<12} populated on {present}/{len(rows)} rows")
        if keys:
            print(f"  keys: {', '.join(sorted(keys))[:300]}")
            hit = sorted(k for k in keys if k.lower() in _FAST_FIELDS)
            print(f"  treated as fast-moving: {', '.join(hit) if hit else '(none)'}")

    # ── The correction that makes this measurable at all ─────────────
    #
    # A row whose snapshot is EMPTY hashes identically to every other empty
    # row. Counting those as "redundant" measures the absence of data, not
    # the presence of repetition — and it is a big majority of the table, so
    # the naive number comes out ~48% and looks like a finding.
    usable = [
        r
        for r in rows
        if any(isinstance(r[c], dict) and r[c] for c in ("technical", "fundamental", "macro"))
    ]
    print(
        f"\nrows with a NON-EMPTY snapshot  {len(usable)}/{len(rows)} "
        f"({100.0 * len(usable) / len(rows):.1f}%)"
    )
    if len(usable) < len(rows):
        print(
            f"  {len(rows) - len(usable)} rows carry an empty snapshot. They are\n"
            "  excluded below: they are identical by ABSENCE, not by content,\n"
            "  and including them reports ~48% redundancy that is pure artefact."
        )
    if not usable:
        print("\nNothing measurable. See the verdict below.")
        usable = []

    def report(label: str, keyfn) -> None:
        if not usable:
            return
        groups: dict[tuple, list[str]] = defaultdict(list)
        for r in usable:
            day = r["triggered_at"].date()
            groups[(r["user_id"], r["symbol"], day)].append(keyfn(r))

        total = sum(len(v) for v in groups.values())
        redundant = sum(len(v) - len(set(v)) for v in groups.values())
        multi = [v for v in groups.values() if len(v) > 1]
        print(f"\n{label}")
        print(f"  symbol-days              {len(groups)}")
        print(f"  calls per symbol-day     {total / len(groups):.2f}")
        print(f"  symbol-days with >1 call {len(multi)}")
        print(f"  redundant calls          {redundant}/{total} "
              f"({100.0 * redundant / total:.1f}%)")
        if multi:
            exact = sum(1 for v in multi if len(set(v)) == 1)
            print(f"  symbol-days fully cached {exact}/{len(multi)} "
                  f"({100.0 * exact / len(multi):.1f}%)")

    report(
        "WHOLE-SNAPSHOT (what a naive hash-everything cache catches)",
        lambda r: _hash([r["technical"], r["fundamental"], r["macro"]]),
    )
    report(
        "SLOW-FIELDS (daily-bar-derived only)",
        lambda r: _hash(
            [_slow(r["technical"]), _slow(r["fundamental"]), _slow(r["macro"])]
        ),
    )

    print(
        "\n" + "=" * 68 + "\nVERDICT\n" + "=" * 68 + "\n"
        "Neither number above answers the question, and the reason is\n"
        "structural rather than a threshold to tune:\n"
        "\n"
        "  `technical` stores {score, thesis, confidence, citations} — the\n"
        "  analyst's OUTPUT. The input feature snapshot that produced it is\n"
        "  persisted NOWHERE. Hashing outputs to find repeated questions is\n"
        "  circular: identical answers is what a cache PRODUCES, not\n"
        "  evidence that the questions were identical.\n"
        "\n"
        "So the cache opportunity cannot be measured retrospectively. The\n"
        "hash column PLAN §B proposed is required, not optional — and it can\n"
        "only collect data going forward, which is nothing while the circuit\n"
        "breaker stays latched (halted since 2026-09-08, unacknowledged).\n"
        "\n"
        "Do not build the cache on the strength of the 48.6% this script\n"
        "reported before the empty-snapshot rows were excluded.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
