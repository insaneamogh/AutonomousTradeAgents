# Fable 5 Findings — Agent System Audit

**Date:** 2026-06-12
**Scope:** Full audit of the agent council, deterministic engine, broker layer, API, data provenance, and the end-to-end "connect broker → agent auto-trades" loop.
**Method:** 3 parallel codebase exploration passes + direct line-level verification of every load-bearing claim. Everything in §3 (loop breaks) and §4 (determinism) was verified by reading the cited source directly, not just reported by exploration.

---

## 1. TL;DR

The one architectural rule — **"agents propose, deterministic code disposes"** — holds. Agents never touch the broker, never fetch data, never `eval()` anything, and the Risk Officer is pure Python. That foundation is real and tested.

But the product promise — *"connect Alpaca/Zerodha and the agent auto-trades for you"* — is **not implemented yet**, and the deterministic safety chain has one serious hole at exactly the moment it matters most: live order placement.

| Area | Verdict |
|---|---|
| Architecture rule (agents propose / code disposes) | ✅ Holds — verified, no violations found |
| Risk engine (14 named rules, ordered, first-veto-wins) | ✅ Solid, well-tested (~1,400 lines of tests) |
| Backtester + 5 reference strategies | ✅ Exists, shares live risk/sizing code |
| **Execution-time risk re-check (live path)** | 🔴 **Hollow context — most rules physically can't fire** |
| **Order persistence / audit chain at execution** | 🔴 **Orders never written to DB; PDT ledger can never populate** |
| **Auto-approval / server-side execution** | 🔴 **Doesn't exist — every trade requires a manual tap** |
| **Daily cron → user notification** | 🔴 **Cron proposals never push; they expire in 15 minutes, unseen** |
| **Circuit breaker data source** | 🔴 **Watches a mock poller with fake equity, fixture user only** |
| LLM determinism controls (temperature, timeout, schema) | 🟡 None set — unbounded variance, silent fallbacks |
| Council input data | 🟡 100% synthetic features, even in the production cron |
| Paper trading realism | 🟡 Fills at the proposal's own assumed price; in-memory book |
| Reflection loop | 🟡 Built, never scheduled — confidence priors never move |
| Observability (Sentry, conversation logs, prompt versioning) | 🟡 Not wired |
| Docs vs code (Zerodha, LiteLLM proxy) | 🟡 CLAUDE.md/PLAN.md are stale relative to the codebase |

**Bottom line:** the system is an excellent *propose-and-audit* machine today. It is not yet a *trading* machine. The five breaks in §3 are the gap between the two, and four of them are deterministic-plumbing work, not agent work.

---

## 2. What's working — architecture compliance

Verified across `apps/agents/`, `packages/engine/`, `packages/broker/`:

- **7-node LangGraph council** ([graph.py](apps/agents/trading_agents/graph.py)): Router (Haiku) → Technical/Fundamental/Macro analysts (Haiku/Sonnet) → Selector (Haiku) → Drafter (Sonnet) → **Risk Officer (no LLM at all** — [risk_officer.py](apps/agents/trading_agents/nodes/risk_officer.py)). Selector HOLD short-circuits to END. A plain-asyncio fallback mirrors the graph when LangGraph isn't installed.
- **Sizing is never the LLM's call.** The Drafter's qty is ignored; sizing is delegated to `engine.sizing.atr_position_size` — a deterministic vol-targeted formula ([atr.py](packages/engine/engine/sizing/atr.py)): risk dollars = `risk_pct × equity × confidence`, qty = risk dollars / (ATR × stop multiple), clamped to min/max % of equity.
- **14 named risk rules** in [packages/engine/engine/risk/rules/](packages/engine/engine/risk/rules/), evaluated in a fixed order (catastrophic → sizing trims → aggregate exposure), first veto wins: `drawdown_halt`, `forbid_short_phase_0`, `lot_size_block`, `min_council_confidence`, `min_specialist_avg_score`, `pdt_block`, `mis_square_off_block`, `max_open_positions`, `max_position_pct_trim`, `derivative_notional_cap`, `correlation_cap`, `sector_concentration`, `single_name_concentration`, `wash_sale_warning`. Every veto carries a named `veto_rule` for the audit log.
- **No boundary violations.** Zero broker imports in agent code; zero direct Alpaca/Kite HTTP calls outside `packages/broker`; no `eval`/`exec` of LLM output; all LLM JSON is parsed then validated against allow-lists (verdict ∈ BUY/SELL/HOLD, strategy ∈ registry).
- **Backtester shares the live code.** The event-driven backtester's `RiskGate` calls the same `engine.risk.evaluate` and the same sizing the live path uses, and 5 hand-coded reference strategies exist (SMA crossover, RSI mean-reversion, momentum, breakout, vol-regime switch).
- **Reflection is properly bounded.** The reflection agent can only nudge per-strategy confidence ±0.10 per cycle, double-clamped to [0.05, 0.95] — LLM output adjusts a prior, never a trade.
- **Audit-first schema.** `agent_decisions` captures the full council run (scores, narratives, proposal JSONB, risk verdict, user response, fills); `ghost_outcomes` tracks counterfactual P&L for vetoed/declined/expired picks; veto ledger, calibration scorecard, and decision-timeline endpoints all read from it.

