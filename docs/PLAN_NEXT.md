# Next steps — the run-up to submission

**Written 2026-08-30 by `ID:MODEL1REAL` after reviewing the four shipped plans.**
Deadline: **Fri Sep 4, 11:00 EDT.** Trading sessions left: Mon 31 · Tue 1 · Wed 2 ·
Thu 3 · Fri 4 (90 min).

> **Read this before `PLAN_EXIT_AGENT.md` §5 or anything else.** The four plans queued
> in `CLAUDE.md` are now largely implemented. This file is what is left, in the order it
> should be done, plus the product gaps a review of the live app turned up.

---

## 0. State of the world (verified 2026-08-30, not assumed)

| Thing | Status |
|---|---|
| Suite | **937 passed, 9 skipped.** ruff 252 (unchanged baseline) |
| Account | `PA3IAZI74E5R`, paper, **$100,000**, **options level 3**, ACTIVE |
| Ratchet (A.1/A.2) | ✅ shipped, revert-checked |
| Aggressive profile (B) | ✅ shipped — `RISK_PROFILE=aggressive_paper` now set on Railway |
| Candlestick patterns (C) | ✅ detector + fit + prompt. **Chart not built.** |
| Alpaca CLI (D.2/D.3/D.5) | ✅ shipped, `USE_ALPACA_CLI=1` now set |
| LLM exit agent (A.3/A.4) | ❌ deliberately deferred |
| Alpaca MCP client (D.4) | ❌ deliberately cut |
| Ledger tie-in (A.5) | ❌ not built |

Railway now carries `ALLOW_OPTIONS=1`, `RISK_PROFILE=aggressive_paper`,
`USE_ALPACA_CLI=1`. All three were missing until today and each one independently
made a whole workstream inert.

---

## 0.5 🚨 BIGGEST BLOCKER — the agent cannot OPEN a trade at all

`approval_mode` is hardcoded `"ask"`; `execute_proposal` is reachable only from two
authenticated HTTP endpoints. The cron's last act is a push notification. So the
agent drafts, files an audit row, and stops — **running it "on auto" Mon–Thu
produces zero trades**, just a full inbox. The exit half already runs unattended;
the entry half does not.

Full design, gates and traps: **[`PLAN_AUTO_APPROVE.md`](PLAN_AUTO_APPROVE.md)**.
Ship it flag-gated OFF; the account owner flips the switch.

---

## 1. 🚨 BLOCKER — the agent cannot trade an option today

~~**Every watchlist row is `asset_class='equity'`. All 45 of them.**~~
**CLEARED 2026-08-30** — 8 liquid underlyings (SPY, QQQ, NVDA, AAPL, MSFT, AMD,
TSLA, META) are now `asset_class='option'`, `ALLOW_OPTIONS=1` is set, and
`options_trading_level` reads **3** in the snapshot. Kept below because the
reasoning still applies if the watchlist is ever rebuilt.

Per `OPTIONS_PLAYBOOK.md` §1.1 an options pass needs **both** `ALLOW_OPTIONS=1`
**and** a watchlist row marked `option`. The env var is now set; the rows are not.
Until this is fixed the options track — a hard eligibility requirement — produces
nothing, no matter what else works.

**Do this first, before anything else in this file.** Either flip rows in the
Settings UI, or:

```sql
UPDATE user_watchlist SET asset_class = 'option'
 WHERE user_id = '43221580-69bc-4134-8e1e-5af75499d874'
   AND symbol IN ('SPY','QQQ','NVDA','AAPL','MSFT','AMD','TSLA','META');
```

Pick liquid underlyings — those eight have the deepest option chains on the
watchlist, which matters directly for the `liquidity` funnel stage (OI ≥ 100,
spread ≤ 12%). Do **not** convert thin names; they will just fill the funnel with
`no_liquid_contract` rejections.

**Verify it took**, don't assume:

```bash
ALLOW_OPTIONS=1 RISK_PROFILE=aggressive_paper \
  uv run --package agents python -m trading_agents.jobs.daily_cron --force
```

Confirm at least one symbol reaches a non-zero `funnel_counts` survivor at every
stage. Zero options fills by **Tuesday's close is the emergency signal** — loosen
the funnel, never the risk caps.

### The other hard ordering constraint

