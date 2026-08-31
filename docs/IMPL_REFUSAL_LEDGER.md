# IMPL 4 — make the Refusal Ledger show real dollars

**Implementation spec.** No dependencies. Written 2026-08-31 by `ID:MODEL1REAL`.
Est **6h**.

> The entry's entire differentiator. Five competitors claim "AI proposes, deterministic
> gates dispose"; **nobody else measures what the refusals were worth.** Today the app
> shows `RISK SAVED $0 · REGRET $0 · VETOES 6 · $0 blocked`. A judge sees a system that
> has measured nothing.

---

## 0. 🚨 Diagnose before you fix — the aggregation is probably correct

**Run this first. Do not touch `build_veto_ledger` until you have.**

```sql
SELECT risk_veto_rule,
       final_action,
       proposal->>'estimatedNotional' AS notional,
       user_response,
       triggered_at
  FROM agent_decisions
 WHERE risk_approved IS FALSE AND risk_veto_rule IS NOT NULL
 ORDER BY triggered_at DESC LIMIT 20;
```

| What you see | What it means | Where the fix is |
|---|---|---|
| `notional` NULL on all rows | The **write** side never populated it on vetoed proposals | `runtime._to_proposal_dto` / the drafter |
| `notional` populated, ledger still $0 | The read/aggregation | `ghost_service.build_veto_ledger` |
| 0 rows | Those "6 vetoes" are something else entirely | re-check the dashboard's source |

`build_veto_ledger` sums `proposal.estimatedNotional` into `blocked_notional`. **If the
key is absent the sum is legitimately 0 and the code is right.** "Fixing" a working
aggregation and leaving the real cause is the most likely way to lose an hour here.

---

## 1. Why `prevented_loss_usd` is null even when ghosts exist

`build_veto_ledger` counts **only `GhostOutcome.status == "final"`**:

```python
if ghost is not None and ghost.status == "final" and ghost.ghost_pnl is not None:
    ghost_finals.append(float(ghost.ghost_pnl))
```

So a window full of `pending`/`partial` ghosts reports `None`, correctly. **That is not
a bug — it is the honesty rule** (§4.1). The fix is to make ghosts *finalize*, not to
count unfinished ones.

### Step 1 — run the evaluator and read the counters

```bash
uv run --package agents python -m trading_agents.jobs.ghost_eval
```

It returns `{"created": n, "updated": n, "finalized": n, "skipped": n}`.

**A high `skipped` is the signal.** `evaluate_ghosts` skips when: `reason is None`,
`entry is None`, `mark_symbol is None` (an options row with no `occSymbol`), side not
BUY/SELL, `qty` falsy, or `daily_closes` returned `[]`. Instrument which branch fired —
add a per-reason counter — before guessing.

### Step 2 — confirm ghosts reach `final`

```sql
SELECT status, count(*), min(first_evaluated_at) FROM ghost_outcomes GROUP BY 1;
```

`status` goes `final` when `_trading_day_offset(start_day, today) >= horizon`
(5 trading days on a `short` horizon). **A veto from yesterday cannot be final.** With
four sessions of runway, seed the window by running the evaluator against decisions from
the *previous* account too if you keep them (§3).

### Step 3 — wire it into EOD

`evaluate_ghosts()` must run **daily**, unattended. Confirm `daily_cron` calls it after
the council loop; if not, wire it. Ghosts need several days of marks to be interesting
by Friday and there are four left. **This is the single highest-leverage backend item
in this document.**

---

## 2. The screens

### 2.1 Per-rule scorecard — Insights

`GET /api/v1/risk/vetoes` already returns everything needed:
`rules[]` (`rule`, `count`, `blockedNotional`, `ghostPnl`, `preventedLossUsd`, `lastAt`),
plus `trims[]` and `totalTrims`.

```
REFUSAL LEDGER                                        30d · 14 refusals

RULE                       FIRED   BLOCKED    WOULD HAVE
max_premium_pct                6   $12,400    −$340  saved
illiquid_contract              4    $8,100      —    pending
min_council_confidence         3    $5,200    +$120  missed
pdt_block                      1    $2,000      —    pending

RISK ALSO SHRANK 6 TRADES
max_premium_pct_trim           6
```

- **`trims` render as a separate section, never summed into the refusal count.** A trim
  approved a *smaller* trade; a veto approved nothing. Summing them inflates the
  headline with events that did not stop a trade.
- `preventedLossUsd > 0` → green "saved". A ghost that would have *made* money → amber
  "missed". **Show both.** A ledger that only reports wins is not a ledger.
- `null` → literal **"pending"**, never `$0`. §4.1.

### 2.2 The story trade — the money shot