This is the part of the codebase to protect. Nothing below requires weakening it.

---

## 3. The auto-trade loop — five verified breaks

### Intended flow vs. what actually runs

```
INTENDED (PLAN.md / CLAUDE.md)
  market calendar → real features → council → risk gate → push to user
   → approve (or auto-window) → execute via broker → persist order + fills
   → reconcile real account → circuit breaker on real drawdown
   → EOD ghost eval → reflection updates priors

ACTUAL (today)
  cron (any day, incl. holidays) → SYNTHETIC features → council (mock LLM
   unless key set) → risk gate (real ctx) → row in DB … ✖ no push
   → proposal expires in 15 min unseen
   → IF user happens to open app and taps approve → mobile calls execute
   → risk re-check with EMPTY positions/halt/PDT … ✖ most rules can't fire
   → order placed → ✖ never persisted to orders table
   → reconciler ticks against MOCK broker data, fixture user only
   → ghost eval ✓ → reflection: ✖ never runs
```

### Break 1 — Execution-time risk re-check runs on a hollow context (live path) 🔴

**Evidence:** [executor.py:336-354](apps/api/app/services/executor.py) — `_build_risk_context` fetches equity and buying power from the broker, then:

```python
# PDT / drawdown halt / wash-sale state lives in our DB, not the
# broker. Phase 3.5 follow-on wires them; for the smoke we use
# conservative defaults (no halt, no PDT count).
return RiskContext(
    ...
    open_positions=tuple(),  # broker positions DTO → PortfolioPosition mapping is a follow-on
)
```

**Impact:** the comment says "conservative defaults" but empty positions are the *permissive* direction for most rules. At the exact moment a real order is placed:
- `max_open_positions`, `single_name_concentration`, `sector_concentration`, `correlation_cap` **pass trivially** (no positions to count).
- `drawdown_halt` **cannot fire** (no halt state loaded) — the circuit breaker is invisible to the executor.
- `pdt_block` **cannot fire** (day-trade count always 0).
- `forbid_short_phase_0` inverts: with no positions visible, **every SELL is blocked**, including legitimate position exits.

The council-time check (Risk Officer) *does* use a real context via `PostgresRiskContextProvider`. So the first gate is real and the last line of defense is hollow — backwards from what you want, since proposals age between drafting and approval.

**Fix:** reuse the existing `PostgresRiskContextProvider` pattern inside the executor: map broker positions → `PortfolioPosition`, load `circuit_breaker_state` + `pdt_ledger` from the DB, and **fail closed** (refuse to execute) if context assembly fails. This is pure deterministic plumbing — no agent changes.

### Break 2 — Orders are never persisted on execution 🔴

**Evidence:** [executor.py:192](apps/api/app/services/executor.py) returns `OrderResponse(id=str(uuid.uuid4()), ...)` with the module docstring stating: *"Real Postgres `orders` persistence — for the Postgres backend this lands in a follow-on; today we return the in-memory `Order` DTO."* No fill polling exists ("Phase 4 hardening").

**Impact:**
- The audit chain `agent_decision → order → order_fills → realized P&L` — the core compliance story of the product — is broken at its most important link. The tables exist (migration 0001); nothing writes them from the live path.
- `pdt_ledger` derives from orders, so it can never populate — which means **even after Break 1 is fixed, `pdt_block` would still have nothing to count.** These two breaks compound.
- `agent_decisions.fill_qty / fill_avg_price / realized_pnl` stay empty → strategy performance, review queue, and calibration scorecard are computing over incomplete data for any executed trade.

