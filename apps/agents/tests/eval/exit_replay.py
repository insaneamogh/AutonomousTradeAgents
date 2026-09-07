"""Replay the option exit ladder against real recorded premium paths.

The exit ladder was never measured. Its numbers imply a **62% breakeven win
rate** (a -40% stop against a +24.5% minimum trailed win is 1.63:1 against),
while the council that feeds it has never produced a conviction above 0.62
and its modal filled trade sat at 0.42. That mismatch is the single biggest
reason the book loses money, and until now there was no way to choose better
numbers except arithmetic.

There is data. ``positions_snapshot`` recorded the whole book every ~30
seconds from 2026-09-01, and each row's ``open_positions`` carries
``avg_entry_price`` and ``market_value`` per contract. Dividing gives a real
premium path for **every option position this system has ever held** — 21
contracts, 8,080 distinct price points.

Two rules make this a measurement rather than a story:

**1. It calls the production ladder, not a copy of it.**
``engine.options.exits.option_ratchet_signal`` is a pure function — no I/O,
no clock, no LLM — so the replay drives the exact state machine the position
manager drives in production. A reimplementation would only ever measure the
reimplementation.

**2. Right-censoring is explicit and never silently counted as an exit.**
A recorded path ends when the position left the book, and most of them left
under the CURRENT ladder. So:

  - a TIGHTER stop, or a LOWER take-profit, fires inside the observed window
    and is fully simulable;
  - a WIDER stop, or a HIGHER arm the position never reached, would need
    prices from after the path ends, and those do not exist.

Treating the last observed price as an exit would silently mark every
would-have-recovered trade as a win at whatever the mark happened to be —
which is exactly the bias that would make a wider stop look free. Censored
paths are reported as ``censored`` and excluded from expectancy, and the
count is printed next to every result so a number computed on four survivors
cannot be mistaken for one computed on twenty.

Offline by design: reads ``fixtures/option_paths.json`` (dumped from
Postgres, committed) so this runs with no keys and no network, like the rest
of ``tests/eval``.

    python -m tests.eval.exit_replay              # current ladder + a sweep
    python -m tests.eval.exit_replay --verify     # reproduce the 3 real closes
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from engine.options.exits import option_ratchet_signal

_FIXTURE = Path(__file__).parent / "fixtures" / "option_paths.json"


@dataclass(frozen=True)
class Ladder:
    stop_loss_pct: float
    trail_arm_pct: float
    trail_giveback_pct: float
    take_profit_pct: float
    scale_out_at_pct: float = 0.0
    scale_out_frac: float = 0.0

    def __str__(self) -> str:
        base = (
            f"stop -{self.stop_loss_pct:g} arm +{self.trail_arm_pct:g} "
            f"give {self.trail_giveback_pct:g}% tp +{self.take_profit_pct:g}"
        )
        if self.scale_out_at_pct > 0 and self.scale_out_frac > 0:
            base += f" | bank {self.scale_out_frac * 100:g}% at +{self.scale_out_at_pct:g}"
        return base

    @property
    def min_trailed_win_pct(self) -> float:
        """The smallest win the trail can produce: arm exactly, then give
        back its fraction of that peak."""
        return self.trail_arm_pct * (1.0 - self.trail_giveback_pct / 100.0)

    @property
    def breakeven_win_rate_pct(self) -> float:
        """Win rate needed for zero expectancy at the ladder's own extremes.
        A property of the GEOMETRY alone — no path data involved."""
        loss, win = abs(self.stop_loss_pct), self.min_trailed_win_pct
        return 100.0 * loss / (loss + win) if (loss + win) else float("nan")


CURRENT = Ladder(stop_loss_pct=40.0, trail_arm_pct=35.0,
                 trail_giveback_pct=30.0, take_profit_pct=150.0)
"""The ladder production actually runs, established by reading the call
sites rather than the field names — and the two are not the same thing.

`position_manager.py:352` feeds `option_ratchet_signal` from `RiskCaps`:
`options_stop_loss_pct` (40), `options_trail_arm_pct` (35),
`options_trail_giveback_pct` (30) and **`options_hard_take_profit_pct`
(150)**. The similarly-named `options_take_profit_pct` (60) is NOT this
number: it belongs to the `else:` branch that runs only when
`options_ratchet_enabled` is False, so it is dead in production today.

