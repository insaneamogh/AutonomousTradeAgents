# Plan E — auto-approve: let the agent open a trade unattended

**Status: BUILT AND MERGED** (`3bce40b2`/`4e46507e`/`9475c83b`, 2026-08-30
22:17 UTC+5:30). Written 2026-08-30 by `ID:MODEL1REAL`; this header was stale
for most of a day (CLAUDE.md §4.2's own trap) — corrected 2026-08-31 after
independently re-verifying the shipped code against every claim below:
`auto_approve_for_user` lives at `apps/api/app/services/orders/auto_approver.py`
(not `.../executor.py` alongside `execute_proposal` — separate file, imports
it), is wired into `ReconcilerFleet.tick()` immediately after
`manage_positions_for_user` as this doc requires, ships an added gate 2b
(per-connection `auto_approve_consent`, not in this doc's original design) on
top of the seven gates below, and gate 2 was personally revert-checked
(inverted the condition, confirmed `test_never_auto_approves_in_live_mode`
and `test_never_auto_approves_when_live_trading_enabled` both fail, restored,
confirmed all 19 tests in `test_auto_approver.py` pass). Whether
`AUTO_APPROVE_ENABLED` is actually set on the live Railway deployment right
now was NOT verified this pass — that's an operator/env question, not a code
one. The rest of this document is the original design and is still accurate
background; treat "not built" language below as historical.

**Priority: 0. Nothing else produces a trade Mon–Thu without this.**

---

## 0. Why this is priority zero

The agent **cannot currently open a position.** It drafts a proposal, writes an audit
row, sends a push notification, and stops. `approval_mode` is hardcoded `"ask"`
(`postgres_store.py:203`) and `execute_proposal` is reachable from exactly two
authenticated HTTP endpoints — no scheduler, no cron, nothing autonomous touches it.

So "run it Monday to Thursday on auto" currently means: the inbox fills with
proposals and **zero trades happen.** The exit half already runs unattended; the
entry half does not.

This is a deliberate pre-hackathon design (human owns the entry), and the user has
now explicitly overridden it for the contest window, on the reasoning that the
deterministic stack ahead of the decision is deep. That reasoning is sound — see §2.

---

## 1. What is verified (measured 2026-08-30 — do not re-derive)

- **`execute_proposal`** (`apps/api/app/services/orders/executor.py:110`) is the single
  chokepoint: resolve pending → **re-run the full risk gate** → place → persist →
  `store.decide("approved", exit_mode=…)`. Idempotent on `proposal_id`. Signature:
  `(*, user_id, proposal_id, store=None, risk_caps=None, exit_mode="agent")`.
- Its **only** callers are `routers/approvals.py:95` and `routers/orders.py:56`.
- **`store.list_pending(user_id)`** (`postgres_store.py:160`) returns proposals where
  `risk_approved IS TRUE AND user_response IS NULL`, and **already filters out expired
  ones** (`expires_at < now`) without writing back. That is exactly the candidate set.
- **`ReconcilerFleet.tick()`** runs every **30s** per user (`reconciler_fleet.py`,
  `FleetConfig.interval_seconds = 30.0`), in a fixed order:
  `reconciler.tick` → `sync_user_orders_and_positions` → `manage_positions_for_user`
  → `sweep_expiring_options_for_user`. Each in its own `try/except`.
  It only starts when `USE_POSTGRES` **and** `RECONCILER_ENABLED` (which defaults to
  `USE_POSTGRES`).
- **There is no market-hours gate anywhere in `apps/api/app/services/orders/`.**
  That whole package runs 24/7.
- `trading_mode()` (`paper_broker.py:46`) → `"paper"` | `"live"`, default `paper`.
- `agent_decisions.approval_mode` is **`String(10)`**. `"auto"` fits; so does `"ask"`.
- `approval_expiry` defaults to 21:00 UTC same/next day
  (`runtime.py`, `AGENT_APPROVAL_TTL_MINUTES` overrides).

## ⚠️ What you must verify before relying on it

- That `execute_proposal` behaves identically when called outside a request context
  (no FastAPI `Depends`, no request-scoped session). Read it end to end first — it
  takes an optional `store`, so it should, but **confirm rather than assume**.
- Whether `store.decide()` leaves `approval_mode` untouched. If it overwrites, your
  `"auto"` stamp needs to be written after, not before.

---

## 2. Design — a sweeper, not a call inside the cron

Put it in **`apps/api/app/services/orders/auto_approver.py`**, called from
`ReconcilerFleet.tick()` **after `manage_positions_for_user`**.

```python
async def auto_approve_for_user(
    *, user_id: str, session_factory, caps: RiskCaps | None = None
) -> int:
    """One pass. Returns the number of proposals executed."""
```

**Why a sweeper on the fleet tick and not a call at the end of `daily_cron`:**

- It catches proposals from **every** source — scheduled cron, scanner triggers, a
  manual council run from the UI — not just the one path you wired.
- It is naturally idempotent: an executed proposal stops being pending, so a re-run
  is a no-op. No bookkeeping needed.
- The daily budget and the kill switch live in **one** place instead of one per
  producer.
- **Exits already ran earlier in the same tick**, so premium freed by a close is
  available to the entry that follows. Ordering matters; do not move it earlier.

### The gate — all seven, or nothing executes

```python
1. env_flag("AUTO_APPROVE_ENABLED")             # default OFF
2. trading_mode() == "paper"
   and not env_flag("LIVE_TRADING_ENABLED")     # HARD-CODED, see below
3. is_us_market_open(now)                       # engine.features.market_calendar
4. proposal age <= AUTO_APPROVE_MAX_AGE_MIN     # default 60
5. auto-approvals today < AUTO_APPROVE_MAX_PER_DAY   # default 5
6. per-tick cap of 1
7. breaker not tripped for this user
```

### 🚨 Gate 2 is not configurable and must not become configurable

**Refuse to auto-approve in live mode, unconditionally, even when
`AUTO_APPROVE_ENABLED=1`.** Not a warning, not a config option — a hard `return 0`
with a log line. The blast radius of this feature in paper is a bad number on a
dashboard; in live it is real money placed by a loop with no human in it.

Write that reasoning into the docstring so nobody "generalises" the flag later.

### Gate 6 is a blast-radius bound, not an optimisation

One order per 30-second tick means a bug that mis-reads the pending list places **one**
wrong order and you have 30 seconds to notice, instead of emptying the inbox in a
single pass. Do not remove it as over-engineering. Same reasoning as the exit agent's
per-tick consult cap.

### What it actually does

For the single chosen proposal:

```python
result = await execute_proposal(
    user_id=user_id, proposal_id=proposal.id, exit_mode="agent",
)
```

`exit_mode="agent"` is **required**, not a default to think about: an auto-opened
position with `manual` exits would be opened by a machine and then owned by nobody.
Every auto-approved entry must be managed by the ratchet, the time stop and the
expiry sweep.

**The risk gate re-runs inside `execute_proposal`.** That is the last line and it is
already there — do not add a second copy of the risk logic in the sweeper
(CLAUDE.md §4.4). If it returns `risk_blocked=True`, the proposal **stays pending**;
log it and move on. Do not retry it in a loop.

### Audit — this is not optional

Stamp `approval_mode = "auto"` on the row after a successful execution.

A judge (and the write-up) must be able to tell which trades a human clicked and
which the machine opened by itself. Without the stamp the two are indistinguishable
in the decision log, and the honest claim — *"N of these were fully autonomous"* —
becomes unprovable. It is also what lets the Refusal Ledger slice by approval source.

Surface it: the Picks / Review rows should show an `AUTO` pill.

---

## 3. Env vars

| Var | Default | Meaning |
|---|---|---|
| `AUTO_APPROVE_ENABLED` | **off** | Master switch. The user flips this, deliberately. |
| `AUTO_APPROVE_MAX_PER_DAY` | `5` | Auto-approvals per user per UTC day. |
| `AUTO_APPROVE_MAX_AGE_MIN` | `60` | Skip proposals older than this — a stale thesis is not worth executing. |

Malformed values keep the default and log a warning — the same fail-to-default
contract `_env_int` / `_env_float` already use in `RiskCaps`. **A typo must never
widen a bound.**

Worst case with the defaults: 5 trades/day × 2.5% premium = 12.5% notional, which
the existing `options_max_total_premium_pct = 12.0` book cap clamps first. The bound
holds without the sweeper knowing about it — which is the point of keeping the risk
engine as the last line.

---

## 4. Tests, and the revert-check matrix

Per CLAUDE.md §4.1 — break the fix, confirm the test fails, restore.

| Test | Break this to make it fail |
|---|---|
| **`test_never_auto_approves_in_live_mode`** | Make gate 2 respect the env flag. **The most important test in this plan.** |
| **`test_disabled_by_default`** | Default `AUTO_APPROVE_ENABLED` to on |
| `test_does_not_approve_outside_market_hours` | Drop the `is_us_market_open` check |
| `test_stops_at_the_daily_budget` | Remove the per-day count |
| `test_at_most_one_per_tick` | Remove the per-tick cap |
| `test_skips_a_stale_proposal` | Drop the age check |
| `test_risk_blocked_leaves_the_proposal_pending` | Mark it declined/approved on a block |
| `test_stamps_approval_mode_auto` | Leave `approval_mode` as `"ask"` |
| `test_uses_agent_exit_mode` | Pass `manual` |
| `test_a_broker_failure_does_not_kill_the_fleet_tick` | Let the exception escape |

Also assert `"auto"` fits `String(10)`. One line, catches a truncation that would
otherwise only appear in production.

**Baseline: 940 passed, 9 skipped.** `git stash` and re-run before blaming your change.

### Live verification, in this order

1. Deploy with `AUTO_APPROVE_ENABLED=0`. Confirm the sweeper runs and executes nothing.
2. Confirm a pending proposal actually exists (`GET /api/v1/approvals/pending`).
3. **The user flips `AUTO_APPROVE_ENABLED=1`.** Not you — enabling autonomous order
   placement is the account owner's decision, and it is the moment this stops being
   a code change.
4. Watch exactly one tick. Confirm: one order at the broker, `approval_mode='auto'`,
   `exit_mode='agent'`, and the position appears in the position manager's book.
5. Only then leave it running.

---

## 5. Where you will go wrong

1. **Making gate 2 configurable.** Re-read §2. Paper-only is the property that makes
   the whole feature safe to ship in four days.
2. **Enabling the flag yourself.** Ship it off. The user turns it on.
3. **Re-implementing risk checks in the sweeper** because "deterministic checks ahead
   of it" sounds like it means more checks here. It does not — the checks already
   exist in `engine.risk` and `execute_proposal` re-runs them. A second copy is the
   §4.4 two-places trap and will drift.
4. **Retrying a `risk_blocked` proposal in a loop.** It stays pending on purpose. The
   condition may clear later; a retry loop turns one veto into a hot loop.
5. **Forgetting `exit_mode="agent"`.** An auto-opened `manual` position is orphaned.
6. **Removing the per-tick cap.**
7. **Skipping the `approval_mode` stamp** because it seems cosmetic. It is the only
   evidence that the agent traded autonomously.
8. **Putting the sweeper before `manage_positions_for_user`** in the tick. Exits free
   premium; run them first.
9. **Letting an exception escape.** Every step in `ReconcilerFleet.tick()` is wrapped
   in its own `try/except` for a reason — one user's broker failure must not stop
   reconciliation for everyone else.

---

*Related: [`PLAN_NEXT.md`](PLAN_NEXT.md) · [`PLAN_LEDGER_SURFACE.md`](PLAN_LEDGER_SURFACE.md) ·
[`OPTIONS_PLAYBOOK.md`](OPTIONS_PLAYBOOK.md) · [`../CLAUDE.md`](../CLAUDE.md)*
