"""Delete the test functions summarize.py classified as SUBSUMED.

Only `subsumed` is ever deleted: a test that kills at least one mutant,
all of which a KEPT test in the same file also kills. `kill_none` tests
are listed, never touched.

Each deleted function goes with its decorators (parametrize tables
included). Helpers and imports left unused are reported by ruff; run
`ruff check --fix` for imports. After pruning, re-run mutation_prune.py
on the same file and compare `killed` with the pre-prune report: equal
is the proof nothing was lost.

  python scripts/test_audit/prune.py classified.json [--dry-run]
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path


def prune_file(path: Path, names: set[str], *, dry_run: bool) -> list[str]:
    source = path.read_text()
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    spans: list[tuple[int, int, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            spans.append((start, node.end_lineno or node.lineno, node.name))
    for start, end, _ in sorted(spans, reverse=True):
        # Also take the blank lines that separated it from the next block.
        while end < len(lines) and not lines[end].strip():
            end += 1
        del lines[start - 1:end]
    if not dry_run and spans:
        path.write_text("".join(lines).rstrip("\n") + "\n")
    return [name for _, _, name in spans]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("classified")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    with open(args.classified) as fh:
        classified = json.load(fh)
    total = 0
    for test_file, groups in classified.items():
        names = {t.split("::")[-1] for t in groups["subsumed"] if t.count("::") == 1}
        skipped = [t for t in groups["subsumed"] if t.count("::") != 1]
        removed = prune_file(Path(test_file), names, dry_run=args.dry_run)
        total += len(removed)
        print(f"{test_file}: removed {len(removed)} of {len(names)}"
              + (f" (class methods skipped: {len(skipped)})" if skipped else "")
              + (f"; kill_none left for review: {groups['kill_none']}" if groups["kill_none"] else ""))
    print(f"total removed: {total}{' (dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