Two further traps found by running the acceptance test below, both of
which would have made this replay quietly wrong:

  - **Per-position stop/take-profit are recorded but not honoured.** The
    agent picks them within `_STOP_LOSS_BAND` (25-50) and
    `_TAKE_PROFIT_BAND` (40-300), and they are persisted to
    `agent_decisions.reasoning.option_exit` — NVDA260914C00225000 carries
    `stop 35 / tp 120`. Neither exit mechanism reads them back. The
    ratchet and the resting broker stop both take `caps.options_stop_
    loss_pct`. So the agent's stated exit plan is audit metadata, not
    behaviour.
  - Seeding this constant from those recorded values (tp=60, or tp=120)
    made NVDA replay as `option_take_profit` at +820 when it really
    exited `option_trail_stop` at +590. The acceptance test caught it."""


@dataclass(frozen=True)
class ContractPath:
    occ: str
    entry: float
    qty: int
    multiplier: int
    samples: tuple[tuple[int, float], ...]

    def pl_pct_at(self, price: float) -> float:
        return (price - self.entry) / self.entry * 100.0


@dataclass(frozen=True)
class ReplayResult:
    occ: str
    exited: bool
    """False means CENSORED: the ladder never fired inside the recorded
    window, so the outcome is unknown — not a win, not a loss."""
    reason: str | None
    pl_pct: float | None
    pnl_usd: float | None
    peak_pl_pct: float
    n_samples: int


def load_paths(fixture: Path = _FIXTURE) -> list[ContractPath]:
    raw = json.loads(fixture.read_text())
    return [
        ContractPath(
            occ=r["occ"], entry=float(r["entry"]), qty=int(r["qty"]),
            multiplier=int(r["multiplier"]),
            samples=tuple((int(t), float(p)) for t, p in r["samples"]),
        )
        for r in raw["paths"]
        if float(r["entry"]) > 0 and r["samples"]
    ]


def replay(path: ContractPath, ladder: Ladder) -> ReplayResult:
    """Drive the production ratchet over one recorded path, one sample at a
    time, exactly as the position manager drives it tick by tick.

    A SCALE_OUT banks its fraction and the remainder keeps running, so the
    reported ``pl_pct`` is the BLENDED outcome — the banked leg at its
    target plus the runner at wherever it finally exits. A path that scales
    out and is then censored is still censored: the runner's fate is
    unknown, and pretending otherwise is the bias this module exists to
    avoid.
    """
    peak: float | None = None
    banked_pl = 0.0
    banked_frac = 0.0
    for _ts, price in path.samples:
        pl = path.pl_pct_at(price)
        out = option_ratchet_signal(
            unrealized_pl_pct=pl,
            peak_pl_pct=peak,
            arm_pct=ladder.trail_arm_pct,
            giveback_frac=ladder.trail_giveback_pct / 100.0,
            hard_take_profit_pct=ladder.take_profit_pct,
            stop_loss_pct=ladder.stop_loss_pct,
            scale_out_at_pct=ladder.scale_out_at_pct,
            scale_out_frac=ladder.scale_out_frac,
            already_scaled_out=banked_frac > 0.0,
        )
        if out.action == "SCALE_OUT":
            banked_frac = out.scale_out_frac or 0.0
            banked_pl = pl * banked_frac
            peak = out.peak_pl_pct
            continue
        if out.action == "CLOSE":
            blended = banked_pl + pl * (1.0 - banked_frac)
            return ReplayResult(
                occ=path.occ, exited=True, reason=out.reason, pl_pct=blended,
                pnl_usd=blended / 100.0 * path.entry * path.qty * path.multiplier,
                peak_pl_pct=out.peak_pl_pct or 0.0, n_samples=len(path.samples),
            )
        peak = out.peak_pl_pct
    return ReplayResult(
        occ=path.occ, exited=False, reason=None, pl_pct=None, pnl_usd=None,
        peak_pl_pct=peak or 0.0, n_samples=len(path.samples),
    )


@dataclass(frozen=True)
class Summary:
    ladder: Ladder
    exits: int
    censored: int
    wins: int
    losses: int
    total_usd: float
    avg_win_pct: float | None
    avg_loss_pct: float | None

    @property
    def win_rate_pct(self) -> float | None:
        return 100.0 * self.wins / self.exits if self.exits else None

    @property
    def expectancy_pct(self) -> float | None:
        """Average % outcome per COMPLETED trade. None when nothing completed."""
        if not self.exits:
            return None
        w = (self.avg_win_pct or 0.0) * self.wins
        loss = (self.avg_loss_pct or 0.0) * self.losses
        return (w + loss) / self.exits


def summarise(results: list[ReplayResult], ladder: Ladder) -> Summary:
    done = [r for r in results if r.exited and r.pl_pct is not None]
    wins = [r.pl_pct for r in done if (r.pl_pct or 0) > 0]
    losses = [r.pl_pct for r in done if (r.pl_pct or 0) <= 0]
    return Summary(
        ladder=ladder,
        exits=len(done),
        censored=len(results) - len(done),
        wins=len(wins),
        losses=len(losses),
        total_usd=sum(r.pnl_usd or 0.0 for r in done),
        avg_win_pct=(sum(wins) / len(wins)) if wins else None,
        avg_loss_pct=(sum(losses) / len(losses)) if losses else None,
    )


# ── ground truth ──────────────────────────────────────────────────────
#
# The three positions this account actually opened AND closed under the
# current ladder. Reproducing them is the acceptance test for everything
# above: a replay that cannot recover what really happened has no standing
# to recommend what should happen instead.

REAL_CLOSES: dict[str, tuple[str, float]] = {
    "AAPL260918C00340000": ("option_stop_loss", -536.0),
    "XLE261016C00067000": ("option_stop_loss", -610.0),
    "NVDA260914C00225000": ("option_trail_stop", 590.0),
}
_TOLERANCE = 0.05
"""Fractional P&L tolerance. Not zero: the replay fires on the nearest
recorded 30s sample, the broker filled at its own tick, and AAPL's exit
came from a resting stop-limit whose fill price is its own event. Matching
the REASON exactly and the dollars to 5% is the honest bar."""


def verify(paths: list[ContractPath] | None = None) -> bool:
    rows = {p.occ: p for p in (paths if paths is not None else load_paths())}
    print("Acceptance — reproduce the real closes under the production ladder")
    print(f"  ladder: {CURRENT}\n")
    print(f"  {'contract':22} {'expected':>26} {'replay':>26}")
    ok = True
    for occ, (reason, pnl) in REAL_CLOSES.items():
        path = rows.get(occ)
        if path is None:
            print(f"  {occ:22} {'MISSING FROM FIXTURE':>54}")
            ok = False
            continue
        r = replay(path, CURRENT)
        got = f"{r.reason} {r.pnl_usd:+.0f}" if r.exited else "CENSORED (no exit)"
        good = (r.exited and r.reason == reason
                and abs((r.pnl_usd or 0.0) - pnl) <= abs(pnl) * _TOLERANCE)
        ok &= good
        want = f"{reason} {pnl:+.0f}"
        print(f"  {occ:22} {want:>26} {got:>26}  {'OK' if good else 'MISMATCH'}")
    print(f"\n  ACCEPTANCE: {'PASS' if ok else 'FAIL'}")
    return ok


def mark_to_last(results: list[ReplayResult], paths: list[ContractPath],
                 ladder: Ladder) -> float | None:
    """Expectancy with every censored path marked at its LAST observed
    price instead of dropped.

    This exists because the censored set is **not random**. It is
    disproportionately the positions that are down but have not yet reached
    the stop — so excluding them systematically flatters a wide stop, which
    is precisely the comparison the sweep is being used to make.

    Neither view is the truth. Completed-only is biased toward the wide
    stop (it hides open losers); mark-to-last is biased against it (it
    realises paper losses that might still recover). They BOUND the answer:
    when both are negative by a wide margin, the conclusion is robust to
    the bias and does not depend on which one you believe.
    """
    by_occ = {p.occ: p for p in paths}
    vals: list[float] = []
    for r in results:
        if r.exited and r.pl_pct is not None:
            vals.append(r.pl_pct)
            continue
        path = by_occ.get(r.occ)
        if path is None or not path.samples:
            continue
        vals.append(path.pl_pct_at(path.samples[-1][1]))
    # NOTE: a censored path that scaled out is marked at its LAST price for
    # the whole position, ignoring the banked leg. That is deliberately the
    # pessimistic reading — it never lets scale-out flatter itself on a
    # position whose runner has not resolved.
    return (sum(vals) / len(vals)) if vals else None


def _fmt(v: float | None, suffix: str = "") -> str:
    return "  —  " if v is None else f"{v:+.1f}{suffix}"


def report(paths: list[ContractPath], ladders: list[Ladder]) -> None:
    print(f"\n{len(paths)} recorded premium paths "
          f"({sum(len(p.samples) for p in paths)} price points)\n")
    print(f"  {'ladder':44} {'BE%':>5} {'exits':>6} {'cens':>5} "
          f"{'win%':>6} {'exp/done':>9} {'exp/all':>9}")
    print("  " + "-" * 96)
    for lad in ladders:
        results = [replay(p, lad) for p in paths]
        s = summarise(results, lad)
        tag = " <- live" if lad == CURRENT else ""
        print(f"  {lad!s:44} {lad.breakeven_win_rate_pct:>5.1f} "
              f"{s.exits:>6} {s.censored:>5} "
              f"{(f'{s.win_rate_pct:.0f}' if s.win_rate_pct is not None else '—'):>6} "
              f"{_fmt(s.expectancy_pct, '%'):>9} "
              f"{_fmt(mark_to_last(results, paths, lad), '%'):>9}{tag}")
    print("\n  BE%       breakeven win rate implied by the ladder's geometry alone")
    print("  exp/done  average % outcome per COMPLETED trade (censored dropped)")
    print("  exp/all   same, but censored paths marked at their last observed")
    print("            price. The two BOUND the answer — see mark_to_last().")
    print("  cens      CENSORED: the ladder never fired inside the recorded")
    print("            window, so the outcome is UNKNOWN — excluded from every")
    print("            other column. A wider stop censors more, which is exactly")
    print("            why a low loss count here is not evidence a stop is safe.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify", action="store_true",
                    help="only run the acceptance test against the real closes")
    args = ap.parse_args()

    paths = load_paths()
    ok = verify(paths)
    if args.verify:
        return 0 if ok else 1
    if not ok:
        print("\nRefusing to sweep: the replay cannot reproduce reality, so any")
        print("recommendation it makes is unfounded. Fix the acceptance first.")
        return 1

    grid = [CURRENT]
    for stop in (25.0, 30.0, 35.0, 40.0):
        for arm in (35.0, 50.0):
            for give in (25.0, 30.0):
                lad = Ladder(stop, arm, give, 150.0)
                if lad != CURRENT:
                    grid.append(lad)
    report(paths, grid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
