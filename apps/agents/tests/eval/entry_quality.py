"""Why the book loses money — measured on the recorded premium paths.

`exit_replay` established what it is NOT: no exit ladder in the sweep turns
this book profitable (every configuration loses 12.7%-36% per trade), so
the exits are not the cause. This module measures the entries instead, and
finds two separate problems.

**1. Positions rarely go green at all.**

Maximum favourable excursion (MFE) is the best unrealised P&L a position
ever showed; maximum adverse excursion (MAE) is the worst. Across all 21
recorded positions:

    median MFE   +2.9%
    median MAE  -20.6%
    8 of 21 never traded above their entry price, not once, ever

That ~7:1 asymmetry is why no exit rule helps. An exit can only capture a
gain that existed; on this book there was usually nothing to capture.

**2. Being directionally right is not enough.**

Comparing each position's underlying move over the holding period against
its thesis (a call wants up, a put wants down):

    thesis right   6
    thesis wrong  11
    underlying flat 1

6/17 = 35%. **This is NOT evidence of a negative edge** — with n=17,
P(<=6 correct | true 50/50) = 0.17 and the 95% Wilson interval is 17%-59%.
The honest claim is that there is no DETECTABLE directional edge, and a
system with no edge that pays theta and spread loses by construction.

The sharper finding is inside the six correct calls:

    underlying moved >= 2%   2 of 2 won   (+43.4%, +22.3%)
    underlying moved  < 2%   1 of 4 won   (the winner made +2.1%)

    GILD155 call  underlying +0.21%  ->  option -23.5%
    GILD150 call  underlying +0.64%  ->  option -13.8%
    AMD     put   underlying -1.45%  ->  option -10.1%

A correct thesis with a small underlying move still loses double digits,
because theta and the spread outrun the delta gain.

**The conclusion: we use signals that predict DIRECTION to trade an
instrument that requires MAGNITUDE AND SPEED.** All five strategies
(`sma_crossover`, `rsi_mean_reversion`, `momentum`, `breakout`,
`vol_regime_switch`) score "which way?". Not one estimates "how far, how
fast?", and nothing downstream compares an expected move against the move
the chosen contract needs just to break even.

That is a missing gate, not a mis-tuned one. See `docs/PLAN_ENTRY_EDGE.md`.

    python -m tests.eval.entry_quality
"""

from __future__ import annotations

import statistics as stats
from dataclasses import dataclass

from tests.eval.exit_replay import ContractPath, load_paths


@dataclass(frozen=True)
class Excursion:
    occ: str
    entry: float
    mfe_pct: float
    """Best unrealised P&L the position ever showed."""
    mae_pct: float
    last_pct: float

    @property
    def ever_green(self) -> bool:
        return self.mfe_pct > 0.0


def excursions(paths: list[ContractPath] | None = None) -> list[Excursion]:
    out: list[Excursion] = []
    for p in paths if paths is not None else load_paths():
        pls = [p.pl_pct_at(px) for _ts, px in p.samples]
        out.append(Excursion(occ=p.occ, entry=p.entry, mfe_pct=max(pls),
                             mae_pct=min(pls), last_pct=pls[-1]))
    return sorted(out, key=lambda e: e.occ)


def report() -> None:
    rows = excursions()
    print(f"{'contract':22} {'entry':>7} {'MFE%':>8} {'MAE%':>8} {'last%':>8}  green?")
    for e in rows:
        print(f"{e.occ:22} {e.entry:>7.2f} {e.mfe_pct:>+8.1f} {e.mae_pct:>+8.1f} "
              f"{e.last_pct:>+8.1f}  {'yes' if e.ever_green else 'NEVER'}")
    never = sum(1 for e in rows if not e.ever_green)
    print(f"\nmedian MFE {stats.median(e.mfe_pct for e in rows):+.1f}%   "
          f"median MAE {stats.median(e.mae_pct for e in rows):+.1f}%")
    print(f"{never}/{len(rows)} positions never traded above entry, not once")
    print("\nAn exit can only capture a gain that existed. That asymmetry is "
          "why\nno ladder in exit_replay's sweep turns this book profitable.")


if __name__ == "__main__":
    report()