`positions_snapshot.options_trading_level` was **`None`** on a level-3 account —
`UserBrokerPoller` never fetched it. **Fixed 2026-08-30** (`6964dbf4a`); the
correct implementation had existed all along in `engine.reconciler.poller.AlpacaPoller`,
which production does not use. Same commit also restores `is_option`/`multiplier`
on polled positions, without which an option position entered the risk context
looking like stock and `max_total_premium_pct` under-counted the book.

Still verify after every cold start — `_cold_boot_fallback` does not set the level,
so it is null until the first reconciler tick lands:

```sql
SELECT options_trading_level, captured_at FROM positions_snapshot
 ORDER BY captured_at DESC LIMIT 1;
```

### P1 code fix — the daily P&L baseline is account-blind

`engine/reconciler/snapshot.py::_daily_pnl` computes today's P&L against **today's
earliest `positions_snapshot` row**, not against the broker's own `last_equity`.

Swap the Alpaca account mid-session and the baseline is the *previous* account's
equity. Measured 2026-08-30: a fresh $100,000 account displayed **−$169.27 all day**
because the 00:00 UTC row held the old account's $100,169.27. Alpaca itself reported
`equity == last_equity == 100000`, i.e. P&L exactly 0.00.

It self-heals at 00:00 UTC and the stale rows have been cleared, but the flaw stands:

- **`breaker.py` reads `daily_pnl_pct`** to trip the −3% drawdown halt. A bogus
  baseline means the only real circuit breaker is measuring against the wrong number.
- Any future key rotation reintroduces it silently.

**Fix:** prefer the broker's `last_equity` (authoritative, account-scoped) and keep
the first-snapshot-of-day as the fallback. Needs a broker handle in `_daily_pnl`, so
it touches the reconciler signature — do it deliberately, with a test, not in a rush.
This sits on the drawdown-breaker path; treat it as risk code.

---

## 2. Product gaps found reviewing the live app

These are real and they undercut the demo more than any missing feature.

### 2.1 Stale data after a key swap — fixed by deploy, but understand why

The dashboard showed **−$169 on a brand-new account**, and the DB snapshot said
`daily_pnl = -169.27` while Alpaca itself reported `equity == last_equity == 100000`
(P&L exactly 0.00). Both broker connection rows are **env-sentinel** rows
(`env:alpaca`), so the broker client resolves `ALPACA_API_KEY` from the process
environment at call time — and the running process still had the previous account's
keys because it had not restarted since the swap.

**The lesson worth encoding:** an env-backed broker connection means *changing the
Railway variable is not enough — the service must restart.* The UI says "Connected
via server configuration", which is true and yet gave no hint that the process was
holding a stale key.

**Worth building (small, high demo value):** surface the broker account number on
the Settings row. `broker_connections.account_number` is `NULL` for both rows.
Populate it on each reconciler tick from `broker.get_account()`. Then "which account
am I actually pointed at" is answerable from the UI instead of from a DB query, and
the submission's required account ID is visible in a screenshot.

### 2.2 The Refusal Ledger reads empty — and it is the entry's whole thesis

Dashboard shows `RISK SAVED $0`, `REGRET $0`, `VETOES 6 · $0 blocked`. Strategies
shows five cards all at exactly `50/100` confidence with `0-0` records. Review shows
`AGREEMENT 0% · 0 reviewed`.

A judge opening this sees a system that has measured nothing. **This is the single
biggest gap between what the README claims and what the app shows**, and it is worth
more than any new feature.

Three causes, in order of cost to fix:

1. **`$0 blocked` on 6 vetoes** — the vetoes are almost certainly strategy-fit HOLDs
   or rows whose `proposal.estimatedNotional` is absent, so there is no notional to
   sum. Check what those 6 rows actually are before assuming the aggregation is wrong.
2. **Ghost P&L needs `evaluate_ghosts()` to have run** and to have found forward
   prices. Run it manually and confirm `GhostOutcome` rows exist and reach
   `status='final'`.
3. **Old-account decisions are still in the DB** (93 decisions, +$47 realised, from
   `PA3RFT091VEB`). They are what populates Strategies and Review today. Decide
   deliberately: either keep them and label the ledger "all history", or scope the
   views to the new account. **Do not leave it ambiguous** — a judge asking "is this
   the account you submitted?" needs one answer.

### 2.3 Nothing renders the two features we just built

`reasoning.contract_funnel` and `RiskDecision.trim_rules` are persisted and exposed
on `GET /api/v1/risk/vetoes`. **No screen reads either.** The Contract Funnel is the
highest demo-value-per-hour item in the whole project:

