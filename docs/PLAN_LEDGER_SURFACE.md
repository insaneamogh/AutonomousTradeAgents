# Plan F — make the Refusal Ledger actually show something

**Status:** plan, not built. Written 2026-08-30 by `ID:MODEL1REAL`.
**Priority: 1, immediately after auto-approve.**

---

## 0. The problem, stated plainly

The Refusal Ledger is the entry's entire differentiator. Five competitors already
claim "AI proposes, deterministic gates dispose"; **nobody else measures what the
refusals were worth.** That claim is on the README, in the write-up, and is slide 2
of the deck.

Open the live app today and it reads:

| Where | Shows |
|---|---|
| Dashboard | `RISK SAVED $0` · `REGRET $0` · `VETOES 6 · $0 blocked` |
| Strategies | five cards, all exactly `50/100` confidence, records `0-0`, `last reflection —` |
| Review | `AGREEMENT 0% · 0 reviewed` |
| Insights | no contract funnel, no trim rows |

**A judge opening this sees a system that has measured nothing.** That gap between
what the README claims and what the app shows is worth more than any new feature —
it is the difference between the thesis landing and reading as vapour.

The data layer is largely built. This plan is about making it produce numbers and
putting them on screen.

---

## 1. What is verified (2026-08-30 — do not re-derive)

- **`build_veto_ledger`** (`apps/api/app/services/council/ghost_service.py:136`)
  selects `AgentDecision` LEFT JOIN `GhostOutcome` where
  `risk_approved IS FALSE AND risk_veto_rule IS NOT NULL`. Per rule it sums
  `proposal.estimatedNotional` into `blocked_notional`, and sums **only**
  `GhostOutcome.status == 'final'` rows into `ghost_pnl` →
  `prevented_loss_usd = max(0, -ghost_pnl)`.
- **`count_trim_rules`** reads `reasoning.risk_trim_rules` off **approved** rows.
  Exposed as `trims[]` + `totalTrims` on `GET /api/v1/risk/vetoes`.
- **`reasoning.contract_funnel`** is persisted on every options pass — approved
  *and* refused — as `{counts, rejection_reason, selected_occ}`.
  `run_council` also returns `reasoning` directly.
- **`evaluate_ghosts()`** (`apps/agents/trading_agents/jobs/ghost_eval.py`) marks
  vetoed/declined/expired decisions forward. Options are marked on the **contract's**
  own bars with the multiplier applied; equities on daily closes. Status goes `final`
  on **elapsed trading days**, not on a bar existing.
- **`reflection_agent_run`** (`nodes/reflection.py:39`) has **no scheduler calling
  it** — its only caller is `cli/reflection.py`. That is why every strategy sits at
  the `NEUTRAL_PRIOR = 0.5` default → `50/100`.
- **93 decisions and +$47 realised in the DB are from the OLD account**
  (`PA3RFT091VEB`, Aug 27). The submitted account is `PA3IAZI74E5R`.
- `_SNAPSHOT_BLOCKS` now includes `"patterns"`, so pattern scores reach
  `reasoning.feature_snapshot`.

## ⚠️ Verify first — do not assume the aggregation is broken

**`VETOES 6 · $0 blocked` most likely means the 6 rows have no
`proposal.estimatedNotional`, not that the sum is wrong.** Look at the actual rows
before touching `build_veto_ledger`:

```sql
SELECT risk_veto_rule, final_action, proposal->>'estimatedNotional' AS notional,
       user_response, triggered_at
  FROM agent_decisions
 WHERE risk_approved IS FALSE AND risk_veto_rule IS NOT NULL
 ORDER BY triggered_at DESC LIMIT 20;
```

If `notional` is NULL on all six, the fix is at the **write** side (the drafter/DTO
must carry it onto vetoed proposals too), not the read side. Getting this backwards
means "fixing" a working aggregation and leaving the real cause.

---

## 2. Work, in order

### 2.1 Make the numbers exist (backend)

**a. Run the ghost evaluator and confirm rows finalize.**

```bash
uv run --package agents python -m trading_agents.jobs.ghost_eval
```

Then check `GhostOutcome`: rows present, `status` reaching `'final'`, `ghost_pnl`
non-null. If everything is `'pending'`/`'partial'`, marks are not being found —
check `entry_source` and whether `daily_closes` returned anything. **`prevented_loss_usd`
counts only `final` rows**, which is why a ledger can look empty while ghosts exist.

**b. Wire `evaluate_ghosts()` into the EOD path** if it is not already firing
daily. A ledger needs several days of marks to be interesting by Friday, and there
are four sessions left. This is the single highest-leverage backend item.

**c. Decide the old-account question, explicitly.** 93 decisions from `PA3RFT091VEB`
currently populate Strategies and Review. Either:
- scope the ledger/strategy/review views to decisions since the account switch, or
- keep them and label the window honestly ("all history, two accounts").