**Fix:** in `execute_proposal`, write the `Order` row (status `pending_submit` → broker response) inside the same transaction scope as the decide() call, link `agent_decision_id`, and add a fill-poller job that updates `orders`/`order_fills` and appends to `pdt_ledger` on same-day round trips.

### Break 3 — Auto-approval doesn't exist; nothing executes server-side 🔴

**Evidence:** `approval_mode` appears exactly once in the entire API — hardcoded `"ask"` at [postgres_store.py:190](apps/api/app/services/postgres_store.py). The only execution entry point is `POST /api/v1/orders/execute/{proposal_id}` ([orders.py](apps/api/app/routers/orders.py)), called by the mobile app after a manual approve.

**Impact:** CLAUDE.md's v1 scope line — *"Self-approval per trade + auto-window"* — is half-built. The "agent auto-trades for the user" promise is currently: agent proposes → user must notice, tap approve, and the app triggers execution. If the user does nothing, nothing ever trades. There is no worker, no queue, no window check anywhere.

**Fix (this is the product feature):** an `approval_mode='auto'` per-user setting with an explicit auto-window (e.g., market hours), a server-side executor worker that picks up risk-approved proposals inside the window, re-runs risk (with Break 1 fixed), executes with per-trade and per-day notional caps, and pushes a *"the agent bought 12 NVDA"* notification after the fact. Manual mode stays the default.

### Break 4 — Daily cron proposals silently die in 15 minutes 🔴

**Evidence:**
- [daily_cron.py](apps/agents/scripts/daily_cron.py) calls `run_council` directly and then ghost-eval. It never calls the notification service and never executes.
- Push fan-out (`schedule_proposal_pending_notification`) is wired **only** in the API route ([agent.py:99](apps/api/app/routers/agent.py)) — the path the cron does not use.
- Proposals carry `DEFAULT_APPROVAL_TTL = timedelta(minutes=15)` ([runtime.py:40](apps/agents/trading_agents/runtime.py)).

**Impact:** the one scheduled producer of trade ideas (13:15 UTC cron over a 10-symbol watchlist) creates proposals that expire by 13:30 unseen, unless the user coincidentally has the app open. In production this looks like "the agent never does anything." Ghost outcomes then dutifully record them all as `expired` — the regret tiles will mostly be measuring this plumbing gap, not user judgment.

**Fix:** (a) have the cron fan out the push after each approved proposal (import the notification service, or route the cron through the API), and (b) set a swing-trade-appropriate TTL — e.g., expire at market close rather than 15 minutes. 15 minutes is an intraday TTL on a product whose v1 scope is 1–10 day holds.

### Break 5 — The circuit breaker watches fake data, for one fixture user 🔴

**Evidence:** [main.py:101](apps/api/app/main.py) — `Reconciler(poller=MockBrokerPoller(), ..., user_id=_DEFAULT_USER_ID)` with the comment "Phase 0/1 default; Phase 2 swaps to AlpacaBrokerPoller". No `AlpacaBrokerPoller` exists yet in [engine/reconciler/](packages/engine/engine/reconciler/).

**Impact:** the drawdown halt — the system's only deterministic kill-switch, and the thing DESIGN.md gives a persistent acknowledgement-required banner — is currently evaluating synthetic equity. A real account could draw down 10% and the breaker would never know. It also only runs for the fixture user, not per connected user.

**Fix:** implement `AlpacaBrokerPoller` against the existing `BrokerInterface` (equity, positions, day-trade count are already on the protocol), and run one reconciler loop per user with an active broker connection. Combined with Break 1's fix, the breaker becomes real end-to-end: reconciler trips it from real data → executor refuses orders because it loads real halt state.

---

## 4. Determinism audit

Direct answer to "is everything deterministic?": **the disposal side is; the proposal side isn't and never will be — but today its non-determinism is unbounded and occasionally leaks into decisions in unintended ways.**

### Tier 1 — Deterministic and verified ✅

