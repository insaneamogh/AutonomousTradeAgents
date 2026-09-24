# Plan: fewer tests, more that matter — scenarios plus mutation-proven units

> **Status: IN PROGRESS, started 2026-09-25** at the operator's request
> ("check OpenClaw ... get rid of so many test cases ... e2e isolation
> tests instead"). Tranche 1 is done (below). Numbers are measured, not
> estimated; re-measure before quoting them.

## 1. What OpenClaw actually does (read 2026-09-25)

OpenClaw does not have fewer tests; it has **layers**, each with one job:

| Suite | Job | Isolation |
|---|---|---|
| unit/integration (`pnpm test`) | pure logic, in-process integration, deterministic regressions | sharded, no shared state |
| e2e (`test:e2e:*`) | real gateway processes, multi-instance, WS/HTTP | temp state dirs, own ports, one worker |
| live (`test:live`) | real providers and models | temp HOME, copied auth |
| QA scenarios (`qa/*.md`) | whole user flows on a synthetic channel | fresh environment per run |

The lesson for this repo: the bugs that reached production here were
**wiring bugs between layers that each had green unit tests** (a stop row
never polled, a fill labelled a manual close, an option "closed
externally" on its first tick, a close filled at acknowledgement never
closing its decision). Unit tests of each layer cannot see those; one
scenario crossing the layers does. So: add the scenario layer, then
shrink the unit layer to what scenarios cannot reach cheaply.

## 2. Measured starting point (2026-09-25)

- 1,872 Python tests (1,645 test functions once parametrized cases
  collapse), 145 files; plus 127 Jest tests.
- Per-test line coverage: **71% of test functions cover no source line
  that another test does not also cover; 662 functions reproduce all
  12,740 covered lines.** That is an upper bound on redundancy, not a
  deletion list: boundary tests run the same lines with other values, and
  those caught real bugs here.

## 3. The method (all in `scripts/test_audit/`)

1. **Where to look:** per-test coverage (coverage.py, `dynamic_context =
   test_function`).
2. **What to delete:** `mutation_prune.py`. Per test file, in a throwaway
   git worktree, mutate every module the file's tests cover and record
   which test catches each mutant. Mutations: comparison flips, and/or,
   dropped `not`, negated conditions, arithmetic swaps, numeric nudges,
   string constants (veto names, reasons, dict keys), `return X` to
   `return None`, and dropping one element from a literal list/tuple/set
   (a rule removed from a sequence).
3. **The rule:** `summarize.py` marks a test SUBSUMED only if it catches at
   least one mutant and tests the greedy pass KEEPS in the same file catch
   every one of them. Tests that catch nothing are never auto-deleted;
   they go to review (a test pinning "two modules share one constant"
   cannot be expressed as a mutant).
4. **The proof:** after `prune.py` deletes subsumed functions, re-run the
   mutation analysis on each pruned file; the number of mutants caught
   must be unchanged.

## 4. Tranche 1 (done)

- **E2E scenario layer** (`apps/api/tests/e2e`, marker `e2e`): one
  Postgres per run, migrated once into a template; each scenario gets a
  private `CREATE DATABASE ... TEMPLATE` copy; `SimBroker` (deterministic
  fills, stop elections, expiry and exercise with Alpaca's activity
  records); the real approvals HTTP route, executor, risk re-check,
  order store, and the production `ReconcilerFleet.tick()` with the
  market clock pinned. Twelve scenarios, about 6 s including server start.
  **They found a real bug on the first run** (617ca2769: an order filled
  at acknowledgement never ran its fill lifecycle, and a close filled
  that way overwrote the entry price).
- **175 engine test functions pruned** in three tranches (28c0d7906,
  90ba3128e, bffc525ad) across 16 files, subsumed per step 3. **Proof per
  step 4: 2,162 caught mutants before, the identical 2,162 after, 0 lost.**
- **Nine mock-session order_sync tests replaced by scenarios**
  (d7639b2c2). Each scenario was revert-checked for the behaviour the
  deleted test pinned. Six of the nine had broken on an unrelated change,
  because a hand-built `execute()` result list was one query short.
- A second scenario found a second real bug: an equity bracket's own stop
  or target fill was recorded as `external_broker` (d7639b2c2).

## 5. Next tranches

| Tranche | Scope | How |
|---|---|---|
| 2 | Remaining engine test files (24 of 40 not yet analysed) | Same method, file by file. |
| 3 | `apps/api` mock-session tests (order_sync, position_manager, kill switch, executor) | Run `mutation_prune.py` with the unit file AND the e2e files together (set `E2E_DATABASE_URL` to a running server so each mutant run skips initdb). A unit test the scenarios subsume goes. Mock-heavy tests are the first candidates: they pin call order and SQL shape, which is why they break on refactors without catching wiring bugs. |
| 4 | `apps/agents` | Slower per run (LangGraph imports); batch overnight. Agent graph wiring gets scenario coverage through the mock-LLM council (`test_council_mock.py`) first. |
| 5 | Jest (127) | Same idea with Stryker only if the Python tranches pay off. |

**Targets.** No test count target. The rule is to keep the mutation
catch count per module unchanged or higher, with the scenario layer
growing. On the tranche-1 ratio (140 of 312 functions subsumed in the
worst files), a suite of roughly 1,000-1,200 tests is plausible. That is
an estimate, not a promise.

## 6. How to run

```bash
.venv/bin/python -m pytest apps/agents apps/api packages -q -m "not e2e"   # fast loop
.venv/bin/python -m pytest apps/api/tests -q -m e2e                         # scenarios
E2E_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres \
  .venv/bin/python -m pytest -m e2e                                        # CI service container
```

The e2e layer skips (with that reason) when there is no
`E2E_DATABASE_URL` and no local Postgres server binaries.