**Do not leave it ambiguous.** A judge asking *"is this the account you submitted?"*
needs one answer. My recommendation: **keep the rows, label the window.** Deleting
history to make a demo cleaner is the kind of thing that reads badly if noticed, and
the ledger's whole pitch is honest measurement.

**d. Either wire `reflection_agent_run` into the EOD cron, or stop rendering a
confidence bar.** Five strategies pinned at exactly `50/100` with `last reflection —`
is worse than showing nothing: it advertises a learning loop that has never learned.
If there is no time to wire it, replace the bar with "not yet calibrated".

### 2.2 The Contract Funnel view — highest demo value per hour

`select_contract` is where **most** refusals happen — far more than the risk engine —
and `reasoning.contract_funnel` has the per-stage counts on every options decision.
Nothing reads them.

Build it on **Insights**. Stepped bar or Sankey:

> **4,128 contracts → 2,064 calls → 1,843 in the DTE window → 130 in the delta band
> → 3 liquid → we bought 1**

Needs a small read endpoint (aggregate `reasoning->'contract_funnel'` across the
window, or return the most recent N). Show the **named `rejection_reason`** on a HOLD
— `no_delta_in_band`, `no_liquid_contract`, `iv_outside_plausible_band`. That is the
answer to *"why did it just say HOLD?"*, which was a real user complaint and is
currently answerable only from the database.

**Nobody in the field of 12 has this.** Cut it last, not first.

### 2.3 Surface trims next to blocks

`GET /api/v1/risk/vetoes` already returns `trims[]` and `totalTrims`. Render them as
a **separate section**, never summed into the veto count — a trim approved a smaller
trade, a veto approved nothing. Label it plainly: *"risk shrank N trades"*.

### 2.4 The story trade — the money shot

One refusal, expanded: the exact contract, the named rule that refused it, the thesis
behind it, and **what that contract did afterwards in dollars**. Aggregate scorecard →
click a row → this.

That is the demo. Lead the video with it, not with the architecture diagram.

### 2.5 Counterfactual equity curve (cut first if time runs out)

Actual account equity vs "what if the agent had taken every trade risk refused."
The per-rule scorecard alone is still unique without it.

---

## 3. Honesty rules for this surface

The ledger's credibility *is* the product. Three rules:

1. **Never present a `pending`/`partial` ghost as a realised number.** Show it as
   pending or omit it. A number that later moves is worse than no number.
2. **State the caps in force alongside the figures.** Stamp
   `reasoning["risk_profile"]` and show it. The caps were widened mid-contest
   (1%/5% → 2.5%/12%) as a reviewed, dated decision — *"here is what we refused,
   under these caps, which we chose and disclosed"* is a **stronger** claim than
   pretending the caps were always these.
3. **Label auto-approved trades** (see [`PLAN_AUTO_APPROVE.md`](PLAN_AUTO_APPROVE.md)).
   *"N of these were opened with no human in the loop"* is a real claim and needs
   real evidence.

---

## 4. Tests

Mostly a rendering job, so the leverage is in the aggregation layer:

| Test | Break this to make it fail |
|---|---|
| `test_pending_ghosts_are_excluded_from_prevented_loss` | Sum non-final rows too |
| `test_trims_are_not_counted_as_vetoes` | Add `totalTrims` into `totalVetoes` |
| `test_funnel_endpoint_returns_named_rejection_reasons` | Return counts only |
| `test_veto_row_with_no_notional_does_not_crash_the_ledger` | Assume the key exists |
| `test_ledger_scopes_to_the_chosen_window` | Ignore `window_days` |

**Baseline: 940 passed, 9 skipped.** `git stash` and re-run before blaming your change.

Verify against the live app, not just tests: after `evaluate_ghosts()` runs, hit
`/api/v1/risk/vetoes` and `/api/v1/ghost/summary` and confirm a **non-zero** dollar
figure appears. The bug this whole plan exists to fix is a screen full of `$0`, and
only a real request proves it is gone.

---

## 5. Where you will go wrong

1. **"Fixing" `build_veto_ledger` before looking at the 6 rows.** The aggregation is
   probably right and the rows probably have no notional. Run the SQL in §1 first.
2. **Deleting the old account's decisions** to make the demo look clean. Label the
   window instead.
3. **Summing trims into the veto count.** They are separate on purpose.
4. **Showing a partial ghost as a final number.**
5. **Building the counterfactual curve before the funnel.** The funnel is the
   differentiator; the curve is a nice-to-have.
6. **Leaving the strategy confidence bars at 50/100** and hoping nobody asks.

---

*Related: [`PLAN_NEXT.md`](PLAN_NEXT.md) · [`PLAN_AUTO_APPROVE.md`](PLAN_AUTO_APPROVE.md) ·
[`HACKATHON.md`](HACKATHON.md) · [`../CLAUDE.md`](../CLAUDE.md)*