> *"4,128 contracts → 2,064 calls → 1,843 in the DTE window → 130 in the delta band
> → 3 liquid → we bought 1."*

Sankey or stepped bar, on the Insights screen. Nobody else in the field has it.

### 2.4 Strategy confidence is inert

All five strategies sit at exactly `50/100` with `last reflection —`. The Reflection
Agent has never run — `reflection_agent_run` has **no scheduler calling it**
(verified: its only caller is `cli/reflection.py`). Either wire it into the EOD cron
or stop showing a confidence bar that cannot move. Showing a "learning loop" that
has never learned is worse than not showing one.

---

## 3. Build order for the remaining time

```
NOW      §1 watchlist asset_class  ← nothing options-related works without this
         verify options_trading_level = 3 after a reconciler tick

Mon 31   Pre-open: confirm a council pass produces an options proposal.
         In-session: WATCH, do not ship. Collect one session of pure-ratchet
         evidence (PLAN_EXIT_AGENT.md's own gate for A.3).
         After 16:00 ET: §2.2 ledger triage.

Tue 1    §2.3 Contract Funnel view  ← highest demo value
         A.3 exit agent behind OPTIONS_EXIT_AGENT=0, flip mid-morning after
         one clean deterministic tick.

Wed 2    §2.1 account_number surfacing. A.5 ledger tie-in if A.3 is solid.
         DELIVERABLES START — write-up, slides, video script. Do not leave
         the video to Friday.

Thu 3    Last full session. Code freeze at the close; bug fixes only.
         Candlestick chart only if everything above is done.

Fri 4    Submission. No code.
```

**Cut order if time runs short:** candlestick chart → A.5 ledger tie-in → A.4 tool
harness (ship the exit agent single-turn) → A.3 entirely. **Never cut** §1, §2.2, or
§2.3.

---

## 4. Submission checklist — start Wednesday, not Friday

- [ ] Public GitHub repo (done)
- [ ] Demo application + live URL (done — `autonomoustradeagents-autonomous.up.railway.app`)
- [ ] **Alpaca paper account ID: `PA3IAZI74E5R`** — judges read P&L from this
- [ ] Video presentation + slide deck
- [ ] One-page write-up: AI logic · risk gates · Alpaca infrastructure
- [ ] Screenshot `market_open_source: "alpaca_cli"` from the scanner status payload —
      that is the eligibility evidence for the CLI requirement
- [ ] (Optional, separate $500×2) up to 5 X/LinkedIn posts tagging @lablabai and
      @AlpacaHQ

**Demo script — lead with the ledger, not the architecture:**
one live council pass → a refusal → the Contract Funnel showing *why* → the Refusal
Ledger showing what that refusal was worth in dollars → the counterfactual curve.
Architecture is slide 4, not slide 1. Five competitors claim the propose/dispose
framing; none of them measure refusals.

---

## 5. Honest disclosures that must stay in the write-up

Do not quietly drop these to make the entry look stronger. Every one of them reads as
rigour, and a judge who finds an undisclosed one reads it as a defect.

- **Paper trading.** Hypothetical, no real fills.
- **15-minute delayed indicative feed**, not consolidated OPRA. Fine for daily-bar
  decisions; not a basis for any execution-quality claim.
- **`earnings_blackout` is permanently inert** — Alpaca publishes no earnings
  calendar. Named and disclosed, never fed a fabricated date.
- **No assignment or exercise handling.** The `DTE ≤ 2` sweep is the only protection,
  and it depends on our loop being alive.
- **Premium exits depend on our loop.** Unlike an equity bracket, which survives our
  downtime at the broker, an unreached stop is an unenforced stop.
- **Caps were widened mid-project** (1%/5% → 2.5%/12%) as a reviewed, dated decision
  for a paper account, with `daily_drawdown_halt_pct = -3.0` deliberately frozen.
  That coupling is the argument — state it, don't hide it.
- **4 sessions of P&L is a sample size of one.** The ledger is the contribution.

---

*Related: [`HACKATHON.md`](HACKATHON.md) · [`OPTIONS_PLAYBOOK.md`](OPTIONS_PLAYBOOK.md) ·
[`PLAN_EXIT_AGENT.md`](PLAN_EXIT_AGENT.md) · [`PLAN_CANDLE_PATTERNS.md`](PLAN_CANDLE_PATTERNS.md) ·
[`PLAN_ALPACA_MCP.md`](PLAN_ALPACA_MCP.md) · [`../CLAUDE.md`](../CLAUDE.md)*
