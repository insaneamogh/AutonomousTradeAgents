"""Which tests actually catch bugs? Mutation analysis, per module.

Line coverage says 71% of this repo's test functions touch no source line
another test does not also touch. That overstates redundancy: a boundary
test runs the same lines as its neighbour with a different value, and
that is exactly the kind of test that caught real bugs here. The honest
question is narrower: if this test were deleted, would any injected bug
go uncaught? This answers it.

For one source module and the test files that exercise it:
  1. A throwaway git worktree at HEAD, so mutating source never touches
     the working tree. PYTHONPATH puts the worktree's packages ahead of
     the venv's editable installs.
  2. Every mutation site in the module: comparison flips (< <=, == !=,
     in / not in), and/or swaps, dropped `not`, negated if/while tests,
     + - and * / swaps, numeric constants nudged, string constants altered
     (veto names, reasons, dict keys), `return X` -> `return None`, and one
     element dropped from a list/tuple/set literal (a rule removed from a
     sequence). Docstrings and f-string fragments are never mutated.
  3. One pytest run per mutant over the given test files; each failing
     test id is a kill.
  4. A test is REDUNDANT for this module when every mutant it kills is
     also killed by some test that is kept. The kept set is a greedy
     minimum that kills every killable mutant.

Usage (from the repo root). Record per-test coverage once, then let the
tool mutate every module the test file reaches:
  coverage run --rcfile=<rc with dynamic_context = test_function> -m pytest ...
  .venv/bin/python scripts/test_audit/mutation_prune.py \\
      --coverage-data .coverage \\
      --tests packages/engine/tests/test_options_selection.py \\
      --out /tmp/selection.json
(--module adds a module explicitly; repeatable.)

Why per test FILE and across every module it reaches: a test redundant
for one module may be the only test of another (a rule's self-gate test
adds nothing to breakeven.py's math). Within one file's run, a test is
only called redundant if kept tests of the SAME file kill every mutant it
kills, in every module, so deleting it cannot lose a kill anywhere.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PKG_ROOTS = ("packages/engine", "packages/broker", "apps/agents", "apps/api")
_FAILED = re.compile(r"^FAILED (\S+?)(?: - |$)")
_ERROR = re.compile(r"^ERROR (\S+?)(?: - |$)")


# ── mutation sites ──────────────────────────────────────────────────

_CMP_SWAP = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
}
_BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}


class _Sites(ast.NodeVisitor):
    """Enumerates sites in a fixed traversal order. ``_Mutate`` walks the
    same order, so site k means the same thing in both."""

    def __init__(self) -> None:
        self.sites: list[tuple[str, int]] = []

    def generic_visit(self, node: ast.AST) -> None:
        if _skip_subtree(node):
            return
        for kind in _site_kinds(node):
            self.sites.append((kind, getattr(node, "lineno", 0)))
        super().generic_visit(node)


def _skip_subtree(node: ast.AST) -> bool:
    """Docstrings (and any bare string statement) and f-strings: mutating
    them changes nothing a test should pin, and only costs runs."""
    if isinstance(node, ast.JoinedStr):
        return True
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


_MAX_DROP_ELTS = 40


def _site_kinds(node: ast.AST) -> list[str]:
    kinds: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        kinds.append("str")
    elif isinstance(node, ast.Return) and node.value is not None and not (
        isinstance(node.value, ast.Constant) and node.value.value is None
    ):
        kinds.append("return_none")
    elif (
        isinstance(node, (ast.List, ast.Tuple, ast.Set))
        and 2 <= len(node.elts) <= _MAX_DROP_ELTS
        and isinstance(getattr(node, "ctx", ast.Load()), ast.Load)
        and not any(isinstance(e, ast.Starred) for e in node.elts)
    ):
        kinds += [f"drop{i}" for i in range(len(node.elts))]
    if isinstance(node, ast.Compare):
        kinds += [f"cmp{i}" for i, op in enumerate(node.ops) if type(op) in _CMP_SWAP]
    elif isinstance(node, ast.BoolOp):
        kinds.append("boolop")
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        kinds.append("drop_not")
    elif isinstance(node, ast.BinOp) and type(node.op) in _BIN_SWAP:
        kinds.append("binop")
    elif (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        kinds.append("const")
    if isinstance(node, (ast.If, ast.While, ast.IfExp)):
        kinds.append("negate_test")
    return kinds


class _Mutate(ast.NodeTransformer):
    def __init__(self, target: int) -> None:
        self.target = target
        self.i = -1
        self.done: str | None = None

    def generic_visit(self, node: ast.AST) -> ast.AST:
        if _skip_subtree(node):
            return node
        for kind in _site_kinds(node):
            self.i += 1
            if self.i == self.target:
                node = self._apply(node, kind)
                self.done = kind
        return super().generic_visit(node)

    @staticmethod
    def _apply(node: ast.AST, kind: str) -> ast.AST:
        if kind == "str":
            node.value = node.value + "_MUT"
        elif kind == "return_none":
            node.value = ast.Constant(value=None)
        elif kind.startswith("drop"):
            node.elts.pop(int(kind[4:]))
        elif kind.startswith("cmp"):
            idx = int(kind[3:])
            node.ops[idx] = _CMP_SWAP[type(node.ops[idx])]()
        elif kind == "boolop":
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        elif kind == "drop_not":
            return node.operand
        elif kind == "binop":
            node.op = _BIN_SWAP[type(node.op)]()
        elif kind == "const":
            v = node.value
            node.value = v + 1 if isinstance(v, int) else (v * 1.5 if v else 0.5)
        elif kind == "negate_test":
            node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
        return node


def enumerate_sites(source: str) -> list[tuple[str, int]]:
    v = _Sites()
    v.visit(ast.parse(source))
    return v.sites


def mutant_source(source: str, k: int) -> str:
    tree = _Mutate(k).visit(ast.parse(source))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


# ── running ─────────────────────────────────────────────────────────


def _run(workdir: Path, tests: list[str], timeout: float) -> tuple[set[str], bool]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(workdir / r) for r in PKG_ROOTS)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [sys.executable, "-m", "pytest", *tests, "-q", "-rfE", "--tb=no",
           "-p", "no:cacheprovider", "-p", "no:randomly"]
    try:
        out = subprocess.run(cmd, cwd=workdir, env=env, capture_output=True,
                             text=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return set(), True
    failed = set()
    for line in out.splitlines():
        m = _FAILED.match(line) or _ERROR.match(line)
        if m:
            failed.add(m.group(1))
    return failed, False


def _collect(workdir: Path, tests: list[str]) -> list[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(workdir / r) for r in PKG_ROOTS)
    out = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q", "--collect-only", "-p", "no:cacheprovider"],
        cwd=workdir, env=env, capture_output=True, text=True,
    ).stdout
    return [ln.strip() for ln in out.splitlines() if "::" in ln]


def covered_modules(coverage_file: str, test_files: list[str]) -> list[str]:
    """Source modules (repo-relative) with at least one line covered by a
    test in ``test_files``. Contexts are ``<test module>.<function>``."""
    from coverage import CoverageData

    stems = {Path(t).stem for t in test_files}
    data = CoverageData(basename=coverage_file)
    data.read()
    out: list[str] = []
    for f in sorted(data.measured_files()):
        rel = os.path.relpath(f, REPO)
        if rel.startswith("..") or "/tests/" in rel:
            continue
        ctx = data.contexts_by_lineno(f) or {}
        if any(c.split(".")[0] in stems for cs in ctx.values() for c in cs if c):
            out.append(rel)
    return out


def greedy_keep(kills: dict[str, set[int]]) -> list[str]:
    remaining = set().union(*kills.values()) if kills else set()
    keep: list[str] = []
    while remaining:
        best = max(kills, key=lambda t: (len(kills[t] & remaining), t))
        if not kills[best] & remaining:
            break
        keep.append(best)
        remaining -= kills[best]
    return keep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", action="append", default=[],
                    help="source module to mutate; repeatable")
    ap.add_argument("--coverage-data", default=None,
                    help="a coverage file recorded with dynamic_context=test_function; "
                         "adds every module the given test files cover")
    ap.add_argument("--tests", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    modules = list(args.module)
    if args.coverage_data:
        modules += [m for m in covered_modules(args.coverage_data, args.tests) if m not in modules]
    if not modules:
        print("no modules to mutate", file=sys.stderr)
        return 2

    wt = Path(tempfile.mkdtemp(prefix="mutprune-"))
    subprocess.run(["git", "worktree", "add", "--detach", str(wt), "HEAD"],
                   cwd=REPO, check=True, capture_output=True)
    try:
        ids = _collect(wt, args.tests)
        baseline, _ = _run(wt, args.tests, args.timeout)
        if baseline:
            print(f"baseline has failures, aborting: {sorted(baseline)[:5]}", file=sys.stderr)
            return 2
        kills: dict[str, set[str]] = {t: set() for t in ids}
        survived: list[dict] = []
        per_module: dict[str, dict[str, int]] = {}
        timeouts = 0
        t0 = time.time()
        for module in modules:
            target = wt / module
            original = target.read_text()
            sites = enumerate_sites(original)
            print(f"{module}: {len(sites)} mutants, {len(ids)} tests", flush=True)
            n_killed = 0
            for k, (kind, line) in enumerate(sites):
                try:
                    target.write_text(mutant_source(original, k))
                except Exception:
                    continue
                failed, timed_out = _run(wt, args.tests, args.timeout)
                timeouts += timed_out
                mid = f"{module}:{k}"
                if failed or timed_out:
                    n_killed += 1
                else:
                    survived.append({"module": module, "site": k, "kind": kind, "line": line})
                for t in failed:
                    kills.setdefault(t, set()).add(mid)
                if k % 50 == 0:
                    print(f"  {k}/{len(sites)} ({time.time() - t0:.0f}s)", flush=True)
            target.write_text(original)
            per_module[module] = {"mutants": len(sites), "killed": n_killed}

        killed = set().union(*kills.values()) if kills else set()
        keep = greedy_keep(kills)
        redundant = sorted(t for t in kills if t not in keep)
        report = {
            "modules": per_module,
            "tests": args.tests,
            "mutants": sum(m["mutants"] for m in per_module.values()),
            "killed": len(killed),
            "timeouts": timeouts,
            "survived": survived,
            "keep": keep,
            "redundant": redundant,
            "kills": {t: sorted(v) for t, v in kills.items()},
        }
        Path(args.out).write_text(json.dumps(report, indent=1))
        print(f"killed {len(killed)}/{report['mutants']} mutants; keep {len(keep)} of "
              f"{len(kills)} tests; {len(redundant)} redundant across {len(per_module)} module(s)")
        return 0
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                       cwd=REPO, capture_output=True)
        shutil.rmtree(wt, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