| Component | Why it's deterministic |
|---|---|
| Risk engine (all 14 rules) | Pure functions of `(proposal, context, caps)`; no randomness; `mis_square_off` takes injectable `now_utc` |
| Position sizing | Pure formula; rounding fixed (2dp notional, 4dp prices) |
| Backtester | Same inputs → same fills/vetoes/equity curve; sim broker fills market orders only (loud `NotImplementedError` otherwise) |
| Ghost evaluator | Same decision + same price source + same day → identical `ghost_pnl` |
| Synthetic providers | Hash-seeded per (symbol, day); fully reproducible |
| Mock LLM | Keyed on prompt role line; canned JSON |
| Graph routing | Conditional edges read state dict keys only |
| Cost ledger math | Pure pricing table |

### Tier 2 — Acceptable non-determinism (LLM proposals), but currently unbounded 🟡

The architecture *intends* LLM variance to exist only in proposal content. Fine. But nothing bounds it:

- **No `temperature`, no `top_p`, no seed** on real calls — [llm.py:93-98](apps/agents/trading_agents/llm.py) passes only model/max_tokens/system/messages, so generation runs at the API default (~1.0). The same symbol + identical features can produce a different regime, different analyst scores, and a different strategy on consecutive runs. *Fix: set `temperature=0` (or ≤0.2) explicitly; document the choice.*
- **No timeout** on `client.messages.create()` — a hung API call hangs the council. *Fix: construct `AsyncAnthropic(timeout=...)` or pass per-call timeout.*
- **No structured-output enforcement** — prompts say "Return strict JSON ONLY", parsing is a lenient fence-strip + `json.loads` ([llm.py:114-120](apps/agents/trading_agents/llm.py)). *Fix: tool-use forced schema (or Pydantic validation + re-ask), so malformed output is retried instead of absorbed.*

### Tier 3 — Non-determinism leaking into decision paths (needs correcting) 🔴

1. **Silent neutral fallbacks change decisions.** On any parse failure: Router falls back to `analyst_subset=["technical"]` ([router.py:32](apps/agents/trading_agents/nodes/router.py)), analysts return score 50 / confidence 0.2, Selector/Drafter fall back to HOLD. No retry, no flag on the decision row. A transient formatting hiccup silently produces a different (and unexplained) decision. *Fix: retry once; if it fails again, mark the run `degraded=true` on `agent_decisions` so downstream calibration can exclude it.*
2. **The two risk gates use different definitions of the same trade.** Council gate: real proposal confidence + real last price. Execution gate ([executor.py:357-376](apps/api/app/services/executor.py)): `confidence = conviction_level / 5.0` and `last_price = estimated_notional / qty`. Identical world state can pass one gate and fail the other (e.g., council confidence 0.55 passes `min_council_confidence=0.50`, conviction 2 → 0.40 fails it at execution). *Fix: carry the original `RiskProposal` fields through the DTO so both gates evaluate the same object.*
3. **`wash_sale.py` reads the wall clock internally** (`datetime.now(timezone.utc)` to build the 30-day boundary) instead of taking `now` from context like `mis_square_off` does. Minor today (rule is informational and silent on the Postgres path anyway), but it's the one rule whose output isn't a pure function of its inputs. *Fix: inject `now` via `RiskContext`.*
4. **Silent mock flip in production.** No `ANTHROPIC_API_KEY` (or a blanked one) → mock LLM with only a log warning ([llm.py:61-62](apps/agents/trading_agents/llm.py)). A misconfigured prod cron would happily emit canned MOCK theses into real users' approval inboxes. *Fix: an explicit `AGENTS_REQUIRE_REAL_LLM=1` guard that hard-fails the cron in mock mode.*
5. **Calendar-day, not market-day.** Cron idempotency keys on UTC calendar date; PDT/wash-sale lookbacks use calendar days; the GitHub Actions schedule fires on market holidays. Documented Phase 1.5 deferral, but it belongs on the fix list. *Fix: `pandas_market_calendars` gate at the top of the cron + business-day lookbacks.*

Float-for-money is used throughout the engine (DB columns are exact `Numeric`). Comparisons are broad thresholds with no accumulation, so this is acceptable for now — worth a documented note, not a rewrite.

---

## 5. Data provenance — where every data point actually comes from

Direct answer to "where do our data points come from?": **today, almost everywhere that matters, they come from a hash function.**

