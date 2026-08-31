# IMPL 3 — the Contract Funnel view

**Implementation spec.** No dependencies — buildable today against existing data.
Written 2026-08-31 by `ID:MODEL1REAL`. Est **5h**.

> **Highest demo-value-per-hour item in the project.** The data has been persisted on
> every options pass since 2026-08-30 and **nothing reads it.** This is pure surfacing
> of work the system already does.

---

## 0. The data that already exists

`agent_decisions.reasoning` (JSONB) carries, on **every** options decision — approved
*and* refused:

```json
"contract_funnel": {
  "counts": {
    "total": 4128, "contract_type": 2064, "dte_window": 1843,
    "delta_band": 130, "liquidity": 3, "iv_present": 3,
    "iv_realized_vol_band": 1
  },
  "rejection_reason": null,          // or "no_delta_in_band" etc.
  "selected_occ": "NVDA260918C00225000"
}
```

Stage order is **fixed** and defined by `_STAGE_REJECTION_REASONS` in
`packages/engine/engine/options/selection.py`:

| Stage key | Label | Rejection reason |
|---|---|---|
| `total` | Contracts in chain | — |
| `contract_type` | Calls (or puts) | `no_matching_contract_type` |
| `dte_window` | 10–45 DTE | `no_expiry_in_window` |
| `delta_band` | In the delta band | `no_delta_in_band` |
| `liquidity` | OI ≥ 100 · spread ≤ 12% | `no_liquid_contract` |
| `iv_present` | IV reported | `no_iv` |
| `iv_realized_vol_band` | IV sane vs realized | `iv_outside_plausible_band` |

Also in `reasoning`: `strategy_fit`, `risk_checks_passed`, `risk_veto_rule`,
`risk_trim_rules`, `sizing`, `feature_snapshot` (incl. `patterns`), `drafter_rationale`.

---

## 1. Backend

### 1.1 New endpoint — `apps/api/app/routers/insights.py`

```
GET /api/v1/insights/funnel?windowDays=30&limit=20
```

```python
class FunnelStageDto(CamelCaseModel):
    key: str          # "delta_band"
    label: str        # "In the delta band"
    survivors: int
    dropped: int      # previous survivors - this

class FunnelRunDto(CamelCaseModel):
    decision_id: str
    symbol: str
    triggered_at: str
    stages: list[FunnelStageDto]
    rejection_reason: str | None
    rejection_stage: str | None      # which stage hit zero
    selected_occ: str | None
    outcome: str                     # "bought" | "held"

class FunnelAggregateDto(CamelCaseModel):
    """Summed across the window — the headline number."""
    stages: list[FunnelStageDto]
    runs: int
    bought: int
    top_rejection_reasons: list[dict]   # [{"reason": ..., "count": n}]

class FunnelResponse(CamelCaseModel):
    window_days: int
    aggregate: FunnelAggregateDto
    recent: list[FunnelRunDto]
```

Service in `apps/api/app/services/council/funnel_service.py`:

```python
async def build_funnel_report(window_days: int, *, user_id: str, limit: int = 20
) -> FunnelReport:
    """Read reasoning->'contract_funnel' across the window.

    Scoped by user_id via the same `_tenant_filters` helper ghost_service
    uses — do NOT write a second tenant-scoping implementation.
    """
```

**Aggregation in Python, not a JSONB predicate.** The row count is one window of one
tenant's decisions, and a `->>` expression here is one more place for the key name to
drift from `runtime._reasoning_block`.

**Tolerance rules (this JSONB has several generations in it):**
- row with no `reasoning`, no `contract_funnel`, or a non-dict → **skip**, do not raise
- missing stage key → treat as absent, not zero (they mean different things)
- `dropped` is `max(0, prev - current)` — never negative even on malformed data

### 1.2 Wire it

Add `FunnelResponse` to `packages/shared-types/src/index.ts` and re-export.
Router already has `_require_postgres()` — reuse it.

---

## 2. Frontend

### 2.1 Hook — `apps/mobile/src/hooks/useFunnel.ts`

```ts
export function useFunnel(windowDays = 30) {
  return useQuery<FunnelResponse>({
    queryKey: ['funnel', windowDays],
    queryFn: ({ signal }) =>
      request<FunnelResponse>(`/api/v1/insights/funnel?windowDays=${windowDays}`, { signal }),
    staleTime: 60_000,
    retry: false,
  });
}
```

Matches `useInsights.ts` exactly — same shape, same `staleTime`, same `retry: false`.

