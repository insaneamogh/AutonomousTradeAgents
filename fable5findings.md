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

---

## Technical debt & follow-ups (as of the 2026-08-25 session)

Everything below was found and *deliberately not fixed* during today's Railway/
bug-fix/cleanup session (full narrative in the Entries below) — flagged rather
than done, either because it's out of this session's scope or because the
investigation that found it came back lower-confidence-of-a-clean-fix. Listed
here once, in one place, instead of only as inline asides inside each entry.

1. ~~**`eslint` and `jest` are declared but non-functional, repo-wide.**~~
   **Resolved 2026-08-25 (`938821c2`).** Root `eslint.config.mjs` (flat
   config) + `jest-expo`/`ts-jest` per package now actually run; see the
   build-log entry below for exact lint/test counts left open by design.
2. **3 mypy nits in `apps/api/app/services/auth/postgres_auth_store.py`**
   (lines ~129, 181, 237) — a `User | None` assigned into a
   narrower-inferred `User` slot across two branches, and `.rowcount` read
   off a `Result[Any]`. Runtime-safe (guarded by an `assert`/the calling
   code's own handling), not bugs — just newly *visible* after this
   session added the missing `py.typed` marker to `engine`/`broker`/
   `trading_agents`, which had been silently hiding them.
3. ~~**Store-selector duplication not collapsed.**~~ **Resolved
   2026-08-25 (`9d938eef`).** Six modules under `apps/api/app/services/*/`
   (`auth_store`, `broker_store`, `notification_store`, `review_store`,
   `store`, `watchlist_store`) each hand-rolled the identical
   in-memory-vs-Postgres singleton-selector pattern. Collapsed into
   `app/core/singleton.py::LazyEnvSingleton`. `broker_store.py`'s second
   singleton (`PendingOAuthCache`) turned out exactly as anticipated here
   — it doesn't fit the helper's shape (no Postgres/Mock split), so it
   kept its own small hand-rolled wrapper, reset alongside the
   shared-helper-based store rather than forced into the abstraction. See
   the build-log entry below for the full shape.
4. **`executor.py` and `order_store.py` still mix concerns internally.**
   Moving them into `services/orders/` (this session) didn't touch their
   insides. `executor.py` has five separable jobs in one file (live-trading
   gate, risk re-evaluation, broker placement, a second parallel
   paper-mode execution path, response-DTO assembly). `order_store.py` has
   four (generic utils, a risk-input read path that's arguably
   risk-domain not persistence, a compare-and-swap concurrency primitive,
   and the order-row CRUD the filename actually promises). Splitting
   either is a deeper behavior-preserving refactor than a file move —
   real, but higher risk, left for a dedicated pass.
5. ~~**`watchlist_store.py`'s new home (`services/council/`) is a
   judgment call, not a clean fit**~~ **Resolved 2026-08-25
   (`9d938eef`).** It had no real coupling to anything else in `council/`
   or any other existing bucket, so it moved to a new `services/watchlist/`
   bucket of its own — the better home this note invited revisiting for.
6. **Product-level open questions are unchanged** — see §8 above
   (Zerodha in/out, auto-window semantics, when to flip cron to
   real-LLM/real-data, wash-sale promotion). Still the user's calls, not
   touched this session.
7. **`docs/architecture/`, `docs/reference/`, `docs/runbooks/` are still
   empty** (`.gitkeep` only) — harmless scaffolding, not a problem, just
   noting they exist unused.
8. **Process note, not a code issue:** this session's 10 commits carry
   `Co-Authored-By: Claude Fable 5` — the wrong model name (the assistant
   is Sonnet 5). Caught by the user mid-session; left uncorrected in
   already-pushed history per their choice (fixing forward, not rewriting
   `main`). Future commits in this repo should say `Claude Sonnet 5`
   unless whichever model is running at the time is actually different.

---

# Build log

> Everything below the audit (§1–§8) is the running history of what's been built on top of it. **Per CLAUDE.md: every commit appends an entry here** so the next agent can resume without re-deriving context. Newest first. Don't edit the audit above; append here.

## Roadmap status (from §7)

| Item | Status | Where |
|---|---|---|
| P0.1 Real `RiskContext` at execution (fail closed) | ✅ Done | `executor._build_risk_context` + `engine.risk.load_db_risk_state` |
| P0.2 Persist orders + fills, populate `pdt_ledger` | ✅ Done | `order_store.py`, `order_sync.py` |
| P0.3 `AlpacaBrokerPoller` + per-user reconciler | ✅ Done | `reconciler_fleet.py` (UserBrokerPoller) |
| P0.4 Cron → push + EOD TTL + market-calendar gate | ✅ Done | `daily_cron.py`, `runtime` (TTL), `engine.features.market_calendar` |
| P0.5 Real technical features + REQUIRE guards | ✅ Done | `engine.features`, `AGENTS_REQUIRE_REAL_DATA/LLM` |
| P1.6 temperature/timeout/retry/degraded | ✅ Done | `llm.py` (`complete_json`) |
| P1.7 Unify the two risk gates | ✅ Done | proposal carries risk fields through the DTO |
| P1.8 Inject `now` into wash_sale; Postgres `recent_losing_closes` | 🟡 Partial | `now` plumbing in place; Postgres losing-closes still `()` (informational-only) |
| P1.9 Schedule reflection | ✅ Done | `daily_cron` runs it inline after ghost eval |
| P1.10 Conversation logging + prompt provenance | ✅ Done (via Langfuse) | `tracing.py` — per-agent trace replaces the S3 idea |
| P2.11 `exit_mode=agent` + server-side executor worker | ✅ Done | approvals execute server-side; `position_manager.py` |
| P2.12 Paper realism (real Alpaca paper account) | ✅ Done | paper routes through the user's Alpaca paper connection |
| P2.13 Sentry / structured logs / rate limiting | ❌ Open | needs a Sentry DSN; not wired |
| P2.14 Docs truth-up + delete legacy dirs | 🟡 Partial | legacy dirs untracked + RUNBOOK updated; CLAUDE.md/PLAN.md Zerodha/LiteLLM drift still to reconcile by the user |

**Scope decisions locked this build:** instruments = US stocks/ETFs, ~~options/futures out~~ (**updated 2026-08-28**: options Phase A — long calls/puts only, no spreads/assignment — now in scope; see `CLAUDE.md` and the entry at the top of the log below; futures still out); exits = Alpaca **bracket** (broker-enforced stop/target) **+** agent early-exit/time-stop for `exit_mode=agent` (options Phase A has no broker bracket available — see the options build-log entries once they land); entries always human-approved; Zerodha stays dark for v1.

## Entries

### 2026-08-28 — `a4209592` test(agents): cross-track end-to-end proof + live UI verification — Phase A complete

Closes out options trading Phase A. Two pieces:

**The capstone cross-track test.** New
`test_run_council_options_proposal_reaches_evaluate_option_and_is_approved`
in `test_council_mock.py`: mocks the chain fetch to return one liquid
contract (the exact fixture the options-drafter unit tests already
validated in isolation), runs the REAL `drafter_node` through the REAL
`run_council()`/`risk_officer_node` path, and asserts the result comes
back `risk_approved` with no veto. This is the one thing neither options
track could prove on its own — each tested its own half against a
hand-built fixture — and it was structurally impossible to prove until
both tracks plus the `risk_officer_node`/`options_trading_level` glue
fixes existed together. Passed on the first run: every seam this session
spent effort reconciling by hand actually agrees.

Also hardened the two `instrument_preference` tests added earlier the
same day: they'd asserted a specific `final_action` ("BUY") for NVDA, but
synthetic features are hash-seeded per (symbol, day) — the same reason
`test_mock_council_produces_buy_proposal_for_nvda` already uses a
deliberately loose assertion. Replaced with the same structural-spy proof
(was the chain fetch called at all) so these can't flake on a different
calendar date.

**Live UI verification**, closing the disclosed gap from the sizing/UI
track's own report ("no live simulator/browser session survived far
enough to screenshot an actual option-shaped proposal"). Confirmed:
this dev environment has no real Alpaca keys configured, so a genuinely
live options chain fetch — and therefore a real option-shaped Positions/
PickDetail row — isn't reachable here regardless of code; that part of
the gap stays open until real credentials exist somewhere this can run.
What IS reachable without a backend option feed — the watchlist equity/
option toggle — was live-verified instead: started `dev-api` +
`mobile-web`, logged in via the dev-token flow, reached the toggle via
Settings → "Manage the agent watchlist" (JS-dispatched clicks, since the
Browser pane wasn't compositing frames for coordinate/ref-based input
this session), and read real computed styles directly. Confirmed the
exact fix (`border-cta bg-cta/10 dark:border-cta-dark dark:bg-cta-dark/10`,
not the original bug's `accent-primary`) is present, the toggle's
selected state switches correctly between the two options, and — using
the app's own in-page theme control, since emulating `prefers-color-scheme`
alone didn't move this app's explicit light/system/dark theme state —
the `dark:` variants resolve to real, distinct, sensible dark-mode colors
rather than silently reusing the light-mode value.

Verified: full combined suite 724→725 passed, 9 skipped; mypy delta zero
(9 pre-existing errors, unchanged count); ruff clean.

**Phase A (options trading, long calls/puts only) is now fully merged,
tested, and live-verified end to end.** Next: the hackathon MCP server.

### 2026-08-28 — `e1ddf7ef` feat(agents,api): wire instrument_preference to a real caller

The last piece of Phase A's cross-track glue: both options tracks
deliberately left this to me (the sizing/UI track's own words: "a product
call, not ours to make"). `run_council()` gains
`instrument_preference: Literal["equity","option"] | None`, threaded into
the initial `CouncilState` for `strategy_fit_node` to read (still gated
by `ALLOW_OPTIONS` — neither flag alone is enough). `AgentRunRequest`
(Python + shared-types TS) gets the matching field, forwarded through
`_execute_council()` — covers both `/agent/run` and `/agent/run/start`,
which share that one call site. Deliberately stopped here: watchlist/
cron-driven auto-wiring (reading each item's own `asset_class`
automatically) is a disclosed follow-up, not attempted now — a manual
trigger is enough to demo the feature and is real scope creep otherwise.

New test proves the kwarg reaches `strategy_fit_node`/`drafter_node`
structurally (a call-count spy on the chain fetch), not by inferring it
from a HOLD-reason string — tried that first and it was a genuine dead
end: the top-level result's `risk_reason` is the same generic
"No proposal — HOLD." for every proposal-less HOLD regardless of why, so
it can't distinguish an options no-candidates HOLD from an ordinary
equity strategy-fit HOLD. Caught before it became a false-confidence test.

Verified: full combined suite 722→724 passed, 9 skipped; mypy delta zero
(two automated baseline-comparison methods — `git show`-piped-to-stdin
and a scratch `git worktree` with its own venv — both proved unreliable
here for cross-module import resolution, so this one was confirmed by
careful manual line-count reconciliation against each edit's exact
insertion size instead); ruff clean.

**Phase A of the options-trading plan is now fully merged.** Next: a
live visual check of the option-aware UI (now finally reachable) and the
plan's cross-track end-to-end verification pass.

### 2026-08-28 — `6c79d573`+`8a71deb3`+`39f9f78f`+`0f84435d`+`6e607c10`+`8378cd1f` feat(options,agents,ui): sizing/contract-selection/agent-council/UI layer for Phase A

The other parallel options-trading track — reviewed with the same
scrutiny as the broker/risk one above, and this pass is why the plan's
"cross-track end-to-end verification" step existed: it caught a real bug
neither track could have caught testing its own half in isolation.

**`6c79d573` — deterministic contract selection + premium sizing.** New
`packages/engine/engine/options/{selection,sizing}.py`. `select_contract()`
runs a 5-stage filter-then-tiebreak (contract type by thesis direction →
21-45 DTE window, deliberately narrower than and independent of
`RiskCaps.options_min_dte`/`max_dte` — one is a selection heuristic, the
other an authoritative re-verified veto, intentionally not the same
constant reused twice → delta band by conviction → liquidity floor →
IV presence) over a chain snapshot, returning one `OptionLegDetails`
(`action="buy_to_open"` hardcoded, matching Phase A) or a named HOLD
reason with per-stage funnel counts. `options_position_size()` is
premium-at-risk floor division, replacing ATR entirely for this path;
deliberately does not self-enforce the portfolio-aggregate cap (the risk
engine's job, mirroring `short_unbounded_loss_cap`'s own precedent).
77 new tests. Resolved an `__init__.py` add/add conflict against the
broker/risk track's own package init at cherry-pick time — clean union,
both halves complementary.

**`8a71deb3` — wired into strategy_fit/drafter + options_context.**
`strategy_fit_node` stamps `instrument="option"` only when `ALLOW_OPTIONS`
AND a per-run `instrument_preference` are both set and a strategy actually
won — every existing return path unchanged. `drafter_node`'s options
branch forces `side="BUY"` unconditionally, even for a bearish thesis
(which buys a PUT, never sells anything to open) — confirmed load-bearing
by reading `engine.risk.rules._short.opens_short` myself: it fires on
`Side.SELL` alone, and an options proposal's `stop_price=None` would
otherwise look exactly like the "short with no stop" case
`short_requires_stop` exists to catch. No chain / no liquid contract /
zeroed sizer ever falls back to equity sizing — each is a named HOLD.
Caught its own real bug before it shipped: `runtime.py::_to_proposal_dto()`
would have silently dropped every option field at the JSONB-persistence
boundary (Pydantic ignores unmapped keys) — the identical bug class the
short-selling work spent 5 commits chasing — fixed in the same commit,
not left for later. New `OptionsContextProvider`/`MinimalOptionsContextProvider`
wired into `RealFeatureProvider`'s existing concurrent `_optional_blocks`
gather. 11 new tests, including a cross-boundary regression pinning that
an options proposal never satisfies `short_requires_stop`.

**The one real bug this review caught, fixed in a follow-up commit
(`8378cd1f`)**: the options drafter set `order_type="MARKET"` and never
wrote a `limit_price` key into the proposal dict at all. The broker/risk
track's executor (reviewed and merged earlier the same day) forces every
options order to `LIMIT` regardless of what the proposal says, and reads
`proposal.limit_price` straight into the broker layer's `LimitOrderRequest`
with **no None-guard** — unlike its `STOP`/`STOP_LIMIT` siblings, which do
raise on a missing price. Every real options order would have reached
Alpaca as a limit order with no limit and failed outright. Neither track
could have caught this testing its own half in isolation — exactly the
failure mode the plan's cross-track verification step exists for. Fixed:
`order_type="LIMIT"`, `limit_price=ask` (already computed and in scope
two lines above for `estimated_notional`). New assertions in the existing
long-call test regression-guard both fields explicitly.

**`39f9f78f` — watchlist `asset_class` widening.** `String(10)` was
already wide enough (no migration). `Literal["equity"]` →
`Literal["equity","option"]` at the schema/store/router/TS layers;
`WatchlistStore.add()` now actually persists the real value instead of
hardcoding `"equity"`. Resolved a second add/add conflict in
`approvals.py` at cherry-pick time — both tracks had independently added
the identical 6 option-snapshot fields; kept one copy.

**`0f84435d`** — a 2-line ruff cleanup, no behavior change.

**`6e607c10` — option-aware UI.** Mobile `pick/[id].tsx`/`positions.tsx`,
desktop `Positions.tsx`/`PickDetail.tsx`, and a mobile watchlist
equity/option toggle — all reusing existing primitives (`DirectionPill`,
`Pill`/`PlanCell`/`CheckRow`), zero new component vocabulary, every
branch keyed off `isOption`/`contractType` rather than `side` (always
`"BUY"` for an option). Honestly disclosed limitation: no live option
proposal could reach the UI to screenshot yet (that needed the
`limit_price` fix above plus the `instrument_preference` wiring still to
come), so light/dark coverage was verified by auditing className pairs
instead of a live render — reasonable given the actual blocker, followed
up below.

**Verified independently after cherry-pick** (2 conflicts resolved as
above): full combined suite 667→722 passed, 9 skipped (+55 new tests,
0 regressions, exactly matching the claimed count); ruff clean; mypy
delta zero on every file this track touched (confirmed via `git show`
baseline comparisons, file by file).

**Left open, now unblocked:** wiring `run_council()`'s `instrument_preference`
to a real caller (deliberately left to me by this track's own choice —
"a product call," not theirs to make) so the feature is reachable outside
a hand-built test state, and a live visual check of the option-aware UI
now that a real option proposal can actually reach it. Both next.

### 2026-08-28 — `6dd0e96a` fix(engine): populate options_trading_level end-to-end at council time

Second connective-tissue fix, same reasoning as the one below it: neither
`MockRiskContextProvider` nor `PostgresRiskContextProvider` ever
populated `RiskContext.options_trading_level` — a different code path
from the executor's `_build_risk_context` (already fixed by the
broker/risk track below). Without this, `options_level_insufficient`
would veto every options proposal unconditionally at council time,
regardless of the real account's approval tier, since `None` never
clears `>= caps.options_min_trading_level`.

Mirrors exactly how `account_equity`/`buying_power` already flow through
this same reconciler → snapshot → provider pipeline: `AlpacaBrokerPoller`
now also calls `broker.get_options_trading_level()`; the value rides
`RawAccountState` → a new nullable `positions_snapshot.options_trading_level`
column (migration `0015`, chained after `0014`) →
`PostgresRiskContextProvider` reads it back. `MockRiskContextProvider`/
`MockBrokerPoller` both default to 3 (Alpaca's "spreads + long/short
singles" tier, per `docs/OPTIONS_PLAN.md`'s live-account check) instead
of `None`, so mock-mode/CI exercises the options path without extra
wiring, while staying overridable to test the insufficient-level veto.

Also closed, while in the same files: the broker/risk track's own
disclosed follow-up — `AlpacaBrokerPoller` now threads `is_option`/
`multiplier` onto `PortfolioPosition` too, and the snapshot round-trip
(`write_snapshot`/`_parse_positions`) carries both — the reconciler's
slower-moving position-display path is now multiplier-aware, matching
the live risk-gate path the executor already had right.

New: 3 tests in `test_reconciler.py`, 3 in new
`test_risk_context_options.py`. Verified: full combined suite 661→667
passed, 9 skipped (unchanged); migration chain resolves; ruff clean;
mypy delta zero (confirmed via `git show <commit>:<file> | mypy --stdin`
baseline comparisons against the pre-fix commit — deliberately not
`git stash`, after a `git stash`/`pop` mid-review popped an unrelated,
long-forgotten stash entry from earlier in this same session and had to
be cleanly unwound; see the entry below for that incident).

### 2026-08-28 — `9751cba0` fix(agents): thread is_option/OptionLegDetails into the live risk-gate node

Connective-tissue fix between the two options-trading tracks (below),
done directly rather than delegated, since it's the one file both
tracks deliberately avoided touching to not conflict with each other.
`risk_officer_node` built `RiskProposal` from `state["proposal"]` with
zero awareness of the option fields the Drafter's options branch
writes — meaning `RiskCaps.options_disabled` (the fail-closed master
switch) was never consulted for a real options proposal in the live
graph, and the entire options risk-rule package below would have been
structurally unreachable. New `_option_details_from_proposal()`
rebuilds `OptionLegDetails` from the persisted proposal dict, mirroring
the same "every field a rule needs must already be in the proposal"
contract the executor's own re-risk-check follows. Purely additive —
an equity proposal (no `is_option` key) is completely unaffected.

New `apps/agents/tests/test_risk_officer_options.py` (3 tests): equity
unchanged, full options fields correctly rebuilt, minimal options
fields default safely to `None` rather than crashing. Verified:
`apps/agents/tests` 77→80 passed, 1 skipped (unchanged); mypy/ruff
clean.

### 2026-08-28 — `2f3277b4`+`7acec41b`+`70db7a9d` feat/fix(options,broker,orders): broker/risk/execution layer for Phase A

The larger of the two parallel options-trading tracks, reviewed with
high scrutiny (real order execution + risk-gating) before merge — read
every diff directly, verified the P&L multiplier math by hand against
the commit's own worked example, independently re-ran every test suite
from a clean cherry-pick.

**`2f3277b4` — the risk-rules package + dispatch.** New
`packages/engine/engine/options/` (11 rules + `evaluate_option()`
orchestrator + `to_risk_proposal()`, the one sanctioned `RiskProposal`
constructor — confirmed it takes an already-built `OptionLegDetails`
rather than a competing intermediate type, exactly matching what the
other track was told to expect, so the two tracks' interfaces line up
with no reconciliation needed). `engine.risk.engine.evaluate()` diverts
any `is_option` proposal to `evaluate_option()` with a full early-return
right after `drawdown_halt`, structurally excluding options from every
equity-only rule rather than trusting each one to self-gate correctly.
Verified by hand, not just by reading the docstring: every rule with an
open/close distinction (`min_dte`, `expiry_day_entry`, `illiquid_contract`,
etc.) actually self-gates on `option.action != "buy_to_open"` *inside its
own function body* — not merely in the orchestrator's audit-trail
bookkeeping — which is what makes a close genuinely always possible
rather than accidentally vetoable. Includes a defensive guard I hadn't
asked for and like: `evaluate_option()` fails closed with a named
`options_malformed_proposal` veto if a hand-built proposal somehow has
`is_option=True` but `option=None`, rather than trusting every rule
below to independently handle that case.

Test highlight: `test_options_proposal_never_reaches_equity_only_rules`
patches all 6 equity-only rules with call-counting spies and asserts
zero calls (not just "happened to pass") for a deliberately-absurd
options proposal — the dispatch is proven structural, not coincidental.
42 new tests.

**`7acec41b` — Alpaca broker options support.** `BUY_TO_OPEN`/
`SELL_TO_CLOSE` → `(OrderSide, PositionIntent)` mapping (Alpaca keeps
these as two separate request fields); a bracket-on-options guard
(`OptionBracketNotSupportedError`) as the last line of defense before
an opaque 422, since Alpaca's `OrderClass` only allows `simple`/`mleg`
for `us_option`; `get_options_trading_level()` added to
`BrokerInterface` (Alpaca real, Zerodha always `None` — no options
concept in Kite) so `RiskContext.options_trading_level` has an actual
data source. `paper=True` hardcoded in the two new contract-lookup
functions matches the existing `lookup_asset` precedent exactly (not a
new gap — checked). 8 new tests.

**`70db7a9d` — options-aware execution, close, sweep, and P&L math.**
The one genuinely load-bearing finding: Alpaca cannot bracket a
single-leg option order at all, so the live-trading gate built for the
short-selling feature ("an unprotected live order is refused, not
silently demoted") would have refused every live options order,
always — that gate's premise (a broker bracket was an available
alternative) never held for options. Now: `use_bracket` is forced
`False` for options and the refusal check doesn't apply to them at all,
surfaced instead as an informational flag
(`options_agent_managed_exit_no_broker_bracket`) — a confirmed,
deliberate Phase A trade-off, not a silent gap. Options are always
priced/executed as `LIMIT` (never `MARKET`), which also correctly
falls out as `TimeInForce.DAY` (never `GTC`) with no separate branch
needed, since that ternary was already keyed off `use_bracket`.
`_close_position` gains an options branch (always `SELL_TO_CLOSE`,
always `LIMIT`) and correctly keeps `orders.side` as plain `"SELL"`
rather than the 13-character `SELL_TO_CLOSE` broker-wire value, which
would have overflowed the DB column's 4-char width. New
`sweep_expiring_options_for_user()` force-closes any option position
within `options_expiry_sweep_dte`, wired into the same reconciler tick
as the existing time-stop/signal closer.

Three P&L spots were off by the multiplier entirely (a $2.50→$3.00 move
on 1 contract computing as $0.50, not $50) and one was a genuine unit
mismatch beyond a missing factor (a multiplier-scaled `market_value`
subtracted directly against a per-contract `avg_entry_price`) — verified
the corrected formulas by hand against the commit's own worked example
and confirmed they resolve to the right dollar figure. 18 new tests
across `test_positions_service.py` (first-ever unit coverage of that
file), `test_order_sync.py`, `test_position_manager.py`, plus the
options mirror of the short-selling feature's own bracket-refusal
regression test.

**Verified independently after cherry-pick** (and after cleanly
recovering from an unrelated self-inflicted `git stash`/`pop` accident
that surfaced and then required dropping a long-stale, already-superseded
stash entry from an earlier session — no data lost, confirmed by content
comparison against the real merged commits it duplicated): full combined
suite (`apps/api`+`apps/agents`+`packages/engine`+`packages/broker`)
**661 passed, 9 skipped** (+69 from this track, +3 from the
`risk_officer_options` fix above, 0 regressions); ruff clean on every
file this track actually touched (`executor.py` even went from 9→8
pre-existing errors); mypy shows zero errors on every production file
touched or created — the only errors anywhere are pre-existing,
already-tolerated test-helper looseness in files this track didn't
touch.

**Left open, disclosed rather than silently skipped:** the reconciler's
own `PortfolioPosition` construction (`reconciler_fleet.py`'s
`UserBrokerPoller` / `engine/reconciler/poller.py`) still doesn't thread
`is_option`/`multiplier` through — the live risk-gate path (executor's
`_build_risk_context`) is fixed and correct, but the reconciler's
slower-moving position-display snapshot isn't yet. Also open: council-time
`RiskContext.options_trading_level` — neither `MockRiskContextProvider`
nor `PostgresRiskContextProvider` populates it yet (a different code path
from the executor's, which this track did fix), meaning
`options_level_insufficient` would still veto every options proposal
unconditionally at council time until that's closed. Both are next.

### 2026-08-28 — `4061f3af`+`a0420d63` feat(agents): attribute LLM calls to their council run and decision

The last wiring-gap item, and the riskiest one in the batch (schema
migration + touches the trading-decision persistence layer) — reviewed
with matching scrutiny before merge. Every `llm_calls` row was writing
with `agent_decision_id`/`user_id` unconditionally NULL: no way to
answer "which LLM calls produced decision X" or "what did user Y's
trading cost in LLM spend."

**The naive fix — write the real `agent_decisions.id` at LLM-call
time — would have made things worse, not better, and the subagent
caught this before writing any code.** `agent_decisions.id` isn't
assigned until strictly *after* every LLM call in a pass completes
(`run_council()` awaits the whole graph before `decision_log.record()`
ever runs); `llm_calls.agent_decision_id` carries a live, non-deferrable
FK, so writing a not-yet-existent id into it would raise
`ForeignKeyViolation` on every insert for that pass — silently
swallowed by the existing best-effort try/except, turning today's
100%-present-but-unattributed rows into 100%-silently-dropped ones.

**Real fix**: a run-scoped `council_run_id` (UUID, deliberately no FK)
generated once per `run_council()` call, before any LLM call, carried
on every `LedgerEntry` written during that pass via new passthrough
kwargs on `LLM.complete()`/`complete_json()`. Once the decision row
commits, a best-effort `backfill_decision_id()` UPDATE (guarded by
`AND agent_decision_id IS NULL`, so a row some other path already
attributed is never clobbered) attaches the real id to every row
sharing that run's `council_run_id`. `DecisionEntry.id`'s default
factory changed from an opaque `"dec-<hex>"` string to a real UUID, and
`_to_decision_entry` now passes `id=council_run_id` explicitly — so the
decision row's own PK, the ledger correlation id, and the backfill
target are all one value with no extra lookup. `reflection.py` gets
`user_id` but deliberately not `council_run_id`/`agent_decision_id` —
one reflection call grades many decisions at once, so a 1:1 link would
be false precision, not a gap.

**A genuine inconsistency in my own brief, caught and correctly
resolved by the subagent, not by me**: I'd specified `complete()`/
`complete_json()` gain only `agent_decision_id`+`user_id` kwargs, but
also required threading `council_run_id` through those same calls —
there's no way to satisfy both without either inventing a third kwarg
or routing `council_run_id` through `agent_decision_id` (which would
reintroduce the exact FK-violation bug this whole design exists to
avoid). It added a third kwarg (`council_run_id`) rather than force
the wrong shape, flagged the deviation explicitly in its own commit
message, and reasoned through why the alternative was actively unsafe
rather than just picking one silently.

**One real regression found and fixed along the way**: `test_node_guards.py`'s
`ScriptedLLM` test double had a narrow `complete()` signature with no
`**kwargs` catch-all (unlike this suite's other LLM doubles, which
already use one for exactly this forward-compatibility reason) — broke
immediately across 9 tests the moment the new kwargs were added. Fixed
by matching the existing convention.

**Migration numbering collision, fixed at merge time (expected, not a
subagent error)**: this work and the same day's options-foundation work
were built in parallel worktrees, both chaining a new migration off
`0012_decision_reasoning` as "0013". Since the options migration
(`0013_options_orders`) landed on `main` first, renumbered this one to
`0014_llm_calls_run_id` and re-pointed its `down_revision` — confirmed
via `alembic history` and an offline `--sql` dry-run across both
migrations in sequence that the chain resolves and the DDL is correct
and in order.

Verified independently after cherry-pick: `apps/agents/tests` 77
passed / 1 skipped; full combined suite (`apps/api`+`apps/agents`+
`packages/engine`+`packages/broker`) 589 passed / 9 skipped; ruff clean
on every touched file; mypy on `apps/agents`+`packages/engine` 144
errors both before and after (pre-existing, untouched-test-helper
debt — zero net new). The new end-to-end test
(`test_run_council_attributes_every_llm_call_to_its_run_and_user`)
runs a real mock council pass and asserts every `llm_calls` row shares
one `council_run_id` equal to the returned decision id, and that
`user_id` populates on every row — a hard failure against the
pre-change code on all three counts, not a shape check.

**Left open, disclosed by the subagent rather than assumed away**: the
migration was verified via `alembic history` + an offline `--sql`
dry-run only — no live Postgres was available in that sandbox to
confirm a real apply-and-query round trip. Worth one live check before
this is relied on in production, though the offline evidence (chain
resolves, DDL is exactly the expected `ADD COLUMN ... UUID` + `CREATE
INDEX`, no FK) is strong.

**This closes out the wiring-gap audit's three code items** (docs
fixes, veto-label consolidation, LLM-call attribution — the "watchlist
already fixed" and "RUNBOOK/HANDOFF redirect" items needed no further
code, only the backfill/redirect already covered in the entry above).
Only the options-trading track (Part 1 of the plan) remains open.

### 2026-08-28 — `20154ac9`+`73f44007`+`618b77fb` feat/fix/refactor: options shared-types foundation + two wiring-gap fixes

First implementation work of the production-grade phase (plan at
`.claude/plans/prancy-meandering-rainbow.md`), built via 1 direct pass +
2 parallel worktree subagents, each independently re-reviewed (real
diffs read, tests re-run from a clean cherry-pick) before merging —
this repo's established review discipline.

**`20154ac9` — options shared-types foundation (Part 1 §1.1, mine
directly, not delegated — everything else in the options track depends
on getting these shapes right).** Additive-only, zero blast radius:
`broker.types` gains `Side.BUY_TO_OPEN`/`SELL_TO_CLOSE` and a strict
`OccSymbol` parser (OCC format: `{underlying}{YYMMDD}{C|P}{strike*1000
zero-padded to 8 digits}`); `engine.risk.types` gains `RiskCaps`
`options_*` caps (fail-closed via a new `ALLOW_OPTIONS` env flag,
mirroring `ALLOW_SHORTS`'s existing convention exactly), a new
`OptionLegDetails` dataclass, `RiskProposal.is_option`/`.option`,
`RiskContext.options_trading_level`, `PortfolioPosition.is_option`/
`.multiplier`; one real Alembic migration (`0013_options_orders`) adds
`orders.{is_option,multiplier,option_action}` — the only schema change
this whole phase needs, everything else rides the existing JSONB
extension points. `ApprovalProposalDto` gets matching fields, with
`option_action` restricted to a 2-value `Literal` at the Pydantic
boundary (a free 422 before the risk engine ever runs). Verified:
`packages/engine`+`packages/broker` 213 passed (unchanged); `apps/api`
298 passed / 8 skipped (unchanged); mypy delta zero on every touched
file, confirmed via `git stash` comparison (same 6 pre-existing
`dict[Any]` errors before and after, just shifted line numbers); ruff
clean on all touched files.

**`73f44007` — stale Fly.io / dead-doc references (wiring-gap item,
subagent-built, independently re-verified).** Deploy target has been
Railway for a while, not Fly.io — fixed in `CLAUDE.md`, `PLAN.md`,
`infra/docker-compose.yml`, `infra/migrations/env.py`.
`docs/RUNBOOK.md`/`apps/api/AUTH.md` were deleted in the `5febf1e4` docs
consolidation; redirected `daily_cron.py`/`scripts/smoke_paper_trade.py`
to `docs/README.md` (which already covers the same ground), and stated
plainly in `crypto.py`/`tokenStorage.ts`/`zerodha_reconnect_cron.py` that
nothing currently documents the flows AUTH.md used to cover, rather than
link to a dead file. `daily_cron.py`'s docstring also described a
fictional "GitHub Actions / Fly machines" scheduling story — rewritten
to describe the real mechanism (`CouncilScheduler`, an in-process
asyncio task, off by default). `zerodha_reconnect_cron.py` never had an
equivalent scheduler at all (Zerodha parked for v1) — now says so
plainly instead of describing the same fiction. The subagent's own
verification pass caught 3 more stale references beyond the ones I'd
named in the brief (`crypto.py`, `tokenStorage.ts`,
`smoke_paper_trade.py`) — fixed those too, same pattern. Verified myself
independently after cherry-pick: `git grep -in "fly\.io\|fly machine"` →
zero tracked-file hits; ruff on the touched Python files shows exactly
one finding, confirmed via `git stash` to be pre-existing (an
already-stale `noqa: BLE001` in `zerodha_reconnect_cron.py`, unrelated to
this change, just shifted line number).

**`618b77fb` — veto-rule label consolidation (wiring-gap item,
subagent-built).** `vetoes.tsx`'s `RULE_LABEL` and `format.ts`'s
`ruleLabel()` were two independent, hand-maintained lookups that had
drifted — and not just by omission: `vetoes.tsx`'s map had **wrong
keys for real, currently-firing rules**
(`low_council_confidence`/`low_specialist_avg_score` vs. the actual
`min_`-prefixed names, a single `drawdown_halt` vs. the actual
`_active`/`_just_tripped` split, `position_size_cap` vs. the actual
`max_position_pct`/`_trim`), meaning the veto ledger screen has likely
been silently falling back to raw identifier strings for several
real vetoes in production, not just a cosmetic gap. `format.ts`'s
`ruleLabel()` had no lookup at all — bare uppercase-and-strip for every
call. New `packages/shared-types/src/vetoRuleLabels.json` (plain JSON,
not `.ts`, so a Python test can `json.load()` it directly) is now the
one canonical map, re-exported from the package index; both TS call
sites now read from it, each keeping its own existing crash-proof
fallback for a truly unrecognized key. New
`packages/engine/tests/test_veto_rule_labels.py` statically scans
`veto_rule=`/`risk_veto_rule=` literals across the rules package +
`live_trading_gate.py` and asserts they're all covered — proven to
actually catch drift (a key was temporarily deleted, the test failed
naming exactly that key, then it was restored). Verified independently
after cherry-pick: re-derived the required-identifier set myself via a
fresh grep (20 `veto_rule=` + 1 `risk_veto_rule=` = 21, matching the
subagent's count exactly, all 21 present in the JSON); `packages/engine/tests`
188 passed including the new drift test; `pnpm --filter @app/shared-types
typecheck` and `pnpm --filter @app/mobile typecheck` both clean;
`pnpm --filter @app/mobile test` 23/23 passed.

**Left open:** the options broker/risk/execution track (Part 1 §1.3-1.5)
and the LLM-call-attribution wiring-gap fix (Part 2 §2.4) are still
running in their own worktrees as of this entry — build-log entries for
those follow once reviewed and merged. The options sizing/contract-
selection/agent-council-wiring/UI track (Part 1 §1.2, 1.6, 1.7) is
queued behind the LLM-attribution fix landing (both touch
`apps/agents/trading_agents/state.py`/`nodes/*.py`).

### 2026-08-28 — `6878d1d6` docs: move options trading in-scope for v1, correct stale phase marker

Demo-readiness work (Google Sign-In, Alpaca connection persistence, the
3 live-reported UI bugs) is done. Per explicit user decision, this
repo's next phase is options trading (Phase A: long calls/puts only, no
spreads or assignment) plus the production-grade wiring-gap fixes
tracked in this log — not broader compliance/RIA work, which stays
parked per `PLAN.md` §14. `CLAUDE.md`'s scope table updated accordingly,
and its "Phase 0 — you are here" marker (stale for a long time — paper
trading has been live end-to-end since well before this entry) moved to
Phase 4 with an honest caveat that real capital isn't enabled yet. This
entry also closes out a backfill: the 7 commits below it (`936c0dab`
through `ef37a200`) landed without build-log entries during the prior
session; entries added now from their actual commit messages/diffs, not
guessed. The options-trading and wiring-gap-fix work itself lands in
its own later entries as it's built, per three parallel design agents
(options broker/risk/execution-safety, options sizing/features/
agent-council/UI, wiring-gap audit re-verification).

### 2026-08-27 — `ef37a200` fix(agents): stop equity=0.0 and last_price=0.0 collapsing to fixture defaults

Found while writing the drafter HOLD-reasoning tests below, flagged as a
follow-up and fixed directly rather than left open: `ctx.get(
"portfolio_equity", 100_000.0) or 100_000.0` treats a genuinely-zero
value the same as an absent one (`0.0` is falsy), so a fully-drawn-down
account would silently size a new trade against the fake $100k fixture
instead of hitting `atr_position_size`'s own `account_equity <= 0`
zero-qty refusal. Same bug on `last_price=0.0`, and the identical
`or`-collapse pattern in `risk_officer.py`'s mock-provider equity read
(dev/CI path only — the Postgres production path was unaffected).
`provider.py`'s own `equity_resolver` fallback was checked and is fine —
it already uses an explicit `equity <= 0` check with a loud warning log,
not an `or`-collapse. Both fixed sites now distinguish "key absent"
(fixture applies) from "key present but zero" (reaches the sizer/risk
context as a real zero, correctly triggering a downstream refusal).

557 passed, 9 skipped (4 new regression tests) —
`apps/agents/trading_agents/nodes/{drafter,risk_officer}.py`.

### 2026-08-27 — `9b607b2d` fix(dashboard): stop calling every HOLD a risk veto in the activity feed

Caught while visually verifying the previous entry's live deploy, one
screen over from the same bug class: the Dashboard's "Agent Activity"
panel read "GLD Vetoed — risk rule fired" for GLD, TSLA, CRM, GOOP, UNH —
every one a strategy-fit or Drafter HOLD, none of which ever reached the
risk officer. `_decision_to_activity` branched on bare `not
row.risk_approved`, true for a HOLD exactly as much as for a real veto,
with a fallback string that asserted a rule ran when none did. Now
branches on `risk_veto_rule` actually being set — exactly "a named rule
refused a drafted proposal," matching the veto-ledger fix two entries
below. Also fixed in passing: `side` defaulted to `"BUY"` via
`.get("side", "BUY")` for every HOLD (a HOLD's `proposal` column is
empty/null), mislabeling the activity row's direction.

### 2026-08-27 — `24dd4d42` feat(decisions): browse every council pass, and explain every HOLD

User-reported: "58 decisions in window" on Strategies with no way to open
any of them, and NVDA flipping BUY → HOLD with zero explanation.

**New `GET /api/v1/decisions`** — paginated, filterable by symbol/action —
plus a Decisions screen reachable from a "View all" link on Strategies'
count, since every council pass writes a row whether or not it becomes a
proposal, but the only prior way to reach one was `/approvals/pending`
(still-pending) or `/positions` (approved) — a strategy-fit HOLD, the
majority of any sweep, was invisible the instant the sweep moved past it.

**The real root cause of "no explanation," a write bug not a display
bug:** `PostgresDecisionLog` wrote `entry.raw_state` (the whole `{regime,
proposal, analyst_subset, degraded_nodes}` envelope) into
`agent_decisions.proposal` whenever there was no approved proposal — and
that envelope is a non-empty dict even when its own nested `proposal` key
is null. `biography_service` read `if proposal:` as "a real proposal
exists," true for every HOLD, so `.get("rationale")`/`.get("side")`
against the wrong dict came back empty — the real explanation never
reached the row at all. Fixed at the write site (store
`raw_state["proposal"]`, not the envelope) and hardened the read site
against historical rows already written the old way (`proposal["side"]`,
not bare truthiness) — confirmed this alone fixes old rows with no re-run
needed.

**The Drafter's own HOLD explanation was being discarded** —
`drafter_node` read the model's bear-case reasoning on a HOLD and then
threw it away. `drafter_rationale`/`bull_case`/`bear_case` now survive
both a model HOLD and a sizer-zeroed-qty HOLD (each distinguished from a
parse-failure HOLD, which has nothing to explain), surfaced in the
theater's live drafter card and the trade biography.

**Per-analyst output was fetched but never rendered** — the timeline API
already returned each analyst's role/score/confidence/thesis; the UI
dropped it. Extracted the duplicated-inline `TimelineCard` into a shared
`TradeBiography.tsx`, now rendering the full breakdown for every event,
not just approved ones.

Verified live against historical rows already in the DB (no re-run
needed): NVDA/UNH/WMT/GLD/XOM all show their real reason (no-strategy-fit
/ drafter-said-no / parse-failure / a real BUY thesis) instead of a bare
"Council [proposed/held] HOLD X". 550 passed, 9 skipped (11 new tests).

### 2026-08-27 — `f5048d47` fix(orders,ui): cancel unfilled orders, fix cold-run error text, fix Close overflow

Three bugs found from live use, bundled in one commit.

- **Cancel an unfilled order.** No way existed to stop a trade that was
  approved but hadn't filled yet — `close_position_now` refused with
  `no_open_position` (technically correct, unhelpful outside market
  hours, where an order can sit accepted-but-unfilled for hours). Now
  dispatches to a new `cancel_pending_order_now` when `fill_qty` is null:
  cancels the broker-side entry order (which takes its bracket's OCO
  children with it, since they aren't live until the parent fills) and
  updates the order row immediately rather than waiting for the next
  reconciler tick. Both position screens reuse the existing Close
  button/endpoint, now reading "Cancel order" for a `pending_fill` row.
- **Wrong "agent server may be cold" message.** `runErrorMessage` only
  handled this app's own `assert_tradable` 422 (`{detail: "<string>"}`) —
  FastAPI's own pydantic validation 422s with `{detail: [{msg, ...}]}`, an
  array, which is exactly the case where the server said precisely what
  was wrong. Now handles both 422 shapes and echoes any other HTTP
  status, reserving "cold"/network wording for when there's no `status`
  on the error at all.
- **Close-button overflow.** Positions' table (8 data + 1 action column)
  had no horizontal scroll region, so once the trade-biography panel
  opened beside it and the card shrank to 8/12 width, the table
  overflowed and clipped the Close/Cancel button instead of scrolling.
  Wrapped in `overflow-x: auto`, matching this repo's own
  wide-content-scrolls-in-its-own-region convention.

541 passed, 9 skipped (2 new cancel tests); tsc clean; mobile jest clean.

### 2026-08-27 — `e0e05fb8` feat(positions): surface approved orders awaiting a fill

User-reported: approved a KO trade and it never showed up in Positions.
Not a bug in the order itself — Alpaca correctly had it queued
(`status: "new"`) for the next market open — but the app had no surface
for that state at all: `/approvals/pending` drops a proposal the instant
it's decided, `/positions` only listed decisions with `fill_qty IS NOT
NULL`, and `/orders` has no GET endpoint. An approved order was invisible
from the moment of approval until the moment it filled, which outside
market hours can be hours.

`list_open_positions` now also returns approved-but-unfilled decisions as
`status: "pending_fill"` (qty from the proposal, no entry price/mark/P&L
— none exist yet), cross-referenced against the newest `orders` row per
decision to exclude ones that already died (rejected/canceled/expired) —
without that check, a dead order would show as "awaiting fill" forever
instead of correctly disappearing. Both position screens render the
state distinctly: an "AWAITING FILL" badge, "not filled yet" in place of
a price, no Close button.

Verified against the live deployment: the user's 5 pending approvals
(KO, SPY, JNJ, UNP, CVX) now list correctly. 539 passed, 9 skipped; tsc
clean.

### 2026-08-27 — `a5a90cf0` fix(auth): de-dupe concurrent refresh() calls so one token race can't kill the session

User-reported: "Approve" sometimes fails with a generic error and the
proposal doesn't execute, no server-side error in the Railway logs —
pointing at the client dying before the request landed.

Root cause: refresh tokens are single-use and rotate server-side via
compare-and-swap. The dashboard runs 4-5 independently polling queries
(positions/scanner/health/review, every 30-60s) against a 15-minute
access token, so two or more in-flight requests commonly hit a 401 in
the same tick. `authStore.refresh()` had no concurrency guard — each
caller read the same stored refresh token and POSTed it independently.
The first won the rotation; the second's already-spent token looked like
a replay to the server, which doesn't just reject it — it revokes the
whole session. The losing caller saw `superseded`, wiped the credential,
and silently signed the user out mid-session; whatever request happened
to lose the race is what the user saw fail (an Approve tap, in this
report).

Fix: share one in-flight refresh promise across all concurrent callers,
so every 401 in the same tick waits on the same network call and gets
the same outcome — this app's own polling can no longer manufacture a
replay against itself. A genuine attacker replaying an old token after a
real rotation is untouched: still exactly one caller with a stale token,
still caught and revoked.

16 authStore tests pass (2 new), 23 mobile tests pass, tsc clean.

### 2026-08-27 — `936c0dab` fix(scheduler): honour the user's curated watchlist, not just the env var

`daily_cron.cli()` had always preferred the `user_watchlist` table over
`AGENT_CRON_WATCHLIST`, but the in-process `CouncilScheduler` — the path
that actually runs in production — read only the env var. Curating a
watchlist in the app therefore changed nothing: both the baseline sweep
and the deterministic scanner kept working off whatever the env var
said, and `/scanner/status` reported the env list's size as the
watchlist size (the stale note this entry resolves, struck through
above). `_watchlist()` becomes async and loads the curated list for the
cron user, falling back to env on an empty list or any load failure (an
unreachable table must not stop the sweep from running at all);
`configured_watchlist()` follows suit so `scanner_status` reports the
size it actually scans.

537 passed, 9 skipped —
`apps/api/app/services/council/{scanner_status,scheduler}.py`.

### 2026-08-27 — `f856858c`+`deb07bc9`+`69b8e7df`+`d66584be` fix: four silent failures found by running the live stack, not the tests

The user reported an empty dashboard — no positions, no recommendations —
against a deployment where everything "worked". Root cause was ownership,
and the hunt turned up three more failures that no test could see because
each one was swallowed by its own best-effort `except`.

**The dashboard blocker (config, not code).** `AGENT_CRON_USER_ID` was
unset, so `scheduler.py` attributed every scheduled decision to the
fixture user `00000000-…-0001` while the real logged-in user is
`43221580-…`. Tenant scoping — working exactly as the F1 CRITICAL fix
intended — then hid all 15 decisions, including 4 approved BUY proposals
with brackets. Set the Railway var to the real user id, reassigned the
existing rows (backup of the id list taken first), and seeded
`user_watchlist`, which was empty so the watchlist screen rendered blank
while the scheduler ran happily off the env-var fallback.

**`f856858c` — cost ledger + push fan-out.**
- `app/services/notifications/__init__.py` was 0 bytes after the services
  split, so `daily_cron`'s package-level import of
  `schedule_proposal_pending_notification` raised ImportError on every
  approved proposal. Its own `except` swallowed it: scheduled picks
  landed in the DB and notified nobody. This is the "lambda-like trigger
  that notifies me" feature — it had never fired.
- `get_cost_ledger()` warned "PostgresCostLedger is not yet wired" and
  returned the in-memory ledger, so every cost row died with the process.
  `llm_calls` was empty across weeks of real runs and /health/full
  reported $0.00 regardless of actual billing. Added
  `memory/cost_ledger_postgres.py`. Verified live: 23 rows and climbing.

**`deb07bc9` — unmanaged positions.** `/account` reported
`openPositions: 1` while `/positions` returned `[]`. Both were right on
their own terms (one counts what the broker holds, the other listed only
open agent *decisions*), but the position they disagreed about was real
money and the screen read as broken. `OpenPositionDto` gains `managed:
bool`; `decision_id` becomes nullable so clients render "close at the
broker" rather than a Close button that would 404. No stop/target is
reported for one — promising an exit plan the agent never authored would
be the worse failure.

**`69b8e7df` — one event loop for the cron.** `cli()` made two
`asyncio.run()` calls, and `engine.db.session` caches one AsyncEngine per
process, so the watchlist load bound its asyncpg connections to a loop
the second call had already closed. Every later pool checkout raised
"attached to a different loop". Visible damage: the equity resolver
failed on every run, its fallback swallowed the error, and the whole
sweep was sized against the **100k fixture instead of real equity** —
precisely the failure the resolver was written to prevent (audit §5). A
45-symbol sweep also wedged indefinitely on a pool ping. Collapsed into
one `_run_cli` coroutine; verified live that the warnings are gone.

**`d66584be` — veto ledger.** Filtered on `risk_approved IS FALSE`, which
also matched every strategy-fit HOLD — symbols that fit no strategy and
short-circuited before the risk engine ran. The live ledger read 28
vetoes, 100% `unnamed_rule`, against 0 actual rule firings. Now requires
`risk_veto_rule IS NOT NULL`, which is exactly "a named rule refused a
drafted proposal".

**Left open:**
- `orders`, `ghost_outcomes` and `decision_review` are still empty, and
  that is correct: they fill from the product loop (approve → order;
  decline/veto → ghost; grade → review). No agent should fabricate them.
- Named risk vetoes are 0 because the book holds one position — the
  concentration, correlation-cluster and max-open-positions rules cannot
  bite until positions accumulate. The ledger fills as trades are taken.
- ~~`scanner/status` reports `watchlistSize` from the env var, not the
  45-symbol DB watchlist.~~ Resolved 2026-08-27 (`936c0dab`).
- Alpaca order history that predates our decision rows is not imported,
  so the NVDA bracket shows as unmanaged with no order row behind it.

### 2026-08-26 — `5a7f8cb2`+`e9c9ac6c`+`e2827fb7` fix(mobile,broker): three live-reported UI/config bugs, one root-cause diagnosis

All found live, from the user's own screenshots of the deployed app right after wiring up Postgres + Alpaca env keys. Two are real, independent code bugs, fixed and tested; the remaining reported symptoms (agent "ran cold", positions not matching a manually-opened NVDA share at Alpaca) trace to ONE root cause that is **not fixable in code** — see the diagnosis at the bottom.

- **`5a7f8cb2`** — `start_alpaca_oauth` was silently falling back to `alpaca_oauth.py`'s `DEV-ALPACA-CLIENT-ID` placeholder whenever `ALPACA_OAUTH_CLIENT_ID` was unset, and neither Settings screen checked for that before navigating. Live symptom: clicking "Connect Alpaca" opened Alpaca's own authorize page, which rejected the placeholder with a generic "Client authentication failed... unknown client" error — no hint anywhere that the problem was server-side config. Couldn't hard-refuse in the backend (`/connect/alpaca/start`) the way `_require_zerodha_configured` does for Zerodha: the existing OAuth test suite deliberately exercises the mechanics (state, PKCE, mocked token exchange) against this exact placeholder on purpose, since none of that needs a real Alpaca app — only an actual browser redirect does, and breaking that suite's design wasn't worth it for this fix. Instead, a new `alpaca_oauth.is_configured()` feeds a dedicated `oauthNotConfigured` boolean on `StartOAuthResponse` — deliberately not folded into the existing free-text `devWarning` field via string-matching (that field also covers an unrelated, non-blocking dev-encryption-key case), matching the same reasoning as the auth refresh-error `code` field added earlier this session. Both Settings screens (native + desktop) now check the boolean and show the warning instead of navigating into the dead end.
- **`e9c9ac6c`** — desktop `Positions.tsx` showed the loading skeleton whenever `positions.isLoading || rows.length === 0` — so a real, successfully-loaded *empty* result rendered as a permanent shimmer, indistinguishable from a hung request. Exactly what the user's screenshot showed: "0 OPEN" in the header badge, skeleton bars in the body, forever. Split into the three states `Picks.tsx`/`Dashboard.tsx` already use elsewhere in this same tree (loading → skeleton, empty → a `pg-empty` block, otherwise → the real table). The native mobile `positions.tsx` already had this right (`isLoading` / `isError` / empty / content as four separate branches) — desktop was the only place with the bug.
- **`e2827fb7`** — follow-up to the login-scrollable fix two entries below: the scrollbar track/thumb itself was visibly showing on web once the screen became tall enough to scroll. `scrollbarWidth: 'none'` is a react-native-web extension its own `ScrollView` implementation special-cases — setting it emits both the Firefox-standard `scrollbar-width: none` *and* an auto-generated `::-webkit-scrollbar{display:none}` rule, so no separate global CSS was needed. Verified directly: at a forced 1280×450 viewport, `scrollTop` still moves on command (5.3 → 200) with `getComputedStyle().scrollbarWidth === "none"`.
- Verified: `apps/api/tests` 279 → **281 passed / 8 skipped** (+2, the new `oauthNotConfigured` cases); `mypy apps/api/app` 59 → **59 errors** (identical); `ruff --select F,I` clean; `apps/mobile` typecheck clean; Jest 21/21 unchanged. All independently re-run.

**Root-cause diagnosis, not a code fix (can't be — needs the user's own Railway dashboard access):** curled the live deployment directly (`request-login` → `verify` → `health/full` → `broker/connections`) using a throwaway test account. Confirmed `USE_POSTGRES=1` genuinely took effect (reconciler ticking), but `/api/v1/auth/request-login` intermittently returned a raw `railway-hikari`-branded 500 (Railway's own edge, not the FastAPI app's JSON error shape) — consistent with the user's own separately-reported Railway panel message ("no database mounted yet... Attempt #7 failed with service unavailable"). This is a Postgres-connectivity problem, most likely because the API service's `DATABASE_URL` was set to the raw public proxy connection string rather than Railway's own recommended intra-project service-reference syntax (`${{ Postgres-CUSN.DATABASE_URL }}`, which resolves to the private/internal network path). Told the user to switch to that reference syntax instead. This almost certainly also explains the "agent ran cold" council-run failures (same Postgres dependency) — not re-diagnosed separately, since it shares the identical root cause.

### 2026-08-26 — `11078dcb` fix(broker): give a user the env-key Alpaca connection at login, not just at boot

Found live, verified against the user's actual Railway deployment
(curled `/health`, ran a real request-login → verify → check
`/api/v1/broker/connections` round-trip against it with a fresh test
user) after they wired up `USE_POSTGRES`/`DATABASE_URL`/the Alpaca env
keys per the shared-paper-account model chosen this session: a
brand-new user showed `broker: "No broker connected"` and an empty
connections list, even with the keys correctly configured and Postgres
confirmed active (reconciler ticking).

Root cause: `ensure_env_broker_connection`'s only call site was
`app.main`'s lifespan, run once at boot, sweeping whichever users
existed AT THAT MOMENT. A freshly-provisioned Postgres has zero users at
boot — so nobody got swept, including every real signup afterward. This
was flagged as a known, optional follow-on when the boot-time fix
landed earlier the same day (`d9ec335d`); it's the actual gap now that
someone depends on the shared-account model for real logins rather than
just the `DEV_AUTH_BYPASS` fixture user.

Fix: new `_bootstrap_broker_for_new_login()` in `routers/auth.py`,
called from the two paths that mint a brand-new session — magic-link
`verify` and Google `google_login` — never from `/refresh` (an existing
session already had its chance; refresh happens every ~15 minutes for
every active user, and the extra check would be pure waste there).
Best-effort, logged-not-raised on failure.

Verified: `apps/api/tests` 277 → **279 passed / 8 skipped** (+2: a real
end-to-end case proving a post-boot login still gets the connection, and
a case proving `/refresh` never triggers it). `mypy apps/api/app` 59 →
**59 errors** (identical). `ruff --select F,I` clean.

### 2026-08-26 — `bd0e2840` fix(mobile): make the login screen scrollable

User-reported, from a live Railway screenshot: the "Continue with dev
token" button was cut off with no way to reach it. Root cause: the
screen's content `View` had no `ScrollView` wrapper; adding the Google
button + divider (this same day, `febba726`) pushed total content past
shorter viewport heights with nothing to scroll. Wrapped in `ScrollView`
+ `keyboardShouldPersistTaps="handled"`, matching `watchlist.tsx`'s
existing pattern. Verified directly, not assumed: at a forced 1280x450
viewport, the dev-token button measured `top:599` (off-screen) before
the fix; `scrollIntoView` reaches it at `top:357` after. Also `chore`
`288aa7b1`: added a `dev-api` entry to `.claude/launch.json` so the API
can be previewed in-browser alongside `mobile-web` (only `mobile-web`
existed before).

### 2026-08-26 — `4bd3f245`…`febba726` feat(auth): "Continue with Google" + a less destructive session-refresh failure mode

The user's "I keep having to re-auth" complaint traces to one root cause,
confirmed by reading the store implementations directly: `USE_POSTGRES=0`
(the shipped default) means both the auth-session store and the broker-
connection store are process-memory dicts, wiped on every API restart —
and the mobile client's own refresh-failure handling then **actively
deletes** its stored credential on the resulting 401, turning "the
backend forgot" into "logged out for good." Google Sign-In alone does not
fix this — a Google-issued session is exactly as ephemeral under the same
store. This entry covers the explicitly-requested Google login plus the
one client-side fix that's a direct, well-scoped contributor to the actual
persistence complaint; a sibling entry below covers the Alpaca-connection
half of the same root cause plus a separate desktop bug.

- **Google ID-token verification** (`apps/api/app/services/auth/google_oauth.py`,
  new): RS256 verification via `python-jose` (already declared + installed
  — no new backend dependency; `google-auth`'s default sync transport
  would've blocked the event loop, which this async-first codebase
  doesn't do). Alg locked to RS256 from the *unverified* header before any
  network call (defeats the classic alg-confusion attack); JWKS fetched
  from Google and cached by `kid` with a TTL, an unknown `kid` forcing
  exactly one refetch (key-rotation window) before failing; `aud`/`iss`
  checked manually since jose's built-in checks only take one expected
  value each and Google needs "one of several client ids" / "one of two
  documented issuer strings"; `email_verified` required to be the literal
  `True`, not merely truthy — an unverified email must never create or
  link an account.
- **`login_with_google()`** (`auth.py`), parallel to `verify_magic_link`,
  reuses the existing `_issue_pair()` so a Google session is
  indistinguishable from a magic-link one downstream — no session/token
  model changes anywhere. `upsert_user`'s existing get-or-create-by-email
  semantics mean one account serves both login methods with **no
  migration** (`auth_method` is free-text, no DB constraint); an existing
  magic-link user who later uses Google keeps `auth_method="magic_link"`
  forever — an accepted, cosmetic quirk, pinned by a test rather than
  silently left to drift.
- **`POST /api/v1/auth/google`** (primary path) + **`POST
  /api/v1/auth/google/exchange`** (fallback, for a Google OAuth client
  type whose platform policy is confidential — typically "Web
  application" — and refuses a secret-less PKCE exchange; holds its own
  separate `GOOGLE_OAUTH_WEB_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET`,
  distinct from the ID-token audience allow-list). Both routes share one
  `_verify_and_login_with_google()` tail so they can't drift on what
  counts as "verified." Unconfigured → 503, deliberately *not* added to
  `production_config_problems()`'s hard-fail boot check — magic-link
  alone stays a valid production config. New per-IP rate bucket (no
  caller-supplied email to key on before the token verifies).
- **The actual persistence fix, mobile side**: `auth.py`'s `refresh()`
  used to collapse "session not found" / "revoked" / "expired" / "token
  invalid" / "superseded" into one undifferentiated 401. It now raises a
  typed `RefreshError` carrying a machine-readable `code`, threaded onto
  the 401 body as a sibling of `detail`. `authStore.ts`'s `restore()` —
  and its sibling `refresh()` action, which was actually the more
  aggressive of the two, wiping storage on *any* thrown error — now only
  wipe the stored credential for `session_revoked`/`token_invalid`/
  `superseded`; a bare `session_not_found` (or any code they haven't
  seen yet, including no code at all from an older API build) just marks
  the app-open unauthenticated *without* deleting the refresh token, so a
  later successful restore doesn't force a brand-new login. One subtlety
  worth naming: a session whose row resolves but whose owning user has
  vanished is mapped to `token_invalid` rather than `session_not_found` —
  a broken data state, not a "backend forgot" state, deliberately not
  treated as something a later restore could fix.
- **Mobile flow**: Authorization Code + PKCE via `expo-auth-session` +
  `expo-web-browser` (new deps, installed via `npx expo install`) against
  Google's standard OIDC discovery document. Tries the client-side
  exchange first; on any failure, falls back to the backend exchange
  endpoint — which of the two actually fires in practice depends on which
  Google Cloud OAuth client type ends up configured, which can't be
  determined by reading this repo. `GoogleSignInButton.tsx` uses Google's
  official multi-color logomark (sourced from Google's own branding
  guidelines, not freehanded) — its brand-color fills are a deliberate,
  named exception to the design-tokens-only rule, since they're Google's
  trademark colors, not this app's palette. A defensive
  `auth/google/callback` deep-link branch exists as an Android-backgrounding
  safety net; the happy path resolves in-process and never touches it.
- **Explicitly deferred**: a `google_sub` column/migration for identity
  binding independent of email — doing it correctly requires reworking
  `upsert_user`'s get-or-create-only semantics to backfill it onto an
  existing row, which is out of scope here; the free-text `auth_method`
  column already makes the core ask schema-cost-free without it.
- Verified: `apps/api/tests` 258 → **277 passed / 8 skipped**; existing
  `test_auth.py`/`test_auth_hardening.py`/`test_auth_p3_hardening.py`
  confirmed **zero byte diff** (purely additive change); `mypy
  apps/api/app` 59 → **59 errors / 19 files** (identical set, one new
  clean file); `ruff --select F,I` clean; `apps/mobile` typecheck clean;
  Jest 7 → **21 passed** (+14, all in the new `authStore.test.ts` — the
  first fetch-mock test in this app's auth surface). All independently
  re-run on `main` after cherry-picking.
- **External prerequisite, not a code gap**: a real Google Cloud OAuth
  client (iOS/Android/Web as needed) must be created before the endpoint
  or the mobile flow can be exercised end-to-end — the actual browser
  round-trip isn't meaningfully testable in Jest and needs real-device QA
  once that exists.

### 2026-08-26 — `d9ec335d`…`8b09fbd3` fix(broker): a genuinely persistent Alpaca connection, and a desktop connect bug

Sibling to the entry above — same root cause (`USE_POSTGRES=0` →
in-memory `InMemoryBrokerStore`, wiped every restart), plus one
independently-confirmed, separate bug: on the desktop/web build, "Connect
Alpaca" most likely never completed at all.

- **Un-gated the env-key bootstrap** (`0c39c95a`): `ensure_env_broker_connection`
  (built earlier the same day in `2fe8b9fd` — auto-links a user's paper
  account straight from `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`, no per-user
  OAuth) only ever ran inside `if use_pg and enable_reconciler:` in
  `main.py`'s lifespan — silently never firing under the shipped
  `USE_POSTGRES=0` default (the mode where the pain is worst), and also
  silently disabled by `RECONCILER_ENABLED=0` even when Postgres *was* on.
  New `bootstrap_env_broker_connections(*, use_pg)`, called unconditionally
  (gated only on its own "are the env keys even set" check): the Postgres
  branch keeps the existing enumerate-every-`User`-row behavior (now
  wrapped in try/except so a DB hiccup can't fail boot); MockStore mode
  targets exactly `FIXTURE_USER_ID` — the one identity guaranteed to
  survive a MockStore restart, since there's no backend-agnostic "list
  all users" accessor to do more than that.
- **Fixed a trust bug found alongside it** (`563ffeb4`): revoking an
  env-bootstrapped connection didn't stick — the *next* restart silently
  recreated it with zero explanation anywhere in the UI. New
  `connectionSource: "environment" | "oauth"` on the connections response
  (a cheap, non-persisted comparison of the decrypted token against the
  known sentinel — no schema change), surfaced as a "connected via server
  configuration" pill in Settings.
- **The desktop OAuth-callback bug** (`8d56dd03`) — confirmed real and
  worse than "missing route": Alpaca's redirect is hardcoded server-side
  to the native `autotrader://` deep-link scheme; the desktop build does
  a real full-page browser navigation to Alpaca and back with nowhere to
  catch `?code&state`. Adding a page in the obvious place (a new Expo
  route) would **not** have worked either: `DesktopShell` unconditionally
  swaps out the entire router subtree the instant `restore()` succeeds
  post-redirect, unmounting any callback screen before it could finish
  its own network call — and the access token is never persisted, so it
  would have no bearer token to call an authenticated endpoint with
  anyway. Fixed by reusing the exact pattern already built and tested for
  Zerodha: `alpaca_callback`'s body extracted into a shared
  `_complete_alpaca_connect(..., expected_user_id)` helper (mirroring
  `_complete_zerodha_connect`), reused by both the existing authenticated
  POST (native, byte-for-byte unchanged) and a new **unauthenticated**
  `GET /connect/alpaca/redirect` (desktop) where the single-use,
  15-minute `state` token itself is the proof of identity — a plain
  server-rendered HTML response that never touches the Expo bundle,
  `DesktopShell`, or the router at all. A new `platform` hint on `/start`
  picks between exactly two *fixed*, server-known redirect URIs — never a
  caller-supplied one, which would be an open-redirect/code-hijack risk.
  The redirect_uri actually used is now stashed on `PendingOAuth` and
  threaded into the token exchange too, since OAuth2 requires the two to
  match exactly — a mismatch would have passed every test that didn't
  check for it specifically while still failing for real against
  Alpaca's API.
- Verified: `apps/api/tests` 237 → **258 passed / 8 skipped**; existing
  `test_broker.py` suite confirmed **zero deletions, pure addition**
  (255 insertions); `mypy apps/api/app` 59 → **59 errors / 19 files**
  (identical); `ruff --select F,I` clean; `apps/mobile` typecheck clean.
  All independently re-run on `main` after cherry-picking.
- **External prerequisite, not a code gap**: whether Alpaca's OAuth app
  (in Alpaca's own developer console) supports registering a second
  redirect URI, or needs a second OAuth client, is outside this repo's
  reach and must be confirmed/configured there directly before the
  desktop fix can be exercised live.

### 2026-08-26 — `d93f21dd`…`41df00c6` feat(watchlist): live Alpaca-backed symbol search on both add-to-watchlist screens

Closes a real gap: `POST /api/v1/watchlist` already had live-Alpaca-backed
search infrastructure (`GET /api/v1/symbols/search`, built earlier the same
day in `103c27ca` but wired only into the ad-hoc "run the council on a
ticker" launcher on desktop) — every actual watchlist-add entry point, on
both platforms, was still a plain text field with only after-the-fact
regex/tradability validation.

- **New `apps/mobile/src/hooks/useTickerCombobox.ts`** — lifts the desktop
  `CouncilLauncher`'s proven combobox state (query/open/activeIndex/hits)
  into a headless, DOM-agnostic hook so both surfaces below share one state
  machine instead of drifting apart.
- **Desktop** (`apps/mobile/src/desktop/screens/Settings.tsx`): the
  watchlist card's plain `<input>` (zero validation, zero typeahead, and —
  found in passing — zero error display; a failed add used to fail
  completely silently) now uses the same combobox pattern as
  `CouncilLauncher`, confirmed byte-for-byte untouched by this change
  (`git diff` against it is empty).
- **Mobile (React Native)**: two new components, `SymbolResultsList`
  (dumb, `.map()`-over-`Pressable` rows — not `FlatList`, since it renders
  inside `watchlist.tsx`'s plain `ScrollView` and nesting a
  `VirtualizedList` there trips RN's own warning) and `SymbolTypeahead`
  (smart wrapper owning the combobox, exposing `onCommitSymbol` for either
  a dropdown pick or the preserved type-exact-ticker fallback).
  `keyboardShouldPersistTaps="handled"` added to the screen's `ScrollView`
  — the RN-native equivalent of the desktop's mousedown-before-blur trick.
- Both surfaces clear the attempted text only on a successful add, leaving
  it in place next to the error otherwise.
- **New tests, all previously absent**: `apps/api/tests/test_symbol_search.py`
  (ranking, caching, `assert_tradable`'s reject/pass/fail-open cases, and a
  first-ever router-level test for `POST /watchlist` itself), a new
  `packages/broker/tests/test_alpaca.py` (`lookup_asset`/`list_tradable_assets`
  field mapping, and — pinned separately — that `shortable`/`easy_to_borrow`
  stay `None` rather than collapsing to `False` when the broker doesn't
  report them), and a Jest render test for `SymbolResultsList` (deliberately
  not the data-fetching wrapper, to keep this repo's hours-old Jest setup
  modest). Both new Python test files had to locally suppress a pre-existing
  `alpaca-py`/`websockets` version mismatch (`trading/stream.py` still
  imports the deprecated `websockets.legacy`) that this repo's
  `filterwarnings = ["error"]` would otherwise turn into a collection-time
  crash — the first tests to actually trigger `broker.alpaca`'s import
  without real keys configured.
- Rate limiting on `GET /api/v1/symbols/search` deliberately deferred —
  authenticated, cheap (in-memory scan), non-sensitive data.
- Verified: `apps/api/tests` 209 → **237 passed / 7 skipped**;
  `packages/broker/tests` 18 → **26 passed**; `apps/mobile`/`@app/ui`
  typecheck clean; `apps/mobile` Jest 4 → **7 passed**. All independently
  re-run on `main` after cherry-picking, not taken on the subagent's word.
- **Demo prerequisite, not a code change**: `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`
  are blank by default in `.env.example` — without real values, every
  typeahead (this one and the pre-existing `CouncilLauncher` one) silently
  shows empty results.

### 2026-08-26 — `5298df57`…`ec7d9adb` fix(orders,engine): short positions can actually open, hold, and close

Closes the gap flagged the same day in `5c042151`: the short-side risk
rules and inverted-bracket sizing were correctly built, but disconnected
from real execution at three independent points — any one alone meant no
short could ever complete a fill; one of the three was a live safety bug,
not just a missing feature.

- **Proposal lifecycle** (`9c29cee5`): the Drafter already emitted
  `direction`/`opens_short` and the context asset block already carried
  `shortable`/`easy_to_borrow`, but `runtime.py`'s `_to_proposal_dto` built
  its camelCase dict by explicit field list and silently dropped both, and
  `ApprovalProposalDto` had nowhere to receive them anyway. Added with safe
  defaults; no Alembic migration needed (`AgentDecision.proposal` is a
  schema-less JSONB column).
- **Executor** (`7ff5f7e8`): `_re_run_risk` never populated `stop_price`/
  `shortable`/`easy_to_borrow` on the execution-time `RiskProposal` —
  `shortable_check`/`short_requires_stop` treat `None` as "unverified,
  veto," so this was the literal reason **no short could complete a fill,
  regardless of `ALLOW_SHORTS`**. Also: `_execute_via_broker`'s bracket
  eligibility (and its paired live-mode unprotected-order refusal) required
  `side == "BUY"`, so a live short would have gone out with no stop and no
  refusal safety net.
- **`order_sync.py` (`c52005bc`) — the most severe find, not in the
  original brief**: `_apply_decision_lifecycle` keyed entry-vs-exit off a
  hardcoded `side == "BUY"` literal. A short's own entry order IS a SELL,
  so its opening fill fell into the *exit* branch and stamped `closed_at`
  the instant it filled — on every reconciler tick, before the position
  was ever visible as open. Fixed by comparing against the decision's own
  recorded entry side instead of a literal, with the same fix shape applied
  to `_maybe_record_pdt` (was blind to a short's entry order for PDT),
  `_detect_external_closes` (`qty > 0` "still held" check excluded every
  short), and `_last_snapshot_mark` (same `qty > 0` exclusion on the
  fallback mark price).
- **Paper simulator** (`489f3017`): `PaperPortfolio.fill` rewritten around
  signed quantities (negative = short, matching Alpaca's own convention)
  — the old SELL branch clamped to held qty (silently no-opping a
  short-open), and the BUY branch never realized P&L on a cover. Verified
  by hand against `equity() = cash + Σ(qty·mark)` across same-sign extend,
  opposite-sign partial/full close, and cross-through-flat in both
  directions.
- **`position_manager.py`** (`b33428e2`): `_close_position` — used by
  *both* the automated time-stop/signal closer and the user's manual
  "Close now" — hardcoded a SELL to close any position. For a short, that
  reads to the risk engine as *opening a new short*, not closing one, so
  it was vetoed every time: **a short could not be closed through this app
  at all, agent- or user-initiated**, worse than the originally-scoped bug.
  Now derives the close side from the held position's sign; same fix
  shape applied to the in-flight-close re-entrance guard (was blind to a
  BUY-to-cover) and the close push-notification's hardcoded "SELL" verb.
- **`drawdown_halt.py`** (`e2f1f8eb`): used to exempt *every* SELL
  unconditionally (letting a new short open during an active halt) while
  blocking *every* BUY (blocking a user from covering an existing short to
  de-risk, contradicting the reason SELL-to-close is exempted at all). New
  `covers_short_only`/`held_short_qty` predicates in `_short.py`, mirroring
  the existing `opens_short`/`held_long_qty`, make the exemption symmetric:
  a SELL exempt only when it doesn't open/extend a short; a BUY exempt only
  when it doesn't cross past the held short into a new long.
- **UI** (`99d55bf8`): `positions_service.py`'s P&L sign and live-mark
  filter were long-only; `OpenPositionDto` gained a `direction` field.
  Direction badges added per-platform — `DirectionPill` (mobile) vs. the
  desktop's own `Pill` (never `DirectionPill` there — the two design
  systems are never blended, per `DESIGN.md`). `vetoes.tsx`'s stale
  `forbid_short` key fixed to `forbid_short_phase_0`, plus labels added for
  the four short-specific veto rules, which had none.
- **`ALLOW_SHORTS=0` documented** in `.env.example` (`ec7d9adb`) —
  fail-closed default, per `engine.env.env_flag`.
- **Known, accepted gap, not fixed here**: `sector_concentration.py`,
  `single_name.py`, `correlation_cap.py`, `max_open_positions.py` all still
  gate on `Side.BUY` only — a short-open is invisible to all four caps, and
  (found while fixing `position_manager.py`) a cover-BUY that de-risks a
  short can in principle be blocked by one of them exactly like a fresh
  long would be. A single-position demo never trips this; a real portfolio
  eventually could. `direction`/`opens_short` were not promoted onto
  `BrokerInterface`/`OrderRequest`. `test_sizing.py` confirmed unchanged
  (0 SELL references).
- Verified: `apps/api/tests` 209 → **214 passed / 7 skipped**;
  `test_risk.py`+`test_sizing.py`+`test_backtester_risk.py` 32 → **35
  passed** (+3 new `drawdown_halt` cases; the other two files confirmed
  byte-for-byte untouched); combined with `test_council_mock.py` (6
  passed/1 skipped, unchanged) and `packages/broker/tests` (18 passed,
  unchanged), the full run across all five is **59 passed / 1 skipped**;
  `mypy` across `apps/api/app`, `packages/engine`,
  `packages/broker`, `apps/agents`: 192 → **190 errors** (net improvement;
  every remaining error traced to a pre-existing, untouched line); `ruff
  --select F,I` clean. All independently re-run on `main` after
  cherry-picking, including hand-verified arithmetic on same-sign extend,
  opposite-sign close, and cross-through-flat cases in the paper broker's
  signed-bookkeeping rewrite.
- **Demo prerequisite, not a code change**: both the automated closer and
  the manual "Close now" button hard-require `USE_POSTGRES=1` **and** a
  real, connected Alpaca paper OAuth connection — a coherent open-and-close
  demo needs `USE_POSTGRES=1` + a real Alpaca paper connection +
  `TRADING_MODE=paper` + `ALLOW_SHORTS=1`, all set together.

### 2026-08-26 — `72ccef8a`…`614fc42d` feat(agents,api,mobile): surface the deterministic scanner on the UI, close a dedup gap

The scanner itself (`packages/engine/engine/scanner/` — 21 named
deterministic trigger rules, zero LLM calls, added earlier the same day in
`699ac789`) was correct and well-tested, but its findings were shown
nowhere on the UI, and a scanner-triggered council run's cost-dedup
guarantee was weaker than `engine.scanner.cooldown`'s own docstring
promised.

- **Dedup fix** (`72ccef8a`): the scheduler's trigger loop called
  `daily_cron.main(force=True, ...)` to skip the market-calendar gate —
  but `force` *also* bypassed the once-per-(user, symbol, day) Postgres
  dedup check, meaning a triggered run after a process restart had zero
  protection against double-spending LLM cost on a symbol already decided
  that day. Added an orthogonal `skip_calendar_gate` kwarg to
  `daily_cron.main()`; the scheduler now passes `force=False,
  skip_calendar_gate=True` instead. `force`'s own contract (the CLI
  operator's "run it anyway," bypassing both gates) is untouched —
  `test_force_runs_even_when_already_decided` passes unmodified, confirmed
  explicitly, not just by a full-suite green.
  **Trade-off, accepted rather than further engineered**: since the
  baseline sweep already covers the full watchlist once a day
  unconditionally, the trigger loop now only adds real value in the window
  *before* that daily sweep fires — a later trigger still shows correctly
  on the new UI (below), but the council run itself becomes a no-op skip.
  This is the correct, intended consequence of honoring the documented
  contract uniformly, not a regression.
- **New `GET /api/v1/scanner/status`** (`ef4231f1`): a dedicated endpoint
  (not a `HealthResponse` extension — that schema is a lossy
  one-line-per-component summary and can't carry a signal list), backed by
  new scheduler observability fields (`last_scan_result`,
  `last_council_run_symbols`, `trigger_loop_armed`,
  `scanner_interval_minutes`, `scanner_max_council_runs`) that were mostly
  already computed and just never read by anything.
- **UI** (`614fc42d`): a new, deliberately *separate* "Scanner" card on
  both the desktop `Dashboard.tsx` and the mobile tab index — not folded
  into "Opportunity Radar" (which means pending council approvals, not
  scanner flags; conflating the two risks a viewer thinking a flagged
  symbol is already an actionable trade). Renders all 4 honest states
  (scheduler off / armed-but-unavailable / clean / signals-present) via a
  new shared `useScannerStatus` hook mirroring `useHealthFull`.
- `.env.example` gained `COUNCIL_SCHEDULER_ENABLED`/`SCANNER_ENABLED`/
  `SCANNER_INTERVAL_MINUTES`/`SCANNER_MAX_COUNCIL_RUNS` plus the 7
  scanner-rule threshold vars from `scanner/select.py` — all previously
  undocumented and off by default everywhere.
- New tests: `apps/agents/tests/test_daily_cron.py` (+2, pinning the
  dedup-fix regression and the calendar-bypass-is-independent case),
  `apps/api/tests/test_council_scheduler.py` (new, 4 tests, including one
  asserting the exact `force=False, skip_calendar_gate=True` kwargs reach
  `cron_main` — the tripwire against ever regressing to the old call),
  `apps/api/tests/test_scanner_route.py` (new, 3 tests).
- **Operational note for future multi-agent sessions in this repo**: this
  work and the short-position work above were built concurrently in
  separate git worktrees, and *both* independently hit the same `git
  stash`/`git stash pop` collision — `refs/stash` is shared across all
  worktrees of one repo (they share a single `.git` dir), so two agents
  stashing around the same time can pop each other's entry. Both recovered
  with zero data loss (reading the dangling stash commit's tree directly
  rather than trusting the ref), but avoid `git stash` inside a
  worktree-isolated subagent when another may be running concurrently —
  use a throwaway branch or a plain diff capture instead.
- Verified: `packages/engine/tests/test_scanner_engine.py` +
  `test_scanner_triggers.py` unchanged (62 passed); full `packages/engine/tests`
  + `apps/agents/tests` 248 → **253 passed / 1 skipped**; full
  `apps/api/tests` 209 → **221 passed / 7 skipped**; `mypy apps/api/app
  apps/agents` 135 → **138 errors** (delta fully attributed — the 3 new
  files check clean, remaining new lines are either this file's existing
  100%-untyped-test-helper convention or line-shifts of pre-existing
  errors); `ruff --select F,I` clean. All independently re-run on `main`
  after cherry-picking.

### 2026-08-25 — `9d938eef` refactor(api): collapse the store-selector duplication into a shared helper

Closes technical-debt items #3 and #5 above. Scope was six files, all
under `apps/api/app/services/`: `auth/auth_store.py`,
`broker/broker_store.py`, `notifications/notification_store.py`,
`council/review_store.py`, `council/store.py`, and
`council/watchlist_store.py` — deliberately excluding
`orders/executor.py`/`orders/order_store.py` (item #4 above), which a
parallel agent was working on in its own worktree at the same time.

- **New `app/core/singleton.py::LazyEnvSingleton[T]`**, matching the
  small-focused-utility style of `core/time.py`/`core/ids.py`.
  Parametrized by **factory callables**, not bare classes, specifically
  so it covers both shapes found in the six files: a Mock/InMemory impl
  defined right there in the module (pass the class itself — a no-arg
  constructor already satisfies `Callable[[], T]`) and a Postgres impl
  that needs a lazy import to defer pulling in SQLAlchemy until
  `USE_POSTGRES=1` actually selects it (pass a small `_build_postgres_x()`
  closure that does the import). `council/store.py` needed the
  closure-wrapper treatment on **both** sides, since it lazily imports
  `MockStore` too, not just `PostgresStore`. `.get()`/`.reset()` are the
  only two methods. Each of the six modules keeps its existing
  module-level singleton variable name, now holding a `LazyEnvSingleton`
  instance instead of `T | None`, plus its two existing public functions
  (`get_x_store`, `reset_x_store_for_tests`) as one-line delegators — the
  ~30 call sites across routers/tests that call those two names were
  left untouched, on purpose.
- **`PendingOAuthCache` (bundled inside `broker_store.py`) does not use
  the shared helper.** It's a second, unrelated singleton in the same
  file (short-lived OAuth state cache, no Postgres/Mock split to switch
  on — always the same implementation, just lazily constructed), so
  `LazyEnvSingleton` doesn't fit its shape. Kept as its own small
  hand-rolled lazy singleton with a comment explaining why, and its reset
  folded into `reset_broker_store_for_tests()` alongside the
  shared-helper-based store's reset — same combined public function as
  before, two singletons cleared under the hood.
- **`watchlist_store.py` moved `services/council/` → `services/watchlist/`**
  (new bucket, `git mv` + one new empty `__init__.py`). It had zero real
  import coupling to anything else in `council/` (verified by grep, not
  assumed) — a "closest fit, not a clean fit" judgment call from the
  file-split commit (`7b74bfd6`) that a dedicated `watchlist/` bucket
  resolves cleanly, matching the one-domain-per-bucket shape of the other
  five buckets. Three import sites updated to the new path:
  `app/schemas/agent.py`, `app/routers/watchlist.py`, and
  `apps/api/tests/test_agent_symbol_validation.py` (all three imported
  `SYMBOL_RE` and/or `get_watchlist_store` by the old dotted path).
- **Added `reset_watchlist_store_for_tests()`** — this function did not
  exist before this commit anywhere in `watchlist_store.py`. Nothing
  currently calls it (no test in the suite exercises cross-test state for
  the watchlist store), so this is purely additive: added for parity so
  all six modules expose the same `get_x_store`/`reset_x_store_for_tests`
  pair, on the theory that a future test needing it shouldn't have to add
  the missing half under time pressure.
- Verified: `apps/api/tests` 209 passed / 7 skipped, byte-for-byte the
  same as the pre-refactor baseline; full Python suite (`apps/api` +
  `apps/agents` + `packages/engine` + `packages/broker`) 378 passed / 8
  skipped, also unchanged. The 18 test files that specifically exercise
  these six stores' `reset_*_for_tests` functions (`test_auth*.py`,
  `test_broker.py`, `test_notifications.py`, `test_health_route.py`,
  `test_orders_route.py`, `test_paper_mode.py`, `test_portfolio_route.py`,
  `test_positions_route.py`, `test_rate_limit.py`,
  `test_tenant_isolation.py`, `test_zerodha_*.py`,
  `test_strategies_route.py`, `test_review_route.py`,
  `test_circuit_breaker_route.py`, `test_agent_symbol_validation.py`) run
  clean in isolation too: 170 passed. `ruff check apps/api --select F,I`
  clean. `mypy apps/api/app`: identical 64 pre-existing errors, same
  files and messages as the pre-refactor baseline (the moved
  `watchlist_store.py`'s one pre-existing `rowcount` nit moved with it,
  unchanged) — zero new findings.
- Nothing left open from this pass. `executor.py`/`order_store.py`
  (item #4 above) stays a separate, dedicated pass by design — out of
  scope here, different worktree, different agent.

### 2026-08-25 — `04559318` fix(api): resolve mypy type-narrowing gaps in postgres_auth_store

First of four items delegated to parallel subagents (in isolated git
worktrees) from the "Technical debt & follow-ups" list two entries below.
This one: the 3 mypy errors that the `py.typed` fix made visible in
`apps/api/app/services/auth/postgres_auth_store.py`.

Both were type-narrowing gaps, not bugs — `upsert_user`'s `row` picked up
an inferred non-Optional `User` from the if-branch's `.scalar_one()`,
making the else-branch's `session.get()` (`User | None`) look like an
incompatible reassignment; fixed with an explicit annotation, the
existing `assert row is not None` untouched. `mark_magic_link_used` and
`rotate_session` read `.rowcount` off a statically-`Result[Any]`-typed
`session.execute()` call; fixed with `assert isinstance(result,
CursorResult)`, verified empirically (a standalone sqlite smoke test, not
just an assumption) that a Core UPDATE with no `.returning()` is always a
CursorResult at runtime.

Mypy on the file: 3 → 0. Whole-package: 64 → 61 (confirmed via
`git stash`/`stash pop` that only this file's count moved). Full
`apps/api` suite: 209 passed / 7 skipped, unchanged. Independently
re-verified on `main` after cherry-picking, not just taken on the
subagent's word.

**Honest gap surfaced, not fixed here:** `test_postgres_stores.py` does
have tests exercising all three fixed code paths, but none of them run by
default — they're `skipif`-gated on `RUN_POSTGRES_TESTS=1` + a live DB,
and the module isn't even imported in the default run (the store factory
imports it lazily behind `env_flag("USE_POSTGRES")`, and each test
imports `PostgresAuthStore` inside its own body, which never executes
once skip-marked). Today's signal for this file is mypy + the ad hoc
runtime check above, not CI. Worth wiring a real Postgres test lane at
some point; not attempted here.

### 2026-08-25 — `5df75196` + `1e4b760d` refactor(orders): split executor.py and order_store.py along their real seams

Fourth of four items delegated to parallel subagents (Opus, given the safety-critical surface). Scoped module-boundary reorg of the two files the "agents propose, deterministic code disposes" rule flows through — no behavior change, verified rather than assumed. `executor.py` (~780 lines) and `order_store.py` (~620 lines) each mixed several concerns behind one filename; split out the four that could be moved with genuine confidence of zero behavior change, and left three explicitly alone with reasons, per the task's own permission to scope down.

- **`order_store.py` → `decision_risk.py`**: `resolve_decision_uuid`, `DecisionRiskRow`, `load_decision_risk_row`, `had_same_day_entry` — the council-inputs read path that feeds the execution-time risk re-run. Moved verbatim.
- **`order_store.py` → `execution_claim.py`**: `EXECUTING`, the in-memory claim set, `claim_decision_for_execution`/`release_execution_claim`/`finalize_execution_claim`/`reset_execution_claims_for_tests` — the compare-and-swap mutex against `agent_decisions.user_response` that makes a double-approve safe. Moved verbatim; imports `resolve_decision_uuid` from `decision_risk.py`. `order_store.py` is now just the order-row CRUD its name promises (`persist_order_submit`/`persist_linked_order_submit`/`persist_order_result`); docstring trimmed to match, now-unused imports (`threading`, `dataclass`, `datetime`/`UTC`, `ZoneInfo`, `Any`, `utc_now`) dropped.
- **`executor.py` → `live_trading_gate.py`**: `_live_trading_enabled` plus the two-key (operator env + per-connection consent) block-check that used to open `_execute_via_broker`, now `check_live_trading_gate(conn, ...)`. Moved verbatim, including the exact log message and `ExecuteResponse` fields.
- **`executor.py` → `execute_response.py`**: `build_execute_response`, a pure function wrapping a placed/filled order into the camelCase `ExecuteResponse`/`OrderResponse` DTO. Both `_execute_via_broker`'s (real broker) and `_execute_paper`'s (in-memory fill) tail-end response construction now call it with the exact same field expressions they used inline — no field mapping changed, just de-duplicated.
- **Grep-verified every call site first**: only `executor.py`, `position_manager.py` (imports `_build_risk_context` from `executor` and `persist_linked_order_submit`/`persist_order_result` from `order_store` — neither moved, so it needed zero changes), and `tests/test_executor_correctness.py` (one import path) referenced any of the moved names anywhere in the repo.
- **Deliberately NOT split** (documented rather than attempted): the risk-reevaluation orchestration (`_load_db_state_or_fail`, `_build_risk_context`, `RiskInputs`/`load_risk_inputs`, `_re_run_risk`) and the in-memory paper-execution engine (`_execute_paper`) stay in `executor.py`. Both are reached through `executor_mod.<name>` monkeypatches in `test_executor_correctness.py`/`test_executor_risk_context.py` (`with_broker_client`, `load_risk_inputs`, `_load_db_state_or_fail`, `load_db_risk_state` are all patched as attributes of the `executor` module) — moving the callees to a different module while the callers stayed in `executor.py` would silently stop those patches from reaching the real call, since Python resolves a bare name against the module the *calling* function is defined in, not wherever the name was originally defined. `test_pdt_block_reaches_the_broker_path` (patches `executor_mod._load_db_state_or_fail`, exercised via `_execute_via_broker` → `_build_risk_context`) is the concrete case that would have broken this way — not loudly in all cases, which is exactly why it wasn't worth the risk. This is the "paper mode is the single riskiest piece" case the task brief flagged in advance; did the smaller, verifiable half instead of forcing the full split.
- **Re-read the two safety-critical tests myself, not just the subagent's account**: opened `test_concurrent_approvals_place_exactly_one_order` and `test_live_agent_buy_without_bracket_is_refused` directly. The first exercises `claim_decision_for_execution`'s in-process `threading.Lock`-guarded compare-and-swap (`_claim_in_memory`) — synchronous, no `await` inside the locked section, and unaffected by which module the function is filed under since it's reached via an ordinary top-level import, not a monkeypatch, in this test. The second depends entirely on the *bracket-legs* check, which was never touched and stayed in `executor.py`; `_patch_broker`/`_patch_risk_inputs` in the test file confirmed the two things this refactor could have broken (`with_broker_client`, `load_risk_inputs`) are patched as `executor_mod` attributes and neither was moved out of that module — exactly the constraint the split respected.
- Files: `apps/api/app/services/orders/{decision_risk,execution_claim,live_trading_gate,execute_response}.py` (new), `order_store.py`, `executor.py`, `tests/test_executor_correctness.py` (import path only, one line).
- Verified independently on `main` after cherry-picking both code commits (docs commit re-authored by hand instead of cherry-picked, to avoid another `## Entries` insertion conflict like the one above): `apps/api` suite 209 passed / 7 skipped; cross-package suite 169 passed / 1 skipped; `ruff check apps/api --select F,I` clean; `mypy apps/api/app` 60 errors, down from the 61-error baseline left by the store-selector entry above — the expected `-1`, zero new findings.
- Follow-up left open: the three concerns still inside `executor.py` (risk-reeval orchestration, broker placement/bracket-leg logic, the paper-execution engine) are fair game for a future pass, but only alongside updating the tests' monkeypatch targets in the *same* change — not as a "just move the code" step, for the reasons above.

### 2026-08-25 — `938821c2` fix(tooling): get eslint and jest actually running across the JS workspace

Closes debt item #1 above: both were declared but non-functional
repo-wide, confirmed before touching anything (no `eslint.config.js`
anywhere; `jest` installed nowhere in the workspace, no
`jest.config.*`) rather than assumed from the earlier flag.

**ESLint** — one root `eslint.config.mjs` (flat config resolves upward
from any package directory, so a single file covers `apps/mobile`,
`packages/ui`, `packages/shared-types`). Checked `airbnb-typescript`'s
actual state first rather than reaching for it by habit: last published
2024-03 on top of `eslint-config-airbnb` (last published 2021-12),
never gained flat-config support, predates ESLint 9's flat-config-only
requirement entirely — not viable. Used `typescript-eslint`'s
`recommendedTypeChecked` (via `projectService`, the monorepo-friendly
per-file tsconfig auto-discovery — no manual `project: [...]` globs)
plus current flat presets from `eslint-plugin-react`,
`eslint-plugin-react-hooks`, `eslint-plugin-jsx-a11y`, and
`eslint-config-prettier`. `eslint-plugin-react-native` ships no flat
preset as of 5.0.0, so its rules are hand-picked rather than spread
from `configs.all` — including `no-color-literals`, a direct
enforcement of CLAUDE.md's "tokens only, no raw hex" rule.

Two tunings, both found by actually running it, not assumed:
- `eslint-plugin-react-hooks` v7's `recommended` folded in the full
  React Compiler readiness rule set (`immutability`, `purity`,
  ref-access timing, compiler config/gating). This repo doesn't use the
  Compiler — not on CLAUDE.md's locked stack — and several fired on
  ordinary, correct RN code (`useRef(new Animated.Value(x)).current`,
  a standard lazy-init pattern, flagged by `refs`). Turned those off;
  kept `rules-of-hooks` / `exhaustive-deps` / `set-state-in-effect` /
  `set-state-in-render` / `error-boundaries`, which catch real bugs
  independent of the compiler.
- `react-native/no-raw-text` doesn't see through custom
  `<Text>`-wrapping components, and `apps/mobile/src/desktop/**` turned
  out to be a deliberately separate, web-only DOM tree (plain
  `div`/`span`/`button` styled via `CSSProperties`, not RN's
  `View`/`Text` model — confirmed from `primitives.tsx`'s own header
  comment, not guessed) that the RN-specific rules don't apply to at
  all. Excluding `desktop/**` from the `react-native` plugin and adding
  the app's own Text wrappers (`TileLabel`, `TileValue`, `HeroHeadline`,
  `HeroSub`, `SectionLabel`) to `no-raw-text`'s skip list took the first
  real run from 439 problems to 34 in `apps/mobile` — the rest were
  genuine.

Fixed what was cheap and mechanical: JSX entity escaping, one useless
regex escape, `consistent-type-imports`/`no-unnecessary-type-assertion`
autofix, one stray unused import in `desktop/Shell.tsx`, and
eslint-disable comments that either named an already-renamed rule
(`no-var-requires` → `no-require-imports`) or — twice, my own mistake,
caught by re-running rather than assumed fixed — were one line off from
the code they were meant to cover. Left as reported findings, not
fixed: `apps/mobile` ends at 34 problems (31 errors, 3 warnings) —
~10 `no-floating-promises` and ~15 `no-unsafe-*`/`no-base-to-string`
across the hooks and `api.ts` (need call-site judgment: await vs. void
vs. `.catch`, not mechanical) — and `packages/ui` ends at 6 (3 errors,
3 warnings) — `no-color-literals`/`no-inline-styles` on
`SwipeDeck.tsx`/`Toggle.tsx`/`ConfidenceBar.tsx`'s shadow styles and one
`set-state-in-effect` in `ApprovalCard.tsx` (design/behavior calls, not
one-liners). `packages/shared-types` is clean. Matches the brief's bar
for this pass: a working setup, not a zero-warnings codebase.

**Jest** — `jest-expo` for `apps/mobile`, version-matched to this app's
actual `expo@~54.0.0` (`jest-expo`'s major tracks the Expo SDK number —
confirmed against the npm registry, not assumed) rather than its own
`latest` dist-tag (57.x, a newer SDK line). Its own dependency graph
pins `babel-jest`/`jest-snapshot`/`@jest/globals` to `^29.2.1`, so
top-level `jest` is pinned to `^29.7.0` rather than the unrelated
`jest@30` — running them together would be an unverified combination
jest-expo's own manifest argues against. Its preset also auto-derives
`moduleNameMapper` from `tsconfig.json`'s `paths` (confirmed by reading
`jest-preset.js`, not assumed), so the `@/*` and `@app/*` aliases used
throughout `src/` and `app/` resolve with zero extra jest config. Added
one smoke test — `api.ts`'s `ApiError`/`TradingLockedError`/trading-lock
state — deliberately not a component render, which would hit
jest-expo's bundled `react-test-renderer@19.1.0` against the app's
pinned `react@19.1.4` (React requires those two to match exactly).

`packages/ui` gets plain `ts-jest` instead — it has no Expo dependency
of its own, RN is only a peer dep, so `jest-expo`'s preset would be
pulling in Expo-specific mocking for a package that never touches Expo.
Added a `lint` script (previously missing) and a smoke test for
`utils.ts`'s pure helpers (`cn`/`formatUsd`/`formatMmSs`/
`formatRelative`/`secondsUntil`). `packages/shared-types` gets the
`lint` script only — it's type-only declarations, nothing to unit test.

**Prettier** was a devDependency with no config and no script before
this — added `.prettierrc.json` + `.prettierignore` (excludes the
Python side, the hand-authored root docs, and `infra/`/`scripts/` — not
this pass's concern) + root `format`/`format:check` scripts. Ran
`format:check`, not `format --write`, across the pre-existing codebase
— confirmed it runs (77 files would need formatting) without
unilaterally reformatting everything as a side effect of wiring the
script.

**Independently re-verified on `main` after cherry-picking, not just
taken on the subagent's word** — including re-deriving, not just
re-running, the riskiest-looking part: the diff touches 31 files at
191/190/104 changed lines apiece in `login.tsx`/`verify.tsx`/`Council.tsx`,
which reads alarmingly large for a pass billed as "mechanical." Diffed
every large file against its parent with whitespace ignored (`git diff
-w`) to isolate the real changes from Prettier reflow: every file
reduced to either JSX entity escapes, the `no-unnecessary-type-assertion`
autofix (verified safe by hand for the `typeof`/`in`-narrowing cases,
and for `desktop/Shell.tsx`'s `{ name: item.id } as DesktopRoute` →
`{ name: item.id }` — a discriminated-union case worth not trusting by
eyeball — by letting `tsc` itself be the authority instead of
hand-simulating the checker), or the useless-regex-escape fix in
`watchlist.tsx`. Then ran `pnpm install --frozen-lockfile` (confirms the
committed lockfile is internally consistent) and independently: `tsc
--noEmit` clean on all three packages, `apps/mobile` test 4/4 and
`packages/ui` test 13/13 pass, and lint reproduces the exact counts
claimed — `apps/mobile` 34 problems (31 errors/3 warnings),
`packages/ui` 6 (3 errors/3 warnings), `packages/shared-types` clean.
- Docs commit re-authored by hand instead of cherry-picked (same
  `## Entries` insertion-point conflict as the two entries above).

### 2026-08-25 — `a6a771a7` + `9aec5e66` + `7b74bfd6`: apps/api cleanup — the backlog the refactor pass parked

The refactor pass two entries below this one explicitly scoped `apps/api`
out ("out of scope here... apps/api is 17,697 lines — more than the other
three combined") and left a ranked backlog. This closes most of it, in the
same "verify against the real code, not the filename" spirit as that pass.

**`a6a771a7` — collapsed the duplicated helpers.** Not 13 copies of the
truthy-env check as originally estimated — **18**, once the local
`_postgres_active()` wrappers and one `_require_postgres()` were counted
alongside the `_is_truthy`/`_truthy` family. All 18 now call
`engine.env.env_flag` (the same helper the agents/engine/broker pass
already centralized) instead of reimplementing the check. Also collapsed:
8+ copies of `datetime.now(timezone.utc)` as a local `_now()` into a new
`app/core/time.py::utc_now`, and 3 copies of a UUID-parse helper into
`app/core/ids.py::to_uuid`. Along the way, added the missing `py.typed`
marker to `engine`/`broker`/`trading_agents` — mypy had been silently
skipping type-checking through all three; invisible until routing 18 new
call sites through `engine.env` made mypy start reporting them as
returning `Any` instead of `bool`. One real bug shipped and caught by the
test suite mid-pass, not by review: a `replace_all` matched inside a
`utc_now()` call this same pass had already inserted, producing
`utcutc_now()` — fixed, then the whole suite re-run clean.

**`9aec5e66` — dead files.** `apps/mobile/src/hooks/useRunAgent.ts`
(superseded by the async start+poll "council theater" pattern —
`useCouncilRun` — with zero remaining callers; the synchronous backend
endpoint it used stays, since 5 test files and the smoke script still
exercise it directly) and `packages/ui/src/PnLBadge.tsx` (exported,
documented in two other components' docstrings, never actually used by
any screen — `PnLPill` is what shipped instead). Also: `expo-haptics`,
`react-native-gesture-handler`, `react-native-reanimated` were imported
directly by three components in `packages/ui` but never declared as
dependencies of that package — only worked by accident of pnpm's hoisted
linker. Added as `peerDependencies`. README.md moved to `docs/README.md`
(the `.dockerignore` carve-out for it turned out to be unused anyway —
nothing ever copied it into any build stage).

**`7b74bfd6` — the big one: `app/services/` split into six subpackages**
(`auth/`, `broker/`, `orders/`, `council/`, `notifications/`, `platform/`),
closing the #1 item on the parked backlog (38-file flat directory → the
biggest readability win left). Grouped by actual import coupling, verified
via grep rather than assumed from filenames — two files landed somewhere
non-obvious: `crypto.py` went to `broker/` (its only importers are
broker-domain) rather than a generic "platform" bucket, and
`watchlist_store.py` went to `council/` as the closest fit despite having
no real coupling to anything — flagged as a judgment call in the commit,
not a clean fit.

The mechanical rewrite (61 files reference `app.services` somewhere) was
scripted rather than hand-edited, and the script broke twice before
landing — both times from the same root cause: the "auth" bucket shares
its name with the "auth" module (same for "notifications"), so a
substitution pass that inserts a bucket prefix and then runs ANOTHER pass
afterward will match the prefix it just inserted, doubling it
(`app.services.auth.auth.auth_store`). A single combined-regex pass over
the *original* text — never re-scanning its own output — was the actual
fix. Both breaks were caught immediately by the test suite failing at
collection (`ModuleNotFoundError`), never by review, and both times the
change was still fully uncommitted, so recovery was a clean `git reset
--hard` back to the prior commit rather than an unwind.

**Verified once, at the end of all three:** full Python suite (`apps/api`
+ `apps/agents` + `packages/engine` + `packages/broker`) still 378 passed
/ 8 skipped throughout — same number the refactor pass below reported
before this backlog existed. `ruff --select F,I` (the rule families this
project actually enables) clean. `mypy apps/api/app`: identical 64
pre-existing errors, same files, only the paths changed — zero new
findings from either the dedup or the file moves. Not fixed (flagged, not
this session's scope): 3 pre-existing, `assert`-guarded, Postgres-only
mypy nits in `postgres_auth_store.py` that the `py.typed` fix made visible
for the first time; the still-broken repo-wide `eslint`/`jest` JS tooling
(flagged in an earlier entry, unrelated to this pass); the 6-module
in-memory-vs-Postgres singleton-selector duplication and the mixed
concerns inside `executor.py`/`order_store.py` that the refactor-pass
backlog also named — both real, both lower-confidence-of-a-clean-collapse
per the investigation that found them, left for a dedicated pass.

### 2026-08-25 — web UI wired onto the Railway domain

User report: visiting the Railway domain showed the raw FastAPI 404
(`{"detail":"Not Found"}`) instead of the app. Root cause: the API has
never had anything registered at `/` — Railway's single service only ever
ran `apps/api`'s Dockerfile, and no web build of `apps/mobile` has ever
existed (no export step, no "build" script, nothing in the image). This
was never a routing bug to fix, it was a missing feature to add.

**What's now wired up**, verified end to end locally by actually serving
the production export through the API (not the Expo dev server):
- `apps/mobile`: added a `build` script (`expo export --platform web`).
  `resolveBaseUrl()` in `src/lib/api.ts` now resolves same-origin
  (`window.location.origin`) for a production web build specifically
  (`!__DEV__`) — the export is served BY the API itself, so same-origin
  is always right and survives a domain change with no rebuild; dev
  (`expo start --web`) is untouched, still falls through to the existing
  debugger-host/localhost logic.
- `apps/api/Dockerfile`: new `web-builder` stage (`node:22-slim`, pnpm
  9.12.0 via corepack) runs the export; the runtime stage copies its
  `apps/mobile/dist` output into the image at the same relative path a
  local checkout would have it, so `main.py` finds it with no env var.
- `.dockerignore`: stopped blanket-excluding `apps/mobile` /
  `packages/ui` / `packages/shared-types` (they were never needed by the
  Python-only build before; `**/node_modules` and `**/.expo` already cover
  the heavy stuff generically, so nothing got less lean).
- `apps/api/app/main.py`: mounts `/_expo` + `/assets` as static files and
  adds a catch-all `GET /{full_path}` — registered dead last, so every
  API route still wins its match first — that serves `index.html` for
  anything else (client-side routing handles the rest), except `/api/*`
  which still 404s as JSON. New `_CSP_WEB` policy (`self` + Google Fonts
  for the desktop tree's Inter/Space Grotesk) scoped to non-API,
  non-`/docs` paths only — the existing strict `default-src 'none'` API
  policy is untouched for `/api/*` and `/health`.
- Entirely inert in local dev unless you've actually run
  `pnpm --filter @app/mobile run build` yourself — `uvicorn --reload`
  without that step logs "Web UI disabled" and behaves exactly as before.

**Verified locally** by building the real export and serving it through
`uvicorn` on port 8000 (not 8081): `GET /` → 200 HTML, `GET /positions`
(an arbitrary client route) → 200 HTML (SPA fallback), `GET
/api/v1/does-not-exist` → still 404 JSON, `GET /health` → unaffected, CSP
header correctly differs by path (`curl` against all four). Then in an
actual browser against that same port-8000 build: fresh dev-token logins
landed correctly on Platinum Glass at desktop width and the calm mobile
UI at 375px, in the SAME build — confirming the `DesktopShell` width fix
and the auth-race fix above both hold in a real production export, not
just the dev server.

**Not verified — no Docker locally in this environment.** The Dockerfile
itself was reviewed by hand (Node 20+ requirement satisfied by
`node:22-slim`, pnpm version matches `packageManager` in package.json,
the exact `pnpm install && pnpm --filter @app/mobile run build` sequence
already proven above) but never actually run through `docker build`.
Railway will build it for real on the next push — worth watching that
first build's logs.

### 2026-08-25 — `4729a13f` + `37e4a4ac`: desktop-on-first-load bug + magic-link verify loop/race

User-reported: Railway showed a bare `{"detail":"Not Found"}`, and locally
they saw "account failed and everything." The Railway 404 is tracked
separately (no web build has ever been wired to that service — see the
next entry). Locally, ran the real stack (`DEV_AUTH_BYPASS=1` API +
`expo start --web`) and reproduced two real bugs by driving the actual
login flow in a browser rather than guessing from the code.

**`4729a13f` — desktop view stuck on mobile UI on first load.** Confirmed
via direct instrumentation (temporary `console.log`, removed before
committing) that `useWindowDimensions()` reports `width: 0` on a brand-new
tab's first paint and never self-corrects — there's no resize event to
prompt a re-read when the viewport was already at its final size before
the bundle ran. `DesktopShell` trusted that value, so a first-time visitor
on a laptop got the phone UI; a plain reload of the same tab "fixed" it,
which is exactly the kind of bug a developer testing via repeated reloads
would never see. Fix reads `window.innerWidth` directly on web (native
untouched). Verified on a brand-new tab's very first load, repeatedly, in
both directions (< 1024px and >= 1024px), both before and after login.

**`37e4a4ac` — magic-link verify could loop and silently sign the user
back out.** Two compounding bugs on `/auth/verify`, found by watching the
network tab through a real dev-token login rather than reading the code
in isolation:
- The verify effect re-firing with the same (email, token) pair 401'd on
  the second POST (tokens are one-shot) — observed as high as 6 repeated
  attempts before the API's own rate limiter started returning 429 and
  cutting it off. A `useRef` guard does not survive a remount, so a first
  attempt at fixing this (still `useRef`-based) did not actually hold;
  moved to module-level state, which does.
- Separately, `AuthBootstrap` unconditionally calls `restore()` on every
  mount, including when landing directly on `/auth/verify` with a token —
  firing an independent `/auth/refresh` against whatever old refresh
  token happened to already be in storage. When that resolved *after* the
  verify screen's own `signIn()` had already established the new session,
  its failure handler wiped the session and dropped status back to
  `unauthenticated` — reproduced directly: verify returned 200, but the
  screen sat on "Signing you in…" forever while an unrelated refresh
  401'd in the background, no error shown anywhere. Fixed by skipping
  `restore()` when the active route is `/auth/verify` with both params
  present — that flow is authoritative for the session in that case.
- Verified clean (exactly one verify + one refresh call, correct landing
  screen) with fresh dev-token logins on both mobile and desktop widths,
  after restarting the API to rule out my own testing's rate-limit noise
  as a confound.

`tsc --noEmit` clean after each change. Not yet re-run against a real
Alpaca-connected account or the production Railway build — this was a
local, `MockStore` + `DEV_AUTH_BYPASS=1` repro.

### 2026-08-25 — `f2156171` + `9e5604f9`: Platinum Glass desktop UI (documented + verified retroactively)

**Process note, stated plainly:** neither commit got a build-log entry when it landed, and
`9e5604f9`'s message is the literal placeholder "Latest commit" — not Conventional Commits. Both
are already pushed to `main`, so per this repo's git-hygiene rules the message is not being
rewritten; this entry (and [`STITCH_DESIGN_SYSTEM.md`](STITCH_DESIGN_SYSTEM.md), added alongside
it) is the missing documentation, written after the fact by re-deriving what the diffs actually
do and verifying it against the running app rather than trusting the commit message.

**What's there.** `f2156171` fixed the desktop build (react/react-dom version mismatch pinned
`react-dom` to 19.1.4; `BiometricGate` now treats web as N/A instead of fail-closed; Alpaca env
name unified between broker and engine) and shipped a *placeholder* `DesktopShell` that just
framed the phone UI in a centered column on wide screens. `9e5604f9` replaced that placeholder
with the real thing: a second, self-contained UI tree at `apps/mobile/src/desktop/` — 18 files,
~4,400 lines — implementing the "Platinum Glass" system against `STITCH_DESIGN_SYSTEM.md` (a doc
that, until this entry, didn't exist in this repo either; see that file's own note on this).
`DesktopShell` gates on `Platform.OS === 'web' && width >= 1024 && isAuthed` and **replaces**
the router subtree rather than wrapping it — native and narrow-web paths never evaluate the
desktop module (guarded `require`), so the phone build is untouched by construction, not by
convention.

**Verified, not assumed:**
- `tsc --noEmit` clean across the whole mobile package (includes the desktop tree).
- Booted `expo start --web` and drove it with a browser: dark mode is the default (matches spec),
  the light/dark toggle works, and all 7 sidebar sections (Dashboard, Picks, Positions,
  Strategies, Review, Insights, Settings) render real content wired to the actual
  `useAccount`/`usePendingApprovals`/`useAuthStore` hooks — not placeholder markup. With no API
  running, screens correctly show shimmer/empty states rather than crashing or printing raw
  "No data," which is what the spec requires.
- Confirmed mobile isolation directly: at 375×812 after a reload, the app renders the plain
  mobile sign-in screen with zero desktop chrome — even with the desktop auth gate temporarily
  forced open for the test (reverted before finishing; net diff on `DesktopShell.tsx` is zero).
- `apps/mobile` lint (`eslint . --max-warnings 0`) does **not** run — root `package.json` pins
  `eslint@^9` but no `eslint.config.js` (flat config) exists anywhere in the repo. This predates
  the desktop work (confirmed via `git log` on the config files — there is no prior config to
  have regressed) and isn't specific to it; flagging here since it means `lint` gave no signal on
  either desktop commit or anything else recently. Worth its own fix.

**Docs added this entry:** [`STITCH_DESIGN_SYSTEM.md`](STITCH_DESIGN_SYSTEM.md) at repo root —
the desktop companion to `DESIGN.md`, with tokens/glass/motion verified line-for-line against
`apps/mobile/src/desktop/theme.ts` rather than transcribed from the original brief. `DESIGN.md`
now links to it. See that file's own change log for specifics.

### 2026-08-25 — `3f0b551a`…`47f7081f` refactor: presentation pass over agents/engine/broker

Six commits making the deterministic + agent packages readable cold, ahead of a
code walkthrough. No behavior changes; **378 passed / 8 skipped** throughout, and
MOCK mode (no API keys) re-verified on every moved entry point.

- **Lint debt cleared** (`3f0b551a`). `uvx ruff check` on these paths went 198 →
  0. Configured rather than suppressed: workspace packages declared isort
  first-party (`engine` was sorting in among PyPI deps), and RUF001/2/3 + UP042
  ignored with the reasoning inline — this codebase writes prose with em-dashes,
  and `(str, Enum)` is not swappable for `StrEnum` without changing `str()`
  output. Re-attached six WHY-comments that rode on now-dead `noqa` directives.
- **`engine.env.env_flag`** (`eb01e954`) — one definition of what a truthy env
  switch is, replacing six private copies in apps/agents. Deleted the empty
  `trading_agents/tools/` package. **apps/api still has 13 copies — see below.**
- **`engine/db/models.py` split** (`5e484439`) — 727 lines / 16 tables in one
  file, whose docstring still described the Phase-0 eight. Now
  `models/{accounts,trading,council}.py`, each with an accurate table map. The
  package `__init__` re-exports all 16, so every existing import across api,
  agents and migrations is untouched and Alembic still sees full metadata.
- **Entry points grouped** (`62cadbc2`) — `trading_agents/cli/` (council,
  reflection — run by a human) and `trading_agents/jobs/` (daily_cron,
  ghost_eval — run by cron). This killed a real bug-in-waiting:
  `apps/agents/scripts/` was a namespace-package collision with the repo-root
  `scripts/`, which is why one test could `from scripts.ghost_eval import …`
  while its sibling needed `sys.path` surgery. Both now import normally. Three
  docstrings/README lines documented three different, mostly wrong, invocations;
  all now say the one correct thing.
- **Analyst nodes de-duplicated** (`2b081aed`) — technical/fundamental/macro
  were the same 55-line node three times. The degradation contract ("never
  raise, name yourself in `degraded_nodes` so the Risk Officer knows the council
  was blind") had three implementations that could drift apart. Now one
  `nodes/_specialist.py`; each analyst is a declaration of what differs. Prompt
  text verified byte-identical before/after, and label widths kept per-analyst
  so prompt-cache entries don't bust.

Line counts moved little (agents 5636→5719, engine 7261→7716 of which +326 is
the concurrent FRED work above, broker 1573→1594) because the cuts were
duplication and the additions were docstrings the audit rules require. **The
honest finding: agents/engine/broker are not where the bloat is.** `apps/api` is
17,697 lines — more than the other three combined — and was out of scope here.

Follow-ups, highest value first, all in `apps/api`:
1. `app/services/` is a flat 38-file / 8,646-line directory. Split into
   `services/{auth,broker,orders,council,notifications,platform}/`. Biggest
   single readability win left in the repo.
2. `executor.py` (787) and `order_store.py` (625) are the two largest files in
   the codebase and both mix concerns.
3. Twelve store modules each hand-roll the same in-memory-vs-Postgres singleton
   selector. One shared helper collapses ~12 copies.
4. 13 copies of the truthy-env helper → `engine.env.env_flag` now exists.
5. 8 copies of `def _now()`, all `datetime.now(timezone.utc)` — and apps/api has
   not had the `UP017`/`datetime.UTC` pass the other packages just got.

### 2026-08-25 — `dd5de4b7` fix(engine): make the FRED macro block outage-proof and concurrent

**FRED verified live** against the real Railway key: VIXCLS **15.13**, DGS10 **4.74%**, DTWEXBGS **118.06**. Answering the "IDK WHY USE FRED" question, and the docs link:

- **Why FRED.** [macro.py](packages/engine/engine/features/macro.py) pulls three daily series that feed the Macro Analyst node's prompt as context (never as a gate): VIX = risk-appetite regime, DGS10 = discount-rate/duration input, DTWEXBGS = the dollar's tailwind/headwind on large-cap earnings. These are the right three for a US-equity swing product, and FRED is the free authoritative publisher of all three.
- **`series/observations` is correct; `release/observations` is not.** They answer different questions: `series/observations` returns the observation history of **one** series (what we want), `release/observations` returns everything published in a **release** — hundreds of unrelated series — and is for release-calendar browsing. Empirically the release path also **404s** on the live API today. The `/v2/` in the URL the user linked is the *documentation site's* path, not an API version; probing `api.stlouisfed.org/fred/v2/series/observations` returns 401 (no such route) while `fred/series/observations` returns 200. **Nothing to adopt — our URL and `file_type=json` + `sort_order=desc` are current.**
- **Missing values confirmed handled.** FRED writes `"."` on non-publication days — verified live: DGS10 is `"."` on 2026-01-01. We pull `limit=10` descending and take the first parseable value, which clears the longest US market closure comfortably. `limit=1` would have returned `None` every holiday, so the existing choice was right.

Fixed (the resilience story, which was the real gap):

- The three series were fetched **serially at a 15s timeout each** — a hung FRED could stall the council **45s per symbol**. Now fetched concurrently under one 8s wall-clock budget (`asyncio.timeout` + `gather`); measured 1.96s → 0.53s warm.
- **Failures were not cached**, so an outage cost a full timeout per series *per symbol* in a run. Failures now negative-cache for 5 minutes; successes still cache per (series, UTC day).
- **`FRED_API_KEY` was leaking into logs.** `logger.exception(...)` on an httpx error prints the exception whose message embeds the full request URL — including `api_key`. Now logs only the exception type / HTTP status.
- `asyncio.CancelledError` was being swallowed as a fetch failure; now re-raised.
- New `reset_fred_cache()` export + [test_features_macro.py](packages/engine/tests/test_features_macro.py) (9 tests): holiday `"."` skipping, day-caching, negative caching, key redaction, total outage → all-`None` block, hung-FRED budget, MOCK mode (no key → no network), and concurrency. Suite 351 → **360 passed / 8 skipped**.

`compute_macro` is confirmed resilient end to end: it never raises and never blocks past the budget, so a FRED outage degrades the macro block to `n/a` rather than 500-ing the council.

**Security work package P3 — verified complete, no code changes needed.** All seven items were already landed by the prior session in `ef9e3563` + `c96fcad1` (F2 logout `sid`, F8 `/auth/verify` rate limit + `asyncio.to_thread` scrypt, F3 `DEV_AUTH_BYPASS` default-off, F5 `JWT_SECRET_PREVIOUS`, F14/15/16 CORS/CSP/`no-store`/`TrustedHost`) and in `5d31d2ff` (F30 compare-and-swap execution claim, F31 live bracket-leg hard-refuse). Each has a named test — see `test_auth_p3_hardening.py` and `test_executor_correctness.py::test_concurrent_approvals_place_exactly_one_order` / `::test_live_agent_buy_without_bracket_is_refused`.

### 2026-08-25 — `42360a2c` fix(api) + `d7ca0d03` fix(api) + `195300a8` feat(agents) + `06647cc4` test: security work package P2 (tenancy + prompt injection + LLM bounds)
- **F1 (critical) cross-tenant leakage.** `/review/queue`, `/review/agreement`, `/review/scorecard`, `/strategies/performance`, `/ghost/summary`, `/risk/vetoes` authenticated the caller and then dropped the identity (`_ = user`), returning **every** tenant's symbols, bull/bear text, fill prices and realized P&L. `/decisions/{id}/timeline` was a plain IDOR — any user could read any decision's full council timeline. Root cause was `DecisionLog.all_decisions()` having no user parameter at all.
  - `all_decisions` and `list_pending_reflection` now take a **required keyword-only `user_id`** ([decision_log.py](apps/agents/trading_agents/memory/decision_log.py)) so mypy forces every call site to declare intent — an `Optional`-defaulting-`None` would have let the same bug reappear silently. Scheduled jobs that genuinely grade the whole book (the EOD reflection pass) pass the new module-level `ALL_USERS` sentinel; **no HTTP handler may**.
  - Rows with `user_id is None` (unattributed CLI/smoke runs) belong to no tenant and are invisible to every real user — only `ALL_USERS` sees them.
  - [postgres.py](apps/agents/trading_agents/memory/postgres.py) filters on the indexed `agent_decisions.user_id`; a malformed tenant id returns nothing rather than widening the query to everything. Same fail-closed rule in [ghost_service.py](apps/api/app/services/ghost_service.py), which also never reaches the database for an unknown tenant.
  - `build_biography` checks ownership before assembling and returns None → 404, with the **same** 404 as "no such row" so the endpoint can't be used to probe which decision ids exist.
  - Follow-on: `apply_grade` now 404s when an operator tries to grade someone else's decision.
- **F22/F25 (high) prompt injection via `symbol`.** [schemas/agent.py](apps/api/app/schemas/agent.py) was a bare `symbol: str` flowing verbatim into all seven council node prompts as `f"Ticker: {state['symbol']}"`. Now constrained by the **same** `SYMBOL_RE` that POST `/watchlist` has always enforced — the agent route simply never got the check. Input is trimmed + upper-cased; interior whitespace is deliberately *not* collapsed, so a multi-line payload is rejected rather than flattened into something valid.
- **Node output guardrails.** Analyst `score`/`confidence` were used raw: a returned `score: 900` propagated into `SpecialistScore` and could single-handedly lift the council average past the `min_specialist_avg_score` floor (45.0). Drafter's `int(data.get("risk_level", 3))` raised ValueError on `"high"` or `3.5` and killed the whole run. New [nodes/_guards.py](apps/agents/trading_agents/nodes/_guards.py) clamps score→[0,100], confidence→[0,1], ordinals→[1,5], and rejects NaN/inf/bool (all of which survive `float()` and poison downstream averages). The guards never raise — a bounded-but-degraded pass is recoverable, a crashed one is not. Enforcement is deterministic Python, never prompt text.
- Tests (+68): [test_tenant_isolation.py](apps/api/tests/test_tenant_isolation.py), [test_node_guards.py](apps/agents/tests/test_node_guards.py), [test_agent_symbol_validation.py](apps/api/tests/test_agent_symbol_validation.py). The isolation suite was verified to actually fail (4 tests) when the tenant predicate is neutered; the score-900 test asserts both that the clamped run vetoes **and** that the raw scores would have cleared the floor, so it keeps its meaning if the clamp is ever removed. Full suite 357 passed / 8 skipped.
- Left open: `/ghost/summary`, `/risk/vetoes` and `/decisions/{id}/timeline` are Postgres-only, so their scoping is pinned at the service layer (emitted SQL must carry a `user_id` predicate) rather than over HTTP — worth an integration pass once a test database is wired.

### 2026-08-25 — `ef9e3563` fix(api) + `c96fcad1` fix(auth): security work package P3 (auth + transport)
- **F14/F15/F16 transport** ([main.py](apps/api/app/main.py)): CORS `allow_credentials=False` (auth is Bearer-only) + enumerated methods/headers; added CSP (strict for the API + Zerodha OAuth landing pages, relaxed only on `/docs`/`/redoc`/`/openapi.json`), Permissions-Policy, COOP; `Cache-Control: no-store` on `/api/v1/auth/*` + `/api/v1/broker/*`; `TrustedHostMiddleware` driven by a new `ALLOWED_HOSTS` env (unset → `*` + a production warning, so it can't brick the existing Railway deploy — **set it there**).
- **F8** `/auth/verify` was unthrottled *and* ran one scrypt(n=2**14, ~16MB/50ms) per outstanding magic-link **on the event loop** → unauthenticated CPU amplification. Now 10/h/email + 40/h/IP ([rate_limit.py](apps/api/app/services/rate_limit.py) `check_verify_rate`) and the candidate loop is `asyncio.to_thread(_match_candidate, …)`; the refresh-hash compare moved off the loop too.
- **F2** logout was a no-op without a body. `AuthedUser.session_id` now carries the access token's already-verified `sid`, so `/auth/logout` with no body revokes for real. The session-binding check in the middleware was correct but unreachable.
- **F3** `DEV_AUTH_BYPASS` now defaults **OFF everywhere** (explicit opt-in). It defaulted ON outside production and `_PRODUCTION_ENVS` has no `"staging"`, so a staging box resolved every unauthenticated request to the fixture user. Railway already has `DEV_AUTH_BYPASS=0`, so no deploy impact.
- **F5** `JWT_SECRET_PREVIOUS` (comma-separated) is accepted for **verification only** — rotating `JWT_SECRET` no longer logs every session out. HS256 header lock + `hmac.compare_digest` untouched; `production_config_problems` rejects a previous-secret equal to the default or to the current one.
- Tests: [test_auth_p3_hardening.py](apps/api/tests/test_auth_p3_hardening.py) (10). `test_auth_hardening.py`'s bypass-default test was inverted to match F3.

### 2026-08-25 — fix(deploy): diagnose the dead Railway deploy — Postgres was never running
Root cause was **not** the container. `apps/api/Dockerfile` was correct the whole time (multi-stage `COPY --from=deps /usr/local`, exec bit, LF endings, `CMD` form — all fine, all ruled out).
- **The database service was down.** `Postgres-CUSN` existed in the `autonomous` environment but had *zero* active deployments (last one `REMOVED` on 2026-06-10). A `*.railway.internal` name resolves only while the target service has a **running** deployment in the **same** environment, so `postgres-cusn.railway.internal` failed DNS with `socket.gaierror: [Errno -2]`. Alembic died, `start.sh` exited 1 after 6 retries, Railway restarted, and uvicorn never bound — hence 18 healthcheck failures with no app ever listening. Redeployed the Postgres service instance; the very next app deploy went green on the first migration attempt.
- **The "no stdout" symptom was a CLI artifact.** `railway logs <id> -d` returns *build* output; container stdout is only reachable with a filter (`--filter 'start.sh'`, `--filter '@level:error'`). The `[start.sh] boot` banner had been in the logs all along. Documented this at the top of [start.sh](apps/api/scripts/start.sh) so the next person doesn't lose hours to it.
- **Hardening:** `start.sh` now preflights the `DATABASE_URL` hostname (10 × 3s) before invoking Alembic and, on failure, prints a one-line named cause instead of a 60-line SQLAlchemy/asyncpg traceback.
- Verified: `GET /health` → `{"status":"ok","env":"staging","version":"0.0.1"}`; deploy `166b5aa1` SUCCESS; logs show `Migrations applied` then `Uvicorn running on http://0.0.0.0:8080`.
- Follow-up: `REDIS_URL` points at `redis-hdnc.railway.internal`, which is also not deployed. Harmless today — nothing under `apps/api/app` imports redis — but it will fail the same way the moment something does.

### 2026-07-25 — `59cf1a33` / `5587eb8d` toolchain: clean-clone breaks + local sim runbook
Found by actually running the stack from scratch. Three independent breaks, none of which a returning contributor could avoid:
- **`make dev-api` could not start.** `apps/api` imports `trading_agents` ([agent.py](apps/api/app/routers/agent.py), [agent_runs.py](apps/api/app/services/agent_runs.py)) but declared neither the `agents` dependency nor its `[tool.uv.sources]` entry, so `uv run --package api` died on `ModuleNotFoundError: trading_agents`. The API only booted with a hand-set `PYTHONPATH`. Declared as the real workspace dep it is.
- **`make test` / `lint` / `typecheck` all failed.** pytest, ruff and mypy are configured in the root pyproject and invoked by the Makefile, but were never declared → "No module named pytest". Added `[dependency-groups] dev`.
- **All 14 route test modules failed to collect** on a fresh resolve: starlette 1.3's `TestClient` rejects httpx<2. Added `httpx2`.
- **Two date-dependent test failures** in [test_daily_cron.py](apps/agents/tests/test_daily_cron.py): both called `main(force=False)`, so the market-calendar gate short-circuited before `run_council` and the assertions only held Mon–Fri (`test_skip_when_already_decided_today` was passing for the *wrong* reason at the weekend — gate-blocked, not idempotency). Autouse fixture pins the gate open; the calendar itself is covered in `packages/engine`.
- Suite after: **278 passed, 8 skipped.**
- RUNBOOK gained a verified **local iOS-simulator quickstart** (Xcode/CocoaPods prereqs, why Expo Go can't work, the `EXPO_PUBLIC_*` rebuild caveat, dev-token login, mock-mode caveats).
- **Corrected an earlier claim of mine:** the previous entry's "approve doesn't execute" note was wrong — it was read off a stale local `main`. On current `main` [approvals.py](apps/api/app/routers/approvals.py) *does* call `execute_proposal` and returns `executed`/`riskBlocked`/`riskReason`, and [order_sync.py](apps/api/app/services/order_sync.py) writes `fill_qty`/`fill_avg_price` on fills plus `realized_pnl`/`closed_at` on closes. Note removed rather than left to mislead.
- **Live finding:** the Railway deploy in `apps/mobile/.env` (`autonomoustradeagents-autonomous.up.railway.app`) returns `Application not found` — the service is gone, not stale. Local API is the only working target until it is redeployed.

### 2026-07-24 — `2c98f0e9` / `7f75d674` / `d05957fd` mobile: build unblock + theme toggle + de-em-dash
- **Build was fully broken.** `app.json` had `newArchEnabled: false`, but Reanimated 4 + RN 0.81 require the New Architecture — `pod install` hard-failed on `assert_new_architecture_enabled`. Flipped it on. Separately, `react` floated to 19.2.7 via `^19.1.0` while RN 0.81.6's bundled renderer is 19.1.4 and demands an exact match (runtime red-screen "Incompatible React versions"). Pinned react to `19.1.4` + a root `pnpm.overrides`. App now builds + runs on the iOS simulator.
- **Theme toggle** (the `_layout.tsx` "deferred" note): dark styling already existed everywhere via NativeWind `dark:`, but was OS-only. tailwind `darkMode` `media`→`class`; new `themeStore` (zustand) persists System/Light/Dark synchronously via MMKV (`src/lib/kv.ts`); applied on boot; Settings › Appearance segmented control. Verified: forcing Dark flips the whole app while the OS stays light.
- **Em dashes** replaced with hyphens in user-facing copy only — mobile rendered strings + API display copy (mock_store seed headlines, health status labels, executor risk_reason, broker/orders error details). Comments, docstrings, and the `'—'` missing-value glyph left alone.
- Gitignored the CNG-generated `apps/mobile/ios/` + `android/`.

### 2026-07-02 — `feat` review batch J: UI code-gaps (drawdown banner, keyboard, icon/splash)
Closing the concrete UI gaps found when asked "is the UI perfect" (answer: no, and it was unrendered):
- **Drawdown circuit-breaker banner** — the DESIGN.md-mandated persistent `danger` banner didn't exist. New vertical: `GET /api/v1/circuit-breaker` + `POST …/acknowledge` ([circuit_breaker_service.py](apps/api/app/services/circuit_breaker_service.py), [routers/circuit_breaker.py](apps/api/app/routers/circuit_breaker.py)), `useCircuitBreaker` hook, and [CircuitBreakerBanner.tsx](apps/mobile/src/components/CircuitBreakerBanner.tsx) mounted on Home + Picks. Shows while `halted`; "Acknowledge & resume" (confirm + warning haptic) flips the breaker to `manual_override` so BUYs pass again. Acknowledge = `require_real_auth`.
- **Keyboard avoidance** (`KeyboardAvoidingView`) on the login + verify input screens.
- **De-hardcoded the "Alpaca paper" broker string** on the approval confirm sheet — now derived from the active connection (falls back to "Paper account").
- **Placeholder icon + splash** generated (dark canvas + mint upward mark, on-token) and wired into app.json (icon / splash / Android adaptive foreground) — no longer ships Expo's default logo; unblocks a real build. Real brand art is still a designer job.
- Tests: circuit-breaker route (status/ack/auth). API 139 green; mobile + shared-types typecheck clean.
- Honest caveat: STILL UNRENDERED. Layout/contrast/Dynamic-Type/VoiceOver need on-device testing (Expo Go or simulator) — see HANDOFF §0h.

### 2026-07-01 — `feat` review batch I: real market calendar (pandas_market_calendars)
- [market_calendar.py](packages/engine/engine/features/market_calendar.py) `is_us_trading_day` now sources the XNYS calendar from `pandas_market_calendars` (cached 2024–2031, ~2008 days), falling back to the static holiday table when the package is absent and fail-open beyond both. Removes the "stale-in-2028" hardcoded-table risk the audit flagged — verified it correctly resolves 2028/2030 holidays the old table never had.
- Added `pandas-market-calendars>=4.4.0` to engine deps (uv.lock updated). Tests: weekends/holidays/observed-closures on both paths. 47 engine+agents green.

### 2026-07-01 — `feat` review batch H: magic-link email delivery (env-gated)
- Production login now works without the pull-token-from-logs workaround. New [email.py](apps/api/app/services/email.py): env-gated provider (`EMAIL_PROVIDER=resend|smtp` + `EMAIL_FROM`), renders the magic-link deep link (`autotrader://auth/verify?...`, overridable via `EMAIL_LINK_BASE`), and sends via Resend HTTP or SMTP. Hard no-op when unconfigured — never raises into login.
- `request_login` sends the email whenever a provider is configured (best-effort; a send failure keeps the token valid to retry); the dev-token deep-link shortcut is preserved in non-prod, and a prod deploy with no provider logs a loud warning.
- Tests: env-gating, deep-link URL, request_login calls the sender with the raw token in prod, dev fallback intact. `.env.example` documents the vars.

### 2026-07-01 — `fix` review batch G: auth concurrency (refresh CAS + magic-link claim, H1/M1)
Closed the two remaining audit races (both needed concurrent same-token requests):
- **Refresh rotation is now compare-and-swap** ([auth_store.py](apps/api/app/services/auth_store.py) + Postgres): `rotate_session(..., expected_current_hash=)` only lands if the row still holds the validated hash (Postgres `WHERE refresh_token_hash=old` + rowcount; in-memory atomic check). The service revokes the session on a CAS miss — two concurrent refreshes off one token can no longer both succeed. Bootstrap (first issue) stays unconditional.
- **Magic-link single-use is now an atomic claim** — `mark_magic_link_used` returns whether THIS call flipped unused→used; `verify_magic_link` only issues a session on a winning claim, so a concurrent double-verify can't mint two sessions off one link.
- Tests: store-level CAS + claim, and route-level second-verify→401. 24 auth tests green.

### 2026-07-01 — `feat` review batch F: iOS / Apple HIG fixes + build config
From the HIG audit agent. Respecting DESIGN.md tokens (no raw hex/spacing):
- **Haptics** on the trading actions (DESIGN.md §9): medium impact on approve/close, light on decline, warning notification on risk-block ([pick/[id].tsx](apps/mobile/app/pick/[id].tsx), [positions.tsx](apps/mobile/app/positions.tsx)).
- **Approval confirm sheet** now uses `SafeAreaView edges={['bottom']}` so content clears the home indicator on notch-less iPhones (was fixed `pb-10`).
- **Filter chips → 44pt** min tap target (were 32pt) — CLAUDE.md/HIG minimum.
- **Pull-to-refresh** (`RefreshControl`) on the Picks + Positions lists (DESIGN.md §5).
- **Broker disconnect** now a confirm dialog + `destructive` button variant (was a silent secondary-style tap).
- **iOS build unblocked**: added `apps/mobile/eas.json` (dev/preview/production profiles + submit placeholders) and removed the `app.json` reference to a missing `notification-icon.png` that would fail prebuild. `extra.eas.projectId` is populated by `eas init` (documented in HANDOFF, not hardcoded).
- Deferred (needs on-device testing, noted in HANDOFF): Dynamic Type at 200%, per-tile VoiceOver hints. Mobile + shared-types typecheck clean.

### 2026-07-01 — `fix` review batch E: auth + production hardening (3-agent audit)
Fail-closed the production defaults a deploy could ship insecurely, from a fresh auth + prod-config + HIG audit (3 parallel review agents):
- **Prod secrets guard** ([config.py](apps/api/app/core/config.py) `require_production_readiness`, called in the lifespan): refuses to boot in production if `JWT_SECRET` is default/short, `BROKER_TOKEN_ENCRYPTION_KEY` is unset (broker tokens would use the public dev key), or `CORS_ORIGINS` is wildcard/empty. (audit C1/C5)
- **DEV_AUTH_BYPASS force-off in production** ([middleware/auth.py](apps/api/app/middleware/auth.py)) regardless of the env var — a forgotten `DEV_AUTH_BYPASS=0` can no longer grant anonymous fixture-user access in prod. (C2)
- **`/approvals/{id}/decision` now `require_real_auth`** — it runs the same broker-execute chain as `/orders/execute`, so it must demand a real session (was bypassable). (C3)
- **`ENV=live` no longer leaks the dev magic-link token** — `request-login` uses `settings.is_production` (which includes "live"), not an inline tuple that omitted it. (C4)
- **Access tokens are session-bound** — `mint_access` embeds `sid`; the middleware rejects a token whose session is revoked/expired. Logout now kills the access token immediately instead of leaving a ≤15-min window on trade routes. (H2)
- **Logout ownership check** — a caller can't revoke via someone else's refresh token. **Refresh endpoint rate-limited** (60/hr/IP). **Security headers** (nosniff / X-Frame-Options DENY / Referrer-Policy / HSTS in prod) on every response.
- Tests: test_auth_hardening (live=prod, bypass-off-in-prod, config guard, logout revokes access token end-to-end, foreign-refresh refused). Full suite green.
- Still open (noted): refresh-rotation compare-and-swap + magic-link single-use claim under *concurrent same-token* requests (H1/M1 — narrow race); multi-worker needs Redis for the rate-limiter/OAuth-state/reconciler singletons (documented, run 1 web worker).

### 2026-06-13 — `feat` review batch D (part 2): per-user live-trading consent
- Live (real-money) trading was a single global `LIVE_TRADING_ENABLED` switch. Now it's a **two-key gate**: a live order requires that env **AND** the connection's own `live_trading_consent` flag. Migration 0011 + `BrokerConnection` column + `BrokerConnectionRecord` field + both store mappings + `set_live_consent` + `POST /api/v1/broker/connections/{id}/consent` (ownership-checked). Defaults False — existing connections stay paper-only until explicitly opted in. Executor blocks with `live_trading_disabled` if either key is missing.
- Tests: env-on-but-no-consent → blocked; env-on-and-consent → executes. 120 API tests green.

### 2026-06-13 — `perf`/`feat` review batch D (part 1): cron index, holiday, rate-limit
- **Cron idempotency** no longer scans all history: `DecisionLog.has_decision_today` is an indexed `(user_id, symbol, triggered_at)` existence query (in-memory + Postgres impls); [daily_cron.py](apps/agents/scripts/daily_cron.py) `_already_decided_today` uses it. Flat latency as history grows.
- **Market-holiday table** extended through 2028 + 2029 New Year ([market_calendar.py](packages/engine/engine/features/market_calendar.py)) so the fail-open warning doesn't trip next year.
- **Rate limiting** on `/auth/request-login`: in-process sliding window, 5/hour/email + 30/hour/IP → 429 ([rate_limit.py](apps/api/app/services/rate_limit.py)); conftest resets it per test. Redis window is the multi-worker upgrade (noted).
- Tests: test_rate_limit; 161 API+agents green. Deferred 🟢 (noted): reconciler 3×→1× broker-connection reuse per tick, structured/request-id logging.

### 2026-06-13 — `feat` review batch C: multi-user (per-user identity threading)
- The API was single-fixture-user despite per-user auth. Now the authed `user.id` is threaded end to end: council attributes the decision row to the user ([agent.py](apps/api/app/routers/agent.py) `run_council(user_id=user.id)`); the Store Protocol + MockStore + PostgresStore methods (`get_account`/`list_activity`/`list_pending`/`decide`) take an optional `user_id` (→ `_uid()` resolves to the user or `DEFAULT_USER_ID` for cron/legacy); account/activity/approvals routes pass `user.id`; the executor scopes its proposal lookup + decide to the caller.
- **Ownership enforced**: `decide`/`list_pending`/`close` filter by `user_id`, so user A can't see/approve/close user B's proposal (returns 404). Two real users no longer collide.
- Backward-compatible (optional param, defaults to fixture) — mock/dev stays single-bucket under DEV_AUTH_BYPASS; 115 API tests still green. New: test_multiuser_scoping (executor threads user_id into store calls; `_uid` resolution).

### 2026-06-13 — `feat` review batch B: close-from-app (the missing spec item)
- **API**: `GET /api/v1/positions` (open agent positions + live mark + exit plan) and `POST /api/v1/positions/{decision_id}/close` ([positions.py](apps/api/app/routers/positions.py), [positions_service.py](apps/api/app/services/positions_service.py)). Close reuses the position-manager's risk-gated path (`close_position_now`) — same bracket-cancel + audit persist as the agent's own closes, only `close_reason='user_manual'`. Works for BOTH manual-mode (user closes when ready) and agent-mode (user overrides early); ownership-checked; Postgres-only (mock → empty/not_found).
- **Mobile**: [positions.tsx](apps/mobile/app/positions.tsx) — per-position rows (entry→mark, unrealized P&L, exit-mode badge, plan) + a confirm-guarded "Close now"; `usePositions` hook; linked from Settings → Agent. Closes the gap where "manual" exit only meant "go close it in Alpaca yourself."
- shared-types: OpenPositionDto + ClosePositionResponse. Tests: test_positions_route (auth, empty-in-mock, 404). Mobile + shared-types typecheck clean.

### 2026-06-13 — `fix` review batch A: safety bugs from the 3-pass code review
- **Position-manager re-entrance guard** ([position_manager.py](apps/api/app/services/position_manager.py)): skip a decision if a SELL is already pending/accepted (`_has_in_flight_close`) — stops the duplicate "agent closing" push + redundant broker calls every tick until the close fills.
- **degraded_nodes persisted** to `agent_decisions` (migration 0010 + model + first-class `DecisionEntry` field + runtime/PostgresDecisionLog wiring) — it dropped before the DB row for approved runs, so reflection/calibration couldn't exclude degraded runs.
- **Paper-bracket guard**: loud WARNING when an `exit_mode=agent` BUY is placed without a real broker-side bracket (missing stop/target on the live path, or the in-memory paper fallback) — no longer a silent unprotected position.
- **Sentry** init in [main.py](apps/api/app/main.py), env-gated on `SENTRY_DSN` (hard no-op without it).
- Tests: re-entrance guard covered in test_position_manager. 226 + new tests green.
- Open from the review (next batches): close-from-app endpoint+screen, multi-user user_id threading, per-user live consent, ops hardening (rate-limit/cron-index/holiday/conn-reuse).

### 2026-06-13 — `fix(ui)` NativeWind type env (unblocks `@app/ui` typecheck)
- Committed NativeWind's generated `packages/ui/nativewind-env.d.ts` (its own header says to commit it) + the `tsconfig.json` `include` for it. `pnpm --filter @app/ui typecheck` now passes — clears the long-standing `@app/ui` tsc gate. Was sitting un-committed from codegen; not part of the feature work.

### 2026-06-13 — `7bdee5ae` docs: build log + main workflow + handoff §0
- Established this build log + the "log every commit here" rule in [CLAUDE.md](CLAUDE.md); switched the documented git workflow to land on `main` directly (user preference).
- Updated [HANDOFF.md](HANDOFF.md) §0 with the extra steps the auto-mode/real-data/Langfuse work introduced (new env, per-user reconciler, agent-managed exits).
- Fast-forwarded the whole `agent-v1/auto-mode-real-data` line into `main` and pushed `main`.

### 2026-06-12 — `b4eaded4` feat(agents): Langfuse per-agent tracing + scheduled reflection
- Env-gated Langfuse ([tracing.py](apps/agents/trading_agents/tracing.py)): one trace per council run, one generation per agent (router/technical/fundamental/macro/selector/drafter/reflection) with OK/WARNING(degraded)/ERROR + tokens + cost. Hard no-op without keys; never raises into a decision. Built against langfuse 4.x.
- Reflection now runs inline in `daily_cron` after ghost eval (it existed but was never scheduled). `--no-reflect` to skip. Langfuse flush before the short-lived process exits.
- Tests: `apps/agents/tests/test_tracing.py` (no-op + degraded/fail outcomes). Follow-up still open: live-Langfuse emission unverified without keys; Sentry still unwired.

### 2026-06-12 — `94c5dc69` / `78af32ba` docs + chore
- RUNBOOK: auto-mode operating model, mock-vs-real run recipes, new env, Langfuse view. Untracked ~30k accidentally-committed files (node_modules/build caches/old India log) — kept on disk, already gitignored.

### 2026-06-12 — `cf7b01f4` feat(api): close the auto-trade loop end to end
- The five audit breaks (§3) fixed: real `RiskContext` at execution + fail-closed; orders persisted (pending→result) linked to the decision; paper routes through the real Alpaca paper account; approving executes server-side with the chosen `exit_mode`; per-user reconciler fleet; `order_sync` (fills, PDT ledger, external-close detection); `position_manager` (time-stops + council-SELL early exits, all through the risk gate); per-user watchlist.
- Verified end-to-end over HTTP in mock mode (council → proposal → approve → filled paper order). 244 Python tests green.

### 2026-06-12 — `520a0446` / `b2090088` / `dee3711e` / `6607c960` feat(engine|broker|agents|mobile)
- engine: real feature providers (Alpaca IEX bars → ATR/RSI/DMA, FRED macro), market-calendar gate, `load_db_risk_state`/`DbRiskState`, migration 0009 (`exit_mode`/`closed_at`/`close_reason` + `user_watchlist`).
- broker: bracket orders + `cancel_open_orders` (Alpaca; Zerodha rejects brackets explicitly).
- agents: real-data provider resolution, LLM hardening (temp/timeout/retry/degraded), EOD TTL, swing-aware cron with push + calendar gate.
- mobile: watchlist screen, exit-plan + exit-mode on the approval card.

### 2026-06-12 — `76bf5c76` docs: fable5 audit findings + pin uv.lock
- The audit above (§1–§8) + committed the previously-missing `uv.lock`.