| Data point | Source today | Source needed (v1) | Consumer |
|---|---|---|---|
| Technical features (price, ATR, RSI, DMA, volume) | `synthetic_features()` — deterministic hash seed per symbol ([features/synthetic.py](apps/agents/trading_agents/features/synthetic.py)); **the only provider wired, including in the daily cron** | Alpaca IEX daily bars → computed indicators | Analysts, Drafter, **sizing (qty + stops!)** |
| Fundamental features (quality, earnings power) | Synthetic | FMP or similar (PLAN.md §8) — or remove the node's inputs until sourced | Fundamental analyst |
| Macro features (VIX, 10y, DXY, sector RS) | Synthetic | FRED (free) | Macro analyst |
| Ghost-outcome marks | `AlpacaPriceProvider` (real IEX closes) if `ALPACA_API_KEY` set, else synthetic walk ([prices/select.py](packages/engine/engine/prices/select.py)) | Same (already real-capable) | Ghost evaluator, regret tiles |
| Account equity / positions (reconciler) | **`MockBrokerPoller` — fake** ([main.py:101](apps/api/app/main.py)) | `AlpacaBrokerPoller` per user | Circuit breaker, snapshots |
| Account equity / positions (executor) | Real broker call, but positions discarded (Break 1) | Full mapping + DB halt/PDT state | Execution risk gate |
| Paper fill prices | The proposal's own `estimated_notional / qty` — **the agent grades its own homework** | Real last quote ± slippage model | Paper P&L, Phase 4 validation |
| LLM responses | Anthropic SDK direct (or mock) — **not the LiteLLM proxy the stack table specifies** | Decide: either wire LiteLLM proxy or update CLAUDE.md | Council nodes, cost ledger |
| News / sentiment | Nothing | Out of v1 unless prioritized | — |
| Bar storage / TimescaleDB hypertables | Nothing stored | Needed once real bars flow (backtests, features) | Backtester, feature pipeline |

Two compounding consequences worth stating plainly:

1. **Position sizing is currently fiction.** Qty and stop-loss derive from synthetic `last_price`/`atr_14`. The first real-money order would be sized off numbers that have nothing to do with the actual market.
2. **Phase 4 "paper validation" as currently wired would validate nothing.** Synthetic features → (likely mock) LLM → paper fills at assumed prices, on an in-memory book that resets on restart. The decision data accumulating in `agent_decisions`/`ghost_outcomes` is structurally great and substantively meaningless until real features + real marks flow.

---

## 6. Where workflows fit

Direct answer to "where can workflows be used?": **the daily trading pipeline is the workflow.** It currently exists as scattered fragments — a GitHub Actions cron, a FastAPI lifespan thread, a manual mobile step, and a CLI nobody schedules. It should be one explicit, resumable, observable state machine:

```
market-calendar gate → account sync (per user) → feature compute
  → council (per symbol; skip-if-decided) → push notify
  → approval wait │ auto-window execute → persist order + poll fills
  → reconcile → EOD: ghost eval → reflection → daily ops report
```

Concrete gaps this closes:

| Job | Today | Should be |
|---|---|---|
| Council run | GH Actions cron, runs on holidays, fixture user | Calendar-gated stage, per user |
| Proposal notification | Only via API route | Stage after every approved proposal |
| Execution | Manual mobile tap only | Auto-window stage (Break 3 fix) |
| Fill polling | Doesn't exist | Post-execution stage |
| Reconciler | Lifespan thread, mock data, single instance | Per-user stage / worker |
| Ghost eval | Tacked onto cron (✓ works) | EOD stage |
| **Reflection** | **CLI exists, never scheduled — priors frozen at 0.5 forever** | EOD stage after ghost eval |
| Proposal-expiry sweep | Implicit filter | Explicit stage that records `expired` + reasons |
| Ops alerting | Nothing (a failed cron is silent; mock-mode-in-prod is a log line) | Failure/degraded-mode alerts per run |

Recommendation: don't reach for Temporal yet. A single worker process owning all scheduled jobs (APScheduler or a simple asyncio loop driven by a `pipeline_runs` state table) gets you resumability and a per-stage audit row with the stack you already have. The LangGraph council is already the right shape for the *intra-decision* state machine — this is about the *inter-stage* orchestration around it.

(Dev-side, secondarily: this audit itself was a fan-out/verify multi-agent workflow; the same pattern works as a recurring CI review on the risk-engine and executor paths.)

---

## 7. Prioritized roadmap

### P0 — Close the deterministic execution chain (before any real-money order)