### 2.2 The component — `apps/mobile/src/desktop/components/ContractFunnel.tsx`

**Stepped horizontal bars**, not a Sankey. A Sankey needs a layout library and reads
worse at 7 stages; stepped bars read instantly and are pure CSS.

```
CONTRACTS CONSIDERED                                   30d · 14 runs · 3 bought

Chain                ████████████████████████████████████████  4,128
Calls                ████████████████████                      2,064   −2,064
10–45 DTE            ██████████████████                        1,843     −221
Delta band           █▎                                          130   −1,713
Liquid               ▏                                             3     −127
IV present           ▏                                             3       −0
IV vs realized       ▏                                             1       −2
                                                          ─────────────
                                                          WE BOUGHT 1
```

Implementation notes:
- Bar width `%` = `survivors / stages[0].survivors`, `minWidth: 2px` so a 1-survivor
  stage is still visible. **A zero-width bar reads as a rendering bug.**
- Use a **log scale toggle** — 4,128 → 1 is three orders of magnitude and linear bars
  make stages 4–7 invisible. Default linear (it is more honest); offer log.
- The `dropped` column is the point of the whole view. Right-align, `pg-num`, prefix `−`.
- Colour the **stage that hit zero** with `--pg-bear-text` on a HOLD run.
- Primitives available: `Card`, `CardHead`, `Label`, `Numeral`, `Pill`, `Row`, `Stack`,
  `SkelRows`, `DataStreamInterrupted`, `StatTile`.

### 2.3 Where it goes

1. **Insights screen** — the aggregate, full width (`Cell span={12}`), top of page.
2. **Decision detail** (IMPL 5 / `PickDetail`) — the single-run version for that
   decision, which is the one a judge clicks into.

### 2.4 The HOLD case is the important one

When `rejection_reason` is set, render a headline strip:

> **HELD — `no_delta_in_band`**
> 1,843 contracts were in the DTE window; none had a delta between 0.35 and 0.75.

That is the direct answer to *"it just says HOLD and I don't know why"*, which was a
real complaint and is currently answerable **only from the database**. Map every
`rejection_reason` to a plain-English sentence — a lookup table in the component, keyed
by the exact strings in `_STAGE_REJECTION_REASONS`.

### 2.5 Empty state

Zero options runs in the window → *"No options passes yet in this window."* **Not** a
funnel of zeroes. A chart of zeroes reads as broken.

---

## 3. Tests

**Backend** — `apps/api/tests/test_funnel_service.py`

| Test | Break this to make it fail |
|---|---|
| `test_aggregates_stages_across_runs` | Sum the wrong axis |
| `test_row_without_contract_funnel_is_skipped` | Assume the key exists → KeyError |
| `test_non_dict_reasoning_does_not_raise` | Assume dict |
| `test_dropped_never_negative` | Plain subtraction on malformed data |
| `test_rejection_stage_is_the_first_zero` | Report the last |
| `test_scoped_to_the_caller` | Drop the tenant filter |

**Frontend** — `apps/mobile/src/desktop/components/ContractFunnel.test.tsx`

| Test | Asserts |
|---|---|
| `test_renders_every_stage_in_fixed_order` | order matches `_STAGE_REJECTION_REASONS` |
| `test_one_survivor_still_renders_a_visible_bar` | `minWidth` applied |
| `test_hold_run_names_the_rejection_stage` | plain-English string present |
| `test_empty_window_shows_an_empty_state_not_zeroes` | |

**Baseline: 969 passed, 11 skipped** (Python) + 28 (jest).

---

## 4. Where you will go wrong

1. **Building a Sankey.** Needs a layout lib, reads worse, costs a day.
2. **Linear bars only.** Stages 4–7 become invisible at 3-orders-of-magnitude drop.
3. **Zero-width bars** for 1-survivor stages — reads as broken. `minWidth: 2px`.
4. **Rendering a funnel of zeroes** on an empty window instead of an empty state.
5. **Treating a missing stage key as 0.** Absent ≠ zero.
6. **Writing a second tenant-scoping helper.** Reuse `ghost_service._tenant_filters`.
7. **Dropping the `dropped` column** because it looks redundant. It is the whole point —
   *"1,713 contracts died at the delta band"* is the sentence.
8. **Hardcoding stage labels out of order.** They must match
   `_STAGE_REJECTION_REASONS`'s insertion order, which is the evaluation order.

---

*Next: [`IMPL_REFUSAL_LEDGER.md`](IMPL_REFUSAL_LEDGER.md)*
