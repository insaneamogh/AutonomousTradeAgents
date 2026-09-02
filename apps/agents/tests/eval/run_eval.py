"""Scorecard runner — prints what the deterministic funnel did.

    .venv/bin/python -m tests.eval.run_eval          # from apps/agents/
    .venv/bin/python -m tests.eval.run_eval --json

The pytest file asserts; this one SHOWS. It exists because "does the
maths fire?" is answered better by a funnel table you can read than by a
green test bar, and because the numbers it prints are the ones to quote
in the write-up.
"""

from __future__ import annotations

import argparse
import json
import sys

from tests.eval.funnel import run_all
from tests.eval.scenarios import golden_scenarios

from engine.risk.types import RiskCaps


def _bar(n: int, total: int, width: int = 28) -> str:
    filled = int(round(width * n / total)) if total else 0
    return "█" * filled + "·" * (width - filled)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--allow-shorts", action="store_true",
        help="enable short/put directions (off by default, matching production)",
    )
    args = parser.parse_args(argv)

    caps = RiskCaps.aggressive_paper()
    report = run_all(golden_scenarios(), caps=caps, allow_shorts=args.allow_shorts)

    if args.json:
        print(json.dumps({
            "total": report.total,
            "reached_llm": report.reached_llm,
            "refused": report.refused,
            "llm_fraction": round(report.llm_fraction, 4),
            "by_reason": report.by_reason,
            "by_archetype": report.by_archetype,
        }, indent=2))
        return 0

    print()
    print("  DETERMINISTIC FUNNEL — 100-case golden dataset")
    print("  " + "─" * 62)
    print(f"  profile: aggressive_paper   shorts: {'on' if args.allow_shorts else 'off'}")
    print(f"  book width: {caps.options_max_total_premium_pct / caps.options_max_premium_pct:.0f} "
          f"concurrent positions   liquidity cap: "
          f"{caps.options_max_pct_of_open_interest:g}% of open interest")
    print()
    print(f"  scanned          {report.total:3d}  {_bar(report.total, report.total)}")
    print(f"  refused free     {report.refused:3d}  {_bar(report.refused, report.total)}")
    print(f"  reach an LLM     {report.reached_llm:3d}  {_bar(report.reached_llm, report.total)}"
          f"   ({report.llm_fraction:.0%})")
    print()
    print("  REFUSALS BY NAMED REASON (zero LLM calls spent)")
    print("  " + "─" * 62)
    for reason, count in sorted(report.by_reason.items(), key=lambda kv: -kv[1]):
        print(f"    {reason:38s} {count:3d}")
    if not report.by_reason:
        print("    (none)")
    print()
    print("  BY ARCHETYPE")
    print("  " + "─" * 62)
    print(f"    {'archetype':24s} {'->LLM':>6s} {'refused':>8s}")
    for archetype, counts in sorted(report.by_archetype.items()):
        print(f"    {archetype:24s} {counts['llm']:6d} {counts['refused']:8d}")
    print()

    sized = [r for r in report.results if r.qty is not None]
    if sized:
        trimmed = [r for r in sized if r.sizing_note and "liquidity cap" in r.sizing_note]
        print("  SIZING")
        print("  " + "─" * 62)
        print(f"    positions sized              {len(sized):3d}")
        print(f"    trimmed by the liquidity cap {len(trimmed):3d}")
        print(f"    contracts, min..max          "
              f"{min(r.qty for r in sized)}..{max(r.qty for r in sized)}")
        print()

    print("  WHAT THIS DOES NOT PROVE")
    print("  " + "─" * 62)
    print("    These are labelled ARCHETYPES, not a historical backtest —")
    print("    no real bars, no measured P&L. This shows the funnel's")
    print("    LOGIC narrows correctly and every refusal is named. It says")
    print("    nothing about whether the strategy makes money.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