1. **Real `RiskContext` in the executor** — map broker positions to `PortfolioPosition`, load `circuit_breaker_state` + `pdt_ledger` (reuse the `PostgresRiskContextProvider` pattern), **fail closed** on context-fetch failure. *(Break 1)*
2. **Persist orders + fills** — write `orders` row linked to `agent_decision_id` at execution; add fill polling; populate `pdt_ledger` from same-day round trips. *(Break 2)*
3. **`AlpacaBrokerPoller` + per-user reconciler** — circuit breaker watches real equity for every connected user. *(Break 5)*
4. **Cron → push + sane TTL** — notify on every cron proposal; TTL = end of market day, not 15 minutes. *(Break 4)*
5. **Real technical features** — Alpaca IEX daily bars → ATR/RSI/DMA provider wired into `run_council` and the cron; sizing finally sees real prices. Add `AGENTS_REQUIRE_REAL_LLM` + a "features must be real" guard for production runs.

### P1 — Agent hardening (determinism + auditability)

6. `temperature=0`/explicit timeout on all LLM calls; one retry on parse failure; `degraded` flag on fallback decisions; structured outputs via forced tool-use schema.
7. Unify the two risk gates: carry the council's `RiskProposal` (confidence, last_price) through the DTO so execution re-checks the same object.
8. Inject `now` into `wash_sale`; implement its Postgres `recent_losing_closes` (TODO at [postgres_context.py:103](packages/engine/engine/risk/postgres_context.py)); market-calendar gating + business-day lookbacks.
9. **Schedule reflection** (EOD, after ghost eval) — it's built and tested; it just never runs.
10. Conversation logging (prompts/responses → S3 or DB, per PLAN.md §8) + prompt content-hash recorded on each `agent_decisions` row — without this there's no eval dataset and no way to attribute behavior changes to prompt changes.

### P2 — Productize auto-trade + operations

11. **`approval_mode='auto'` + auto-window server-side executor worker** with per-trade/per-day notional caps and post-trade push — this is the actual product promise. *(Break 3)*
12. Paper realism: fill at real last quote ± slippage model; Postgres-backed paper book (survives restarts) so Phase 4 numbers mean something.
13. Wire Sentry (dependency already declared), structured logs, cron-failure + mock-mode-in-prod alerting; basic API rate limiting.
14. Truth-up docs vs code: CLAUDE.md/PLAN.md say Zerodha is out of v1 and LLM calls go through LiteLLM — both false in the codebase. Either change the code or change the docs. Delete dead legacy dirs (`backend/`, `frontend/`, root `mobile/` — verified unreferenced). Minor: `_already_decided_today` scans all decisions (O(n) forever-growing) — add a date-indexed query.

---

## 8. Open questions (yours to decide, not mine)

1. **Zerodha in v1 — yes or no?** It's fully built (broker impl, OAuth routes, India risk rules) despite the docs excluding it. If yes: note that Zerodha has **no paper mode** — every Zerodha order is real money behind a single global `LIVE_TRADING_ENABLED` env var. That deserves a per-user, per-session consent step, not just an operator env flag. If no: quarantine it behind a feature flag so v1 surface area stays US-only.
2. **Auto-window semantics** — what does the user actually configure? (window hours, max notional per trade/day, symbol allow-list, halt-on-first-loss?) This shapes the P2 worker and the mobile settings screen.
3. **When to flip the cron to real LLM + real features** — both flips cost money (Anthropic + data) and both are currently silent-fallback. Recommend flipping them together with the P0 guards, so "running" always means "running real."
4. **Wash-sale**: keep informational-only, or promote to a blocking rule once the Postgres path is implemented?

---

*Audit fidelity note: §3 and §4 claims were verified line-by-line in source during this audit ([executor.py](apps/api/app/services/executor.py), [daily_cron.py](apps/agents/scripts/daily_cron.py), [llm.py](apps/agents/trading_agents/llm.py), [main.py](apps/api/app/main.py), [agent.py](apps/api/app/routers/agent.py), [approvals.py](apps/api/app/routers/approvals.py), [store.py](apps/api/app/services/store.py)). Inventory-style claims (rule list, schema, screens, endpoints) come from exhaustive exploration passes over `packages/engine`, `packages/broker`, `apps/api`, `apps/agents`, `apps/mobile`, and `infra/migrations`.*
