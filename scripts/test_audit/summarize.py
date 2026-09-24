"""Summarize mutation_prune.py reports: what each test file can lose.

  subsumed   kills >= 1 mutant, and every mutant it kills is also killed by
             a test the greedy pass KEEPS in the same file. Safe to delete:
             no injected bug goes uncaught.
  kill_none  kills nothing. NOT auto-deletable: the mutation operators do
             not touch strings, list membership or dict keys, so a test of
             a caption or of a rule's place in a sequence lands here too.
             Review by hand.

Function-level: a parametrized test is subsumed only when every case is.

  python scripts/test_audit/summarize.py <report.json>... [--json out.json]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict


def classify(report: dict) -> dict[str, list[str]]:
    kills = {t: set(v) for t, v in report["kills"].items()}
    keep = set(report["keep"])
    kept_kills = set().union(*(kills[t] for t in keep)) if keep else set()
    by_fn: dict[str, list[str]] = defaultdict(list)
    for t in kills:
        by_fn[t.split("[", 1)[0]].append(t)
    out = {"keep": [], "subsumed": [], "kill_none": []}
    for fn, cases in sorted(by_fn.items()):
        if any(c in keep for c in cases):
            out["keep"].append(fn)
        elif all(not kills[c] for c in cases):
            out["kill_none"].append(fn)
        elif all(kills[c] <= kept_kills for c in cases):
            out["subsumed"].append(fn)
        else:
            out["keep"].append(fn)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reports", nargs="+")
    ap.add_argument("--json")
    args = ap.parse_args()
    total = {"keep": 0, "subsumed": 0, "kill_none": 0}
    everything = {}
    for path in args.reports:
        with open(path) as fh:
            r = json.load(fh)
        c = classify(r)
        everything[r["tests"][0]] = c
        for k in total:
            total[k] += len(c[k])
        n = sum(len(v) for v in c.values())
        print(f"{r['tests'][0]:60s} fns={n:3d} keep={len(c['keep']):3d} "
              f"subsumed={len(c['subsumed']):3d} kill_none={len(c['kill_none']):3d} "
              f"mutants={r['killed']}/{r['mutants']}")
    print(f"TOTAL functions keep={total['keep']} subsumed={total['subsumed']} "
          f"kill_none={total['kill_none']}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(everything, fh, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