Click a rule row → the single most extreme refusal under it:

> **NVDA260918C00225000 — refused by `max_premium_pct`**
> The council wanted 12 contracts at $2.17 ($2,604 = 2.6% of equity, cap 2.5%).
> **Bull case:** …  **Bear case:** …
> Five sessions later the contract was worth **$0.94**.
> **That refusal saved $1,476.**

Needs a `GET /api/v1/risk/vetoes/{rule}/exemplar` returning the row with the largest
`abs(ghost_pnl)` among finalized ghosts for that rule.

**This is the demo.** Lead the video with it, not the architecture diagram.

### 2.3 Dashboard tiles

`RISK SAVED` / `REGRET` already read `/ghost/summary`. Change only the empty rendering:
when `saved_usd` is 0 **and** there are pending ghosts, show
`"$— · 4 marks pending"` rather than `$0`. `$0` claims a measurement; `—` admits one is
outstanding.

---

## 3. The old-account decision — make it explicitly

93 decisions and +$47 realised in the DB belong to **`PA3RFT091VEB`**, not the submitted
`PA3IAZI74E5R`. They populate Strategies and Review today.

**Recommendation: keep the rows, label the window.** Add a `Since <date> · account
PA3IAZI74E5R` caption on every analytics screen, and either filter by
`triggered_at >= <switch date>` or show both with the boundary marked.

**Do not delete history to make the demo cleaner.** The ledger's whole pitch is honest
measurement; quietly pruning it is exactly the thing that reads badly if noticed. But do
not leave it ambiguous either — a judge asking *"is this the account you submitted?"*
needs one answer.

---

## 4. Honesty rules — these are the product

### 4.1 Never present an unfinished ghost as a realised number

`pending`/`partial` → **"pending"** or omitted. A number that later moves is worse than
no number, and it is the one failure a judge would actually catch.

### 4.2 Report the caps in force

Caps were widened mid-contest (1%/5% → 2.5%/12%) as a reviewed, dated decision. Stamp
`reasoning["risk_profile"]` on every decision and show it:

> *"Here is what we refused, under these caps, which we chose and disclosed."*

That is a **stronger** claim than pretending the caps were always these — it makes them
a variable the ledger reports rather than a boast the README makes.

### 4.3 Label autonomous trades

`approval_mode='auto'` rows get an `AUTO` pill (already shipped on Decisions). *"N of
these were opened with no human in the loop"* is a real claim that needs real evidence.

---

## 5. Also: Strategy confidence is pinned at 50/100

All five strategies show exactly `50/100` with `last reflection —`, because
`reflection_agent_run` **has no scheduler calling it** (only `cli/reflection.py`). So
they sit at `NEUTRAL_PRIOR = 0.5` forever.

**Either wire it into the EOD cron, or replace the bar with "not yet calibrated".**
Advertising a learning loop that has never learned is worse than showing nothing.

---

## 6. Tests

| Test | Break this to make it fail |
|---|---|
| **`test_pending_ghosts_excluded_from_prevented_loss`** | Sum non-final rows |
| **`test_trims_not_counted_as_vetoes`** | Add `totalTrims` into `totalVetoes` |
| `test_veto_row_without_notional_does_not_crash` | Assume the key exists |
| `test_null_prevented_loss_renders_pending_not_zero` | Render `$0` |
| `test_missed_upside_is_shown_not_hidden` | Only render positive saves |
| `test_exemplar_picks_the_largest_finalized_ghost` | Pick the most recent |
| `test_ledger_scopes_to_the_window` | Ignore `window_days` |
| `test_ghost_eval_counters_name_the_skip_reason` | Return a bare total |

**Baseline: 969 passed, 11 skipped.**

### Verify against the live app, not just tests

After running `evaluate_ghosts()`, hit `/api/v1/risk/vetoes` and `/api/v1/ghost/summary`
and confirm a **non-zero dollar figure**. The bug this document exists to fix is a screen
full of `$0`; only a real request proves it is gone.

---

## 7. Where you will go wrong

1. **Editing `build_veto_ledger` before running §0's SQL.** The aggregation is probably
   right and the rows probably have no notional.
2. **Counting `pending` ghosts** to make the number non-zero. That is fabrication.
3. **Deleting the old account's rows.** Label the window instead.
4. **Summing trims into the veto count.**
5. **Rendering `$0` for "not yet measured".** `—` and "pending".
6. **Only showing saves.** A refusal that cost money is data, and hiding it makes every
   other number suspect.
7. **Leaving the 50/100 confidence bars** and hoping nobody asks.

---

*Next: [`IMPL_DEMO_SESSION.md`](IMPL_DEMO_SESSION.md)*
