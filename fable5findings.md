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

## 📍 Start here if you are a model picking up this repo

Two models work on this codebase in alternating sessions on two different accounts,
because the user hits a 5-hour limit on one and hands over to the other. **You cannot
see the other model's conversation. This file and the git log are the only channel
between you.** The user should never have to repeat context that is already written
here.

**Read, in this order:**

1. [`CLAUDE.md`](CLAUDE.md) — §0 tells you which identity trailer to put on your
   commits (`ID:MODEL1REAL` for Opus, `ID:MODEL2OFF` for Sonnet or anything else).
   §4 is the engineering standard this repo is held to; it is not boilerplate — every
   rule in it exists because a real bug shipped past a green test suite.
2. [`docs/HACKATHON.md`](docs/HACKATHON.md) — **what we are actually doing right now**
   and why. Deadline, hard requirements, our positioning, and an explicit "do not do
   these" list.
3. The newest entries below, and `git log -1 --format=%B`. They are written for you.

**The single most useful habit:** when an entry says *"Verified live: …"*, that claim
was actually executed and its output pasted. When it says *"left open"*, that work is
genuinely not done. Trust the distinction and preserve it in your own entries —
separate what you verified from what you believe.

**Current mission in one line:** we are entered in the Alpaca AI Trading Agents
Hackathon (deadline **Fri Sep 4, 11:00 AM EDT**) as *"The Refusal Ledger"* — the only
team measuring, in dollars, what the agent's refusals were worth. Options trading and
use of **Alpaca's own** MCP server or CLI are hard eligibility requirements. See
`docs/HACKATHON.md` §5 for the MCP requirement, which has already been misread once.

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

### 2026-09-01 — `96555e5c`/`78dea6e9` fix(desktop): two cosmetic layout bugs — Settings table overflow + Stream panel scrollbar

Two independent, pure-CSS/layout bugs, no business logic touched.

- **`96555e5c`** — Settings' Broker connections table (7-span card) had no
  horizontal scroll region. At laptop widths (~1300-1450px) the table's real
  min-content width exceeds the card's width, and the last column (Revoke
  button + its helper caption) painted over the Appearance card next to it.
  Fix: wrapped the table in `overflowX:'auto'`, same as the two tables in
  `Positions.tsx` (identical failure mode, identical fix already precedented
  there).
- **`78dea6e9`** — the council-run Stream panel's event log used a bare
  `overflowY:'auto'` div, so it fell back to the browser's default
  scrollbar instead of this design system's thin/rounded one (already
  applied to `.pg-main`/`.pg-sidebar`). Fix: added a `.pg-scroll` class
  carrying the same `::-webkit-scrollbar` rules, applied to the Stream log
  div.

**What I verified, and how:** no live backend in this environment (no
Postgres/Alpaca/auth configured in the worktree), so I couldn't drive the
real running screens. Instead built an isolated HTML repro using theme.ts's
*actual* `PLATINUM_CSS` text (copy-pasted, not reinvented) and each
component's *actual* JSX structure translated to plain markup, served over
a local `http.server` (file:// URLs render as inert static snapshots in
this session's browser tool — a real `http://localhost` origin is required
for live resize/scroll/JS). Confirmed bug 1 via `getBoundingClientRect`
(table 615px vs card 578px at 1366px viewport → 58px overflow) and
screenshots at 1300/1366px (broken) and 1920px (fine, both before and
after — the fix adds no scrollbar when there's already room). Confirmed
bug 2 by screenshot: native square scrollbar before, the app's thin rounded
one after. `pnpm -s exec tsc --noEmit -p apps/mobile/tsconfig.json` and
`pnpm --filter mobile exec jest --silent` (11 suites / 83 tests) both clean
before and after; neither screen has existing test coverage to begin with.

**Left open:** the worktree was behind `main` by several commits
(`e0a21c16` → `7f81a1f1`, including the very "Connected" column this fix
had to account for) — fast-forwarded via `git merge main --ff-only` before
editing, per this session's brief. `.pg-typeahead`'s dropdown list
(`theme.ts`) has the same bare-`overflow-y:auto`-with-no-scrollbar-styling
pattern as bug 2 did; out of scope for what was asked, not fixed here.

### 2026-09-01 — `452bb044`/`7dabbd4a` fix(mobile): "opened 5d ago" positions — diagnosed the shared-account confusion, not a bug in the connect flow

**The report:** user saw closed positions labeled "opened 5d ago" and was alarmed —
they believed they'd created a fresh, personal paper account the day before
("new paper acc was created yesterday itself").

**What I verified, directly against production Postgres** (read-only queries via
`asyncpg`, using the `DATABASE_URL` in the main checkout's `apps/api/.env` — this
worktree has no `.env` of its own):

- `users`: 3 rows. The demo fixture (`00000000-…-001`), the operator's real login
  `amoghpatil2001@gmail.com` (created 2026-08-26T17:11:18Z), and a second personal
  email `amoghpatil01@gmail.com` (created 2026-08-30T17:33:00Z).
- `broker_connections`: the operator's row (`cda6b8ce…`) has been `status='active'`
  continuously since **2026-08-26** — `created_at` never moved. `account_number` is
  NULL and its ciphertext is exactly the length Fernet produces for a 10-byte
  plaintext — matching the `"env:alpaca"` sentinel's length precisely, and matching
  the demo fixture's own row byte-for-byte. Could not decrypt the ciphertext itself
  (prod uses a real `BROKER_TOKEN_ENCRYPTION_KEY`; the local dev-fallback key does
  not round-trip it, checked directly), but a real completed OAuth callback always
  writes a real `account_number` from Alpaca's response — NULL here, on every active
  row, is conclusive on its own. **This has been the operator's shared env-linked
  paper account the entire time, not something created "yesterday."**
- The row's `updated_at` (2026-08-31T15:13:53.044005Z — "yesterday" from the user's
  report) is explained exactly, to the microsecond, by `auto_approve_consent`
  flipping to `true` on that same row — not a reconnect, not a new row. Confirmed by
  reading every code path that writes `broker_connections`: `upsert_connection` (env
  bootstrap or a completed OAuth callback), `revoke_connection`, `set_live_consent`,
  `set_auto_approve_consent`. Clicking "Connect Alpaca paper" while already connected
  doesn't even reach the DB (`/connect/alpaca/start` only stages an in-memory PKCE
  state; the button also isn't rendered once a connection exists). 37 minutes after
  the flip, the auto-approve sweeper opened VZ; BAC and CVX followed that evening —
  all three `approval_mode='auto'` in `agent_decisions`. **The user's Dashboard
  AUTO/ASK pill (`AutoApprovePill.tsx`), not the Connect button, is almost certainly
  what they actually pressed.**
- The "opened 5d ago" CLOSED positions (CVX, JNJ, KO, SPY, UNP, XOM) are real:
  `agent_decisions` shows all six opened 2026-08-27 with `approval_mode='ask'` (a
  human tapped Approve on each) and closed as one batch at 2026-08-30T06:18Z. 8/27 to
  9/1 is genuinely 5 days. Nothing fabricated, nothing duplicated onto this account.
- Separately, and this IS a real (now-fixed) finding: the third user
  (`amoghpatil01@gmail.com`) got this same env-sentinel connection auto-attached on
  signup, 2026-08-30T17:33:00Z — 39 minutes *before* commit `2709d236` (the
  multi-tenant allowlist fix from `docs/PLAN_MULTI_TENANT.md` §1) landed at 18:12Z
  the same day. That leaked connection was revoked afterward and has zero orders/
  decisions against it — real exposure, but briefly, and no trading harm. No such
  leak exists for any signup since. `docs/PLAN_MULTI_TENANT.md`'s header still said
  "plan, not built" for this — corrected in `7dabbd4a` (CLAUDE.md §4.2: code wins,
  then fix the doc).

**What I fixed (`452bb044`):** the connection's real `createdAt` was already
returned by `/broker/connections` and already typed on the frontend
(`BrokerConnection.createdAt`) but never rendered anywhere — neither Settings
screen showed the account's age at all. Added a "Connected" column (desktop
Settings, reusing the existing `ago()` formatter) and a "Connected since <date>"
line (mobile Settings). Also added one sentence to `AutoApprovePill`'s confirm
dialog clarifying that arming it acts on the account already connected in
Settings — history and all — not a fresh one. Pure copy/display change; skipped
the §4.1 revert-check as the rule itself permits for copy-only changes.

**Verified:** `pnpm -s exec tsc --noEmit -p apps/mobile/tsconfig.json` clean;
`pnpm --filter mobile exec jest --silent` — 82/82 passed, 10/10 suites; existing
Python broker/env-bootstrap tests (`test_env_bootstrap.py`, `test_broker.py`) —
43 passed, 1 skipped, untouched by this change. Did **not** visually render the
new column/line in a live browser this session — only type-checked + unit-tested.

**Left open / needs a human decision:** true per-user isolated paper accounts
(so a second signup gets a genuinely empty account instead of the operator's
shared one) is a materially larger feature — new Alpaca account provisioning or
a much deeper OAuth-only posture — and was explicitly out of scope for this
session. Recommend **against** building it in the remaining 3 days given the
hackathon deadline; flagging per the task brief rather than building it. Also
not investigated: whether `last_used_at` on `broker_connections` is written
anywhere at all — every row in production currently shows it as NULL, which
would make the existing "Last used" column on desktop Settings permanently
show "—" regardless of real usage. Noticed in passing; did not chase it down,
out of scope for this task.

### 2026-09-01 — `3f32f5d0` fix(mobile): positions screen didn't refresh after approving a proposal

`ID:MODEL2OFF`. User's ask: "i went to picks i selected apple ran council
picked up the stock and approved it but unable to see it in actual open
positions fix this as well." The orchestrating session had already hit
the API directly and confirmed the backend was correct — the real
decision (`a1ae9934-b227-4e38-9280-07836e0e6abe`, AAPL, 15 shares, BUY,
approved) existed, and `GET /api/v1/positions` returned it
(`status: pending_fill`, `managed: true`) right after approval — so this
task was scoped to find the gap between "user clicks approve" and "the
Positions screen shows the row," not to re-litigate the backend.

**Root cause, found by reading `apps/mobile/src/hooks/useApprovals.ts`
and `usePositions.ts` side by side:** `useDecideApproval`'s `onSettled`
invalidated `QK.pendingApprovals`, `QK.account`, and `['activity']` —
never `['positions']`. `useOpenPositions` (`usePositions.ts`) has a 15s
`staleTime` and a 30s `refetchInterval`. A user who approves and checks
Positions inside that 15s window — the obvious thing to do right after
tapping Approve — gets served the pre-approval cached list, because
nothing told the cache a decision had settled. This was a missing
invalidation, not a backend lag; the brief's hypothesis was correct.

**Confirmed before touching code, not assumed:**
- Exactly one `QueryClientProvider` (`app/_layout.tsx:112`, the
  `queryClient` singleton from `lib/queryClient.ts`) wraps the whole
  app. `DesktopShell` replaces the router `<Slot/>` in place *inside*
  that same provider (`_layout.tsx:159-161`) — so
  `apps/mobile/app/positions.tsx` (native/narrow-web) and
  `apps/mobile/src/desktop/screens/Positions.tsx` (wide-web) read the
  exact same `['positions']` cache entry. One fix in `useApprovals.ts`
  covers both surfaces; there is no separate desktop query client to
  patch too.
- `useDecideApproval` is the *only* decision-mutation implementation —
  grepped for `/decision` and `useDecideApproval` across
  `apps/mobile/`: four hits total, and the two call sites
  (`apps/mobile/app/pick/[id].tsx` and
  `src/desktop/screens/PickDetail.tsx`) both call this same hook. The
  `(tabs)/approvals.tsx` feed only reads `usePendingApprovals` and
  links into `/pick/[id]` for the actual decision — no bypass path.

**Fix:** one line in `onSettled` —
`qc.invalidateQueries({ queryKey: ['positions'] })` — plus an updated
doc comment. Deliberately a raw literal rather than importing
`usePositions.ts`'s (unexported) `POSITIONS_KEY` const: the codebase
already has this exact idiom (`['activity']` in the same function,
matching `useActivity.ts`'s `QK.activity(limit)` by prefix;
`['approvals']` in `_layout.tsx`'s `PushTapHandler`), so this follows
existing convention instead of introducing a new one.

**New test, revert-checked per CLAUDE.md §4.1**
(`apps/mobile/src/hooks/useApprovals.test.tsx`): a real `QueryClient` +
`QueryClientProvider` (no TanStack Query mocking), only `@/lib/api`'s
`request` mocked, a tiny harness component capturing
`useDecideApproval().mutateAsync`, `jest.spyOn(qc, 'invalidateQueries')`.
Asserts `'positions'` is among the invalidated keys once
`await mutateAsync(...)` resolves (which only happens after
`onSettled` has run — this is a documented TanStack Query v5
guarantee, not an assumption). Removed the new
`invalidateQueries(['positions'])` line and reran: test failed with
`Received array: [["approvals","pending"],["account"],["activity"]]`
(no `positions`) — exactly the pre-fix behavior. Restored the line,
reran: passes.

**Verified, commands + output:**
- `pnpm --filter mobile exec jest useApprovals.test --silent` — 1
  passed, both before writing the revert-check and after restoring.
- `pnpm --filter mobile exec jest --silent` (full suite) — **83 passed,
  11 suites** (was 82 per the prior entry below). One generic
  "A worker process has failed to exit gracefully" note from Jest,
  unrelated to this change (my test's own `QueryClient` is disposed via
  `qc.unmount()` in `afterEach`, and this line didn't appear at all
  when running just this one test file — it surfaced only in the
  full-suite run, so it traces to some other pre-existing test's
  teardown, not this one).
- `pnpm -s exec tsc --noEmit -p apps/mobile/tsconfig.json` — clean, no
  errors.

**What I deliberately did NOT do, and why:** did not drive a live
browser session to actually click Approve on a real pending proposal
and watch the Positions screen update. CLAUDE.md §8 is explicit —
"You may not execute trades, even on paper. Placing/approving orders is
the user's action" — and approving a proposal is exactly the action
gated there, paper or not. The fix is a pure cache-invalidation change
verified by a test that exercises the real mutation lifecycle directly
(a more precise check of the actual mechanism than a UI click-through
would give anyway), plus the full regression suite. The backend fact
this whole task rests on — that the position exists immediately after
approval — was independently verified live by the orchestrating session
before this task started; I did not re-verify it myself.

**Left open:** nothing from this specific bug report. Out of scope but
noticed in passing, not touched: `usePositions.ts`'s `POSITIONS_KEY`
const isn't exported and isn't in `lib/queryClient.ts`'s `QK` object,
unlike `pendingApprovals`/`account`/`activity` — the codebase already
mixes both conventions (`QK.*` centralization vs. raw-literal-prefix
invalidation) rather than picking one, which is exactly the kind of
"same key in two places" shape CLAUDE.md §4.4 warns about for numeric
thresholds. Not a bug today (both idioms happen to agree on the
literal string), but worth a deliberate pick one day if a third
cross-file consumer of the positions key shows up.

### 2026-09-01 — `56da03fb` fix(options): guard.py computed the contract funnel and threw it away

`ID:MODEL2OFF`. User's ask: "loosen the funnel a bit i dont see any options
making here in veto" — the Insights screen's contract funnel panel showed
"No options passes yet in this window," and the veto ledger showed very
few options-specific vetoes. CLAUDE.md §4.3 names a near-identical-looking
prior incident (a liquidity gate rejecting 89% of valid contracts due to a
volume-field bug, not a bad threshold) and explicitly warns against
cranking a threshold before measuring. Diagnosed before touching anything,
per that instruction and per the brief's own explicit options: bug,
miscalibration, or an upstream bottleneck. It was none of the three named
candidates — it was a persistence bug one layer downstream of all of them.

**What I verified, with real numbers, in order:**

1. **`packages/engine/engine/options/selection.py` read in full.** Six
   fixed-order stages (`contract_type` → `dte_window` → `delta_band` →
   `liquidity` → `iv_present` → `iv_realized_vol_band`), `_MIN_VOLUME=1`
   (already fixed from the CLAUDE.md §4.3 incident), delta bands widened
   and frozen 2026-08-30 per `docs/PLAN_AGGRESSIVE_PROFILE.md`. Nothing
   here looked wrong, and per `docs/HACKATHON.md` §8 the thresholds are
   frozen for the contest window regardless.

2. **Real DB query, live production Postgres** (`DATABASE_URL` from
   `apps/api/.env`, cron user `43221580-69bc-4134-8e1e-5af75499d874`,
   read-only, scripts discarded — not committed):
   - Watchlist: 8 active `asset_class='option'` symbols (AAPL, AMD, META,
     MSFT, NVDA, QQQ, SPY, TSLA), 37 equity. Matches `a7b3a379`'s commit
     message exactly.
   - Last 7 days, those 8 symbols: **68 decision rows** — `final_action`
     13 BUY / 8 VETOED / 47 HOLD. 39/68 reached the Bull/Bear council with
     a `bull_case`, 31/68 with a `bear_case`.
   - Cadence (raw `triggered_at` gaps per symbol, last 2 days): 0-93
     minutes between consecutive runs on the option symbols — healthy,
     not scan-starved. `a7b3a379`'s options-first scan-budget fix and
     today's earlier `36930944` direction-scoring fix are both visibly
     working in this data (real BUYs, real VETOEDs, both bull and bear
     cases populated on most rows).
   - **First query used `reasoning ? 'contract_funnel'` (jsonb has-key)
     and got 18/68 "yes" — WRONG, and I almost reported it as a partial
     win.** `runtime._reasoning_block()` unconditionally writes the
     literal dict key `"contract_funnel": final.get("contract_funnel")`,
     so the key exists (as JSON `null`) on every row `runtime.py` writes,
     whether or not anything real was ever computed. Re-ran with
     `jsonb_typeof(reasoning->'contract_funnel') = 'object'`, matching
     `funnel_service._extract_funnel`'s actual `isinstance(funnel, dict)`
     tolerance rule exactly — **0 of 196 rows, every tenant, all history,
     ever had one.** This is the same class of trap CLAUDE.md §4.3 names
     (a naive check that LOOKS like it measured something but didn't) —
     noting it here rather than in the playbook, since it's a "how to
     query this repo" methodology point, not an options trading rule:
     future sessions querying JSONB presence should default to
     `jsonb_typeof(...) = 'object'`/`'array'`, never a bare `?` (has-key),
     whenever the writer is known to persist explicit `null`s for absent
     data — as `runtime._reasoning_block()` always does here.
   - Broken down by `final_action` × real-funnel-present: **BUY: 0/13,
     VETOED: 0/8, HOLD: 18/47 have the *key* but ALL 18 have `null`
     value** (confirmed by inspecting `rejection_reason`/`counts` on each
     — every one was `None`/`None`). Those 18 rows' `bull_case`/
     `bear_case` presence confirmed they went through the LIVE Bull/Bear
     path (14 of 18 have both), not the legacy drafter.py path.
   - The only rows with the FULL legacy reasoning shape (`strategy_fit`,
     `router_rationale`, `drafter_rationale`, …) are from 2026-08-26/-28,
     predating either the options-agent fork going live or this field
     being added to that writer — historical, not a live signal.

3. **Code trace confirmed the mechanism exactly.** `docs/OPTIONS_PLAYBOOK.md`
   §0.5 says `USE_OPTIONS_AGENT=1` is set in production, meaning every
   live options pass goes through `options_council_node` →
   `run_options_agents` → `ToolGuard._before_open_option_trade`
   (`options/tools/guard.py:555`), which calls `select_contract` directly
   — completely bypassing `nodes/drafter.py` (confirmed via
   `options/agents.py`'s own comment, now corrected: "contract_funnel is
   written by nodes/drafter.py, which the options fork skips entirely").
   `guard.py` had `selection.funnel_counts` in hand at every one of its
   four denial sites plus its success path, and dropped it every time:
   only `selection.rejection_reason` (a bare string) made it out, via
   `GuardVerdict(False, reason)` with no payload, further discarded by
   `dispatch_tool_call`'s denial branch
   (`{"is_error": True, "content": {"denied": verdict.reason}}` —
   `verdict.payload` wasn't even read on denial). The existing tests
   (`test_options_pass_persists_the_contract_funnel`,
   `test_contract_funnel_explains_an_options_hold` in
   `test_council_mock.py`) stayed green the entire time because they
   monkeypatch `drafter_mod._fetch_option_candidates` directly and never
   set `USE_OPTIONS_AGENT=1` — they test the legacy path, which was never
   broken. Textbook CLAUDE.md §4.1 shape: a green test on a path
   production doesn't take.

**Verdict: (a), a genuine bug — not (b) miscalibration, not (c) a pure
upstream bottleneck** (upstream WAS broken until earlier today, per
`36930944`/`a7b3a379`, and both fixes are confirmed live and working by
the cadence/BUY/VETOED numbers above — but fixing them did not, and could
not, fix this, because this sits one layer downstream of all of it: the
data was never wired to the database at all, regardless of how well the
funnel or the scan cadence perform).

**Fix** (all in `56da03fb`, no risk rule or `selection.py` threshold
touched):
- `engine.options.selection.funnel_block()` — new public function, the
  one shared shape (`{"counts", "rejection_reason", "selected_occ"}`).
  `drafter.py`'s existing private `_funnel_block` left alone (already
  correct, already tested, still the legacy/rollback path).
- `guard.py`: the "no contract survived" denial and all four
  `_ledger_refusal` call sites now pass `selection` through; the success
  payload carries `funnel_block(selection)` too.
- `dispatch_tool_call`: denial branch now merges `verdict.payload` into
  `content` instead of hard-dropping it.
- `trade.py`'s `open_option_trade`: persists `guard_payload["contract_funnel"]`
  on its own successful-open row (the one `runtime` skips writing, via
  `decision_row_written`).
- `nodes/options_council.py`: new `_contract_funnel()` reads the tool
  transcript and threads the result onto `state["contract_funnel"]`, so a
  HOLD persists it exactly like `drafter.py`'s path always has.

**Tests — 8 new, every one revert-checked** (removed the specific fix
line, ran the one test, confirmed it failed with the exact error its
docstring names, restored the fix, confirmed green again):
- `test_tool_guard.py`: `test_a_denied_open_carries_the_contract_funnel_to_the_transcript`,
  `test_a_risk_vetoed_open_ledgers_the_contract_funnel`,
  `test_a_successful_open_persists_the_contract_funnel_too`. One existing
  test (`test_risk_veto_returns_is_error_not_an_exception`) had its
  assertion narrowed from exact-dict-equality to a subset check, since
  `content` now legitimately carries an extra key by design.
- `test_options_council_wiring.py`: `test_contract_funnel_reads_a_denied_opens_content`,
  `test_contract_funnel_is_none_when_the_tool_was_never_called`,
  `test_contract_funnel_reads_a_successful_opens_content`,
  `test_a_denied_trade_threads_the_contract_funnel_onto_state`,
  `test_agents_disagreeing_never_fabricates_a_contract_funnel`.
- Also independently revert-checked the `dispatch_tool_call` merge line
  in isolation (separately from the two `guard.py` payload sites that
  feed it), confirming all three links of the chain are each covered.
- Full suite: **1295 passed, 11 skipped** (8 of those are the new ones).
  Ruff clean on every touched file — the 2 ruff errors present in this
  tree (`test_tool_guard.py:1331`, `guard.py:1281`) confirmed pre-existing
  via `git stash` (identical errors, identical files, unrelated lines,
  nowhere near anything this change touches).

**Left open / believed-not-verified:**
- **No backfill.** Historical rows (all 196, everyone) will show
  `contract_funnel: null` forever — the underlying `funnel_counts` were
  genuinely never computed for those specific past runs, so there is
  nothing to backfill from. The Insights screen starts showing real data
  only from the next options pass that reaches `select_contract` after
  this deploys.
- **Did not touch the frontend.** `ContractFunnel.tsx` and
  `funnel_service.py` were read and confirmed to already do the right
  thing once real data exists (`_extract_funnel`'s `isinstance(funnel,
  dict)` check is exactly what a real `funnel_block()` output satisfies)
  — no reason to change either, and I didn't.
- **Did not verify against a live options pass post-deploy** — this is
  paper trading and I cannot place trades per CLAUDE.md §8, and the
  market was closed for the remainder of this session. The fix is
  verified by test (including revert-checks) and by full code trace, not
  by watching a real row land with real funnel data. Say so plainly: this
  is "tests pass, logic traced end to end" — not "watched it happen
  live."
- **`bull_case` populated on 39/68 but `bear_case` only 31/68** for the
  7-day option-symbol window — noticed in the raw query output, not
  investigated (out of scope for a funnel-persistence question; flagging
  in case a future session is looking at Bull/Bear resolution quality).
- Per the task brief: did NOT touch `MIN_FIT_TO_TRADE`, premium caps, or
  `daily_drawdown_halt_pct` — out of scope, confirmed unnecessary by the
  data (upstream funnel/cadence is healthy; the gap was purely in
  persistence).

### 2026-09-01 — `2310c3dc` feat(positions): closed-position history — where the P&L actually came from

`ID:MODEL2OFF`. User complaint (their words): "nowhere can i see my past
trades this ghost pnl and ledger dont seem to be wired. also i see a
profit of 241$ but where did i exactly made that profit?? ... nowhere are
my past positions tracked as to what was opened and closed." Two bundled
complaints; investigated both before writing any code, per the task
brief and CLAUDE.md §4.3 — don't reason about it, measure it.

**Investigation first — confirmed which half was a real bug.**

1. Read `order_sync.py` and `position_manager.py` end to end. Migration
   0009 already gives `agent_decisions` `closed_at`/`realized_pnl`/
   `close_reason`/`exit_mode` columns, and `order_sync._apply_decision_
   lifecycle` / `_detect_external_closes` already write them on every
   close path — agent time/signal/expiry/premium-exit, an in-app user
   close (`close_position_now`), or the user closing directly at Alpaca
   (`external_broker`, detected after the fact). Confirmed by reading
   the code that exit PRICE is never stored directly — only entry price
   (`fill_avg_price`) and `realized_pnl`. There are two ways to recover
   it: the actual closing `Order` row's own `avg_fill_price` (exists for
   an agent/user-manual close, which places a real order), or — for
   `external_broker`, which places no order at all — back-solving from
   `realized_pnl`, entry, qty and multiplier using the exact same sign
   convention `order_sync` itself already uses to compute that
   `realized_pnl` in the first place.
2. **Verified live against the real production Postgres** (per the task
   brief's instructions: `DATABASE_URL` from `apps/api/.env`, read-only
   queries only — the DNS-blocked warning in that file's comment turned
   out to be stale, the host resolved and connected fine from this
   machine):
   ```
   agent_decisions total rows: 196
   agent_decisions with closed_at IS NOT NULL: 6
   close_reason breakdown: [('external_broker', 6)]
   sum(realized_pnl) across closed decisions: 46.93
   orders with agent_decision_id IS NULL (unlinked): 0
   ghost_outcomes status breakdown: [('partial', 7), ('pending', 1)]
   ```
   So: the closed-position data was 100% real and already correct — a
   surfacing problem, not a data problem, confirming the task brief's
   hypothesis. All 6 real closes so far are `external_broker` (the user
   closing positions directly at Alpaca, not through this app), summing
   to +$46.93 (KO +25.65, CVX +55.79, SPY +11.34, JNJ +20.97 winners;
   XOM -35.03, UNP -31.79 losers).
3. **The "$241" is a different number than realized P&L, and that's the
   other half of the user's confusion, not a bug.** A second query:
   `first snapshot ever: equity=$100,000.00` (2026-08-26) vs. `latest
   snapshot: equity=$100,240.82` → total account gain ≈ $240.82, i.e.
   the "+$241" on the dashboard. That is NOT the $46.93 realized — it's
   realized + unrealized combined, matching Alpaca's own live equity
   number. A third query broke down the latest snapshot's 9 held broker
   positions and summed their approximate unrealized P&L: **$241.20** —
   matching the dashboard number almost exactly. Only 3 of those 9
   broker positions have an open `agent_decisions` row at all; the other
   6 are "unmanaged" (no decision behind them, per `positions_service.
   _unmanaged`). So: most of the user's gain is CURRENTLY UNREALIZED,
   sitting in a mix of agent-tracked and unmanaged open positions (the
   existing open-positions screen's per-row `unrealizedPnl` already
   covers this), and only ~$47 of it has actually been banked so far —
   across exactly 6 trades, which is what this change makes visible for
   the first time, itemized.
4. **Ghost P&L / veto ledger re-verified, not touched.** `ghost_outcomes`
   status breakdown above: 0 `final`, 7 `partial`, 1 `pending`, matching
   last night's finding that finalization needs `elapsed >= horizon`
   trading days (`ghost_eval.py`, default horizon 5) since the decision
   was created — only 1-2 trading days have elapsed since most of these.
   Confirmed the UI already renders this honestly rather than a bare
   misleading $0: `format.ts`'s `pendingAwareUsd` renders `"$— · N marks
   pending"` when `amount===0 && pendingCount>0`, and `Insights.tsx`
   renders the literal word "pending" for a null `ghostPnl`, never `$0`.
   This part of the complaint is a real, disclosed, time-bound
   constraint (5 trading days), not a regression — left untouched.

**Built** (the surfacing fix):
- `GET /api/v1/positions/history` (`apps/api/app/routers/positions.py`)
  — `symbol`/`limit`/`offset` query params, same pagination shape as
  `GET /decisions`. Uses `get_current_user` (not `require_real_auth`) —
  same read-only auth as the existing open-positions GET, so a
  read-only demo/judge session sees this too.
- `positions_service.list_closed_positions` — sourced from
  `agent_decisions.closed_at IS NOT NULL`, newest-`closed_at`-first, plus
  a single batched query (not N+1) against `orders` for the whole page
  to find each decision's own closing fill when one exists.
- `_estimate_exit_price` — back-solves the exit price from
  `realized_pnl` for a close with no Order row (`external_broker`).
  Exactly inverts `order_sync`'s own forward formula; does not invent a
  new one. `_closed_from_decision` — the pure DTO builder, prefers the
  real closing Order's `avg_fill_price` when one exists, else the
  estimate; sets `exit_price_source` ('order_fill' | 'estimated_from_pnl')
  so the client can label an estimate honestly rather than presenting
  it as a broker fact.
- `ClosedPositionDto` / `ClosedPositionListResponse`
  (`apps/api/app/schemas/positions.py`) + the TS mirror in
  `packages/shared-types/src/index.ts`.
- An "Open / Closed" toggle on the EXISTING Positions screen, both
  `apps/mobile/app/positions.tsx` (mobile) and `apps/mobile/src/desktop/
  screens/Positions.tsx` (desktop) — no new route/nav wiring needed on
  either platform. New `useClosedPositions` hook in `usePositions.ts`.
  Client-side `close_reason` → plain-English label maps duplicated per
  screen, matching this codebase's existing convention for
  `CLOSE_ERROR_COPY` (server sends codes, client renders copy).

**Known gap, disclosed rather than silently dropped**: a position with
NO `agent_decisions` row at all (opened before this deployment's history,
or opened directly at the broker) has nothing for this endpoint to join
against, even after today's earlier `close_unmanaged_position_now`
closes it — that path persists an unlinked `orders` row
(`agent_decision_id=NULL`) with no decision to stamp. Confirmed live
this is currently a theoretical gap, not an observed one: `orders` has
zero rows with `agent_decision_id IS NULL` in production right now.

**Verified:**
- Full suite: **1300 passed, 11 skipped** (was 1264 passed, 11 skipped
  immediately before this change — net +36, no other deltas).
- **Revert-check (CLAUDE.md §4.1)**: flipped the SELL-side sign in
  `_estimate_exit_price` (`entry - delta` → `entry + delta`), reran
  `test_positions_service.py`, confirmed
  `test_estimate_exit_price_short_equity_gains_when_price_falls` (95.0
  expected, got 105.0) and `test_closed_from_decision_short_direction`
  (158.97 expected, got 156.71) both failed with the wrong number,
  restored the fix, confirmed 38/38 green again.
- **`list_closed_positions()` called directly against the real
  production DB** (read-only, via a one-off script, not just against
  fixtures): returned the exact 6 real rows with correct entry/exit/
  realized numbers, a `symbol="ko"` filter correctly narrowing to 1 row,
  `limit=2&offset=1` correctly paginating (skipping KO, showing XOM then
  CVX in `closed_at DESC` order), and an unknown user id returning
  `(0, [])`.
- `tsc --noEmit -p apps/mobile/tsconfig.json` clean both before and
  after the desktop-screen edit. `jest --silent`: 73/73 passed,
  unchanged (no pre-existing render-test precedent for either Positions
  screen to extend — matches the convention noted in the prior
  "close an unmanaged position" entry below).
- `ruff check` on the touched files: 1 new `B008` (`Depends()` in a
  route default) on the new endpoint — confirmed via `git stash` that
  the SAME warning already exists 72 times across `routers/` on `main`
  with zero changes, so this is the endemic, already-accepted
  FastAPI-`Depends()` pattern, not a new class of issue.
- **Live in a real browser** — Expo web (`--web`) + this worktree's own
  API (`uvicorn`, MockStore mode, `DEV_AUTH_BYPASS=1`), both started
  directly from this worktree's own checkout on non-default ports, NOT
  the prebuilt `apps/mobile/dist` export and NOT the `.claude/
  launch.json`-driven preview (which resolves to the MAIN checkout, not
  this worktree — confirmed by its logs printing the main checkout's
  path; a real environment quirk for a worktree-isolated agent, not
  specific to this task). Logged in via the dev-token flow. Desktop
  screen: toggled Open ↔ Closed live, both render correctly including
  the empty state; patched `window.fetch` for `/positions/history` to
  return three rows shaped exactly like the real production data (two
  `external_broker` equity closes plus one `order_fill` option close)
  and confirmed the table renders the ticker/direction, the "closed by"
  pill plus plain-English close reason, entry→exit with the "(est.)"
  qualifier appearing ONLY on the two estimated rows (never on the
  `order_fill` one), correctly color-coded realized P&L, and relative
  "closed X ago" timestamps — screenshot taken, matches the design
  exactly. Mobile screen: confirmed the Open/Closed toggle itself works
  live (correct sub-copy and empty-state text for both tabs) but did
  NOT get a populated-data screenshot — the mobile route has no direct
  link from the Home tab bar in this build (reached it via a
  `history.pushState` trick instead), and the query had already cached
  an empty result before the fetch-patch landed; forcing a refetch
  needed more environment wrangling (no exposed queryClient handle on
  `window`) than the remaining time justified. Not a code-correctness
  gap: the mobile screen renders via the identical `useClosedPositions`
  hook and `ClosedPositionDto` shape already confirmed live on desktop,
  plus a clean `tsc` pass — believed correct, not independently watched
  render with data.

**Left open**: no new "total realized since inception" dashboard tile —
out of scope per the task brief (the historical list was the core ask,
not a dashboard redesign); the existing open-positions screen's per-row
`unrealizedPnl` already covers the other half of "where did my gain come
from" for currently-open positions. The unmanaged-close gap above.

### 2026-09-01 — `279eac80` fix(mobile): reload logging users out + focus loss resetting nav to Dashboard

`ID:MODEL2OFF`. User reported two web/desktop bugs: (1) "every time i
reload i have to login again the app is not persistant", (2) "every time
i am on say insights screen i go to another window and go back i am
always taken to the dashboard only". Both traced to real races between
`authStore.restore()`'s boot logic and everything else that fires the
instant the app mounts — not the storage-adapter or overeager-clear-on-
boot hypotheses the brief suggested checking first. Full detail is in the
commit message; short version + what was actually verified below.

**Bug 1, auth persistence — TWO compounding bugs, both fixed.**
1. `authStore.restore()` posted its own direct `/auth/refresh` call
   instead of going through `refresh()`'s `inFlightRefresh` de-dupe.
   **Verified live** against the real backend (before any fix): a cold
   reload fired TWO concurrent `POST /auth/refresh` calls with the same
   stored refresh token; separately confirmed re-presenting an
   already-rotated refresh token deterministically returns
   `401 {"code":"superseded"}` — which the client already treats as a
   dead credential and wipes storage on, per `CREDENTIAL_DEAD_CODES`.
   Fixed: `restore()` now calls `get().refresh()` instead of posting its
   own request (`authStore.ts`).
2. Even with (1) fixed, live testing still showed **six** separate
   `/auth/refresh` calls on one reload (one superseded) — `inFlightRefresh`
   only merges callers overlapping in the same tick, and ~8 screens'
   queries mounting at once don't all 401 in that same tick on a real
   round trip. Fixed at the source: `api.ts`'s `_request()` now waits for
   `authStore` to leave `'idle'`/`'restoring'` before an authenticated
   call fires at all (`waitUntilBootstrapped`, wired from
   `app/_layout.tsx` via the existing lazy-getter — `api.ts` still never
   imports the store). **Verified live** (this worktree's real build,
   see the tooling note below): three consecutive reloads, exactly one
   `/auth/refresh` call each, zero forced logouts.

**Bug 2, nav reset on focus loss.** `BiometricGate`'s AppState
background/foreground listener had no web guard. react-native-web's
`AppState` is a polyfill over the Page Visibility API (read the actual
node_modules source to confirm, not just the type signature) — a
browser tab losing focus (switch tabs OR switch to a different app
window) fires it exactly like a native app backgrounding. Locking
unmounts `children`, which on desktop web is everything below the gate
including `NavProvider` (`src/desktop/nav.tsx`) — a bare `useState` with
no persistence, so remounting resets to the hardcoded dashboard default.
Fixed: skip that effect on web (matches `prompt()`'s existing web
pass-through reasoning — no biometric API in a browser). **Verified
live**: navigated to Insights, dispatched a real `visibilitychange`
hidden→visible cycle (the actual DOM event, not a simulated tab switch —
see the tooling note), stayed on Insights.

**Tooling trap worth flagging for whoever reads this next:** this
session's `preview_start`/Browser-pane tooling resolves `.claude/launch.json`'s
relative `cwd`s against the MAIN checkout, not the calling worktree. Named
dev-server previews (`dev-api`, `mobile-web`) serve the unfixed
main-checkout code regardless of what's rebuilt in the worktree — confirmed
by diffing the served JS bundle's content hash against the worktree's own
`apps/mobile/dist/index.html`. An absolute `cwd` in launch.json did NOT
fix this (the tool still resolved the same way) — the reliable workaround
is to launch the process manually via Bash from the worktree path and
`preview_start` a `url` pointing at that port, confirming via `curl` that
the served bundle hash matches before trusting anything the browser shows.
Every "still broken" observation earlier in that investigation reflects
real, unfixed behavior (useful — it's what proved the bugs exist); only
the final round (port 8001) is against this worktree's actual fix.

**What I verified vs. believe, explicitly:**
- Verified live: both original bugs are real (reproduced against running,
  unfixed code, multiple ways).
- Verified live: both fixes resolve the reported symptom (reproduced
  against this worktree's actual build once the tooling trap above was
  worked around).
- Verified mechanically: all three fix points have a Jest test that fails
  on the pre-fix code and passes on the fix (reverted + restored each to
  check, per §4.1) — `authStore.test.ts`, `api.test.ts`,
  `BiometricGate.test.tsx` (new file).
- Verified: full suite 82 passed (was 76), `tsc --noEmit` clean, changed
  files lint-clean. The one lint error `eslint` reports on
  `BiometricGate.tsx` (`react-hooks/set-state-in-effect`, line 119, an
  effect this change never touched) is confirmed pre-existing via
  `git stash` against the unmodified baseline — not introduced here.
- Did NOT touch: `apps/mobile/src/desktop/nav.tsx` itself (no persistence
  was added to the route stack) — the fix is that nothing should be
  unmounting it on a mere focus change in the first place. If a future
  need arises for nav to survive an ACTUAL full reload too, that's a
  separate, deliberate feature, not this bug.
- Left open: the MockAuthStore-backed local dev API has its own latent
  bug — `rotate_session`'s compare-and-swap reads `expected_current_hash`
  off a live, shared `SessionRecord` object reference rather than a value
  captured at the time of the original hash check, so a genuinely
  concurrent pair of `refresh()` calls can silently double-rotate instead
  of the loser being detected and revoked (`apps/api/app/services/auth/auth_store.py`'s
  `MockAuthStore.rotate_session` vs. `PostgresAuthStore.rotate_session`,
  which does a real atomic SQL `UPDATE ... WHERE` and does not have this
  problem). Out of scope for this fix (backend, not the reported frontend
  bug; discovered as a side effect of forcing the race to prove the
  frontend diagnosis) — flagged here rather than fixed.

### 2026-09-01 — `36930944`/`abdbce7c` fix(agents): why every position was long/calls-only — one deliberate gate, two real bugs

`ID:MODEL2OFF`. User looked at their real position list and asked: every
position is long (equity buys, and every option position is a CALL — no
puts, no shorts, anywhere). Is the agent structurally incapable of a
bearish trade, or has the market just not favored one? Mid-task the
mandate was raised explicitly: options must be tradable in both
directions by morning, and if equity shorting is genuinely missing that
matters too — but the post-LLM risk veto stack (MIN_FIT_TO_TRADE, premium
caps, drawdown halt, min_council_confidence, wash_sale, pdt_block,
options_disabled, …) was explicitly off-limits to loosen. Answer required
tracing the equity strategy layer, the options Bull/Bear council, and
checking the real production DB — not guessing. Did all three.

**Verified live against the real production Postgres** (`DATABASE_URL`
from `apps/api/.env`, read-only queries only, script discarded after —
not committed): 188 `agent_decisions` rows total, all one tenant
(`43221580-69bc-4134-8e1e-5af75499d874`), 2026-08-26 through 2026-08-31.
`final_action`: 137 HOLD / 43 BUY / 8 VETOED — **zero SELL rows, ever, of
any kind.** `reasoning->'strategy_fit'->>'allow_shorts'` is `false` on
180/188 rows (the other 8 are the options-council's own separate write
path, which doesn't persist that block — see the open item below) —
never `true`, anywhere. `reasoning->'strategy_fit'->'winner'->>'direction'`
is `"long"` on all 126 rows that ever picked a winner, `None` on the other
62 — never `"short"`. Even the full ranked candidate list
(`reasoning->'strategy_fit'->'ranked'`, every strategy scored, not just
the winner) contains a `"short"` entry on **zero** rows across the whole
history. Of the 8 rows that ever reached `is_option=true` (a contract
actually selected), 100% are `contract_type=call`, `direction=long`; the 2
that got risk-vetoed (`min_council_confidence`) were vetoed calls, not
puts. 9 more rows reached the options Bull/Bear council and resolved
`"Agents did not agree (abstained)."` without ever attempting a trade.

**Equity shorts: NOT a bug. Deliberate, documented, fully built, off by a
fail-closed flag.** `ALLOW_SHORTS` (`engine.env.env_flag`, default off,
typo-safe) gates a real, tested short-selling stack:
`forbid_short_phase_0` (master switch), `shortable_check` (borrow),
`short_requires_stop` (bracket geometry — a short's stop must sit ABOVE
entry), `short_unbounded_loss_cap`/`short_gross_exposure_cap` (tighter
notional caps because a short's loss is convex/unbounded, unlike a long's).
All real code, all covered (`test_paper_short_open_with_allow_shorts` and
friends). `apps/api/.env` (local) and `.env.example` both read
`ALLOW_SHORTS=0` explicitly, matching the DB's own history. **Not
changed** — the user's mandate said equity shorts "matter too" only if
genuinely missing, and this is a reviewed, working, intentionally-disabled
feature, not a gap. Turning it on live is a one-line Railway env var, no
code change, and is the user's call to make (same standing as any other
live risk-posture change) — flagged in the response, not flipped here.

**Options PUTs: two real, compounding, evidenced bugs. Both fixed, neither
touching a risk veto.** Traced every hop the coordinator asked about —
`engine/options/selection.py`'s contract funnel genuinely searches PUTs
for a "short" thesis (`wanted_type = "call" if direction == "long" else
"put"`, no hardcoding); the `open_option_trade` tool schema genuinely
accepts `"direction": "short"`; `ToolGuard._before_open_option_trade`
requires the model's `direction` arg to equal `ctx.resolved_direction`
with no long-only special case; `resolve()` doesn't drop a PUT
recommendation, it just requires both agents to independently agree
(working as designed, not a bug). The actual two bugs were both upstream
of all of that:

1. `strategy_fit_node` called `best_strategy(..., allow_shorts=
   env_flag("ALLOW_SHORTS"))` unconditionally, for an options pass exactly
   like an equity one. A cleanly bearish underlying — the best PUT
   candidate there is — scored badly on every strategy's LONG side, never
   cleared `MIN_FIT_TO_TRADE` (0.42, **unchanged**), and the options
   Bull/Bear council never even ran for it (`graph.py`'s
   `if not state.get("selected_strategy"): return state` fires before the
   options fork). Both downstream consumers of a "short"
   `selected_direction` were already correctly built and tested for it —
   `drafter._draft_option_proposal`'s existing
   `test_options_drafter_bearish_thesis_buys_a_put_but_side_stays_buy`
   passed all along by constructing `selected_direction="short"` BY HAND,
   because production never generated it. Fixed in `36930944`:
   `strategy_fit_node` now also scores "short" when the pass is already
   options-eligible (`ALLOW_OPTIONS=1` + `asset_class='option'`),
   regardless of `ALLOW_SHORTS` — because a bought PUT never opens a short
   position, it was never supposed to need that flag. A plain equity pass
   is provably unaffected (new test:
   `test_short_direction_never_scored_for_a_plain_equity_pass`).
2. `OPTIONS_BEAR`'s prompt has always said "do NOT return null merely
   because you found no BEARISH edge" — convert a weak edge into an
   honest low-conviction "long". `OPTIONS_BULL` had no mirror rule, so it
   defaulted to null the moment the call case looked weak, even when the
   same evidence argued for a put — and `resolve()` only trades when BOTH
   independently agree, so this alone meant the pair could functionally
   only ever agree on "long". Caught live, not hypothesised: the META row
   at 2026-08-31T16:56:24Z has Bear's own persisted `bear_case` reading
   "...so buying a put to express the bearish structural trend ... is
   more consistent with the evidence than the proposed long call" — and
   the pass still resolved `abstained` because Bull's own answer was also
   a stand-down. Fixed in `abdbce7c`: `OPTIONS_BULL` now carries the same
   anti-null instruction, mirrored.

**What I did NOT touch, on purpose:** `MIN_FIT_TO_TRADE` (still 0.42),
`resolve()`'s agreement requirement and conviction-gap threshold, any of
the 13 options risk rules, any of the shared equity risk rules, the
contract funnel's thresholds, `ALLOW_SHORTS`'s own default or its equity
behavior (pinned by a new test:
`test_allow_shorts_alone_still_scores_short_for_a_plain_equity_pass`).
Both fixes are on the "agents propose" side of CLAUDE.md §3's line, not
the "deterministic code disposes" side.

**Verified:** full `apps/agents` suite 344 passed/1 skipped, full
`packages/engine` 428 passed, full `apps/api` 433 passed/10 skipped — all
green after both fixes. Every new test revert-checked per CLAUDE.md §4.1
(fix temporarily reverted via `git stash`, confirmed the new tests fail —
3 of 4 strategy_fit tests, the 1 prompt test — with the exact expected
failure, restored). Environment note for whoever runs pytest here next:
this worktree's `.venv`-shared editable installs (`_editable_impl_*.pth`
under the MAIN checkout's `.venv`) point at the **main checkout's**
`apps/agents`/`packages/engine`/etc, not this worktree's copies —
`PYTHONPATH` must be prepended with this worktree's own
`packages/engine;packages/broker;apps/agents;apps/api` or pytest silently
tests the wrong (unmodified) source tree. Lost real time to this before
noticing; leaving it here so it isn't rediscovered from scratch.

**Left open / not verified this pass:**
- Did not verify Railway's CURRENT live env vars directly (no Railway
  access this session) — inferred `ALLOW_SHORTS`/`ALLOW_OPTIONS`/
  `USE_OPTIONS_AGENT`'s effective production values from local `.env` +
  the DB's own `reasoning.strategy_fit` history (which is a real record of
  what actually ran, not a guess) rather than a direct Railway variable
  read. If Railway's values differ from local right now, that would only
  ever make the fix MORE likely to matter (both bugs were "even when
  everything else is on, this one gate still says no"), not less.
- Did not verify against a live Anthropic call that OPTIONS_BULL's edited
  prompt actually changes real model behavior — that is fundamentally not
  unit-testable; the new test pins the INSTRUCTION's presence and wording,
  not the model's response to it. First real signal will be the next live
  options council pass once deployed.
- Noticed but did NOT chase (CLAUDE.md §4.7 — out of scope for this ask):
  on 2026-08-31 the SAME symbols (MSFT, AAPL) each produced TWO
  differently-shaped options decision rows minutes apart — one camelCase
  (`isOption`/`contractType`, the new Bull/Bear `options_council` path,
  ending HOLD/BUY) and one snake_case (`is_option`/`contract_type`, the
  OLDER shared-equity-council options branch in `drafter.py`, ending
  `VETOED` via the ordinary `risk_officer`/`min_council_confidence` rule).
  Both are individually correct and already tested, but I did not
  determine WHY the same symbol hits both mechanisms on the same day
  (likely something in how `USE_OPTIONS_AGENT`/`instrument_preference` is
  decided per-pass rather than per-symbol) — flagged for the user
  separately rather than silently expanding this fix's scope.

### 2026-09-01 — c36050ad fix(options): the Bull/Bear trade tools never wrote an orders row

`ID:MODEL2OFF`. Investigated a live-production bug report: 9 auto-approved
decisions today, 6 (all NVDA/SPY/QQQ options) missing an `orders` row
entirely, AND (separately reported) 6 option positions on the Positions
screen labeled `UNMANAGED`/"no council decision behind it" despite a
matching `agent_decisions` row existing for each. Queried the real
Railway Postgres directly, read-only, before touching any code (per the
brief's own instruction) — `DATABASE_URL` isn't in this worktree's
`apps/api/.env` (gitignored, worktrees don't copy untracked files), so I
copied it in from the main checkout via a single `cp`, read it, and passed
`DATABASE_URL=...` inline on each script invocation rather than `source`
(this agent's shell sandbox refuses `source`/subshell-heavy commands that
reference paths outside the worktree — plain single-purpose commands are
fine).

**First finding: these are the SAME 6 decisions, not two bugs.** The
occSymbols in Bug 1 (missing orders rows) and Bug 2 (UNMANAGED) are
byte-identical sets: `NVDA260918C00215000`, `NVDA261002C00225000`,
`NVDA261009C00230000`, `QQQ260918C00708000`, `SPY260918C00765000`,
`SPY261002C00771000`. My working assumption going in (per CLAUDE.md
§4.7/the brief's own "don't assume, check") was that these might be
separate cohorts — equity auto-approvals vs. older option positions —
since the brief described them on different timeframes. They are not:
same 6 rows, two symptoms of one missing write.

**Root cause, traced to the actual code, not inferred from the DB alone:**
`apps/agents/trading_agents/options/tools/trade.py`'s `open_option_trade`
(the Bull/Bear options-council direct-execution path — completely
separate from `apps/api`'s `executor.py`/`auto_approver.py`, which the
brief's own hypothesis assumed was involved and which is, in fact,
innocent here) calls `broker.place_order()` directly and only ever wrote
an `agent_decisions` row via `decision_log.record()`. Confirmed via
`grep` and a full read: zero calls to `persist_order_submit`/
`persist_linked_order_submit` anywhere in `trade.py` or `guard.py`, before
this fix. `approval_mode='auto'`/`user_response='approved'` on these rows
come from `guard.py`'s OWN `ToolGuard._stamp_auto_approval` (called from
`after()`) — a function with the same name and shape as
`apps/api/.../auto_approver.py`'s, but a completely separate
implementation for a separate execution path. Verified this distinction
by reading `PostgresDecisionLog.record()`
(`apps/agents/trading_agents/memory/postgres.py`) end to end: it does not
set `approval_mode`/`user_response` at all, so if `guard.after()` weren't
also stamping them, these rows would look like an ordinary `'ask'`/`NULL`
pending proposal — they don't, because `guard.after()` does that stamp
right after the trade succeeds.

**Consequence, not just an audit-trail gap:**
`position_manager.py`'s `manage_positions_for_user` (the ratchet/
stop-loss/time-stop loop) AND `sweep_expiring_options_for_user` (the
supposedly-unconditional DTE<=2 expiry sweep) both filter
`fill_qty IS NOT NULL`. With no `orders` row, `order_sync.py` never had
anything to poll, so `fill_qty` stayed NULL forever — meaning **none of
docs/OPTIONS_PLAYBOOK.md §3's five exits have been running on any of
these 6 real, open, paper positions since they opened.**

**Two more bugs found in the same investigation, same TRAP pattern
(OPTIONS_PLAYBOOK.md §5 item 1: symbol=underlying, occSymbol=contract)
but different code, found independently by a sibling agent's parallel
investigation and cross-verified by me before fixing:**

- `positions_service.py`'s `_unmanaged()` built its `covered` set from
  `OpenPositionDto.symbol` (always the underlying) and compared it
  against broker-reported keys (OCC for an option, confirmed by reading
  `packages/broker/broker/alpaca.py::_position_from_alpaca` and
  `packages/engine/engine/reconciler/snapshot.py`, which writes
  `PositionsSnapshot.open_positions[].symbol` straight from
  `broker.list_positions()`) — so a decision-backed option could NEVER
  register as covered. This is the actual mechanism behind the
  "UNMANAGED / no council decision behind it" label, independent of Bug 1
  — it would still occur even for an option that DID get a proper
  `orders` row and a populated `fill_qty` through the ordinary
  human-approval path. Same mismatch also made `marks.get(d.symbol.upper())`
  miss for every OPEN option, so `last_price`/`unrealized_pnl` silently
  read `None` even for correctly-managed option positions.
- `order_sync.py`'s `_detect_external_closes()` had the same mismatch on
  the WRITE side: `held_qty` keyed by the broker's OCC symbol, looked up
  by the decision's underlying — so a genuinely still-held option would
  be wrongly stamped `close_reason='external_broker'` on the very first
  tick after its `fill_qty` ever populated, permanently disabling exit
  management right after "fixing" it looked like it worked. Hadn't fired
  yet on the live 6 only because Bug 1 excludes them from this query too
  (same `fill_qty IS NOT NULL` filter) — fixing Bug 1 alone, without this
  one, would have let it fire the moment `fill_qty` first populated.

`64979a8c` (2026-08-29, "address the broker by OCC contract, not the
underlying") already fixed this exact trap in `executor.py` and
`position_manager.py` ("the close path had the same bug in four more
places") — its own diff-stat shows it touched only those two files, never
`order_sync.py`/`positions_service.py`. These are the trap's 5th and 6th
sites, not a new bug.

**Third bug, from a sibling agent's live HTTP check against the deployed
API** (GET `/api/v1/positions` returning `isOption:false`/`occSymbol:null`
for every position, including the obviously-OCC ones): `schemas/
positions.py`'s `OpenPositionDto` already had
`is_option`/`occ_symbol`/`contract_type`/`strike`/`expiry_date`/
`multiplier` fields — added, per its own comment, "purely additive, so the
wire contract is ready the moment that track wires population." Nothing
ever did. A $2,392-notional NVDA call was rendering as "NVDA LONG qty 4"
with no indication it was a 100x-levered contract. Populated in both
`_from_decision` (from the proposal JSONB) and `_unmanaged` (from the
snapshot's `is_option`/`multiplier` plus `OccSymbol.try_parse` on the
broker's own OCC string) — an unmanaged option's `symbol` now reports the
underlying too, matching the managed path's display convention (it used
to show the raw OCC string).

**Fix:** new `guard.persist_placed_order()`, called from all three of
`trade.py`'s `broker.place_order()` sites (open, scale-in, exit) right
after each succeeds — writes the same `orders` row shape `order_store.py`
already writes, so `order_sync.py` needs zero changes to pick these up
going forward. Resolves `user_id -> broker_connection_id` via a direct
`engine.db.models.BrokerConnection` read (packages/engine is a shared
dependency; `apps/agents` deliberately does not depend on `apps/api` —
matched the existing "reimplement, don't cross-import" precedent already
documented in `guard.py`'s own `_trading_mode` docstring). Wrapped in its
own try/except that logs and returns rather than raising: by the time it
runs, the broker order is already real, so a DB hiccup here must not
report `tool_failed` to the model (which would be a lie — the trade
happened) — same contract `executor.py`'s own `persist_order_result` call
site already documents. `positions_service.py`/`order_sync.py` got their
own local `_broker_key_for_decision` helpers (small, duplicated per file
rather than a new shared module — consistent with this codebase's
existing convention for this exact cross-package-boundary tradeoff, but
flagged in the commit as a reasonable follow-up refactor).

**What I VERIFIED, and how:**
- Live Postgres queries (read-only) confirmed: the 6 decisions'
  `proposal->>'isOption'='true'`, `fill_qty IS NULL`, zero matching
  `orders` rows by decision id, by symbol, and by
  `client_order_id LIKE 'agent-exec-%'` (i.e., genuinely never attempted
  via the human-approval path either) — this ruled out a deploy-lag
  theory I considered and discarded once `70db7a9d`'s diff-stat showed
  the persist-before-place ordering in `executor.py` predates these
  trades by 3 days.
  - Re-confirmed the SAME 6 decisions' current state right before writing
  the backfill script (values unchanged from the first query).
  - Queried the latest `positions_snapshot` row directly: all 6 OCC
  contracts are real, live, filled Alpaca paper positions with exact
  qty/avg_entry_price — used verbatim in the prepared backfill script.
  - Full suite: `1268 passed, 11 skipped` (`apps/agents apps/api
  packages/`), run with `PYTHONPATH` pointed at this worktree's source
  (the worktree has no `.venv` of its own; used the main checkout's venv
  interpreter with `PYTHONPATH` prepended so it resolves imports from
  worktree source, confirmed by the parameter-rename test failures I'd
  expect only against MY edited signatures).
  - Revert-check per CLAUDE.md §4.1: `git stash push` on just the 4
  source files (guard.py, trade.py, order_sync.py, positions_service.py),
  keeping the new tests — collection fails with `ImportError` for
  `persist_placed_order`/`_broker_key_for_decision` in exactly the 3
  files that should reference them; `git stash pop` restored, full suite
  green again (1268/11 skipped, unchanged).
  - Found and fixed 4 pre-existing test call sites broken by
  `_unmanaged`'s `managed: list[OpenPositionDto]` -> `covered: set[str]`
  signature change (2 in `test_positions_service.py`, 1 local wrapper in
  `test_positions_route.py`) — these were NOT part of my new coverage,
  just mechanical fallout from the signature change, confirmed by
  re-running the full suite before declaring done.

**What I did NOT do, and why — left open, explicitly:**
- **Did not run the data backfill.** The 6 existing broken decisions stay
  exactly as broken as they were until someone runs
  `scripts/backfill_option_orders_2026_08_31.py --apply` (dry-run by
  default; prints what it would change; exact values already verified
  live against the current snapshot). The code fix only stops this from
  happening to NEW trades. I did not execute this against production
  myself: the task's own process for this whole investigation was
  "fix the code, I review and merge personally, it's safety-critical" —
  a direct prod DB mutation is at least as consequential as a code merge
  and wasn't itself asked for. This is the single most important thing
  for whoever picks this up next to see: **the 6 positions are still
  unmanaged until the backfill runs** (or until a human closes them
  manually at the broker).
- Did not consolidate the now-four+ separate implementations of "is this
  decision an option, and if so what's its OCC symbol"
  (`executor.py::_wire_symbol_for`, `position_manager.py`'s inline
  version, and the two new ones this commit adds) into one shared
  `packages/engine` helper — each of the two files I touched got its own
  small local copy, matching the existing per-file convention, but a
  shared helper would be a reasonable follow-up now that the trap has
  bitten a 5th and 6th time.
- Did not touch `apps/api`'s `auto_approver.py`/`executor.py` at all —
  traced them thoroughly (they're where the brief's own hypothesis
  pointed) and confirmed they are NOT implicated: the persist-before-place
  ordering there is correct and already covered by existing tests.

### 2026-09-01 — feat(positions): close an unmanaged position from the app (no more "close at broker")

`ID:MODEL2OFF`. User complaint: real paper option positions on the Positions
screen, labeled MANUAL/UNMANAGED, showed "close at broker" as dead text —
"why do i need to go to broker api to actually close the trade? user should
be able to close it." Task: build a real close action reachable from the
UI, for both agent-managed and manual/unmanaged positions.

**First finding — most of this already existed.** `close_position_now` +
`cancel_pending_order_now` (`apps/api/app/services/orders/position_manager.py`)
and `POST /api/v1/positions/{decision_id}/close` (`routers/positions.py`)
were already fully built, tested, and wired to a working button on BOTH
screens (`Positions.tsx` desktop, `app/positions.tsx` mobile) for any row
with a `decisionId` — agent-managed AND manual-mode-with-a-decision. And
"Cancel order" for a not-yet-filled row already works end to end through
the SAME endpoint (`close_position_now` dispatches to
`cancel_pending_order_now` when `fill_qty` is null) — confirmed via the
existing `test_cancel_pending_order_cancels_at_the_broker_and_updates_status`
/ `..._with_no_working_order_refuses` tests, both already green. Neither of
these needed fixing.

**The actual gap**, confirmed by reading `positions_service.py`'s own
docstring: a genuinely **unmanaged** position (`managed=False`,
`decision_id=None` — opened directly at the broker, or predating this
deployment's decision history) has NO `AgentDecision` row at all, so
`close_position_now`'s decision_id lookup structurally cannot reach it.
`_unmanaged()`'s own comment says it plainly: "the client offers no close
button for them." Both screens rendered a dead `<span>`/`<Text>` instead —
exactly the user's complaint, and exactly the MANUAL+UNMANAGED combination
they described (an unmanaged row always shows `exitMode='manual'`, so it
carries both pills).

**What was built:**
- `position_manager.close_unmanaged_position_now` (gated public wrapper,
  mirrors `close_position_now`'s Postgres-gate-then-delegate shape) +
  `_close_unmanaged_position` (ungated worker, mirrors `_close_position`
  but simpler — no decision/proposal fallback branch is possible since
  `held` must already exist at the broker to proceed at all). Keyed by
  `symbol` (the broker's own position key — OCC for an option, ticker for
  equity) instead of a decision_id. Same risk gate
  (`engine.risk.evaluate`), same bracket-cancel, same broker abstraction as
  every other close in this file.
- Ownership is enforced STRUCTURALLY, not by an owner-field check: the
  close only ever opens the CALLING user's own broker connection
  (`with_broker_client(user_id, ...)`) and only matches a position inside
  THAT connection's own `list_positions()` — there is no shared resource
  keyed by a guessable id for a second user to reach.
- `order_store.persist_unlinked_order_submit` (+ shared
  `_insert_pending_order_row` helper, refactored out of
  `persist_linked_order_submit` with no behavior change to that function).
  `agent_decision_id` is always NULL — structurally, that absence IS the
  audit signal distinguishing a user-initiated unmanaged close from every
  other close this module persists (an agent close or a decision-linked
  manual close both stamp a `close_reason` on the owning decision; this
  one has no decision to stamp, so "unlinked order + logged
  reason=user_manual_unmanaged" is the record instead).
- `POST /api/v1/positions/unmanaged/{symbol}/close` (`require_real_auth`,
  same as the decision route) — registered ahead of `/{decision_id}/close`
  in the file; the two never actually compete (different path-segment
  counts) but the more specific route reads clearer listed first.
- `ClosePositionResponse.decision_id` widened to `str | None` + new
  `symbol: str | None` field (one response shape for both close routes) —
  additive, backward compatible. Mirrored in
  `packages/shared-types/src/index.ts`.
- UI: both screens now render a real "Close"/"Close now" button for an
  unmanaged row instead of dead text, via a new
  `useCloseUnmanagedPosition()` hook (`apps/mobile/src/hooks/usePositions.ts`).
  **Also added a confirmation gate that was missing on desktop**: the
  existing decision-based Close button on `Positions.tsx` fired
  `close.mutate()` directly on click with NO confirmation at all (the
  mobile screen already had one via `Alert.alert`) — added a
  `window.confirm()` gate ahead of BOTH the existing and the new mutation
  on desktop, matching the mobile screen's existing behavior.

**Verified live, not just tests green:**
- Ran the real API against Postgres-off (MockStore) mode:
  `OPTIONS .../positions/unmanaged/AAPL/close` → `allow: POST` (route
  registered correctly); `POST` → `401` unauthenticated, `409
  {"detail":"no_open_position"}` authenticated — matches the router's
  `_CLOSE_ERROR_STATUS` mapping exactly.
- Ran the ACTUAL Expo dev server (`expo start --web`, not the prebuilt
  static export `apps/mobile/dist` that `apps/api`'s "Web UI enabled" path
  serves — that export predates this change and does not contain it),
  logged in via the dev-token flow, patched `window.fetch` in the live
  page to inject two unmanaged rows (one equity, one losing option
  contract — `AAPL260828C00250000`) alongside one normal agent-managed
  row. Both desktop (`Positions.tsx`) and mobile (`app/positions.tsx`)
  screens rendered a real, correctly-labeled "Close"/"Close now" button for
  both unmanaged rows (previously dead text) and left the agent-managed
  row's existing button untouched. On desktop: confirmed `window.confirm()`
  genuinely gates the action (clicking Close with the dialog un-patched
  produced ZERO network requests — auto-dismiss = cancel, correctly
  blocked); with `window.confirm` monkey-patched to auto-accept, clicking
  Close fired `POST /api/v1/positions/unmanaged/GME/close`, got back real
  `409 {"detail":"no_open_position"}` from the real backend (correct for
  this MockStore environment — no live broker/position exists), and the
  UI's error banner correctly rendered "Close refused / This position was
  already closed — nothing left to close." End-to-end: click → confirm
  gate → real HTTP call → real router → real service → real structured
  error → real UI rendering, all confirmed live, not mocked.
- **Left unverified, and why**: could not click through the mobile
  screen's `Alert.alert`-based confirm gate in a browser — verified by
  reading `node_modules/react-native-web/src/exports/Alert/index.js` that
  react-native-web's `Alert.alert` is a hard no-op (`static alert() {}`,
  does nothing, no callback ever fires) under `expo start --web`. This is
  pre-existing (the ORIGINAL decision-based close button already used
  `Alert.alert` before this change) and does not affect production: the
  prebuilt static web export served by `apps/api` is built from the
  SEPARATE desktop tree (`src/desktop/`), so `app/positions.tsx` is only
  ever reached on native iOS/Android in real usage, where `Alert.alert` is
  the real native API and works normally. Confirmed the button itself
  renders correctly and confirmed the mutation wiring by code review +
  TypeScript + the fact that it's the identical call shape as the
  already-shipped, already-working decision-based path — but did not watch
  the mobile confirm→mutate transition fire live the way I did on desktop.

**Tests**: 14 new (11 in `test_position_manager.py` covering
`_close_unmanaged_position` long/short/option/no-position/audit-linkage +
`_has_in_flight_unmanaged_close` + the gated wrapper's mock-mode/bad-uuid/
in-flight/happy-path branches; 3 in `test_positions_route.py` covering
auth + the mock-mode 409 + the two routes' non-collision). Per CLAUDE.md
§4.1: temporarily hardcoded `is_short = False` in the new equity close
branch (the exact short-covering bug class this codebase has shipped
before) and confirmed `test_close_unmanaged_position_covers_a_short_with_a_buy`
failed — with the SAME `forbid_short_phase_0` veto message the historical
bug produced — then restored it and confirmed green again.

Full suite: `1264 passed, 11 skipped` (up from the prior baseline — net
+14 from this change, matching the count above; no other deltas). Ruff on
the touched files: 3 `B008` (`Depends()` in a default arg) — confirmed
pre-existing/endemic by running ruff over the whole `routers/` directory
(72 of the same warning repo-wide); not introduced by this change.
`tsc --noEmit -p apps/mobile/tsconfig.json` clean. `jest --silent`:
`73 passed` (unchanged — no new frontend unit tests added; the screens
themselves have no pre-existing render-test precedent to extend, only the
shared `ClosePositionButton` does, and it wasn't touched).

**Left open** (out of scope for this task, noted for whoever picks these
up): (1) `OpenPositionDto.is_option`/`contractType`/`strike`/`occSymbol`
are NOT populated by `positions_service.py` for a REAL broker position
today (confirmed by re-reading `_from_decision`/`_unmanaged` — neither sets
them) — the schema comment says this is "a separate track's scope",
matching the parallel investigation into mislabeled unmanaged positions
mentioned in this task's brief; I did not touch it. (2) The desktop
Close/Cancel button still bubbles its click up to the row's own
`onClick` (opens the trade-biography panel) for a decision-backed row —
pre-existing (present before this change too), not fixed here.

### 2026-09-01 — diagnosis, no code change: the "AWAITING FILL" positions are options that already filled, not stuck equity orders

`ID:MODEL2OFF`. Asked to investigate NVDA/SPY/QQQ positions reported stuck
"AWAITING FILL" for hours with no stop/target shown, and to verify whether
CVX/BAC/VZ (which filled in the same batch) got a broker-side bracket.
Read-only against the live production Postgres the whole way
(`apps/api/.env`'s `DATABASE_URL` against the real Railway DB, via
`engine.db.session.async_session_factory()` — same pattern CLAUDE.md's
§4.3 asks for: measure the live funnel, don't reason about it). No writes,
no code changes this session.

**Finding 1 — the six "stuck" rows are OPTIONS proposals, not equity.**
The task brief described them as equity buys (NVDA qty 4, SPY qty 2, NVDA
qty 2, QQQ qty 1, SPY qty 2, NVDA qty 4). Queried `agent_decisions` for
this user (`43221580-69bc-4134-8e1e-5af75499d874`) and every one of those
six rows carries `proposal.isOption: true`, `proposal.orderType: "LIMIT"`,
plus `occSymbol`/`strike`/`contractType`, and
`estimatedNotional == qty * limitPrice * 100` (the options multiplier —
e.g. QQQ qty=1 @ limitPrice 16.35 → estimatedNotional 1635.0, not 16.35).
The six qtys match the report exactly (QQQ×1, SPY×2, NVDA×3). This rules
out both hypotheses in the brief (equity limit-price-stale, missing equity
bracket) — neither one applies; these six never touched the equity/
executor.py path at all.

**Finding 2 — equity order construction + bracket attachment: verified
correct and working, live, no gap, no fix needed.**
`apps/agents/trading_agents/nodes/drafter.py:296` hardcodes
`"order_type": "MARKET"` for every equity proposal (a stale LIMIT price
cannot occur — there is no equity code path that emits LIMIT).
`apps/api/app/services/orders/executor.py:227-294` attaches a broker-side
bracket (GTC, `take_profit_price`/`stop_loss_price`) whenever
`exit_mode=agent` and the proposal carries both `stop_loss`/`target_price`
(populated by `engine.sizing.atr_position_size` in the drafter). Queried
literally every order row this user has ever placed — 9 total (CVX×2, BAC,
VZ, XOM, UNP, SPY, JNJ, KO): **100% are `order_type=MARKET`,
`order_class=OrderClass.BRACKET`, `status=filled`**, each with a real
take-profit LIMIT leg + stop-loss STOP leg on the wire (`raw_response`'s
`legs`), GTC, priced directly off that same proposal's stopLoss/
targetPrice. The 5 orders submitted pre-market 2026-08-27 (09:27-09:32 UTC,
before the 13:30 UTC/9:30 ET open) correctly sat accepted until the open
and filled 1-5 minutes after it — expected Alpaca behavior for a DAY MARKET
order submitted pre-open (`positions_service.py`'s own comment predicts
exactly this), not a bug.

**Finding 3 — the six option positions already filled at the broker and
are running with ZERO exit management. This is worse than "still
working."** `positions_snapshot` (populated every ~30s directly from
`broker.list_positions()` — confirmed live and current: 5 consecutive rows
18:53:29-18:55:35 UTC) shows all six as real, currently-open Alpaca
positions: `NVDA260918C00215000` qty2@8.70, `NVDA261002C00225000`
qty4@5.95, `NVDA261009C00230000` qty4@5.30, `QQQ260918C00708000`
qty1@16.25, `SPY260918C00765000` qty2@9.35, `SPY261002C00771000` qty2@8.73
— qty and fill price matching each decision's proposal almost exactly.
Yet every one of the six `agent_decisions` rows still has `fill_qty IS
NULL` (confirmed: 6/6, zero matching `orders` rows each). Root cause, read
directly from the code:

  - `apps/agents/trading_agents/options/tools/trade.py`'s `open_option_trade`
    (the options council's trade tool — `docs/OPTIONS_PLAYBOOK.md` §0.5)
    calls `broker.place_order(...)` directly, then writes ONLY an
    `agent_decisions` row (`decision_log.record(entry)`), with
    `fill_qty=order.filled_qty or None` captured once, synchronously,
    milliseconds after submission (line ~176). It never calls anything
    like `order_store.persist_order_submit`. Grepped `trade.py` + `guard.py`
    for `Order(`/`persist_order`: zero matches, in either file. The equity
    path's whole audit-chain mechanism — `executor.py` writes the `orders`
    row BEFORE calling the broker specifically so a later poll has
    something to find — was never built for this tool.
  - `apps/api/app/services/orders/order_sync.py::_sync_open_orders` is the
    ONLY code that ever re-polls a broker order for a later fill, and it
    only looks at existing `orders` rows. Zero rows for these six means
    zero chance of ever being polled, no matter how long they sit.
    Confirmed `order_sync` itself is not the problem — it's exactly what
    healed CVX/BAC/VZ from pending to filled within seconds, live, this
    session.
  - `apps/api/app/services/orders/position_manager.py:127` (the exit
    ratchet: stop-loss/take-profit/trail/time-stop) and `:496` (the DTE≤2
    expiry sweep — `OPTIONS_PLAYBOOK.md` §3: "not optional") both filter on
    `AgentDecision.fill_qty.is_not(None)`. All six fail that filter, so
    **none of the five documented option exits can fire on them, including
    the unconditional expiry sweep.** They're real (paper) positions,
    ~$10.7k notional combined, with no automated protection of any kind,
    for as long as they're left alone.
  - The Positions screen's "—/—" under STOP/TARGET for these (and for
    every option position, filled or not) is separately correct and
    by design, not a bug: `positions_service.py:186-187` reads
    `proposal.get("stopLoss")`/`.get("targetPrice")`, and
    `trade.py::_proposal_dto` never sets those keys for an option — Alpaca
    cannot bracket a single-leg option (`OPTIONS_PLAYBOOK.md` §3). That
    part of the report is expected behavior. `fill_qty` silently staying
    NULL on a real position is the actual bug.

**Scope note — overlaps with, but goes further than, the sibling
worktree's assignment.** The task brief said a different agent in a
different worktree is already investigating "why these six have no
`orders` row," and asked me not to duplicate that. I didn't attempt a fix
(no code changed this session) — but the framing "why is the row missing"
undersells it: **these are not pending orders, they are open, filled,
completely unmanaged positions, right now.** Closing this needs two
things, not one: (a) `open_option_trade`/`adjust_option_position` persisting
an `orders` row the way `executor.py` does, so future entries are pollable;
and (b) a one-time backfill of `fill_qty`/`fill_avg_price` on the six
`agent_decisions` rows that already exist and already filled (ids:
`6dca9c80-4a0f-48b5-ab42-ca666dcf8d66`, `dc52edd3-03e3-4bfb-bfee-942725c5ab82`,
`909e076d-827c-456d-abbb-e8ff08de9781`, `d52b169c-ccb4-4129-83e8-cbfb2936fe7c`,
`c1f5e59e-c1b8-4635-b82a-7d806bd112d8`, `57baf2a5-d6c3-4fd4-9d76-ff8b0ef08976`)
— (a) alone only protects the NEXT trade; these six stay exposed until
someone also does (b). I did not do the backfill myself: it is a
production data mutation, it wasn't what this task asked me to do
(read-only), and it belongs with the other worktree's code fix, not
separately from it.

**Verified live, by direct query output (not inferred):** every claim
above, against the real Railway Postgres — three read-only queries over
`orders`, `agent_decisions`, `positions_snapshot` for user
`43221580-69bc-4134-8e1e-5af75499d874`. **Not verified:** Alpaca's raw API
response directly — this environment has no Alpaca credentials (local
`.env` ships blank `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`; the production
OAuth token is encrypted with a `BROKER_TOKEN_ENCRYPTION_KEY` I don't have
either). "These six are open positions at Alpaca" rests on
`positions_snapshot`, itself a direct, unmodified `broker.list_positions()`
read the reconciler took ~1-2 hours after the trades — the closest thing
to broker ground truth this system records, but one hop removed from
Alpaca's API itself.

**Left open:** whether `open_option_trade`'s LIMIT-at-ask would ever
realistically sit unfilled for a meaningful window (i.e. whether this
would ALSO have been a genuine fill-latency problem if the audit-chain gap
didn't exist) — moot for these six since they filled, but worth knowing.
Not investigated; out of scope once the question turned out to be about
missing plumbing rather than fill latency.

### 2026-08-31 — docs: PLAN_AUTO_APPROVE.md's "not built" header was stale — the feature shipped 08-30

`ID:MODEL2OFF`. User pointed at `docs/PLAN_AUTO_APPROVE.md` and asked to
start implementing Plan E (unattended entry execution). Before dispatching
anything, checked whether `apps/api/app/services/orders/auto_approver.py`
already existed — it did: `3bce40b2`/`4e46507e`/`9475c83b`, merged
2026-08-30 22:17-22:18, a full day before this check. The doc's own "Status:
plan, not built" header never got updated — the exact CLAUDE.md §4.2 trap
("docs/OPTIONS_PLAN.md says 'proposal, not built' — most of it shipped"),
now confirmed a second time on a different doc.

Independently re-verified the shipped code rather than trust the plan doc's
old claims or the commit messages: `ReconcilerFleet.tick()` calls
`auto_approve_for_user` immediately after `manage_positions_for_user`
(correct order — exits free premium before entries consume it); a real
gate 2b (per-connection `auto_approve_consent`, DB column + migration +
router + store, not a stub) sits on top of the plan's original seven gates;
`test_auto_approver.py` has 19 tests covering every gate in the plan's
revert-check matrix plus the added 2b cases. Personally revert-checked gate
2 (the single most safety-critical line: hard-coded paper-only, must never
become configurable) — inverted `if not (trading_mode() == "paper" and not
env_flag("LIVE_TRADING_ENABLED")):` to `if False and ...`, confirmed
`test_never_auto_approves_in_live_mode` and
`test_never_auto_approves_when_live_trading_enabled` both fail exactly as
predicted, restored, confirmed all 19 tests in the file pass again.

Updated the doc's header to point at the real merged commits and the real
file location (`.../auto_approver.py`, a separate module, not inline in
`executor.py` as an earlier draft of the plan implied). Left the rest of
the document as accurate historical design background.

**Not verified this pass:** whether `AUTO_APPROVE_ENABLED` is actually set
on the live Railway deployment right now, or whether any real
`agent_decisions` row has ever carried `approval_mode='auto'` — that needs
either Railway access (blocked this session — see below) or a working
`DATABASE_URL` against the real Postgres (local `.env`'s value doesn't
reach it: `password authentication failed for user "app"`). The user
separately reported seeing "options getting picked by the app auto mode,"
which is consistent with this feature being live and enabled, but that's
an observation, not something I confirmed against a database row.

**Railway access, separately:** the user provided a Railway token this
session. `railway whoami --json` against it failed —
`invalid peer certificate: UnknownIssuer` reaching
`backboard.railway.com`. A raw `curl` to the same host AND to
`api.github.com` both failed too, via a DIFFERENT error
(`schannel: ... CRYPT_E_NO_REVOCATION_CHECK`), while `git` (bundles its own
TLS stack rather than using Windows schannel) reaches `github.com` fine.
Best read: this sandboxed shell's network egress doesn't reach
`backboard.railway.com` at all, independent of the token's validity —
not something a different token would fix, and not something to route
around by disabling TLS verification. Left as an open item; did not
retry with `--browserless` or any cert-bypass flag.

### 2026-08-31 — `9f824b4b` docs(options): add the adjust_option_position gate asymmetry to the trap list

`ID:MODEL2OFF`. Follow-up to `dcf58ca4` (below) and to the cross-session
audit's `1b329241`/`b02a1977` schema fix. While merging agent work I found
a second, fully independent worktree (`claude/keen-saha-f25628`, branched
from an old point in history, never merged) that had rediscovered and
fixed the *exact same* `adjust_option_position` gate gap 11 minutes after
`dcf58ca4` landed — same three checks, same `GuardVerdict` reason
strings, byte-for-byte equivalent logic. Confirmed via `git merge-base`
that it branched before `dcf58ca4`, so it was never aware the fix had
already shipped; its guard.py diff is now a pure no-op against main and I
did not merge it. Its only genuinely new content — a docs/OPTIONS_PLAYBOOK.md
§5 entry, and ~20 test permutations across all 5 adjust actions vs. main's
4 — is a real signal this exact trap was worth documenting so a third pass
doesn't burn time rediscovering it a third time. Added §5 item 6
referencing the real merged commit (`dcf58ca4`), left the stale branch/
worktree in place (worktree removal hit a Windows file-lock, harmless to
leave; branch itself costs nothing to keep around). Did not port the extra
20 tests over — the gate runs unconditionally before the action switch in
both versions, so main's 4 tests (one per check) already prove it holds
for all 5 actions by construction; the fuller matrix is redundant
rigor, not a coverage gap. Verified: `docs/OPTIONS_PLAYBOOK.md` renders
correctly; no code touched, no test run needed.

### 2026-08-31 — consolidated: `ffe75213`..`f3fbe74e` (6 commits) + cross-check/fix pass

`ID:MODEL2OFF`. This is ONE entry standing in for 6 commits that should
each have gotten their own (CLAUDE.md §6 requires one per commit; none of
these got it at the time). Per the dispatching instruction, I am not
reconstructing full historical detail for each — just what they did and
what I verified about how they interact with the options work that landed
earlier the same day (`51d1457f`..`ab4bda33`, the Bull/Bear agents +
guard + tools + escalation loop).

**Identity note, because it matters for the next reader:** of these 6,
`ffe75213`/`fcb00320`/`ef14352e`/`a7b3a379` all carry `ID:MODEL1REAL`
(Opus) — this was NOT a second model's session, contrary to how this task
was framed when dispatched to me. The last two, `c3565e0a` and
`f3fbe74e`, carry **no identity trailer and no conventional-commit
message at all** — both bodies are verbatim, all-caps user instruction
text ("FIX EVERYTHING DELEGATE 5 SUB AGENTS" / "5 SUBAGENTS"), which reads
as the user's own prompt landing in the log verbatim rather than a
model-authored summary. Whoever picks this repo up next should know the
git-log channel (CLAUDE.md §0) was not followed for these two.

**What the 6 commits did, briefly:**
- `ffe75213` — `list_option_contracts` never paginated Alpaca's
  `/v2/options/contracts` (100-row default, no auto-page). 98% of every
  chain arrived with no `open_interest`, so `_passes_liquidity` failed
  everything and the funnel emptied to zero on every symbol, SPY
  included. Now loops `next_page_token` (limit 10k, 20-page hard stop).
- `fcb00320` — wired `run_options_agents` into both graph branches behind
  `USE_OPTIONS_AGENT` + `instrument=="option"` + a fit strategy. Handles
  the double-write hazard (`decision_row_written`) and skips
  `risk_officer` downstream of a trade the guard already risk-cleared.
- `ef14352e` — docs only: recorded that the Railway Anthropic key 401s
  under `AGENTS_REQUIRE_REAL_LLM=1` (no mock fallback, no decision row on
  raise), and that measured `strategy_fit` scores (0.46-0.88 vs a 0.42
  floor) mean that gate is not what was holding options back.
- `a7b3a379` — options moved from once-a-day dedup to a cooldown
  (`OPTIONS_RESCAN_COOLDOWN_MINUTES`, default 45) and now take scan
  budget before equities.
- `c3565e0a` — the big one, 13 files. Three separate fixes bundled: (1)
  **confidence fabrication**: `executor._re_run_risk` used to substitute
  `conviction_level/5` (a 1-5 bet-size scaled down) for a missing
  `council_confidence` (a 0-1 "how likely to work") and score THAT
  against `min_council_confidence` — live case: AMZN drafted at
  confidence 0.54, refused at approval-click as "0.40 below floor 0.42".
  Fix: `RiskProposal.confidence` is now `float | None`;
  `min_council_confidence` (both `risk/engine.py` AND
  `options/risk.py::evaluate_option` — symmetrically) self-gates out
  (returns `None`, not a veto) when confidence was never recorded, rather
  than inventing a stand-in; `ApprovalProposalDto`/`_to_proposal_dto` now
  actually persist `councilConfidence` (0 of 30 approved equity rows
  carried it before this). (2) **options agents abstaining on
  everything**: the pre-pass promised IV rank / funnel counts / liquidity
  but rendered only strategy fit + patterns + one vol number; 6 of 6
  observed abstentions cited the missing IV rank. Fixed by rendering
  every promised block unconditionally via the shared
  `nodes/_specialist.render_features` (same renderer the equity Technical
  analyst uses) and adding an explicit prompt rule that `n/a` is a feed
  limit, not a finding. (3) **the options Refusal Ledger**: guard denials
  from `select_contract` onward now write an `agent_decisions` row via
  the new `ToolGuard._ledger_refusal` before denying — previously every
  such denial was returned to the model and persisted nowhere, so the
  options path (the one the contest requires) was structurally invisible
  to `ghost_service.build_veto_ledger`. Also a small mobile change:
  `apps/mobile/src/lib/api.ts`'s `assertSecure` now lets
  `http://localhost:*` through even in release builds, tagged
  `// LOCAL-REPRO-HACK` — flagging this, not fixing it (out of scope):
  it's a release-build HTTPS-guard relaxation with no test and no
  tracked follow-up to remove it.
- `f3fbe74e` — docs only (`docs/OPTIONS_PLAN.md`), no code.

**What I actually checked, against the 4 specific risks named in this
task's dispatch:**

1. **`TIGHTEN_STOP` sign convention.** `tools/guard.py`'s docstring
   (§"Sign convention", top of file) enforces: `TIGHTEN_STOP` needs a
   STRICTLY SMALLER `stop_loss_pct` (smaller magnitude = tighter, this
   repo's `RiskCaps.options_stop_loss_pct` convention);
   `RAISE_TAKE_PROFIT` needs a strictly LARGER `take_profit_pct`. Checked
   every place that makes the same claim to the model:
   `options/prompts.py::OPTIONS_ESCALATION` states it correctly ("Move
   the stop to a SMALLER stop_loss_pct... a SMALLER number is the
   TIGHTER stop"). `options/escalation.py`'s own module docstring states
   it correctly too. **But `options/tools/schemas.py`'s
   `ADJUST_OPTION_POSITION["description"]` — a THIRD place carrying the
   same claim, and one that reaches the model directly (`llm.py::
   complete_tools` passes the raw schema dict, description included,
   straight into the Messages API's `tools` param every round) — still
   had the plan docs' literal-but-backwards wording: "Stops and
   take-profits may only move UP (tighter/higher)."** This is a real
   conflicting-signal bug: the model reads a correct system prompt and an
   incorrect tool-schema description in the same turn. FIXED — reworded
   to state each field's real direction separately (stop: smaller;
   take-profit: larger). Added
   `test_adjust_schema_description_matches_stop_loss_sign_convention` in
   `apps/agents/tests/test_options_agents.py`; revert-checked per CLAUDE.md
   §4.1 (confirmed it fails against the pre-fix text, passes after).
   `agents.py`/`prompts.py` themselves needed no fix — they were already
   correct.

2. **`_ledger_refusal`'s reuse of `_proposal_dto`.** Traced every reader
   of a persisted `agent_decisions.proposal` I could find:
   `ghost_service.build_veto_ledger`/`build_veto_exemplar` (reads
   `estimatedNotional`/`isOption`/`occSymbol` — already tolerant of both
   camelCase and snake_case, matches `_proposal_dto`'s keys exactly, and
   `estimatedNotional` already bakes in the option multiplier so no
   double-count); `jobs/ghost_eval.py::evaluate_ghosts` (same fields,
   correctly marks the OCC contract not the underlying, and a
   `no_liquid_contract`/`size_rounds_to_zero` refusal — `ask=None`/`qty=0`
   — is gracefully SKIPPED via named skip reasons `entry_is_none`/
   `falsy_qty` rather than crashing or fabricating a ghost); `list_pending`
   (filters `risk_approved.is_(True)`, so a VETOED refusal row never
   surfaces there regardless of DTO shape); `position_manager.py`'s
   option branches and `funnel_service.py` (reads `reasoning.
   contract_funnel`, never `proposal`, so unaffected — and
   `_ledger_refusal` deliberately does not set `contract_funnel`, which
   its own docstring explains). Found no incompatibility. One thing
   worth naming for the next reader: the equity path has ALWAYS written a
   populated `proposal` on a VETOED row too, when the Drafter drafted
   something before risk vetoed it (`runtime._to_decision_entry`'s
   `audit_proposal = proposal_dto if proposal_dto is not None else
   _to_proposal_dto(final)` fallback) — so "a VETOED row can have a real
   proposal" was not entirely new, only new for the OPTIONS path
   specifically (which previously wrote NO row at all for a guard
   denial). No fix needed here.

3. **Escalation loop vs. the now-wired options council.** Traced the
   `council_run_id`/`decision_id` chain end to end for a real trade:
   `runtime.run_council` generates `council_run_id` -> `state[
   "council_run_id"]` -> `options/agents.py::run_options_agents` threads
   it into `GuardContext.council_run_id` -> `trade.py::open_option_trade`
   writes `DecisionEntry(id=str(ctx.council_run_id))` ->
   `PostgresDecisionLog.record()` keeps that as the row's real PK ->
   `options_council_node` reads it back as `trade["decision_id"]` and
   sets `out["decision_id"]` -> `runtime.py` takes it via
   `decision_row_written`. All the same string throughout. Confirmed
   `build_position_brief`'s field reads (`limitPrice`, `rationale`,
   `fill_avg_price`) match what `open_option_trade` actually persists,
   and that `entered_at` (`user_responded_at` from
   `ToolGuard._stamp_auto_approval`, falling back to `triggered_at`) is
   always populated close to real open time either way. Confirmed
   `escalation.py`'s reuse of `ctx.council_run_id = brief.decision_id`
   (an already-open position's row id, not a fresh pass id) never
   collides with `_ledger_refusal` (escalation never calls it —
   `_before_adjust_option_position` has no ledger-refusal path) or with
   `_persist_tool_log`'s `ctx.council_run_id` fallback (moot, since
   `adjust_option_position`'s `decision_id` arg is schema-required and
   always present). Also confirmed `manage_positions_for_user`'s query
   (`fill_qty IS NOT NULL AND closed_at IS NULL AND user_response=
   'approved'`) can never pick up a refusal row (`fill_qty` is always
   `None` on one), so `build_position_brief` never runs against a VETOED
   row's shape at all. No conflict found; no fix needed.

4. **Suite/lint state, actually run just now (not carried forward from
   any commit message):**
   - `python -m uv run pytest apps/ packages/ -q` (after `uv sync
     --all-packages`, clean tree, no concurrent edits):
     **1254 passed, 11 skipped, 0 failed** (221s). This INCLUDES my new
     test above. (A first background run overlapped with my own edits —
     test added mid-run, fix landed after collection — and produced a
     misleading transient "1 failed"; discard that number, it was a race
     in my own process, not a real suite state. The 1254/11/0 figure is
     from a clean re-run with the tree quiescent throughout.)
   - `ruff check apps/agents/trading_agents/options/
     apps/api/app/services/orders/position_manager.py
     apps/api/app/services/orders/reconciler_fleet.py`: **all clean**.
   - `ruff check apps/ packages/` (whole tree): **256 errors** — far more
     than CLAUDE.md §7's stated "9 pre-existing... as of 2026-08-29".
     Confirmed neither of my two changed files appears anywhere in that
     output, so none of the 256 are mine; the "9" baseline in CLAUDE.md
     is simply stale and should be treated as such by whoever reads it
     next (I did not chase down when it grew, that's outside this task).
   - `mypy apps/agents/trading_agents/options/
     apps/api/app/services/orders/position_manager.py
     apps/api/app/services/orders/reconciler_fleet.py`: **21 errors, 2
     files** (`position_manager.py`, `reconciler_fleet.py`) — the options
     package itself is mypy-clean (0 errors across 13 source files
     checked). All 21 are `[type-arg]` (bare `async_sessionmaker`/`dict`/
     `Task` generics) and `[no-untyped-def]`, and `git blame` traces every
     flagged line back to `cf7b01f45` (2026-06-12) — predates both of
     today's sessions by months, not something either introduced.

**Net result of this pass:** one real, fixed bug (the schema-description
sign-convention contradiction in item 1); everything else checked in
items 2-4 held up. Fixed with a test that fails on revert and passes
after (§4.1), committed separately from this doc update.

**Left open / not mine to fix:** the `api.ts` `LOCAL-REPRO-HACK` release-
build HTTPS bypass (flagged above, out of scope for this pass); the
repo-wide 256 ruff errors and the missing per-commit build-log entries
for the 4 Opus commits in this batch (also out of scope — CLAUDE.md asked
for one consolidated entry here, not retroactive reconstruction).

### 2026-08-31 — `5707d690` fix(agents): rebalance analyst score calibration + run analysts concurrently

`ID:MODEL2OFF`. Diagnosed the gap the orchestrator's own triage notes
recorded in `docs/OPTIONS_PLAN.md` ("agent 5 is instrumenting a full pass
to find the exact stage") and `docs/PLAN_NEXT.md` §0.45: `strategy_fit`
scores SPY/QQQ/NVDA/AAPL 0.65-0.88 against a 0.42 floor, while the LLM
analysts (fundamental/macro/technical) independently score the SAME names
28-42/100 against `min_specialist_avg_score`'s 40-45 floor — so every
equity pass HOLDs downstream of a perfectly healthy deterministic gate.

**Could NOT verify live.** `apps/api/.env` (and the file it was copied
from, per this task's own instructions) both carry `ANTHROPIC_API_KEY=`
empty and `AGENTS_REQUIRE_REAL_LLM=0` — no real Sonnet/Haiku analyst call
is reachable from this checkout, and the shell environment has no key
either (`os.environ.get("ANTHROPIC_API_KEY")` → empty). Everything below
is diagnosed by reading the actual prompt templates in full and by running
a real (not live-market, but real code) experiment against
`strategies.fit.best_strategy` — never by re-observing the reported 28-42
scores directly, which I could not reproduce.

**Root cause**: all three analyst prompts
(`prompts/{fundamental,macro,technical}_analyst.py`) enumerated multiple
explicit "score DOWN when X" heuristics (weak `quality_score`, VIX > 30,
rising yields into a rate-sensitive name, a strong dollar, >15% below the
200DMA, RSI > 75) and, across all three prompts COMBINED, exactly ONE
explicit "score UP" trigger (macro's "sector RS positive AND regime=bull").
Paired with "Honesty over enthusiasm" / "Be honest, if X is weak say so" /
"lean neutral" language repeated in all three, the rubric gave a model many
named reasons to mark down and almost none to mark up — an asymmetric
rubric that primes the output distribution low independent of the actual
evidence. This compounds a second, structural mismatch: `strategy_fit`
takes the BEST of 5 independently-lenient strategies (generous ramps —
e.g. `sma_crossover`'s `price_vs_20dma` ramp maxes at only +3% above the
20DMA; missing data defaults to NEUTRAL=0.5, never penalized), while
`min_specialist_avg_score` MEANS three independently-skeptical single-shot
judgments. A max-of-lenient reads higher than a mean-of-skeptical for
IDENTICAL evidence even under a perfectly symmetric prompt — the asymmetric
enumeration just made an already-structural gap categorically worse.
Reproduced directly (real code, offline, not live): a deliberately
UNREMARKABLE "boring uptrend" feature dict (2%/3% above the 20/50-DMA, RSI
58, Sharpe 0.3, realized vol 18%, average volume — the kind of tape the OLD
prompts' own "lean neutral"/"don't reach for extremes" language would
calibrate a human-like reader toward ~50) scores **0.854** via
`best_strategy` (`sma_crossover`), because `trend_regime_aligned` alone
maxes at 1.0 (weight 0.35) purely from `trend_regime == "uptrend"` being
true — full component breakdown in the commit's test-file docstring.

**Fix — prompt wording only, no scoring-formula or threshold change**
(honored the task's explicit constraint: did not touch `MIN_FIT_TO_TRADE`
or any `RiskCaps`). Rewrote all three analyst prompts to: (1) state a
symmetric 0-100 calibration anchor scale naming **65-84 as the ORDINARY
"genuinely good, tradeable" range**, not a stretch reserved for extremes;
(2) add at least one concrete, domain-specific "score UP" trigger to each,
matching its existing "score down"/"flag" triggers one-for-one; (3)
decouple confidence from score explicitly ("confidence is a SEPARATE axis
... low confidence is not a reason to pull the score toward 50") so
thin-evidence handling no longer drags the SCORE down, only the reported
confidence; (4) reframe "honest" as "accurate in either direction,"
removing "Honesty over enthusiasm" and "lean neutral" as standalone
directives.

**Also confirmed and fixed the task's second check**: the 3 equity
analysts ran SEQUENTIALLY, not in parallel, in **both** `graph.py` code
paths — a plain `for` loop in `_run_linear`, and a
technical→fundamental→macro chain of three separate LangGraph nodes in
`_build_langgraph` (whose own comment said "Phase 2 swaps the serial
analyst path for parallel fan-out via a join node"; `PLAN_OPTIONS_AGENTS.md`
§8 independently flagged this exact question as a "free win to check
first" and left it unresolved — "the LangGraph branch may not be"
sequential; it was). Fixed via one `_run_analysts_parallel` helper
(`asyncio.gather`, mirroring `options/agents.py::run_bull_and_bear`
exactly, including an elapsed-time-based test). Deliberately implemented
as ONE combined step in each path rather than three fanned-out LangGraph
nodes: all three analyst node functions read-modify-write the shared
`degraded_nodes` state key (`nodes/_specialist.py`), and LangGraph rejects
more than one writer to the same channel key per super-step without a
reducer. `_merge_analyst_results` reconstructs the combined
`degraded_nodes` list by hand instead of adding a reducer, which would
also have required touching `router.py`/`drafter.py` (the other two
read-modify-write callers of that key) — outside this change's scope.

Verified: `.venv/Scripts/python.exe -m pytest apps/agents apps/api packages/ -q`
— my own baseline (re-derived fresh, not trusted from any prior note):
**1242 passed, 11 skipped** (this matches the number already embedded in
`docs/OPTIONS_PLAN.md`'s pasted transcript, a useful cross-check that the
worktree state matched what that transcript described). After this change:
**1248 passed, 11 skipped** (+6 new tests: `test_analysts_run_concurrently`,
`test_analysts_parallel_merges_degraded_nodes_without_duplication` in
`test_council_mock.py`, and the 4 tests in the new
`test_analyst_prompt_calibration.py`), zero regressions. Revert-checked
both fixes per CLAUDE.md §4.1: (a) reverted `_run_analysts_parallel` to a
sequential loop — `test_analysts_run_concurrently` failed (0.625s for
3×0.2s calls, i.e. ~3x not ~1x) as expected, restored; (b) reverted
`fundamental_analyst.py` to its pre-fix wording — all 4 tests in
`test_analyst_prompt_calibration.py` failed as expected, restored. Ruff
clean on every changed file. mypy on `graph.py` shows 4 errors, but all 4
are pre-existing on HEAD (confirmed by temporarily restoring the unmodified
file and re-running mypy against it directly — same 4 errors at their
shifted line numbers); my new code introduced zero new mypy errors (fixed
one real one: `_ANALYSTS`'s callable type was `Awaitable[CouncilState]`,
which `asyncio.create_task` doesn't accept — narrowed to
`Coroutine[Any, Any, CouncilState]`, matching the type
`options/agents.py::run_bull_and_bear`'s identical pattern already gets
right, confirmed via `mypy options/agents.py` → zero issues). Smoke-tested
both graph code paths end-to-end in MOCK mode via
`python -m trading_agents --symbol NVDA` (LangGraph, default) and
`--symbol AAPL --no-langgraph` (linear fallback) and `--symbol SPY`
(LangGraph again, post type-fix) — all three produce a sane, fully-formed
BUY proposal DTO.

**Left open, explicitly**: the calibration fix is pinned only at the level
of "the prompt's own wording is symmetric and anchored, and the old
asymmetric phrases are gone" — it is NOT verified against a real LLM call,
because no usable `ANTHROPIC_API_KEY` existed anywhere in this environment.
**The next session with a working key should re-run the exact SPY/NVDA/AAPL
comparison `docs/PLAN_NEXT.md` §0.45 recorded** and confirm the specialist
average actually moves meaningfully off 28-42 — this fix is a strong,
evidence-based hypothesis about the mechanism, not a confirmed cure.
Separately noticed but explicitly OUT of this task's scope (CLAUDE.md
§4.7 — flagging, not fixing): `prompts/drafter.py` hardcodes *"If
specialists' average score < 45 → HOLD"* as a literal prompt string,
disconnected from `RiskCaps.min_specialist_avg_score` (45.0 conservative /
40.0 aggressive) — the exact "same number in two places" trap CLAUDE.md
§4.4 already names for `options_min_volume`. Under the aggressive profile
the Drafter's own internal HOLD rule is now stricter (45) than the risk
engine's actual floor (40) it's supposedly mirroring; worth a follow-up
task.

### 2026-08-31 — `d8cf7871` fix(mobile): the "server didn't respond" fix never reached two screens

`ID:MODEL2OFF`. Task: the user reported "server didn't respond" on a first
council run is STILL happening despite bcaf0693 (diagnosis) + d150471d
(the network-error-retry fix). Also asked to rule the dead Railway
`ANTHROPIC_API_KEY` (this file's own 0.4 entry below) in or out as the cause.

**Verified live (via a new test, not just reading the source):** an upstream
LLM auth failure does NOT hang. Wrote
`apps/api/tests/test_agent_run_llm_failure.py`, which injects a real
`anthropic.AuthenticationError` mid-council-pass (fake `AsyncAnthropic`,
patched `graph.strategy_fit_node` to force a pass rather than coupling to a
symbol's synthetic-hash outcome) through the ACTUAL HTTP surface
(`POST /agent/run/start` → `GET .../progress`). Result: the POST returns 202
in <2s regardless of the LLM outcome (the background-task pattern in
`agent_runs.py` — `AgentRunRegistry.start` fires the pass via
`asyncio.create_task`, never awaited by the handler — already guarantees
this), and the existing `except Exception` in `_drive()` already catches the
failure and records a real, legible error (`"Error code: 401 - invalid
x-api-key"`) onto the run, fast (the SDK doesn't retry a 401). **Revert-check:**
narrowing that `except` to `except RuntimeError` makes the new test fail
exactly as predicted (run stuck at `status="running"` forever) — the test
has teeth.

So the dead key, if it recurs, was never going to explain a hang by itself.
Chasing why the symptom still reproduces found the real cause: TWO mobile
screens the prior fix never touched, both independent of the LLM key:

1. `app/(tabs)/approvals.tsx` — the actual phone/narrow-web "Run" button —
   had its OWN hardcoded string ("Couldn't start the run - is the server
   reachable?"), never migrated. d150471d only edited the DESKTOP screen's
   module-private `runErrorMessage`; this screen never imported it.
2. `app/council/[runId].tsx` — the theater screen a successful Run click
   lands in — still had the literal PRE-FIX fallback string ("Couldn't reach
   the agent server.") with no retry button, shown whenever the progress
   poll itself blips or a failed run's `error` is empty. Its desktop twin
   (`Council.tsx`) already had a proper `DataStreamInterrupted`/`onRetry`
   treatment.

**Fixed:**
- Promoted `runErrorMessage` into `apps/mobile/src/lib/api.ts` (exported),
  rewired all three call sites (desktop `Picks.tsx`, phone `approvals.tsx`,
  theater `[runId].tsx`) to import the one implementation instead of
  hand-rolling a copy — structurally prevents this exact drift recurring.
- `[runId].tsx` now shows the run's real server-side error on a genuine
  failure vs. the shared transport message on a poll blip, plus a "Try
  again" button.
- Widened `retryOnNetworkError` from 1 retry/1s to 2 retries at 1s/2s,
  matching TanStack Query's own default query backoff (`queryClient.ts`) —
  a single 1s retry was never guaranteed to outlast a real Railway cold
  start/redeploy.
- `apps/api/app/main.py`: `lifespan()` was `await`ing `warm_symbol_cache()`
  directly — a live Alpaca fetch its own comment says takes ~6s — which
  blocks EVERY request (health checks included) on every fresh container
  boot, independent of the LLM key. Its contract was already best-effort;
  moved to a fire-and-forget `asyncio.create_task`, cancelled on shutdown.

**Verified:** full Python suite 1243 passed/11 skipped (was 1242/11 — +1 is
the new test, nothing else moved); `pnpm --filter mobile exec jest --silent`
9 suites/73 passed; `tsc --noEmit` clean; `ruff check` clean on every
touched/new file. Both new-behavior tests (Python + the widened-retry JS
test) revert-checked and confirmed to fail on the old behavior.

**Left open / could NOT verify:** the deployed `ANTHROPIC_API_KEY`'s CURRENT
live status (no Railway access, no usable local key). Whether the user is on
the phone build or the narrow desktop-web view — fixed both since I
couldn't tell which. The live Railway cold-start duration — no way to
measure it directly; the `warm_symbol_cache` fix is justified by the code's
own "~6s" comment + the structural fact that lifespan blocks all traffic
until it returns, not by a live before/after measurement. Separately found
and deliberately NOT touched (flagged as a follow-up task, out of scope for
the council-run path this was scoped to): `app/positions.tsx`,
`app/auth/login.tsx`, `app/auth/verify.tsx`, and
`src/desktop/screens/Positions.tsx` each carry their own independent
"Couldn't reach the agent server"-style hardcoded string.

**Also found and worked around, worth flagging for whoever reads this next:**
this session's file-edit tool silently corrupted a directly-typed curly
apostrophe into an invalid UTF-8 byte (decoded back as U+FFFD) in
`api.test.ts` on this Windows box — a live, reproduced instance of this
file's own "PS mojibake hazard" dev-env-quirk note, just via the edit tool
rather than PowerShell itself. Worked around by keeping new string literals
plain-ASCII and doing a couple of edits via a Python read/replace/write
(`encoding='utf-8'`) instead of the edit tool wherever the surrounding text
already contained a non-ASCII character (e.g. main.py's pre-existing em-dash
in a log message, preserved byte-for-byte by extracting and reusing it
programmatically rather than retyping it). Worth the same caution in any
future session on this box.

### 2026-08-31 — `dcf58ca4` fix(options): adjust_option_position was missing the paper-only/market-hours gate

`ID:MODEL2OFF`. Found while reviewing the (not-yet-merged) escalation-loop
workstream: `_before_adjust_option_position` never checked
`AUTO_TRADE_ENABLED`/paper-only/market-hours before allowing `EXIT_NOW` or
`SCALE_IN` through — both reach `packages/broker` via `trade.py`'s
`_exit_now`/`_scale_in` and place a real order once the guard says allow.
`_before_open_option_trade` has always checked all three as its first
three steps; `_before_adjust_option_position` had none, for any of its
five actions — directly contradicting `PLAN_OPTIONS_AGENTS.md` §4's own
words ("checked in `before`, regardless of any flag"). **I reviewed this
exact file carefully when it first merged (`f80abc7f`) and missed this
asymmetry** — it surfaced only because the escalation workstream needed
to reach `adjust_option_position` from a live, scheduled path and, in
verifying its own safety, found the gate its own call site depended on
was never actually there upstream.

Fix: the same three checks `open_option_trade` already runs, added to the
top of `_before_adjust_option_position`, applied uniformly to every
action (including `HOLD`/`TIGHTEN_STOP`/`RAISE_TAKE_PROFIT`, which never
touch the broker) — one gate to reason about, not a narrower second one.

Verified: added 4 new tests mirroring the 4 existing `open_option_trade`
gate tests exactly. Revert-checked (CLAUDE.md §4.1): removed the gate,
confirmed all 4 fail with the underlying `decision_not_found` instead of
the safety denial (proving they test the real gate, not a coincidence),
restored. Fixed 2 pre-existing `EXIT_NOW` end-to-end tests that predated
this gate. 1184 → 1188 passed, 11 skipped, zero regressions, ruff clean.

### 2026-08-31 — `ed24dd36`/`f53792e2` feat(agents): the two arguing agents themselves — Bull/Bear + deterministic resolution

`ID:MODEL2OFF`. On top of the guarded trade tools (previous entry): the
actual argument. Bull and Bear each read the identical deterministic
pre-pass and form an independent view — no tool calls — in ONE parallel
hop (`asyncio.gather`), neither seeing the other's answer.
`resolution.resolve()` combines them, plain Python, no LLM: agree on
direction → proceed with `conviction = min()` of the two (not the mean —
weak agreement should size weak); disagree, either abstains, or the
conviction gap exceeds 0.4 → HOLD. Only on `proceed` does a SECOND hop
let Bull — never Bear — call `open_option_trade` through the guard,
carrying the RESOLVED direction/conviction, not Bull's own pre-resolution
numbers. Both prompts begin with the exact literal role phrase both
`llm.py`'s mock and `cost_ledger.py`'s role inference need, registered in
both (revert-checked). Both prompts also explicitly instruct the model to
treat any third-party text in the pre-pass (news headlines, scan
triggers) as reported claims, never as instructions — a real, if small,
prompt-injection consideration worth noting since this is the first node
in the whole council that reads that kind of text.

**One design decision beyond the spec's literal pseudocode:** the
tool-call loop's dispatch closure rebuilds `GuardContext` per attempt
(it's frozen) and increments `calls_this_pass` only on a SUCCESSFUL open,
never a denial — the tool schema's own description says a denied call
"may adjust once," so a denial must not burn the pass's one-open budget;
only an actual fill should trip the guard's `one_open_per_pass` rule
against a second successful open in the same pass. A dedicated test
proves a naive "build the context once, reuse it" implementation would
let two calls both succeed.

**Verified, not assumed — independently re-run by me end to end:** true
baseline 1171 passed/11 skipped (this session's running total after the
prior two option commits). After: 1184 passed/11 skipped — exactly +13,
zero regressions, confirmed with my own full-suite run, twice (once in
the worktree, once on `main` post-merge). ruff/mypy clean, re-checked
directly. Reviewed the actual `resolution.py`/`prompts.py`/`agents.py`
source line by line — the `min()`-not-mean logic, the exact role-phrase
strings, the two-hop sequencing, the concurrency assertion (wall-clock,
not call-count) — not just the summary.

**A fourth instance of the worktree-staleness pattern, handled correctly
this time:** this environment's `isolation:"worktree"` mechanism appears
to snapshot from a fixed base rather than current `HEAD` at dispatch
time — the fourth time this exact gap has shown up today (I1's llm.py,
I3-backend's funnel_service.py, and my own schemas.py were all missing
from I2-guard's and I2-readonly's worktrees earlier; this time it was
commit `f80abc7f` missing from THIS worktree). This agent, briefed on the
pattern, checked `git log` first per instruction, found the gap, verified
a clean fast-forward via `git merge-base --is-ancestor`, and merged real
`main` into its own branch before writing any code — no reconstruction-
from-guesswork needed this time, no reconciliation work left for me.
**Worth remembering for any future worktree-isolated dispatch here:
always check `git log` against a known recent commit first, and merge
real `main` in if it's missing, rather than assuming the isolated
worktree reflects current `HEAD`.**

**Not built here, on purpose:** `adjust_option_position`'s prompts/wiring
and the escalation loop (`IMPL_OPTIONS_AGENTS.md` §5) that would call
it — a separate, not-yet-started workstream. `AUTO_TRADE_ENABLED`/
`USE_OPTIONS_AGENT` remain off.

### 2026-08-31 — `f80abc7f`/`51d1457f` feat(agents): guarded options trade tools — open_option_trade / adjust_option_position

`ID:MODEL2OFF`. **This is the headline deliverable** — I2 of `docs/IMPL_
OPTIONS_AGENTS.md`, two arguing options agents' actual trade tools (the
Bull/Bear agent nodes themselves, `options/agents.py`/`prompts.py`/
`resolution.py`, are next — not yet built). Two parallel subagents
(I2-guard: `tools/guard.py`+`trade.py`; I2-readonly: `tools/readonly.py`+
`registry.py`+6 read-only schemas) plus real reconciliation work, since
neither workstream ever actually saw the other's code (see below).

**The guard runs the full 12-step stack before `open_option_trade`
reaches the broker** — market hours, symbol/strategy/direction/thesis
validation, `select_contract`, `options_position_size`, then the SAME
`engine.risk.evaluate()` every equity/options proposal already runs
through. **The ratchet invariant is enforced with a real correctness
catch**: both spec docs say `TIGHTEN_STOP` needs the value to "increase",
which is backwards for `stop_loss_pct` given this repo's own convention
(50.0→40.0 = "cut losers early", i.e. smaller = tighter) — implemented
"strictly smaller than current" instead, per CLAUDE.md §4.2. Flagged
prominently for whoever builds the Bull/Bear prompts next: they must
tell the model this same direction or every `TIGHTEN_STOP` will be
denied. Six read-only tools all reuse existing computation (funnel
service, chain-fetch, ratchet math) rather than re-deriving it;
`get_iv_rank` is honestly a process-local in-memory rank, never a
fabricated vendor number, returning `null` below 5 samples.

**Root cause of the reconciliation work, confirmed directly:** I wrote
the frozen `schemas.py` contract earlier but never committed it — sat
untracked the whole session. Neither subagent's isolated worktree had it
(`git worktree add` only checks out committed content), so both
independently reconstructed it byte-identical from the schema content
quoted in their own dispatch prompts — no functional harm there, but it
meant the two workstreams built against each other's INTENDED shape,
never each other's actual code, and diverged in three real ways:

1. `dispatch_tool_call` calls every handler as `(args, ctx, guard_payload)`
   — I2-guard's own deliberate addition beyond the spec's literal 2-arg
   pseudocode. I2-readonly's six handlers took only 2 args, built
   against that literal spec. Fixed: `guard_payload: dict | None = None`
   on all six — optional so the real 3-arg call and every existing 2-arg
   test both keep working.
2. `ToolGuard.before()` had no branch for the six read-only tool names —
   would have denied all of them as `unknown_tool`, permanently, since
   each workstream's own tests called handlers directly and never
   exercised the real dispatch path together. Added a branch deriving
   the allowed names from `schemas.py`'s own `READ_ONLY_TOOLS` (CLAUDE.md
   §4.4 — not a second hand-written list).
3. `registry.py` combined trivially (both docstrings literally
   anticipated this: "merge is a non-event"). `test_options_agents.py`
   needed real work — same filename, two non-overlapping files; combined
   imports, and rewrote 4 tests that assumed the registry would only
   ever hold 6 read-only entries. Added a new test proving the (2) fix
   actually works through the real dispatch + real registry, not just
   that the code compiles.

**Verified, not assumed:** both subagents independently reproduced the
true baseline (969 passed/11 skipped) in their own isolated runs. After
combining + fixing all three gaps: 1171 passed/11 skipped on the full
suite (+102 this commit, zero regressions across this session's entire
run of merges today). ruff/mypy clean on the whole `options/` package
and both test files. Reviewed the actual guard/trade/readonly code line
by line (not just the reports) — the 12-step order, the ratchet
invariant, the whole-column-overwrite prevention, the tenant-scoping
defense-in-depth.

**Left open, disclosed, not touched:** a per-position stop/TP override IS
persisted by `adjust_option_position` but `position_manager.py`'s
deterministic sweep still reads only the global `RiskCaps` values —
wiring that is separate scope. `AUTO_TRADE_ENABLED`/`USE_OPTIONS_AGENT`
ship off — this lands the guarded tools and their tests; it does not
turn on autonomous trading, which stays the account owner's call.

**The general lesson, a fourth data point on top of this session's other
three:** "each subagent's own tests are green" is not proof two
independently-built halves of one system actually work TOGETHER —
particularly when (as here) a missing commit meant they never even ran
against each other's real code during development, only against each
one's own guess at the other's shape.

### 2026-08-31 — `701a7580` fix(biography): tolerate snake_case estimatedNotional on old vetoed rows

`ID:MODEL2OFF`. Direct follow-up to `927dc415` (below), which explicitly
flagged this exact file as an out-of-scope instance of the same bug:
`biography_service.py::build_biography` reads `proposal.get("estimatedNotional")`
with no snake_case fallback, so the 6 decisions vetoed before `927dc415`
landed show a blank notional on their trade-biography timeline, even
though the same rows now show the correct dollar figure everywhere else.

Grepped the rest of the codebase for the same pattern before assuming
this was the only remaining instance: `position_manager.py`,
`positions_service.py`, and `notifications.py` all read similarly-shaped
multi-word camelCase fields (`timeStopDays`, `stopLoss`, `targetPrice`,
`rMultiple`) off a `proposal` dict, but every one of those operates on an
ALREADY-APPROVED proposal (an open position, or a pending-approval push)
— which is only ever the camelCase DTO shape by construction, since
`risk_approved=True` is required to reach any of those code paths at
all, and that's exactly the gate that already produces the camelCase
shape. Only `biography_service.py` reads the raw persisted column for a
decision regardless of outcome, including vetoed ones — confirmed this
is genuinely the one remaining instance, not assumed.

Extracted `_proposal_estimated_notional()` as a small pure helper rather
than fixing the read inline, matching this file's own established
pattern (`_analyst_summaries`/`_proposed_or_held_summary` are both pure
helpers pulled out of `build_biography` for the identical reason —
that function needs a live DB session and three joined queries to
exercise directly, disproportionately heavy for testing one field's
fallback in isolation).

Verified: 10/10 in the touched test file, 1069 passed/11 skipped on the
full suite (1065 + 4 new, zero regressions). Revert-checked per CLAUDE.md
§4.1 — removed the fallback, confirmed the new snake_case test fails
with the exact `assert None == 4922.08`, restored. ruff clean on both
touched files; the file's one pre-existing mypy finding (an unrelated
Order/OrderFill annotation, line 190) confirmed nowhere near either
touched region by reading the actual diff, not just asserted.

### 2026-08-31 — `927dc415` fix(ledger): vetoed proposals persisted snake_case, so estimatedNotional never matched

`ID:MODEL2OFF`. Implements `docs/IMPL_REFUSAL_LEDGER.md`. Ran the §0
diagnostic first, exactly as instructed, before touching
`build_veto_ledger`:

```sql
SELECT risk_veto_rule, final_action, proposal->>'estimatedNotional' AS notional,
       user_response, triggered_at
  FROM agent_decisions
 WHERE risk_approved IS FALSE AND risk_veto_rule IS NOT NULL
 ORDER BY triggered_at DESC LIMIT 20;
```

Result: 6 rows, all `single_name_concentration`, all `notional: null`.
Per the doc's own decision table that is **Case 1 — the write side never
populated it**, not the aggregation. Confirmed by dumping the raw
`proposal` JSONB for those rows directly: all 6 carry
`estimated_notional` (snake_case), present and correct;
`estimatedNotional` (camelCase) absent entirely.

**Root cause:** `runtime.run_council` only builds the camelCase DTO
(`_to_proposal_dto`) when `risk_approved` is True. For a vetoed proposal,
`PostgresDecisionLog.record()`'s fallback persists `raw_state["proposal"]`
untouched — the Drafter's snake_case dict — which no camelCase reader
(`ghost_eval`, the veto ledger) can ever match. The identical bug
independently broke `ghost_eval._entry_price()` too: **103 of 104**
candidate rows were being skipped with `entry_is_none` before this fix.

**Fix:**
1. `runtime._to_decision_entry` normalizes the persisted proposal through
   `_to_proposal_dto` regardless of `risk_approved` — does **not** touch
   the separate, still-gated `proposal_dto` that drives the actionable
   `"proposal"` key / push notification, so a veto still never looks
   approvable.
2. `ghost_eval._entry_price` and `ghost_service.build_veto_ledger`'s
   notional sum now accept either key casing, rescuing the 6
   already-written rows with no DB backfill.
3. New `GET /api/v1/risk/vetoes/{rule}/exemplar` — the "story trade":
   largest `abs(ghost_pnl)` among FINALIZED ghosts for a rule, never
   most recent. 404s cleanly when nothing has finalized yet.

**Verified live against real Postgres, not just tests:**
- `build_veto_ledger`'s `total_blocked_notional`: **$0.00 → $29,107.74**.
- `evaluate_ghosts()`: `{created: 6, updated: 7, finalized: 0,
  skip_reasons: {entry_is_none: 97}}`. 0 ghosts reached `final` —
  correctly so; these vetoes are from 2026-08-28, `short` horizon needs
  5 elapsed trading days, so `final` won't hit until ~Sept 4.
  `prevented_loss_usd` correctly still renders `None`, not `$0`.
- **Caveat found and disclosed, not hidden:** `ghost_outcomes.
  price_source = 'synthetic'` on all 7 rows — both this worktree's and
  the original `apps/api/.env` have `ALPACA_API_KEY`/`SECRET` present as
  keys but empty as values, so `engine.prices.select` silently falls
  back to synthetic. The mechanism is verified correct; today's specific
  dollar figures are not real market data yet.

969 → 994 passed (+25), 11 skipped, zero regressions — re-confirmed
independently by me, not just trusting the report. All 8 behaviors from
the doc's §6 revert-check matrix actually broken and confirmed to fail,
then restored, per CLAUDE.md §4.1. ruff/mypy checked against baseline
via `git checkout HEAD -- <path>` (not `git stash` — see below).

**Two honesty-rule decisions made explicitly, not silently:**
- **§3 (old-account label):** left undone. `agent_decisions` has no
  account-id column and all 132 rows share one `user_id` — the account
  swap wasn't a user swap, so the boundary isn't mechanically queryable,
  only inferable by date, and the one plausible cutover event
  (`broker_connections.updated_at`) more likely reflects
  `auto_approve_consent` shipping that day than an account swap.
  Recommend labeling by date once a human confirms the real cutover.
- **§5 (confidence bars):** chose "not yet calibrated" over wiring
  reflection. `daily_cron.main()` already calls `reflection_agent_run`
  unconditionally (that part of the doc is stale) — the real gap is
  `COUNCIL_SCHEDULER_ENABLED=0` locally, so nothing invokes it
  unattended. Did not flip that flag (real LLM-cost + auto-trading
  consequences, outside scope) or run reflection manually (the only 6
  gradeable decisions are all pre-contest). `/strategies/performance`
  already exposes `lastReflectionAt`, so the frontend can render "not
  yet calibrated" with zero backend change.

**Found, flagged, not fixed (out of scope):** `biography_service.py` has
the identical camelCase-only read gap for a vetoed row's displayed
notional — spawned as a separate follow-up task.

**Environment note — third independent confirmation of the git-stash
risk in this batch:** this worktree's `git stash` collided with a
DIFFERENT concurrently-running agent (the funnel-UI workstream) — the
recovered stash content was confirmed identical to the funnel-UI work
already reviewed and merged separately (`fab53c59`/`3640a64a`), and the
now-fully-redundant stash entry was dropped after confirming that. `git
stash` is not worktree-scoped in this environment; treat that as
established, not a one-off.

**Merge note, mine:** landed alongside the funnel view and both I5
halves merged earlier today — `apps/api/app/routers/insights.py` had a
real textual conflict (both this commit's `veto_exemplar` endpoint and
the funnel endpoint insert at the same point in the file); combined both,
kept every class/function from each side, confirmed with `ast.parse` and
a full suite re-run after resolving.

### 2026-08-31 — `0eaaad8c`/`10374339` feat(mobile): demo banner + disabled buttons — and the gap between the two I5 halves

`ID:MODEL2OFF`. Client half of `docs/IMPL_DEMO_SESSION.md` (`0eaaad8c`):
`?demo=<token>` exchanged through the exact same `authStore.signIn()` path a
magic-link login uses, stripped from the URL immediately via
`history.replaceState`; a persistent `DemoSessionBanner`; 6 mutating buttons
(native + desktop trees for each of Approve/Decline, Close/Cancel,
Revoke/Disconnect) DISABLED with a stated reason, never hidden. jest 3
suites/28 tests → 9 suites/68 on `main` post-merge, tsc clean, both
independently re-confirmed by me after installing the worktree's own
`node_modules`.

**The one thing worth remembering from this pair of commits:** reviewing
I5-frontend against the ALREADY-MERGED I5-backend (`70bcbd20`) surfaced a
real integration bug neither agent could have caught alone — the frontend's
`useIsDemoSession()` reads `user.authMethod === 'demo'`, documented as
"mirrored from the server ... through the exchange response," but the real,
merged `IssuedTokensResponse` schema never had an `auth_method` field and
the `/auth/demo` handler never set one. Every demo session would have
gotten a completely silent, working-as-far-as-anyone-could-tell UI with the
banner simply never appearing and the buttons never disabling — while the
actual server-side security enforcement (`is_dev_bypass` → `require_real_auth`
→ 401) stayed fully intact regardless, since that's a separate mechanism.
**Neither git nor tsc would ever catch this**: two different files, no
merge conflict; a wire-shape mismatch, not a type error, since a TS
interface describing an HTTP response doesn't get validated against what
the server actually sends. Only tracing the real data flow between two
independently-built halves of the same spec surfaced it.

Fixed separately, `10374339`, right before the frontend merge:
`IssuedTokensResponse` gains `auth_method: str | None = None` (purely
additive — every other issuer already omits it), the `/auth/demo` handler
sets `auth_method="demo"`. Added the assertion to `_demo_access_token()`,
the one helper all 12 of `test_demo_session.py`'s exchange-flow tests
already share, so the coverage is broad rather than one isolated test.
Revert-checked (CLAUDE.md §4.1): removed the fix, ran the suite — **12
tests failed** with the exact `assert None == 'demo'` — confirming the new
assertion genuinely catches the gap; restored, full suite 1040/11 unchanged
(purely additive field, zero regressions).

**The pattern, generalized, for whoever runs the next batch of parallel
subagents against a spec split across a backend and a frontend workstream:**
each agent will faithfully implement ITS OWN reading of the shared contract
in isolation, and both halves can be individually correct, individually
well-tested, and still not actually work together. A clean merge (no
conflict markers) and green types (tsc/mypy) are necessary, not sufficient —
after merging paired backend/frontend work, trace at least one real request
through both sides by hand before calling it verified.

### 2026-08-31 — `70bcbd20` feat(auth): read-only demo-session link for judges (IMPL_DEMO_SESSION.md)

`ID:MODEL2OFF`. A judge-facing link that shows the REAL trading account
with REAL history and changes nothing — built on the existing security
boundary (`require_real_auth` already refuses any `AuthedUser` with
`is_dev_bypass=True`, and every one of the 6 money-moving routes already
calls it) rather than a new authorization mechanism. Two `typ`s reusing
`verify()`'s existing generic `expected_typ` check (not a new mechanism):
a long-lived `typ="demo"` link token minted **offline** by
`scripts/mint_demo_link.py`, and a short-lived `typ="access"` token (same
15-min TTL as any other) carrying an `extra={"demo": true}` marker — both
`mint_access`'s `extra` param and `Claims.extra` already existed pre-this-
commit, reused rather than invented.

Closed the 3 routes that accepted the OLD dev-bypass and would otherwise
have been reachable by a demo session, in the order the doc's own §6
requires (ship this before closing those and judges reach them): `/agent/run`
+ `/run/start` (unbounded LLM spend/call), `/watchlist` add+remove (changes
what the agent trades), `/review/{id}/grade` (pollutes calibration data) —
each swapped `get_current_user` for `require_real_auth`, zero new
authorization code.

**Verified, not assumed — independently re-checked by me, not just the
subagent's report:** 969→990 passed/11 skipped in isolation, →**1040 on
`main` post-merge** (+21, zero regressions). Went through all 20 ruff
findings on the 8 touched files LINE BY LINE (not by trusting a stash-based
"zero net-new" claim, given the confirmed git-stash cross-contamination risk
in this environment's worktrees — see the funnel-view entry above): the 12
`Depends()`-in-default findings are the same FastAPI idiom already present
at every `Depends(...)` call site in this repo, tripped identically whether
the dependency inside is `get_current_user` or `require_real_auth`; the 4
`datetime.UTC`-alias findings are all inside untouched pre-existing
`mint()`/`verify()`; the 3 unused-`noqa` findings in `rate_limit.py` are the
same pattern on 3 untouched limiter functions — the new `check_demo_rate()`
this commit adds carries no `noqa` at all. Confirmed zero net-new findings.

Revert-checked (CLAUDE.md §4.1): `test_demo_session_refused_by_every_
mutating_route` and `test_every_mutating_route_uses_require_real_auth` (the
latter introspects the actual FastAPI dependant tree — confirmed it names
the real offending route when reverted, not a stub that always passes) both
independently confirmed to fail when broken, restored. Verified live,
end-to-end, beyond unit tests: minted a real link, exchanged it via a real
`TestClient`, confirmed `GET /account` → 200 with real data on that session
while `POST /watchlist` and `POST /circuit-breaker/acknowledge` both → 401
on the exact same session.

**Left open, explicitly out of scope for this commit:** the mobile client's
`?demo=` query-param handling, session storage, and the read-only banner /
disabled-button UI — a separate parallel workstream (I5-frontend) covers
this, reviewed separately. `DEMO_SESSION_ENABLED=1` needs setting on Railway
by the operator before this is live anywhere.

### 2026-08-31 — `81fa9b9c` feat(agents): llm.py tool-use support — the I2 foundation, now landed

`ID:MODEL2OFF`. Implements `docs/IMPL_LLM_TOOLS.md` in full — **this is the
gate**: `docs/IMPL_OPTIONS_AGENTS.md` (I2, two arguing options agents with
guarded real trade tools) cannot start until this lands, per its own header.
It has now landed and is fully verified; I2 is next.

Three commits, each gated on its own green suite: (1) a block-walk helper
replacing the old `msg.content[0].text` (which assumed the FIRST content
block is always text — true for every existing council node, false for any
tool-using response), with `LLMResponse` gaining `tool_calls`/`stop_reason`,
both defaulted so no existing construction site breaks; (2) `complete_tools()`
as a genuinely separate sibling method — NOT an overload of `complete()`,
which hardcodes a single user turn and is on the hot path of all 5 council
nodes; (3) `run_tool_loop()` in new `llm_loop.py`, where the round budget
lives in the CALLER (`max_rounds=3` default), and the guarded `dispatch`
callback is wrapped in try/except converting any raise to `is_error` — belt
and suspenders on top of the contract already asked of callers.

**Verified, not assumed — independently re-run by me, not just trusting the
subagent's report:** true baseline 969 passed/11 skipped (`git stash`,
matches the doc's own recorded number); after, **1005 passed in the
worktree, 1019 on `main` post-merge** (+36 new tests on top of the 14 from
I3-backend already merged), zero regressions, re-confirmed with my own full
suite run both times. Ruff clean on all 3 touched files, re-checked
directly. 6 revert-checks actually executed, each with the specific failure
observed (not just "would fail"): the block-walk itself, `test_mock_never_
emits_tool_use` (doc's own "#1 most important" — a mock returning a canned
`ToolCall` failed all 4 parametrizations), `test_loop_bounded_at_max_rounds`
(the other "most important"), dispatch-error-becomes-`is_error`, and both
halves of the dual mock/cost-ledger role-registration branch (`_mock_response`
+ `infer_role_from_system_prompt`) — disabling either broke exactly one row,
confirming correct isolation.

`cost_ledger.py` ends with **zero net diff** — no new agent role was
registered in this commit (correctly out of scope; I2 will add Bull/Bear),
so the two "new role" tests are parametrized over the 7 roles that exist
today, proving the dual-branch pattern holds as the template a real
addition must not break.

Two disclosed deviations from the doc's literal pseudocode, both
improvements not behavior changes: `dispatch()` wrapped in try/except
inside `run_tool_loop` even though the doc's own sketch has none (its own
§6 trap #4 says "letting dispatch raise" is a mistake — this makes that
belt-and-suspenders rather than relying solely on the caller's contract);
`assert resp is not None` before the final return, needed for mypy strict,
behavior-identical for every real caller.

**Left open, noted not fixed (CLAUDE.md §4.2 — code wins, doc is wrong):**
the doc's §0 calls `LLMResponse` "frozen"; it is a plain `@dataclass`, not
`frozen=True`, and always has been. Out of scope to change here.

**Merge note:** touches only `llm.py`/`llm_loop.py`/its own test file — zero
overlap with the I3/I4 frontend work merged earlier today, clean merge, no
conflicts.

### 2026-08-31 — `fab53c59`/`3640a64a` feat: the Contract Funnel view, backend + frontend

`ID:MODEL2OFF`. Implements `docs/IMPL_CONTRACT_FUNNEL_UI.md` in full — the
"highest demo-value-per-hour" item per its own header, since every options
council pass has been writing a `contract_funnel` block into
`agent_decisions.reasoning` since 2026-08-30 and nothing read it back until
now. Two subagent workstreams, reviewed and merged separately:

**Backend (`3640a64a`):** new `GET /api/v1/insights/funnel`. Aggregation is
pure Python (`build_funnel_report_from_rows`, testable with no DB), scoped
by the same `ghost_service._tenant_filters` the veto ledger already uses.
Absent-vs-zero handled correctly (a stage a given row never emitted
contributes nothing to that stage's sum, rather than reading as a 100%
rejection); `dropped` clamped at 0; `rejection_stage` derived from the
counts themselves, not the stored `rejection_reason` string, so it's right
even for `select_contract`'s `"no_candidates"` case. 969->983 passed, 11
skipped, zero regressions; all 6 of the spec's revert-checks independently
re-verified.

**Frontend (`fab53c59`):** `ContractFunnel.tsx` — stepped horizontal bars,
not a Sankey. Two mount points: Insights (window aggregate) and Decisions
(the single selected decision's own run — chosen over `PickDetail` because
that screen only ever shows a pending approval, and a HOLD never proposes
one, which would make the spec's own "the HOLD case is the important one"
unreachable there). `minWidth:2px` floor, real empty state (never a funnel
of zeroes), HOLD banner as a lookup table keyed on the exact
`_STAGE_REJECTION_REASONS` strings.

**Both workstreams independently touched `packages/shared-types/src/index.ts`
and (frontend) `Insights.tsx`, in parallel worktrees off the same 604a2001
baseline — landing them together needed two hand-fixes, both mine, disclosed
in `fab53c59`'s own commit body:**
1. Both branches declared `FunnelStageDto`/`FunnelRunDto`/`FunnelAggregateDto`/
   `FunnelResponse` independently, in different parts of the same file. Git's
   line-based merge combined both sets with **no conflict marker** (the
   insertions were non-overlapping lines) — which compiles fine as a diff but
   is two colliding definitions of each interface. Caught by grepping for
   duplicate `export interface Funnel` lines after merging, not by trusting
   the merge went cleanly just because git didn't flag it.
2. The refusal-ledger commit's `Insights.test.tsx` mocks every data hook the
   screen uses and says outright "no QueryClientProvider needed because the
   real useQuery never runs" — true when written, but it predates this
   commit's `useFunnel()` call in the same screen. Merged in unmocked, the
   real hook's real `useQuery` fired with no provider and failed 7 tests.
   Fixed by adding the same `jest.mock` pattern already used for the other
   three hooks.

**The general lesson for whoever runs the next batch of parallel subagents:**
a clean `git merge` with no conflict markers is not proof the result is
correct when two branches touched the same file in different places — TS
duplicate-declaration errors and a test file's own unmocked-hook assumptions
are both invisible to git's line-based conflict detection. Ran the real test
suites after every merge in this batch, not just checked for `CONFLICT` in
the merge output.

**Verified together, after both merges + both fixes:** 983 passed/11 skipped
(Python, re-run independently), 6 suites/53 tests passing (jest, re-run
independently — up from the 5/41 the ledger commit alone had), `tsc --noEmit`
clean.

### 2026-08-31 — `9296071d` feat(desktop): render the Refusal Ledger's honesty rules

`ID:MODEL2OFF`. First landed piece of the 5 new IMPL specs (`docs/IMPL_*.md`,
commit `604a2001`) — implements `docs/IMPL_REFUSAL_LEDGER.md` §2/§4, the
rendering half only (a parallel subagent workstream, `I4-backend`, is
diagnosing and fixing why the underlying numbers are `$0` — this entry is
purely "make the UI honest about whatever numbers it's given").

**What changed:** trims now render in their own "Risk also shrank N trades"
section, explicitly never folded into `totalVetoes` (a trim let a smaller
trade through; a veto let nothing through). A `null` ghost P&L renders the
literal word "pending", never `$0` — the doc's central honesty rule, since a
completed $0 and an unmeasured one must never look the same. A ghost that
would have *made* money (a miss, not a save) renders amber and is shown, not
hidden next to the wins. Dashboard/Insights tiles render `"$— · N marks
pending"` instead of a bare `$0` when nothing has finalized yet. The risk
profile in force (`reasoning["risk_profile"]`) is captioned on the ledger —
falls back to "disclosure pending" honestly, since the backend doesn't stamp
that field yet. New `ExemplarCard` ("story trade") wired to a rule-row click
against a documented `VetoExemplarResponse` shape the backend hasn't built
yet either — degrades to a clear message instead of crashing.

**Found before writing any code:** `packages/shared-types/src/index.ts`'s
`VetoLedgerResponse` never declared `trims`/`totalTrims` at all, even though
`apps/api/app/routers/insights.py` already returns them — the frontend was
structurally incapable of rendering data the API already sends.

**Verified, not assumed:** built by a subagent in an isolated worktree, then
independently re-verified by me directly (not just trusting its report) —
`pnpm --filter mobile exec jest --silent` -> 5 suites/41 tests passing (up
from 3/28), `tsc --noEmit -p apps/mobile/tsconfig.json` clean. The subagent
revert-checked (CLAUDE.md §4.1) the trims-into-vetoes guard, the
pending-vs-$0 render, and the missed-tone render independently — each
matching test failed for the right reason when broken, then was restored.

**Left open, explicitly, for the parallel backend workstream:** the
`GET /api/v1/risk/vetoes/{rule}/exemplar` endpoint needs building, and
`reasoning["risk_profile"]` needs to be stamped at write-time and threaded
onto `VetoLedgerResponse.riskProfile`.

**Environment note for whoever runs the next batch of parallel subagents:**
one of the other concurrently-running agents (I3-backend) discovered `git
stash` is **not worktree-scoped** — all worktrees off one repo share a single
stash stack. Two agents both doing "`git stash`, run baseline, `git stash
pop`" concurrently can pop *each other's* stash into the wrong working tree.
It recovered safely by reconstructing verified content and re-checking
before committing, but the next round of parallel dispatches should avoid
`git stash` for baseline comparisons entirely (use `git diff <base>..HEAD`
or a separate clean checkout instead).

### 2026-08-30 — `2709d236` fix(auth,broker): stop auto-attaching every signup to the operator's own Alpaca account

`ID:MODEL2OFF`. Implements `docs/PLAN_MULTI_TENANT.md` §1 + §3 — the live
security issue flagged in the handoff (`d9240326`): any new signup
(magic-link or Google, first login, no exotic path) was silently handed a
write-capable connection to the SERVER's own Alpaca keys — the exact paper
account being scored. A real authenticated user, so `require_real_auth`
passed: they could approve a pending proposal, close a position, revoke
the connection, or arm auto-approve, on someone else's account.

**Fix (§1):** new `_env_connection_allowlist()` in `env_bootstrap.py` —
`ALPACA_ENV_CONNECTION_USER_IDS` (comma-separated) if set, else
`AGENT_CRON_USER_ID` alone (matches `.env.example`'s existing
`AGENT_CRON_USER_ID == FIXTURE_USER_ID` convention, so a correctly-set-up
single-tenant deployment is unaffected). Checked inside
`ensure_env_broker_connection` itself — the ONE function both the
per-login catch-up (`routers/auth.py`) and the boot-time sweep
(`bootstrap_env_broker_connections`) already call — so there's exactly one
gate to remember, not two (CLAUDE.md §4.4). Neither var set → nobody gets
the connection; fails closed. Mechanism itself is untouched — the operator
still needs it to trade at all, per the plan's own explicit warning not to
delete it.

**Fix (§3):** `PostgresStore.get_account` was returning the SAME hardcoded
cold-boot fixture (`equity=$100,000, buying_power=$200,000,
status="connected"`) for BOTH "genuine cold boot, connection exists" and
"no connection at all" — a judge with zero connections (now correctly the
case, per §1) would have seen a confident fake portfolio instead of an
honest empty state. Now checks for an active connection first:
none → `status="disconnected"`, zeroed fields; connection but no snapshot
yet → the legitimate fixture, unchanged; connection + snapshot → real
numbers, unchanged. `AccountStatus` already had `"disconnected"` as a
valid literal — the schema anticipated this, nothing downstream changed.

**Verified:**
- Baseline reproduced myself: 961 passed, 10 skipped (matches the plan's
  own stated number).
- Revert-checked both fixes per CLAUDE.md §4.1 — disabled each gate in
  turn, confirmed the matching new test failed with a real, legible
  assertion diff, restored, confirmed green again.
- Found and fixed a 7th pre-existing test I'd have otherwise missed:
  `test_broker.py::test_connections_response_flags_environment_vs_oauth_source`
  called `ensure_env_broker_connection` directly with no allowlist
  configured — caught by running the FULL suite, not just the file I
  thought I'd touched.
- Updated 6 more pre-existing tests (all in `test_env_bootstrap.py`) that
  exercised `ensure_env_broker_connection`/`bootstrap_env_broker_connections`
  with a plain test user id — each now sets `AGENT_CRON_USER_ID` explicitly
  so it keeps testing its OWN original concern (idempotency, non-clobber,
  paper-vs-live) rather than being silently entangled with the new gate.
- Replaced the one test whose original assertion WAS the vulnerability
  (`test_login_after_boot_still_gets_env_broker_connection`, which proved
  *any* new signup got the connection) with two: one proving a genuinely
  new signup now gets zero connections, one proving the catch-up mechanism
  still fires for an allowlisted user (the plan's own named
  `test_owner_still_gets_the_env_connection`, guarding against
  over-narrowing this into breaking the operator's own login).
- Added a `RUN_POSTGRES_TESTS=1`-gated test (skipped here, no live
  Postgres) proving the boot-time sweep respects the allowlist across
  several real `User` rows, not just MockStore's single fixture user.
- Final: **969 passed, 11 skipped** (+8/+1, exactly the new tests, zero
  regressions). ruff's 15 findings on touched files all confirmed
  pre-existing via `git diff` (none land near my actual changes). mypy
  clean on both production files.

**Left open, per the plan's own scope:**
- §2 (register an Alpaca OAuth app + set `ALPACA_OAUTH_CLIENT_ID`/
  `_SECRET`) — external, on Alpaca's own dashboard, not committable.
- The empty-state UI polish (extend `Settings.tsx`'s existing "No broker
  linked" pattern to Dashboard/Positions for `status="disconnected"`) —
  the API now tells the truth; nicer frontend messaging is a follow-up,
  not a blocker for the security fix itself.
- Not deployed from this session — same Railway-CLI-access limitation
  already noted for the auto-approve toggle work (no linked project, TLS
  cert error reaching Railway's API from this environment).

### 2026-08-30 — `d0774438` feat(desktop): AUTO pill on the Decisions list

`ID:MODEL2OFF`. Closes the deliverable the frontend toggle work (`a70cd210`)
deliberately left open, now that the backend landed the real field
(`DecisionSummaryDto.approval_mode` / wire `approvalMode`, `7362a3a3`).

**Checked where it actually belongs rather than guessing**: mobile has no
screen consuming `useDecisions`/`DecisionSummaryDto` at all — `(tabs)/review.tsx`
is a different feature (the swipe hand-grading deck, `useReviewQueue`, closed
trades only). Desktop's `Decisions.tsx` is the only real target. Mobile's
`decision/[id].tsx` reads a third, separate endpoint
(`/api/v1/decisions/{id}/timeline`) neither prior workstream touched — left
alone rather than scope-creeping a new field through it.

Extended `Tone` (`primitives.tsx`) with `'warn'`, mapped to the `pg-pill--warn`
token the toggle work already added to `theme.ts`, instead of a one-off
className. Decisions.tsx renders it beside the existing action pill when
`d.approvalMode === 'auto'`.

**Verified**: `tsc --noEmit -p apps/mobile/tsconfig.json` clean (the `Tone`
union extension doesn't break an exhaustive switch anywhere), `jest` 23/23
unchanged. **Not verified live** — an authenticated desktop Decisions view
needs real Google OAuth or a live Postgres-backed API, unavailable here; the
toggle commit (`a70cd210`) already accepted the identical limitation for the
same reason. Recommend a real look once deployed.

### 2026-08-30 — `db79fa78` fix(orders): auto-approver's own try/except must cover gate 2b too

Found reviewing the merged auto-approve sweeper (`3bce40b2`/`4e46507e`/`7362a3a3`)
before pushing, `ID:MODEL2OFF`. `auto_approve_for_user`'s docstring claims
*"Never raises: a broker or DB failure mid-sweep is logged and swallowed"* — but
the try/except only wrapped gates 5-7 + `execute_proposal`. Gate 2b's connection
lookup (`_resolve_paper_connection`, a real store/DB call) and `store.list_pending`
sat outside it, so a transient failure there would propagate straight out of the
function — a CLAUDE.md §4.2 "docstring claims something the code doesn't do" case,
on the one file in this feature where that claim matters most.

**Not a live bug**: `ReconcilerFleet.tick()` already wraps the entire call in its
own per-user try/except, matching every other tick step — so a DB hiccup here was
already caught one layer up, and one user's trouble still couldn't stop
reconciliation for everyone else. Fixed anyway so the function's own contract is
self-sufficient, not dependent on the caller happening to wrap it.

**Fix**: widened the try/except to start right after gate 2 (the two pure
env-var reads, which genuinely cannot raise), covering gate 2b onward through
execution in one block; removed the now-redundant second `try:` that used to sit
before gate 5.

**Verified**: added `test_a_connection_lookup_failure_is_also_swallowed`. Per
CLAUDE.md §4.1: reverted to the pre-fix file (`git show` off the merge commit),
confirmed the new test fails with the `RuntimeError` propagating out uncaught,
restored, confirmed 19/19 pass again. Full suite **961 passed, 10 skipped**
(was 960 — +1, zero regressions). mypy on this file: identical 3 pre-existing
`async_sessionmaker` type-arg errors before and after, confirmed by running
mypy against the real pre-fix file content directly (not piped through stdin,
which this session already found unreliable for cross-module imports) rather
than trusting a stdin diff.

**Left open**: the per-row `AUTO` pill (Picks/Review) the frontend workstream
deliberately deferred — the real field is now known
(`DecisionSummaryDto.approval_mode` / wire `approvalMode`, `GET /api/v1/decisions`,
landed in `7362a3a3`). Live verification of the whole feature (deploy with
`AUTO_APPROVE_ENABLED=0`, confirm inert, then the account owner — not a model —
flips both keys) is unchanged from the plan's own §4 order and correctly not run
this session.

### 2026-08-30 — `7362a3a3` feat(api): expose approval_mode on the decision list so auto pills can render

`ID:MODEL2OFF`. Third and last of three commits this session. `approval_mode` lived only
on the DB model + `PostgresStore` internals (confirmed by grep before writing anything)
— no API-facing DTO carried it, so nothing could render an AUTO pill against a real
field.

**Checked both candidate homes the brief named, rather than picking one on a guess:**
`ApprovalProposalDto` (backs the Picks screen, `GET /approvals/pending`) is the WRONG
home — read `PostgresStore.list_pending`'s query and `append_pending`'s hardcoded
`approval_mode="ask"` at creation, and read `Picks.tsx` itself: it is specifically the
PENDING-approvals inbox, and an auto-approved row's `user_response='approved'` means it
is never in that list by construction. `approval_mode` would read `"ask"` on literally
every row there, forever — not a useful field to add. `DecisionSummaryDto` (backs
`GET /api/v1/decisions`, read by `Decisions.tsx` via its `useDecisions` hook, traced
through the import chain rather than assumed from the filename) is the RIGHT home — it
lists every council decision regardless of outcome. Added `approval_mode` there,
populated in `decisions_list.py`, plus the matching field on the hand-maintained TS
mirror in `packages/shared-types/src/index.ts` (confirmed no codegen exists for these
DTOs — this file is manually kept in sync, same as every other field on it).

**This is flagged explicitly, not silently resolved:** the brief said a parallel
frontend agent might be building against either of the two guessed locations. If that
agent assumed `ApprovalProposalDto`, this commit's home (`DecisionSummaryDto`) is the
one that actually works and needs reconciling.

Verified: full suite -> 960 passed, 10 skipped, unchanged from the prior commit (a field
addition, not new behavior). Confirmed via grep that no test constructs
`DecisionSummaryDto` directly or hits `GET /api/v1/decisions`, so the new required field
broke nothing. Installed the JS workspace fresh (this worktree had no `node_modules`)
and ran `pnpm -s exec tsc --noEmit -p apps/mobile/tsconfig.json` (clean) +
`pnpm --filter mobile exec jest --silent` (**23 passed, 3 suites**) to confirm the
shared-types edit doesn't break the mobile build.

**Session total, all three commits:** baseline **940 passed / 9 skipped**, verified
myself before touching anything (matches `PLAN_AUTO_APPROVE.md`'s own claimed number
exactly) -> final **960 passed / 10 skipped** (+20 passing: 18 in `test_auto_approver.py`
+ 2 in `test_broker.py`; +1 skip: the new Postgres-gated `auto_approve_consent`
round-trip, not run locally, no Postgres in this sandbox). Every gate in
`auto_approve_for_user` was individually broken and confirmed to fail its matching test
before being restored (CLAUDE.md §4.1) — see the two commits above for the full list.
Never flipped `AUTO_APPROVE_ENABLED` or `auto_approve_consent` to true outside a test's
own isolated fixture; never touched the real deployed Railway app or the real Alpaca
connection.

**Left open / for the next agent:** the frontend AUTO-pill work itself (out of scope —
this session was backend-only, per the brief); reconciling whichever DTO the parallel
frontend agent actually built against, if it wasn't `DecisionSummaryDto`; live
verification of the sweeper end-to-end (deploy with `AUTO_APPROVE_ENABLED=0`, confirm it
runs and executes nothing, then the ACCOUNT OWNER — not a model — flips the flag and
grants consent from the app) was explicitly out of scope for this session per its own
operating rules (never run anything against the real deployed app or real broker
connection) and is unchanged from `PLAN_AUTO_APPROVE.md`'s own §4 live-verification order.

### 2026-08-30 — `4e46507e` feat(orders): auto-approve sweeper — the agent can now open a trade unattended

`ID:MODEL2OFF`. Second of three commits. Built `docs/PLAN_AUTO_APPROVE.md` end to end —
`apps/api/app/services/orders/auto_approver.py::auto_approve_for_user`, wired into
`ReconcilerFleet.tick()` after both exit paths — plus the new `auto_approve_consent`
gate from the prior commit, wired in as its own explicit gate 2b per the user's
instruction not to fold it silently into gate 1.

**Both of the plan's own "verify before relying on it" items were actually verified,
not assumed** (see the commit body for the full detail): `execute_proposal` resolves
its store/broker-store through module-level singletons with no FastAPI `Depends`
anywhere on that path, so it behaves identically called from the reconciler fleet as
from a route. `store.decide()` and `finalize_execution_claim()` were both read end to
end — neither touches `approval_mode` — so the `"auto"` stamp is applied via its own
dedicated `UPDATE`, strictly after a successful execution.

**All 18 tests (10 named in the plan's §4, 2 for the new consent gate's two-key AND
property + a no-connection edge case, 1 extra fresh-proposal-is-not-skipped companion,
1 extra under-daily-budget companion, 1 extra literal `agent`-exit-mode-fired
confirmation, 1 String(10) fit check) were individually revert-checked live** —
broke each gate in the source, ran the specific test, watched it fail with the exact
wrong number/behavior, restored, confirmed the full file green again. Every one behaved
exactly as predicted; none needed a second attempt. The per-tick-cap check (gate 6) was
the most involved to break realistically — simulated "the cap was removed" by having
the sweeper loop `execute_proposal` over every eligible proposal instead of picking one,
which correctly turned `len(executed) == 1` into `len(executed) == 5`.

**Design choices beyond the plan's literal text, decided and stated rather than left
implicit:**
- The consent gate resolves the user's Alpaca connection filtered to `is_paper=True`
  explicitly (a small local helper, not the shared `broker_use.get_active_broker_connection`,
  which has no such filter and would happily read a LIVE connection's consent flag —
  exactly the wrong row for a feature that must stay paper-only).
- The two exit steps (`manage_positions_for_user`, `sweep_expiring_options_for_user`)
  both free premium, so the sweeper runs after BOTH, not just the one the plan names —
  same reasoning the plan gives, applied to the second exit mechanism it didn't
  explicitly mention.
- `_env_int` is a small local copy of `engine.risk.types`'s private helper of the same
  name and contract (fail-to-default, warn-and-keep-default on a malformed value), not
  an import of that underscore-private name across the api/engine package boundary —
  the CONTRACT is shared, the actual env var names/defaults never overlap, so this
  isn't the §4.4 "same number in two places" trap.

Verified: full suite `apps/agents apps/api packages/` -> **960 passed, 10 skipped**
(940+20 passing / 9+1 skipped across all three commits this session, exactly accounted
for). Ruff clean. mypy: 3 new "missing type arguments for async_sessionmaker" errors in
this file, same pre-existing untyped-generic pattern `reconciler_fleet.py`'s own
`session_factory` field already carries (confirmed via `git stash` baseline) — not a
new class of error, not fixed here.

### 2026-08-30 — `3bce40b2` feat(broker): add auto_approve_consent, the account owner's own two-key gate

`ID:MODEL2OFF`. First of three commits implementing `PLAN_AUTO_APPROVE.md` (the sweeper
itself, in a later commit) plus a UI-facing extension the user asked for this session on
top of the plan: a per-connection `auto_approve_consent` flag so the account owner — not
just the Railway operator env — controls autonomous entries from inside the app. This
commit is the extension's plumbing; the sweeper that actually reads it lands next.

**Baseline verified first**, per CLAUDE.md's own instruction: `apps/agents apps/api
packages/` → **940 passed, 9 skipped**, matching `PLAN_AUTO_APPROVE.md`'s own claimed
number exactly (no drift to chase, unlike the last session's stale "792").

Copied `live_trading_consent` (migration 0011) field-for-field, per the brief's explicit
instruction to mirror that exact shape rather than invent a new one:
- Migration `0016_auto_approve_consent`, chained onto `0015_snapshot_options_level`
  (confirmed a single head via `alembic heads` — no branching).
  `BrokerConnection.auto_approve_consent`, `BrokerConnectionRecord.auto_approve_consent`,
  both default `False`.
- `BrokerStore.set_auto_approve_consent` on the Protocol, implemented identically in
  `InMemoryBrokerStore` and `PostgresBrokerStore` (mirrors `set_live_consent`'s
  compare-on-`status=="active"` update exactly).
- `POST /api/v1/broker/connections/{id}/auto-approve-consent`, body `{"enabled": bool}`,
  response `BrokerConnectionResponse` (now carrying `auto_approve_consent` /
  wire `autoApproveConsent`), ownership-checked the same way `/consent` is. **This exact
  path and body shape is the contract a parallel frontend agent is building against —
  unchanged from the brief.**

**Tested the same way `live_trading_consent` is tested — which turned out to be less
than the brief assumed, so I extended it rather than just mirroring gaps too:**
searching the existing suite found `live_trading_consent`'s own round-trip is
Postgres-gated only (`test_postgres_stores.py`, skipped without
`RUN_POSTGRES_TESTS=1`) — there was no router-level ownership-check test for the
`/consent` endpoint itself anywhere. I mirrored the Postgres-gated round-trip
(`test_postgres_broker_store_auto_approve_consent_round_trip`: defaults false,
independent of `live_trading_consent`, refuses on a revoked connection — same three
assertions `set_live_consent` would need) AND added the router-level pair that didn't
already exist for either endpoint (`test_auto_approve_consent_defaults_false_and_round_trips`,
`test_auto_approve_consent_other_users_connection_is_404`, in `test_broker.py`, mirroring
the existing `test_revoke_other_users_connection_is_404`'s OAuth-seeded-connection
pattern). One line added to `test_env_bootstrap.py` confirming the env-bootstrap path
defaults the new flag false too, alongside the existing `live_trading_consent` assertion.

**Verified, not assumed:** ran `apps/api/tests/test_broker.py
apps/api/tests/test_postgres_stores.py apps/api/tests/test_env_bootstrap.py` directly →
**38 passed, 9 skipped** (the 9 skips are the pre-existing `RUN_POSTGRES_TESTS`-gated
file in full, my new Postgres test included — not run here, no local Postgres). Ruff
clean on every file this commit touches. mypy: `postgres_broker_store.py` already had 2
pre-existing `"Result[Any]" has no attribute "rowcount"` errors before this change
(`revoke_connection`, `set_live_consent`) — confirmed via `git stash` — my new
`set_auto_approve_consent` mirrors that exact existing pattern and is a 3rd occurrence
of the SAME pre-existing gap, not a new class of error.
### 2026-08-30 — `a70cd210` feat(mobile,desktop): auto-approve mode toggle, arm-gated by a real confirmation

`ID:MODEL2OFF`. Built in an isolated worktree; not merged to `main` by me. UI-only
session — did not touch `apps/api` or any risk/execution path, per this session's own
instructions and CLAUDE.md §8.

- The UI half of [`PLAN_AUTO_APPROVE.md`](docs/PLAN_AUTO_APPROVE.md): a pill near the
  top of Home (mobile, `AutoApprovePill.tsx`) and Dashboard (desktop, `AutoApproveControl`
  in `Dashboard.tsx`'s hero `CardHead`) showing **ASK** (default) or **AUTO**, backed by
  a new `useSetAutoApproveConsent` hook calling `POST
  /api/v1/broker/connections/{id}/auto-approve-consent {enabled}` and a new
  `autoApproveConsent: boolean` on the `BrokerConnection` type
  (`apps/mobile/src/hooks/useBrokerConnections.ts`). Built against a **pre-agreed
  contract** from a parallel backend session I could not see the code of — mirrors the
  existing `live_trading_consent` / `POST .../consent` pair in
  `apps/api/app/schemas/broker.py` / `routers/broker.py` exactly (same `_camel`
  alias_generator, same `SetConsentRequest`-shaped body). **Neither the endpoint nor the
  field exist server-side yet** — confirmed, not assumed (see below).
- Turning ON requires an explicit confirmation naming the real consequence (paper-only,
  daily cap, still risk-gated) — mirrors `CircuitBreakerBanner`'s
  confirm-the-risky-direction-only asymmetry. Mobile: `Alert.alert` (existing precedent).
  Desktop: **no existing confirm/modal precedent** — checked first (grepped the desktop
  tree for confirm/Modal/dialog: nothing; desktop's own broker-disconnect button has zero
  confirmation) — so `ConfirmAutoApproveOverlay` is a small purpose-built overlay off
  existing `.pg-card`/`.pg-btn` classes, not a new generic Modal primitive. Turning OFF is
  immediate on both surfaces.
- Neither pill optimistically flips — both render `autoApproveConsent` straight from the
  query cache, with a busy label ("Arming…"/"Turning off…") while the mutation is in
  flight; a failed call just leaves the cached value (and pill) untouched, surfaced via
  `isError`. Explained in `useSetAutoApproveConsent`'s docstring.
- Desktop Platinum Glass had no warning/amber token (only bull/bear/error) — added
  `--pg-warn` / `--pg-warn-wash` (light+dark) + `.pg-pill--warn` + a `.pg-pill-btn`
  (clickable-pill variant, 44px min-height) to `theme.ts`, same semantic slot as mobile
  `DESIGN.md`'s `warning` token. Mobile needed no new tokens — reused
  `warning`/`warning-subtle` and `StatusPill`'s existing chip visual language.

**Verified:**
- `pnpm -s exec tsc --noEmit -p apps/mobile/tsconfig.json` — clean before (`git stash -u`)
  and after.
- `pnpm --filter mobile exec jest --silent` — **23/23 passing**, same 3 suites as
  untouched baseline.
- `pnpm --filter mobile exec eslint` on every touched file: 3 findings, all 3 confirmed
  pre-existing via `git stash -u` (shifted line numbers, identical rule+file+message).
  Fixed the 2 *new* findings my own code introduced (a floating promise in my mutation's
  `onSuccess`; a `jsx-a11y` violation from `stopPropagation` on the overlay's inner div —
  reworked to an `e.target === e.currentTarget` check on the outer backdrop instead).
- Hit the exact "backtick inside the `PLATINUM_CSS` template literal" trap `521f7251`'s
  own commit message warns about, despite reading that warning first (it's literally
  ~100 lines above where I added text). `tsc` caught it immediately (TS1005), same
  signature that commit describes. Fixed before committing.

**Left open — deliverable 2, the per-row "AUTO" pill (Picks/Review rows):** Not built.
Checked thoroughly, not skipped: grepped all of `apps/api` for `approval_mode` /
`approvalMode` and plausible alternates (`opened_by`/`auto_opened`/`decision_source`/
`entry_source`) — the only repo-wide hit is the hardcoded `approval_mode="ask"` literal
at `postgres_store.py:203`; none of `ApprovalProposalDto`, `DecisionRequest`,
`DecisionResponse`, `DecisionSummaryDto`/`DecisionListResponse` carry anything like it.
Also checked every other branch/ref (`agent-v1/auto-mode-real-data`, the other active
worktree branch, `origin/claude`) in case the parallel backend PR had already landed
somewhere readable — **all three are byte-identical to `main` right now**. This task's
own instructions explicitly forbid guessing the field name, so this is left for whoever
lands the real DTO to wire up (the visual pattern to copy is already well-established:
`Positions.tsx`'s `<Pill tone={p.exitMode === 'agent' ? 'bull' : 'neutral'}>` on desktop,
the rounded-full bordered badge in mobile `positions.tsx`).

**Also not done, and said plainly rather than silently:** no live visual verification.
Reaching Home/Dashboard needs a real session (Google OAuth) or a `DEV_AUTH_BYPASS=1`
backend + a from-scratch Python env (no `.venv` in this worktree, `uv` not on PATH) —
disproportionate to stand up for a UI-only change, and the endpoint this depends on
doesn't exist server-side yet regardless. Every color/spacing value is an existing,
already-shipped token (`StatusPill`'s warning chip, `Pill`'s bull/bear wash pattern,
`.pg-card`/`.pg-btn`) — nothing here is a novel visual primitive — but I have not
personally seen this exact composition rendered.

### 2026-08-30 — `538b119f` feat(engine,agents): candlestick pattern detection feeding strategy fit

`ID:MODEL2OFF`. Built [`PLAN_CANDLE_PATTERNS.md`](docs/PLAN_CANDLE_PATTERNS.md) end to
end in one commit — greenfield module, provider/audit-row wiring, two-strategy fit
integration, prompt wiring — minus the TradingView chart (see "Left open" below). Built
in an isolated worktree; not merged to `main` by me.

**Baseline verified first**, per CLAUDE.md's own instruction: ran `apps/agents apps/api
packages/` before touching anything → **853 passed, 9 skipped**, matching the handover
note exactly (it did NOT match the plan's own stale "792" — the plan predates the
Aggressive Profile / Exit Agent / clock-wiring commits already on `main`; verified via
`git log` that all three had already landed before I started, so 853 is correct and 792
is simply out of date, not a discrepancy to chase). Now **904 passed, 9 skipped** — 51
new tests (47 in `test_features_patterns.py`, 4 in `test_fit_patterns.py`), zero
regressions. Ruff repo-wide: **252 → 252** (`git stash` comparison, matches
`HACKATHON.md`'s number, not the stale "9"). Mypy on `apps/agents` + `packages/engine`:
**178 → 178**. Every pre-existing error in a file I touched (`runtime.py` ×4,
`technical_analyst.py` ×1) individually confirmed via `git stash` to predate this change,
same line-shifted messages before and after.

#### `packages/engine/engine/features/patterns.py` — the detector (new file)

`PatternBlock` (frozen dataclass, `as_dict()`) + `detect_patterns(bars, *, atr,
trend_regime)`. 18 named patterns across four families (single-bar, two-bar, three-bar,
range), pure stdlib `math` — no pandas/numpy/pandas-ta, confirmed the forbidden-import
reasoning is real: `pandas-ta` does emit a deprecation warning on import under this
repo's pandas/numpy pins, and `pytest`'s `filterwarnings = ["error"]` would fail
collection repo-wide, not just one test, if anything imported it.

Every pattern scores `quality × magnitude × context`, all ramps. **One deliberate
departure from a single universal magnitude ramp**, spelled out in the module docstring:
compression (`inside_bar`/`nr7`) is a coil — its whole claim is a SMALL range — so
`_coil_magnitude` ramps the opposite direction from `_magnitude` (rewards low `rng_atr`,
not high). Reversal/continuation/indecision/expansion all share the plan's own
`_ramp(rng_atr, low=0.5, high=1.5)`. Family aggregation is `max`, never `sum`, per the
plan. `atr <= 0` or fewer than 7 bars (the binding minimum, needed by `nr7`'s trailing
window — every other pattern needs at most 3) returns the all-zero block; never raises.

**Piercing line / dark cloud cover needed a fade the plan didn't specify**, found by
measuring, not reasoning (CLAUDE.md §4.3): an early version scored a CLEAN bullish
engulfing fixture at `piercing_line ≈ 0.45` too, because penetration >100% still ramped
to 1.0 — meaning a full engulf would sometimes get NAMED as a piercing line instead of
an engulfing, since both patterns share the same color gate and often co-occur on the
same two bars. Classical TA defines a piercing line as explicitly NOT fully engulfing —
that is what distinguishes it from an engulfing pattern — so `_penetration_ramp` now
fades the score back to 0 once penetration passes ~1.3 (full retracement plus margin).
Verified live: reusing the bullish-engulfing positive fixture as a piercing-line input
now scores `< 0.1` (`test_piercing_line_fades_out_on_a_full_engulf`).

**Every fixture was measured against the real implementation before being written down**
— a standalone script computed real `PatternBlock` values for every candidate fixture
first; two arithmetic mistakes in my own first-draft fixtures (a hammer bar with
`rng_atr=0.27` when I intended ~1.0; a doji with `rng_atr=0.55` when I intended ~1.5,
both from picking wick lengths that didn't add up to the range I meant) were caught this
way, not by trusting the ramp formulas on paper.

**All 5 revert-checks performed live** (CLAUDE.md §4.1), each: broke it, confirmed the
specific test failed (and which one), restored, confirmed green again:
- `_magnitude` short-circuited to always return `1.0` → `test_a_micro_range_hammer_scores_zero`
  failed exactly as predicted: `reversal_bull` 0.0 → 1.0, `"hammer"` became named.
- `_reversal_context` short-circuited to always return `1.0` →
  `test_hammer_in_an_uptrend_is_heavily_discounted` failed: uptrend and downtrend both
  scored 1.0 (no discount at all).
- `reversal_bull`'s aggregate changed from `max(...)` to `sum([...])` →
  `test_three_weak_patterns_do_not_outscore_one_clean_one` failed: reported ~0.58 (the
  sum of hammer/bullish_engulfing/piercing_line, all genuinely nonzero on one crafted
  2-bar tail) instead of ~0.29 (the max).
- The `atr <= 0 or len(bars) < 7` guard removed entirely → `detect_patterns([], ...)`
  raised `IndexError` immediately (single-bar patterns index `bars[-1]`) — before even
  reaching `nr7`'s `bars[-7:]`. Proves the guard is load-bearing, not decorative.

#### Provider + audit-row wiring

`RealFeatureProvider.__call__` (`packages/engine/engine/features/provider.py`) computes
`detect_patterns(bars, atr=float(technicals["atr_14"]), trend_regime=str(technicals["trend_regime"]))`
immediately after `compute_technicals`/`compute_quant` — parameters, not recomputed, per
CLAUDE.md §4.4 — and adds `"patterns": patterns.as_dict()` to the returned feature dict.
Re-exported `PatternBlock`/`detect_patterns`/`MIN_BARS_FOR_PATTERNS` from
`engine.features.__init__` alongside every sibling module's symbols, alphabetized into
the existing CONSTANTS/Classes/functions groups (verified with `ruff check --fix` on the
isort rule, not hand-sorted). Added `"patterns"` to `_SNAPSHOT_BLOCKS` in
`apps/agents/trading_agents/runtime.py` — the plan's own named easy-to-forget step;
`test_patterns_reach_the_audit_row` pins it directly.

Deliberately did NOT add a `"patterns"` block to the synthetic MOCK provider
(`apps/agents/trading_agents/features/synthetic.py`) — the plan's own §3 says the
degradation to NEUTRAL when the block is absent "is correct and automatic — do not
special-case it," and the MOCK provider lacking new blocks until a real one is wired is
exactly the existing, established pattern that block was already following for `quant`.

#### Fit integration — exactly two strategies

`apps/agents/trading_agents/strategies/fit.py`: `_Features` gained `.pattern(key)`
(mirrors `.tech()`/`.quant()`). Two new `FitComponent`s, both `directional=True`:
- `rsi_mean_reversion` → `candle_reversal_confirms`, weight 0.15, scored off
  `reversal_bull`/`reversal_bear` by direction.
- `breakout` → `candle_confirms_break`, weight 0.10, scored off
  `max(continuation_bull/bear, expansion)` by direction — chose `max` over an average
  specifically so a doji (near-zero continuation, whatever its expansion) reads as a
  probe even if its wicks happen to be wide, and a clean marubozu alone is sufficient to
  confirm even without also being an outside bar. Not specified exactly this way by the
  plan ("combined with expansion"); this is the reading I judged matches "a marubozu is
  a break; a doji is a probe" most literally, and it is revert-check-covered indirectly
  through `test_absent_patterns_block_barely_moves_the_fit`.

Did NOT touch `momentum`, `sma_crossover`, or `vol_regime_switch` — confirmed via `grep
candle_ fit.py`, exactly 2 matches, in exactly the 2 named functions.

**THE collision test** — `test_blind_weight_stays_below_the_trade_floor`
(`apps/agents/tests/test_fit.py`, written by the Aggressive Profile work, NOT
duplicated) — re-run unmodified after both components landed. **Still passes.** Measured
`blind_weight_fraction` directly:

```
sma_crossover        0.1500  (untouched)
rsi_mean_reversion    0.1500 -> 0.1304
momentum              0.0000  (untouched)
breakout              0.3500 -> 0.3182
vol_regime_switch     0.4000  (untouched)
```

Every strategy strictly below `MIN_FIT_TO_TRADE = 0.42` — `vol_regime_switch`'s 0.400 is
still the tightest case, exactly as before, since it was never touched. Matches the
plan's own `~0.130`/`~0.318` predictions almost exactly.

**One pre-existing test needed an intentional, documented update.**
`test_blind_weight_fraction_still_works_on_an_empty_dict` hardcoded
`rank_strategies({})[0].score == pytest.approx(0.60, ...)`. Adding `rsi_mean_reversion`'s
5th component — which degrades to NEUTRAL (0.5) on `{}` since there is no `patterns` key
— renormalises the weighted mean even though the new term itself contributes nothing
informative: `(0.6 + 0.5*0.15) / 1.15 = 0.5870`, hand-derived AND confirmed against the
live code. Updated the assertion to `0.587` with a comment explaining the shift, rather
than silently loosening the tolerance or deleting the assertion.

**An honest finding beyond what the plan estimated**, written up as a new test rather
than left as a surprise for later: `test_absent_patterns_block_barely_moves_the_fit`
confirms `|Δfit| ≤ 0.03` for the specific case the plan's §3 describes — patterns
**absent** (MOCK provider / thin history), so the new component defaults to NEUTRAL —
verified on both the empty dict (Δ=-0.013, the 0.60→0.587 shift above) and a data-rich
fixture (Δ=+0.0245 for `rsi_mean_reversion`, Δ=-0.0173 for `breakout`), all comfortably
inside 0.03. But a **genuinely present, near-zero reading** ("no notable
pattern today", the common case — NOT the same as "absent") moves `rsi_mean_reversion`'s
fit by **~-0.063** on the same data-rich fixture — measured directly, more than double
the plan's 0.03 estimate. Not a bug: 0.0 is simply farther from NEUTRAL (0.5) than the
"absent" default is, and it's the exact same renormalisation math either way — just
larger than the plan's own worked example implied. Pinned in
`test_a_typical_pattern_reading_can_move_the_fit_by_more_than_the_absent_case_does` so a
future reader who re-measures this does not mistake a real, intentional effect for a
regression.

#### Technical analyst node + prompt

`PATTERN_FEATURES` tuple (`top_pattern`, `top_pattern_score`, the 7 aggregate scores) —
`names` deliberately excluded, a tuple that `render_features` (which expects scalars)
would otherwise stringify oddly. Prompt (`prompts/technical_analyst.py`) explains
ATR-normalisation and trend-context-gating explicitly, warning the model not to
re-apply the trend itself (double-counting), matching the plan's exact wording.

#### Left open

- **The TradingView Lightweight Charts visual** (plan §5) — not started. The plan
  itself frames this as the last, explicitly-optional step ("the feature is the pattern
  detection; the chart is presentation") and flags its own unverified LICENSE claim.
  Building it means touching `apps/mobile/src/desktop/` (React/TypeScript, Platinum
  Glass design system) — a different stack from everything else in this session, and I
  did not want to rush a UI feature I could not properly verify (light+dark, the
  license) in the budget remaining after the detector + fit work above. This is a
  reasonable place for the next model to pick up, per the plan's own explicit
  permission to stop here.
- Whether `RealFeatureProvider`'s live pass actually produces a populated `patterns`
  block against real Alpaca bars was NOT verified live (market context / no live keys
  exercised this session) — only verified against hand-built `DailyBar` fixtures and the
  full test suite. The provider wiring is a 6-line, low-risk change (mirrors
  `compute_quant`'s exact call shape) but say so plainly rather than claim it as
  live-verified.

### 2026-08-30 — D.4 (MCP client) assessed and deliberately NOT built — docs only, no commit

**D.4 from `docs/PLAN_ALPACA_MCP.md`** is explicitly "the first thing to cut" and was
attempted last, only after D.3 (`16986692`) and D.5 (`a9a8b820`) were both solid, per
the plan's own build order. Assessed seriously rather than skipped on sight — here is
what was actually checked, so the next model doesn't have to re-derive it:

- **The `mcp` Python SDK is already available workspace-wide** — `apps/mcp_server`
  depends on `mcp[cli]` already, and `python -m uv sync --all-packages` (run for the
  D.3 baseline) installs it into this worktree's shared `.venv`. Confirmed importable:
  `from mcp.client.stdio import stdio_client, StdioServerParameters; from mcp import
  ClientSession` succeeds right now. So D.4 would NOT need a new third-party
  dependency or a `uv.lock` regeneration for the SDK itself — a real risk the plan
  worried about turned out not to apply, same shape as the `APCA_` env-var trap in
  D.0 not materializing.
- **`alpaca-mcp-server` exists on PyPI** (confirmed via `pypi.org/pypi/alpaca-mcp-
  server/json`, latest `2.3.0`) and PyPI is reachable from this sandbox.
- **What actually blocks a genuinely verified implementation:**
  1. **No `uvx` binary in this sandbox** (`which uvx` → not found) — only `uv` itself,
     invoked as `python -m uv`, which doesn't register a standalone `uvx` entry point
     the way a `pip install uv` does. The plan's own client launches `uvx
     alpaca-mcp-server` directly; I cannot exercise that exact invocation here to
     prove it actually starts and speaks MCP.
  2. **No real Alpaca credentials in this sandbox** to actually call `get_option_chain`
     / `get_option_snapshot` against Alpaca's live data — and this session should not
     be entering or requesting trading credentials into anything per its own operating
     rules.
  3. **The read-only tools' response SCHEMA was never verified, only their NAMES.**
     D.0 confirmed `get_option_chain`/`get_option_snapshot`/etc. exist in the
     `options-data` toolset (verbatim from the README), but not their JSON shape.
     Building `apps/agents/trading_agents/mcp_client/alpaca_mcp.py`'s bridge into
     `engine.options.contracts.ContractQuote` (`occ_symbol, contract_type, strike,
     expiry, bid, ask, open_interest, volume, delta, implied_volatility` —
     `packages/engine/engine/options/selection.py:179`) means mapping the MCP
     response's actual field names/nesting/units onto those ten fields correctly.
     Guessing that mapping without a real response to check against is exactly the
     class of bug this repo has shipped before under green tests (CLAUDE.md §4.2/§4.3
     — "do not trust a docstring", "measure against reality") — a bridge covered only
     by mocked unit tests, never exercised against the real server, would be an
     *inert-looking-functional* integration, not a verified one.
- **Decision: skip D.4 this session.** Per the plan's own text, D.3 alone already
  satisfies the hackathon's eligibility requirement ("MCP server OR CLI"), and per
  `docs/PLAN_ALPACA_MCP.md` §5: *"Two half-working integrations is a worse submission
  than one working one."* Building the client anyway, unable to verify it against the
  real server, would produce exactly that — a plausible-looking `USE_ALPACA_MCP=1`
  path nobody has confirmed actually works.
- **For whoever picks this up next**, in an environment with `uvx`/network/real paper
  keys available: run `uvx alpaca-mcp-server --help` (or start it and inspect a real
  `get_option_chain` response) BEFORE writing the `ContractQuote` bridge, per the same
  "open the spec, don't infer it" discipline `docs/PLAN_ALPACA_MCP.md` D.0 already
  modeled for the CLI/MCP-server surface itself.

### 2026-08-30 — `a9a8b820` chore(deploy): ship Alpaca's own CLI binary in the API image (D.5)

**D.5 from `docs/PLAN_ALPACA_MCP.md`** — own commit, independently revertible from
D.3's behavior change (`16986692`), per the plan's explicit instruction not to couple
the image change with the behavior-flip.

- **What changed and why:** new Stage 2 (`alpaca-cli`, base `python:3.12-slim` —
  reusing the same tag `deps`/`runtime` already use rather than introducing a fourth
  distinct base image) installs curl+ca-certificates, downloads the pinned Linux/amd64
  CLI release, verifies its sha256 against a checksum baked into the Dockerfile,
  extracts just the `alpaca` binary, and asserts `alpaca version` runs (SUBCOMMAND — the `--version` flag form exits 1 with an auth error; corrected 2026-08-30) — so a bad
  download or a broken extraction fails the BUILD, not a runtime `alpaca clock` call in
  production (`railway.toml`: 3 restarts, 600s healthcheck timeout, four days from the
  deadline). Stage 3 (runtime) `COPY --from=alpaca-cli`s only the extracted binary and
  re-asserts `alpaca version` there too — the runtime image itself never gains curl
  or a Go toolchain, matching the plan's "no curl/wget today" constraint on that stage.
  `USE_ALPACA_CLI` stays `0` (`packages/engine/engine/features/clock.py`, `16986692`)
  — this commit only puts the binary in the image, it does not turn the behavior on.
- **What I VERIFIED, and how:**
  - **Linux/amd64 release asset naming** — D.0 explicitly flagged this as not found in
    the CLI's README and told this session to check the real GitHub Releases page
    rather than guess a pattern. `gh api`/plain `curl` both failed in this sandbox (see
    below), so fetched `api.github.com/repos/alpacahq/cli/releases/latest` via
    PowerShell's `Invoke-RestMethod` instead: latest is `v0.0.14`, assets include
    `cli_0.0.14_linux_amd64.tar.gz` (plus darwin/windows/arm64 siblings and a
    `checksums.txt`) at
    `.../releases/download/v0.0.14/cli_0.0.14_linux_amd64.tar.gz`.
  - **Downloaded the actual asset and cross-checked it three independent ways**:
    downloaded size (3,701,968 bytes) matches the API's own `size` field exactly;
    `Get-FileHash -Algorithm SHA256` matches both the API's `digest` field AND the
    release's own published `checksums.txt` line for this asset (all three agree on
    `6c82ef31...ff2617` truncated here, full value in the Dockerfile); `tar -tzf` on
    the real downloaded archive shows a flat layout (`LICENSE`, `README.md`, `alpaca`
    — no subdirectory), confirming `tar -xzf ... -C /usr/local/bin alpaca` is the
    correct extraction command as written.
  - **Docker build NOT run.** `docker`/`podman`/`nerdctl` are not installed in this
    sandbox — checked via both Bash (`which docker` → not found) and PowerShell
    (`Get-Command docker` → not found, no `com.docker.service`/docker process running
    either). **Not claiming a successful `docker build -f apps/api/Dockerfile .`** —
    that step is still owed before this truly ships. What stands in for it: the
    download/checksum/extract shell logic was proven against the real release URL via
    the PowerShell steps above (a direct Bash `curl` to github.com hit a sandbox-local
    Windows-schannel TLS quirk — `CRYPT_E_NO_REVOCATION_CHECK` / `SEC_E_INVALID_TOKEN`
    — unrelated to how curl behaves inside the actual Linux build stage), plus manual
    review of the Dockerfile's POSIX `sh` syntax (`sha256sum -c -` reads the check line
    from stdin correctly; `tar`/`sha256sum` are both part of Debian's essential package
    set, present in `python:3.12-slim` without an extra apt install).
  - Full Python suite re-run after this Dockerfile-only commit: still **828 passed, 9
    skipped**, unchanged from the D.3 entry above (expected — no Python touched).
- **Anything left open:**
  - **`docker build -f apps/api/Dockerfile .` still needs to run somewhere with
    Docker available before this is truly proven** — flagged plainly rather than
    assumed.
  - D.4 (MCP client) — assessed, not built; see the entry below.

### 2026-08-30 — `16986692` feat(engine,api): Alpaca CLI clock behind `USE_ALPACA_CLI` (D.3)

**D.3 from `docs/PLAN_ALPACA_MCP.md`** — picked up in a fresh worktree on top of
D.0/D.1/D.2 (`1bd33849`, `40eae29b`, and the two docs commits above/below this one),
all already merged to `main`; this entry does not repeat them.

- **What changed and why:**
  - New `packages/engine/engine/features/alpaca_cli.py`: `cli_clock()` runs
    `alpaca clock` (the D.0-verified subcommand — no `get` suffix) via
    `asyncio.create_subprocess_exec` with an argv list, never `shell=True`. Returns
    `None` on ANY failure — missing binary, non-zero exit, timeout, unparseable JSON,
    or any other unexpected exception — and never raises. The timeout path calls
    `proc.kill()` **and** awaits `proc.wait()` (kill alone signals but doesn't reap);
    skipping either leaks one zombie subprocess per scan tick on the 30s scheduler
    loop. The subprocess `env=` is a plain `dict(os.environ)` passthrough — D.0
    verified the CLI reads `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` directly, so the
    plan's own predicted `APCA_`-prefix remapping trap does not apply here.
  - New `engine.features.clock.resolve_market_clock()`: three-step fallback, each
    link reporting its own `source` — CLI (only when `USE_ALPACA_CLI=1`) → `AlpacaClock`
    REST → local calendar. `use_alpaca_cli()` defaults OFF (same truthy-set convention
    as `USE_POSTGRES` in `position_manager.py`), so every existing call site is
    behaviourally unchanged until deliberately flipped. New `ResolvingClock`
    (a `ClockProvider`) + `resolved_clock_from_env()` **replace** the bare
    `clock_from_env()` that `engine.scanner.select.scanner_from_env()` was passing as
    `Scanner.clock` — this is the part that makes the CLI step actually reachable from
    a live scan rather than an unused function. Confirmed the wiring reaches the API:
    `apps/api/app/services/council/scheduler.py` constructs its scanner via
    `scanner_from_env()`, and `scanner_status.py`'s `_build()` already reads
    `result.market_open_source` into `ScannerStatusResponse` (the `40eae29b` work) —
    so `market_open_source: "alpaca_cli"` will reach the scanner-status API response
    once `USE_ALPACA_CLI=1`, the judge-visible eligibility artifact the plan asks for.
- **What I VERIFIED, and how:**
  - **Measured the baseline myself, not trusted from a doc**: this worktree had no
    `.venv`, so `python -m uv sync --all-packages` first, then `python -m uv run
    pytest apps/agents apps/api packages/ -q` → **795 passed, 9 skipped** — matches
    the plan's stated baseline exactly.
  - **After the change: 828 passed, 9 skipped** — exactly +33, matching the 33 new
    tests added across `tests/test_alpaca_cli.py` (11) and `tests/test_clock.py` (22).
    Skip count unchanged.
  - **Revert-checked per CLAUDE.md §4.1, four separate behaviors**, each restored
    immediately after confirming a real, legible failure (not a vacuous pass):
    1. Removed `proc.kill()`/`proc.wait()` from the timeout path →
       `test_cli_clock_returns_none_on_timeout_and_kills_the_process` failed on
       `assert proc.killed is True` (not just "result is None").
    2. Removed the `except OSError` handler **and** the outer safety-net
       `except Exception` (the safety net alone would have masked the missing
       specific handler and given a false pass) →
       `test_clock_falls_back_when_the_cli_binary_is_missing` failed with a real
       `FileNotFoundError` propagating out of `cli_clock`.
    3. Same double-removal for `except json.JSONDecodeError` →
       `test_cli_clock_returns_none_on_unparseable_json` failed with a real
       `json.decoder.JSONDecodeError: Expecting value` propagating.
    4. Replaced `if use_alpaca_cli():` with `if True:` in `resolve_market_clock` →
       `test_resolve_market_clock_never_touches_cli_when_flag_is_off` failed via an
       `AssertionError`-raising spy (`cli_clock must not be called when
       USE_ALPACA_CLI is off`).
    All four restored and the full 828/9 re-confirmed green afterward.
  - **`which alpaca` genuinely finds nothing in this dev sandbox** (confirmed via
    `which alpaca` → exit 1), so
    `test_cli_clock_returns_none_when_binary_is_genuinely_absent` exercises the real
    `FileNotFoundError` path end to end, not a simulated one — this is the exact
    dev-laptop scenario D.3 exists to handle silently.
  - **ruff check + `ruff format --check` + `mypy` strict all clean** on every
    touched/added source file (`alpaca_cli.py`, `clock.py`, `features/__init__.py`,
    `scanner/select.py`, `scanner/engine.py` docstring-only). `ASYNC109` (async fn
    with a `timeout` param) fired on `cli_clock` — this is the plan's own literal
    signature (`cli_clock(*, timeout: float = 5.0)`), so suppressed with a one-line
    `# noqa: ASYNC109` justification rather than reshaped away from the spec.
  - **The repo-wide `ruff check apps/ packages/` finding count (252) is identical
    before and after this change** (`git stash` / re-run / `stash pop`) — confirmed
    none of it sits in a file this commit touches. (This is a materially larger
    number than CLAUDE.md §7's "9 pre-existing errors" — that figure evidently comes
    from a narrower invocation than a full `apps/ packages/` sweep; not investigated
    further, out of scope here, flagging so the next model doesn't assume 9 is the
    whole-repo number.)
  - **Test-file `mypy` findings are pre-existing-pattern, not new debt**: both new
    test files trip the same `"Module ... does not explicitly export attribute"`
    finding that `test_features_macro.py` already has unfixed (verified by running
    mypy on that file directly) — this repo does not hold test files to strict mypy,
    consistent with CLAUDE.md §7 only listing `pytest`/`ruff` as required gates.
- **Anything left open:**
  - D.5 (Dockerfile) is the next commit, its own independently-revertible change per
    the plan's own instruction — see the entry directly above/after this one.
  - D.4 (MCP client) — deliberately not built this session; see the dedicated entry
    for the reasoning (mcp SDK is already available workspace-wide, but no `uvx` in
    this sandbox and no real Alpaca credentials to verify the actual `get_option_chain`
    response shape against, so a faithful `ContractQuote` bridge could not be verified
    rather than guessed).
  - `alpaca clock`'s JSON shape is an inference (same `is_open`/`next_open`/
    `next_close` fields as the REST `/v2/clock` payload, since the CLI wraps the same
    Trading API and its README says JSON-out mirrors the API by default) — **not**
    verified against a real running binary, since D.0 verified the flag/env/subcommand
    surface but not the literal JSON shape, and this sandbox has no `alpaca` binary to
    invoke for real. `_parse_cli_clock()` treats a payload missing the `is_open` key
    as a parse failure (`None`) rather than guessing, so a wrong shape degrades safely
    (silent fallback) instead of misreporting a made-up answer under a real-looking
    `source="alpaca_cli"` label.
### 2026-08-30 — `f736c438` feat(risk,agents): aggressive paper profile for the contest window

`ID:MODEL2OFF`. Picked up the handover from the four-plans commit below and built
[`PLAN_AGGRESSIVE_PROFILE.md`](docs/PLAN_AGGRESSIVE_PROFILE.md) end to end — one commit,
code + both docs together per that plan's own §5. Built in an isolated worktree; not
merged to `main` by me — the orchestrating session reviews and merges.

**Baseline verified first**, per CLAUDE.md's own instruction rather than trusting the
doc: `git stash`, ran `apps/agents apps/api packages/` → **792 passed, 9 skipped**,
matching the plan's §7 exactly. Now **818 passed, 9 skipped** (829 with `apps/mcp_server`
via `uv sync --all-packages`) — 26 new tests, zero regressions. **Every new test
revert-checked per §4.1**: broke the fix, confirmed the specific new test failed (and
only that one), restored it. Ruff + mypy clean on every touched file (`ruff check`,
`mypy`) — checked per-file both before and after, not against the repo-wide baseline
(see the ruff finding below).

#### `RiskCaps.aggressive_paper()` — `packages/engine/engine/risk/types.py`

New classmethod, six numbers, merged via a dict rather than sibling `**kwargs` (so a
caller can still override one of the same six fields without a "got multiple values for
keyword argument" `TypeError` — my own test needed this, fixed it rather than deleting
the test):

```
options_max_premium_pct        1.0  -> 2.5
options_max_total_premium_pct  5.0  -> 12.0
min_council_confidence         0.50 -> 0.42
min_specialist_avg_score       45.0 -> 40.0
options_stop_loss_pct          50.0 -> 40.0   ("cut losers early")
max_correlation_cluster        3    -> 4
```

`daily_drawdown_halt_pct` (-3.0) and `max_position_pct` (5.0) do **not** move, in either
profile — `test_aggressive_profile_leaves_the_drawdown_halt_alone` /
`..._leaves_max_position_pct_alone` pin this and are revert-checked. `from_env()` now
reads `RISK_PROFILE` (default `conservative`) via a new `_select_risk_profile()` helper —
same fail-to-default contract as `_env_int`/`_env_float`: unset or unrecognised value
keeps conservative and logs a warning (`test_unknown_risk_profile_falls_back_to_conservative`,
revert-checked by making the fallback silently pick aggressive instead). Every existing
`cls.X` fallback inside `from_env` for the env-tunable fields is now `base.X` (the
resolved profile's own value), so an explicit env var still wins over either profile's
default, verified live (`OPTIONS_STOP_LOSS_PCT=33` under `aggressive_paper` → 33.0, not
40.0). `RiskCaps()` bare-constructor call sites (tests, `lot_size_block`, etc.) are
completely untouched — only `from_env()`'s body and docstring changed structurally.

**The real reason for the premium-cap change, not just "more risk appetite"**: at $100k
equity, `qty = floor(budget/(ask*100))` with the OLD 1% cap means any contract over
$10.00 floors to zero and the pass silently becomes a HOLD — never reaching the Refusal
Ledger, since the sizer emits a HOLD via `.notes`, not a veto. Verified with the actual
arithmetic, not just cited from the plan:
`test_the_old_one_percent_cap_floored_a_twelve_dollar_contract_to_zero` (qty=0 at 1%) /
`test_a_twelve_dollar_contract_sizes_to_at_least_one` (qty=2 at 2.5%), both computing
`budget_usd` the same way `drafter.py` really does. Revert-checked by putting
`options_max_premium_pct` back to 1.0 inside `aggressive_paper()` — fails exactly as
predicted.

#### The empty-features evidence gate — `apps/agents/trading_agents/strategies/fit.py`

Verified the plan's central claim myself before touching anything:
`best_strategy({})` on the **unmodified** code returns `rsi_mean_reversion` at 0.60,
`tradable=True` — `not_a_trend_break` reads `trend_regime != "downtrend"` and the
missing sentinel `"unknown"` satisfies that as TRUE, not NEUTRAL. A data outage was
spending 5 LLM calls and could originate a trade.

New `_has_usable_features(features) -> (bool, str)`: technicals non-empty AND
`trend_regime not in ("", "unknown")` AND ≥3 non-None values among the 9 quant keys the
scorers read. Wired into `best_strategy` **only** — confirmed `score_strategy`/
`rank_strategies` are untouched by directly testing that `rank_strategies({})[0].score`
still reports ~0.60 (the real, ungated number) even though `best_strategy({})` now
refuses to call it a winner.

**`test_fit.py` did not exist** — `fit.py:93`'s `blind_weight_fraction` docstring claims
a test asserts the direction-blind-weight invariant; there was no `test_fit.py` anywhere
in the repo. Written it, and wrote `test_blind_weight_stays_below_the_trade_floor`
FIRST, confirmed it passed against the **original, untouched** `MIN_FIT_TO_TRADE = 0.45`
before changing that constant at all (per the plan's explicit instruction). It still
passes at the new 0.42, and — revert-checked live — fails if `MIN_FIT_TO_TRADE` is set
to 0.40: `vol_regime_switch`'s blind fraction is exactly 0.400.

`MIN_FIT_TO_TRADE`: 0.45 → 0.42 (hard floor is 0.41, per the above — do not go lower).

**Also revert-checked the "wrong function" failure mode** the plan warned about
(§8.3): moved the gate into `score_strategy` instead of `best_strategy` and confirmed
`test_blind_weight_fraction_still_works_on_an_empty_dict` fails — but worth flagging
precisely **how** it fails, since it surprised me: `blind_weight_fraction` itself calls
`_SCORERS[sid]` directly and never touches `score_strategy` at all, so it is NOT what
catches the misplacement. The test catches it via its OTHER assertion,
`rank_strategies({})[0].score`, which collapses toward 0 once `score_strategy` (which
`rank_strategies` does call) is gated. If a future edit ever trims that second assertion
because it "looks redundant" with the `blind_weight_fraction` loop above it, this
specific protection is what would quietly disappear.

`strategy_fit_node`'s rationale branch (the plan's own "where you'll go wrong" #5) now
tells the two HOLD reasons apart — "no strategy clears the fit floor" (genuine, e.g. the
`_no_fit_features` fixture) vs. "feature data too thin to trade" (the gate fired; the
nominal top score may well be ≥ the floor, which is exactly why the OLD single message
would have read as self-contradictory: "best was X at 0.60, holding"). `fit_block` now
carries `usable_features` + `unusable_reason`. Two new node-level tests
(`test_hold_rationale_names_thin_evidence_not_the_fit_floor` /
`..._still_names_the_fit_floor_for_real_data`), revert-checked by stashing just
`strategy_fit.py` and confirming both fail against the pre-change node.

#### A real regression this workstream caused, found and fixed in the same commit

Adding the evidence gate broke **8 previously-green tests**
(`test_mock_council_produces_buy_proposal_for_nvda` and 7 others in
`test_council_mock.py`/`test_reflection.py`) the moment it landed. Root cause (CLAUDE.md
§4.6 — fixed the cause, not the symptom): `trading_agents/features/synthetic.py`'s
`synthetic_features()` — the default `feature_provider` `run_council()` falls back to
when no explicit one is supplied — has **never had a `"quant"` block**, only
`"technicals"`/`"fundamentals"`/`"macro"`. It predates that schema entirely (it's a
Phase-0 offline/demo/test mock; confirmed live it is NOT reachable in the real
production path — `daily_cron.py` and `apps/api/app/routers/agent.py` both always call
`resolve_feature_provider()` explicitly, which only falls back to synthetic when
`AGENTS_REQUIRE_REAL_DATA` is unset, i.e. dev/CI only). Every quant-driven
`FitComponent` has silently sat at NEUTRAL for every mock-LLM/offline pass this whole
time — harmless until my new gate started reading "no quant block at all" as
indistinguishable from a genuine data outage.

Fixed by giving `synthetic_features()` a deterministic per-symbol `"quant"` block
mirroring `engine.features.quant.compute_quant()`'s real keys and realistic value
ranges — NOT by loosening the gate to tolerate a stale mock schema, which would have
been fixing the symptom. Confirmed via `git stash` that the missing-quant-block gap
itself predates this commit entirely (it's real, just never observable before).
**Checked side effect, and safe**: NVDA under the mock provider now wins on
`"momentum"` (~0.90) instead of `"sma_crossover"` (0.787) — grepped the whole suite,
confirmed no test asserts the specific winning strategy id anywhere, only registry
membership / `BUY` / proposal shape, and corrected the one comment
(`test_council_mock.py`) that cited the old number. The mock LLM's Drafter branch reads
`selected_direction` from state and always emits whichever side is required
(`_extract_required_side`), so this does not change any test's asserted `final_action`
either — verified by re-running the full suite, not just reasoning about it.

#### `selection.py` delta bands — widened, then FROZEN

`engine/options/selection.py`: `[0.40,0.70]`/`[0.25,0.55]` → `[0.35,0.75]`/`[0.25,0.65]`,
per the plan and `docs/HACKATHON.md` §8 (pre-Monday's-open only, one reviewed change).
Added three tests that pin the exact new edges (deltas 0.63 / 0.37 / 0.73) rather than
relying on existing "some delta near ATM still works" coverage — the existing suite
only needed ONE fixture value changed (`test_low_conviction_rejects_a_too_close_to_the_
money_delta`'s `delta=0.60` → `0.70`, since 0.60 now falls inside the widened low band).
Revert-checked: reverting to the old band constants makes exactly the three new tests
fail, nothing else.

#### Docs, same commit per `OPTIONS_PLAYBOOK.md`'s own §0 rule

`OPTIONS_PLAYBOOK.md` §0/§2/§3/§4 rewritten to state both profiles explicitly (was
"superseded pending implementation" prose). Take-profit row deliberately LEFT as the
fixed +60% — `PLAN_EXIT_AGENT.md`'s trailing ratchet has not landed; do not describe it
as shipped. `HACKATHON.md` §3/§8 updated from "decided" to "decided AND shipped, same
day", with the exact numbers. `HACKATHON.md` §9's known-good numbers corrected: full
suite is 818/9 (was stale at 757), and — **found while updating this, not something I
went looking for** — the "9 pre-existing ruff errors" figure is wrong. Measured live: a
fresh `uv sync --all-packages` + `ruff check apps/agents apps/api packages/` gives
**252** errors, confirmed via `git stash` to predate this commit entirely (exists on
`main` with zero code changes). 124 of the 252 are `RUF100 unused-noqa`, consistent
with a ruff-version bump (this pulled 0.16.0) making old suppressions redundant — a
plausible cause, **not fully re-verified** (would need to pin the old ruff version and
diff), so stated as a hypothesis in the doc, not a fact. Flagged in `HACKATHON.md`
directly rather than silently trusting the old "9".

**Deliberately did NOT touch** `CLAUDE.md` §7's own stale "757 passing" figure — same
staleness, but that file is outside this task's scope and I was explicitly told the
instruction-boundary around modifying it; flagging here instead of silently editing it.

#### Left open, per the plan's own scope

- [ ] **The take-profit trailing ratchet** (`PLAN_EXIT_AGENT.md`) — not built. The
      `+60%` row in `OPTIONS_PLAYBOOK.md` §3 is still accurate; the ratchet knobs
      (`options_ratchet_enabled`, `options_trail_arm_pct`, `options_trail_giveback_pct`)
      are that workstream's own fields on `RiskCaps` — this diff stayed additive/narrow
      in `types.py` on purpose so the two merge cleanly.
- [ ] Alpaca MCP/CLI integration, candlestick patterns — untouched, other workstreams.
- [ ] Nothing here is deployed. `RISK_PROFILE=aggressive_paper` has to actually be set
      in Railway for any of this to take effect live — that's a deploy/config step, not
      a code change, and it's the user's action.
- [ ] `min_council_confidence`'s "verify it actually binds" caveat from the plan's §0 is
      still unverified — would need `agent_decisions.judge_confidence` from a day of
      real (non-mock) passes on the new account, which does not exist yet.
### 2026-08-30 — e552ab73 feat(options): wire the trailing ratchet into the close path (A.2)

Continuing `PLAN_EXIT_AGENT.md` in the same session as A.1 (previous entry, below).
This is **A.2** — the plan's own build order (§7) puts a deploy-and-watch checkpoint
right after these two, before any LLM code, and explicitly says stopping here to
report back is expected. That's what this entry is.

**What shipped:** the ratchet from A.1 now actually runs. `_exit_reason`'s options
branch calls `_ratchet_outcome_for` (a new glue helper in
`apps/api/app/services/orders/position_manager.py`) instead of `option_exit_signal`
whenever `caps.options_ratchet_enabled` (default True) — flipping that flag back off
reproduces the old flat-threshold behavior exactly, both directions covered by an
explicit test. The high-water mark is real persisted state now: read from
`decision.reasoning["option_exit"]["peak_pl_pct"]` (already on the loaded row, no
extra query) and written back via `jsonb_set` — `COALESCE(reasoning, '{}'::jsonb)` so
a null column doesn't get blanked, and the payload MERGES over whatever already lives
under that key (so a future exit-agent's `consults`/`log` fields, once A.3 exists,
survive a ratchet-only tick) rather than replacing the whole key. Written only when
`RatchetOutcome.peak_advanced` — the plan's own ~10-writes-vs-~800-per-session math.
`manage_positions_for_user` computes the `RatchetOutcome` exactly once per decision
per tick and threads it into both the close decision and the write gate.
`docs/OPTIONS_PLAYBOOK.md` §3/§4/§6 updated in the same commit (this repo's own §0
rule) to describe the ratchet as current, not scheduled.

**Verified, not assumed:**
- 824 passed, 9 skipped (+15 from A.1's 809, zero regressions). `ruff check` clean
  (one real `RUF059` unused-variable catch along the way, fixed). `mypy` on
  `position_manager.py`: 16 errors total, but I checked the baseline properly this
  time by piping `git show HEAD:<path>` (the pre-A.2, pre-A.1-touching-this-file
  version) through mypy directly rather than assuming — **11 were already there**.
  The 5 new ones are the identical two categories already all over this file
  (untyped `decision`-shaped params, `async_sessionmaker`/`dict` missing generic
  args) — matching the file's own established convention, not a new problem. Did
  not attempt a file-wide mypy-strict cleanup; out of scope for this commit.
- **No live Postgres anywhere in this suite** — confirmed by grep before assuming I
  could test jsonb_set's real runtime behavior (`text|create_async_engine|sqlite` etc.
  across every existing test file: zero hits; also confirmed no `docker`, no `psql`,
  no `.venv` even existed in this worktree until I ran `uv sync` myself for the
  baseline check in A.1). So `_option_exit_peak_update_stmt` is tested by asserting
  the compiled SQL text and bound params directly (split from the execute wrapper
  for exactly this reason), not by executing it. jsonb_set's actual behavior against
  a real Postgres is NOT independently verified by me this session — it rests on
  documented Postgres semantics (`jsonb_set(NULL, ...) IS NULL`, hence the COALESCE;
  `create_missing=true` on the 4th positional arg) plus this statement's shape being
  correct, which is exactly what the SQL-text tests pin. **Flagging this explicitly
  as the one place in A.1+A.2 that is reasoned-about rather than executed against
  the real database** — worth a real end-to-end check against a live Postgres
  connection before trusting it in anger, same spirit as the plan's own ⚠️ about
  `unrealized_plpc` timing.
- **Per CLAUDE.md 4.1, actually broke and restored six things, not the usual
  smaller number**, because A.2 has more moving parts than A.1's pure function:
  jsonb_set -> whole-column overwrite (SQL tests failed); dropped COALESCE (failed);
  dropped the existing-state merge in `_persist_option_exit_peak` (failed with a
  `KeyError` on the preserved `consults` field — exactly the failure mode the merge
  exists to prevent); dropped the `peak_advanced` gate in `manage_positions_for_user`
  (failed — both test positions persisted instead of one); forced
  `_ratchet_outcome_for` to ignore the persisted peak, both at its own level and
  through `_exit_reason` (both failed); forced the disabled flag to be bypassed at
  the `_exit_reason` level AND separately at the `_ratchet_outcome_for` level (both
  independently caught — this confirmed the two checks are a real second layer, not
  one masking the other, which I wasn't sure of until I tried breaking each alone).
- **Found and fixed a latent bug in my own test while doing the peak-advanced
  revert-check**, worth recording so nobody re-derives the same confusion: the new
  `test_manage_positions_persists_the_peak_only_when_it_advanced` calls the REAL
  `manage_positions_for_user`, which computes `now = datetime.now(UTC)` — the actual
  wall clock — unlike every other test in this file, which passes the fixed
  `NOW = datetime(2026, 6, 12, ...)` constant straight into `_exit_reason`. My first
  draft used that same fixed constant for the fixture's `triggered_at`, which is
  ~2 months in the past relative to whenever this suite actually runs, so the TIME
  STOP fired regardless of the ratchet and `_close_position` was attempted (and
  errored, harmlessly caught) on bare fixtures that don't have `fill_qty`. The
  test's own assertions still happened to pass either way, but for a confused
  reason. Fixed by setting `triggered_at`/`user_responded_at` to `datetime.now(UTC)
  - timedelta(hours=1)` explicitly. Left a comment on the fixture explaining why,
  since this is exactly the kind of thing that looks like flakiness later.

**One existing test recalibrated, as flagged in the A.1 entry it would need to be:**
`test_premium_take_profit_fires_before_the_time_stop` used pl=72.4% against the old
flat 60% threshold; with the ratchet enabled by default that no longer closes (it
arms the trail and holds — the entire point of this feature). Recalibrated to
pl=160% (above the new 150% hard-take-profit backstop) to keep testing the same
"fires before the time stop" ordering property; added
`test_ratchet_arms_and_holds_instead_of_closing_at_the_old_flat_threshold` to cover
the 72.4% HOLD case explicitly rather than let it go untested. Every other existing
premium-exit test was hand-checked against both code paths (ratchet on/off) and
needed no change — they all produce the identical result either way (stop-loss,
inside-both-thresholds, missing-mark, wrong-occ-key, equity-never-premium-exited).

**Left open, as the plan's own build order calls for:**
- A.3 (the LLM exit agent node, prompt, mock branch, post-filter) and A.4
  (`complete_tools()` + the four read-only tools + the 2-round loop) are unstarted.
  Nothing about A.1+A.2 blocks them — `RatchetOutcome.may_consult` already exists and
  is threaded through, just unread by anything yet.
- The live-Postgres jsonb_set verification named above.
- Per the plan's own §0: whether `unrealized_plpc` on an option position updates
  promptly enough for a 30s loop is still an assumption, not something this session
  could check (no live option position open). Check it Monday with one real
  contract before trusting the trail's timeliness in anger.

### 2026-08-30 — ecc22725 feat(options): trailing ratchet as a sibling exit rule (A.1)

Picking up `PLAN_EXIT_AGENT.md` (queued item #2 in CLAUDE.md). This is **A.1 only** —
the plan's own build order (§7) calls out A.1+A.2 as one shippable, revertible unit
that lands *before* any LLM code, and explicitly says it's fine to stop and report back
at that boundary. This entry is A.1; A.2 (the jsonb_set high-water-mark persistence +
wiring the ratchet into `_exit_reason`) is the next commit, same session.

**What shipped:** `option_ratchet_signal()` in `packages/engine/engine/options/exits.py`
— a pure function, sibling to the existing `option_exit_signal` (untouched, all 10 of
its own tests still green). State machine per the plan's §3 exactly: stop (rule 1) >
hard take-profit backstop (rule 2) > proportional trail (rule 3, `peak * (1 -
giveback_frac)`, only once armed at +35%) > hold. Four new `RiskCaps` fields
(`options_ratchet_enabled` default **True**, `options_trail_arm_pct` 35.0,
`options_trail_giveback_pct` 30.0, `options_hard_take_profit_pct` 150.0), all
env-tunable, all wired into `from_env()`. **Nothing calls the new function yet** —
`_exit_reason` still reads only `option_exit_signal`, so this commit is a pure
no-op behaviorally. Confirmed by the full-suite count not moving beyond the new tests
themselves (see below).

**Verified, not assumed:**
- Baseline reproduced myself before touching anything: `python -m uv sync
  --all-packages` (no `.venv` existed yet in this worktree — first run here), then
  `python -m uv run pytest apps/agents apps/api packages/ -q` -> **792 passed, 9
  skipped**, matching PLAN_EXIT_AGENT.md §0's stated number exactly.
- After: **809 passed, 9 skipped** (+17 new tests, zero regressions).
- `ruff check` on all 4 touched files: clean. `mypy` on both production files (`exits.py`,
  `types.py`): clean.
- `ruff format --check` flags all 4 touched files — but I also ran it against two files
  I did not touch at all (`position_manager.py`, `db/models/council.py`) and it flags
  those identically (multi-line calls it wants collapsed, inline comments it wants
  reformatted). This is pre-existing, repo-wide formatter drift — CLAUDE.md's own
  verification section only lists `ruff check` as the enforced gate, not `ruff format`,
  and this confirms why. Not introduced by this commit.
- **Per CLAUDE.md 4.1, actually broke each of the plan's five named invariants in turn
  and confirmed the matching test failed, then restored:** reordered rules 1/3 (stop
  vs. trail) — `test_stop_wins_over_trail_on_a_gap_through_zero` failed exactly as
  predicted (`option_trail_stop` instead of `option_stop_loss`); dropped the `armed`
  guard on the trail check — `test_trail_does_not_fire_before_arming` failed (spurious
  CLOSE); made `trail_line` read `pl` instead of `peak` —
  `test_ratchet_closes_on_a_peak_retracement` failed (spurious HOLD through a real
  retracement); let `peak` take `pl` directly instead of `max(prior, pl)` —
  `test_peak_is_monotone_across_ticks` failed (peak regressed 40->30); coerced a
  missing mark to `0.0` instead of early-returning —
  `test_no_mark_holds_and_leaves_the_peak_alone` failed, and **worse than I expected**:
  with a 45%-peak already-armed position, treating the missing mark as `0.0` doesn't
  just fabricate a reading, it fires a spurious `option_trail_stop` close (0.0 reads as
  "retraced to flat"). That one surprised me and is the strongest argument in this
  commit for why "no mark, no fabricated data point" has to be an unconditional early
  return rather than a coerce-and-fall-through.
- Proportional-giveback formula checked against the plan's own two worked examples
  (peak +80 -> line +56.0; peak +200 -> line +140.0) — both pass exactly.

**Left open, deliberately, for the next commit (A.2, same session unless I run out of
budget first):**
- The high-water-mark persistence (`agent_decisions.reasoning->option_exit` via
  `jsonb_set`, never a Python read-modify-write — the plan's §4 is explicit about why:
  a whole-column overwrite would silently eat `contract_funnel`/`strategy_fit`).
- Wiring `_exit_reason`/`manage_positions_for_user` so the ratchet actually runs instead
  of `option_exit_signal` — this is where 3 of the existing premium-exit tests in
  `test_position_manager.py` will need real, deliberate updates (not just left alone),
  because `options_ratchet_enabled` defaults **True** and the ratchet's default hard-TP
  (150%) is a materially different ceiling than the old flat one (60%) — a pl of 72.4%
  that used to fire `option_take_profit` will instead arm the trail and HOLD. That is
  the intended behavior change this whole plan exists to make; I'm flagging it here so
  it isn't a surprise in the next diff.
- `docs/OPTIONS_PLAYBOOK.md` §3/§4/§6 updates (the plan asks for the 30%-not-10%
  giveback rationale to be disclosed there) — held for the A.2 commit deliberately,
  since the playbook documents what the code *does*, and the ratchet doesn't actually
  run yet as of this commit. Documenting it now would itself be a CLAUDE.md 4.2 case
  (doc ahead of the code) for the few minutes between these two commits.
- A.3 (the LLM exit agent) and A.4 (the tool harness) are unstarted. Not a regression —
  the plan's own build order puts a deploy-and-watch checkpoint between A.1+A.2 and
  these.

### 2026-08-30 — docs: `HACKATHON.md` §5 truth-up after D.0/D.2 landed

Small follow-up, folded into its own commit rather than widening `40eae29b`. Two
statements in §5 went stale the moment my own prior commits landed:

- *"The early-close / halt awareness already exists in Python and is simply
  unwired"* — false as of `40eae29b`. Struck through, replaced with a note that it's
  wired, citing that sha and the new `clock`/`market_open_source` surface.
- *"The exact clock subcommand below was never verified"* — false as of `1bd33849`'s
  D.0 findings. Replaced with the verified fact: **`alpaca clock`, no `get` suffix** —
  the section's own prior `alpaca clock get` guess was wrong.

Docs-only (`docs/HACKATHON.md`). No code touched, so the suite is unaffected —
last confirmed at 795 passed, 9 skipped in the `40eae29b` entry below, still current.

### 2026-08-30 — `40eae29b` feat(engine,api): wire AlpacaClock into the scanner via an optional clock provider

**D.2 from `docs/PLAN_ALPACA_MCP.md` — the actual functional upgrade** (the CLI in
D.3 is only the eligibility artifact; this is the part that changes behavior).

- **What changed and why:** `engine/features/clock.py::AlpacaClock` (real `/v2/clock`)
  and `clock_from_env()` had zero non-test callers — confirmed by reading the file and
  grepping the repo. `engine/scanner/engine.py:86` called the hardcoded local-calendar
  `is_us_market_open(at)` directly instead, so an unscheduled early close or halt was
  invisible to the scanner. Wired the real clock in behind an optional
  `Scanner.clock: ClockProvider | None = None` field — `scan()`'s new `_market_open()`
  helper asks it when present, falls back to the local calendar otherwise, so every
  pre-existing call site (none of which pass `clock=`) is behaviourally unchanged.
  `ScanResult` gains `market_open_source: str = "local_calendar"`, wired through
  `scanner_status.py` → `schemas/scanner.py`'s `ScannerStatusResponse` alongside the
  `market_open` field it already carries. `clock_from_env()` wired at the real
  construction site, `engine/scanner/select.py::scanner_from_env()` — it reads the same
  `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` that function already requires to construct a
  Scanner at all, so this adds no new precondition.
- **What I VERIFIED, and how:**
  - **Measured the baseline myself, not trusted from a doc**, per the operational
    instruction to do so: `python -m uv run pytest apps/agents apps/api packages/ -q`
    → **792 passed, 9 skipped** — matches `docs/PLAN_ALPACA_MCP.md`'s stated baseline
    and the prior entry below, confirmed independently rather than assumed. This
    worktree had no `.venv` yet; plain Windows `python` (67 collection errors,
    `ModuleNotFoundError: No module named 'app'`) and even a bare
    `python -m uv run pytest` before syncing (same 67 errors, `No module named
    'engine'`) both failed for environment reasons, not code reasons — resolved with
    `python -m uv sync --all-packages` first.
  - **After the change: 795 passed, 9 skipped** — exactly +3, matching the 3 new tests
    (skip count unchanged).
  - **Revert-checked per CLAUDE.md §4.1**: temporarily reverted `_market_open()` to
    ignore `self.clock` (hardcoded the old local-calendar-only behaviour) and ran the
    3 new tests. `test_scanner_reports_market_open_source` and
    `test_scanner_skips_scan_when_injected_clock_reports_closed` both failed with a
    real, legible assertion diff (`'local_calendar' == 'alpaca'` and
    `True is False`) — not vacuously. `test_scanner_uses_the_local_calendar_when_no_
    clock_is_injected` correctly kept passing (that path doesn't touch the clock).
    Restored immediately after.
  - **ruff clean on every touched file.** Two pre-existing findings live in files this
    commit touched — RUF100 (unused `noqa: E402`) ×6 in `test_scanner_route.py`'s
    import block, RUF012 (mutable default) ×1 on `ScanSignalDto.context` in
    `schemas/scanner.py` — both confirmed unchanged via `git stash` / re-run / `stash
    pop`; neither is on a line this commit added.
  - **mypy clean** on all 6 touched source files (not one of the checks
    `docs/HACKATHON.md`/CLAUDE.md's own verification list names as required today,
    run anyway given the new `Protocol` and dataclass fields).
- **Anything left open:** D.3 (CLI subprocess wrapper), D.4 (MCP client), D.5
  (Dockerfile) are the plan's own next steps — explicitly out of this session's scope,
  and the plan says D.3 starts only after D.0-D.2 land, specifically because it needs
  D.0's verified facts (see the entry below). Also flagged, found but deliberately not
  touched (pre-existing, separately tested, out of scope): `Scanner.scan()`'s
  `force=True` path still reports `market_open=True` in the `ScanResult` even when the
  market is actually closed, whenever there are no symbols/no triggers to report on
  (`test_force_bypasses_the_hours_gate` asserts exactly this, unchanged) —
  `market_open_source` is threaded through honestly regardless of that quirk (it
  always reports which source actually answered), but the boolean's forced-bypass
  semantics were not in scope to fix here.

### 2026-08-30 — D.1: resolve the two-session MCP contradiction in `apps/mcp_server/README.md`

- **`docs/HACKATHON.md` §5's two-session table was already deleted** — by the previous
  commit (`7d073a9d`, "four implementation plans"), per its own build-log entry below.
  Verified by reading the file directly: §5 already states the one-read-only-session
  resolution. No change needed there.
- **`apps/mcp_server/README.md` still had it.** A `research`/`execution` two-session
  `ALPACA_TOOLSETS` table, with the `execution` session mounting `trading,account`
  "Yes, and only downstream of `engine.risk`". That directly contradicts this same
  file's own "Will never build" section forty lines below it (which quotes
  `mcp_server/tools.py:9-19` almost verbatim: mounting execution-capable tools into an
  LLM tool loop "would violate this codebase's one architectural rule"). The file
  argued both sides of the same question.
- **Replaced with the one-session resolution**: exactly one Alpaca MCP session, ever,
  read-only, no `trading` toolset mounted anywhere — not gated by policy, not
  downstream of a risk check, not behind a flag. Folded in the D.0-verified
  `ALPACA_TOOLSETS` legal values (see the entry below) so whoever builds D.4 has a
  checked list to choose a read-only subset from, without this doc prescribing that
  subset itself (that's D.4's call, out of this session's scope).
- Docs-only change (`apps/mcp_server/README.md`). Suite unaffected — still 792 passed,
  9 skipped (the baseline verified below, before any D.2 code change).

### 2026-08-30 — D.0 verification gate: Alpaca MCP server + CLI facts, quoted

**`docs/PLAN_ALPACA_MCP.md` §0 is a blocking gate: fetch both pages for real, quote
exact facts into this file, don't infer one tool's surface from the other's
conventions, stop if either page can't be fetched.** Both pages were reachable — gate
passes. Method: live fetches of the actual GitHub READMEs (not from training memory).
The CLI page was fetched twice, independently, with more targeted prompts on the
second pass, specifically to cross-check the two facts the plan flagged as unverified
traps (the clock subcommand, the auth env var prefix) — both passes agreed.

**A. `github.com/alpacahq/alpaca-mcp-server`**

1. **Entry point:** `alpaca-mcp-server`, invoked via `uvx alpaca-mcp-server`. Matches
   `docs/HACKATHON.md`'s existing assertion.
2. **Toolset-scoping env var:** `ALPACA_TOOLSETS` — "Comma-separated list of toolsets
   to enable". Legal values, verbatim: `account`, `trading`, `watchlists`, `assets`,
   `stock-data`, `crypto-data`, `options-data`, `corporate-actions`, `news`,
   `fixed-income-data`, `locates`.
3. **Option tools in the read-only toolsets:** `get_option_chain` ("Full option chain
   for an underlying"), `get_option_snapshot` ("Snapshot with Greeks and IV"), plus
   `get_option_contracts`, `get_option_contract`, `get_option_latest_quote`,
   `get_option_bars`. (`place_option_order` lives in the `trading` toolset — not
   read-only, not quoted verbatim here since D.0 only asked for the read-only tool
   names, but keeping it OUT of the one session D.1 describes is the entire point.)
4. **Transport:** the README does not state a default in so many words. It documents
   `--transport streamable-http` and `--port` as opt-in HTTP flags; the absence of an
   equivalent explicit stdio default statement, plus stdio being the MCP norm for a
   local process launched via `uvx`, is why stdio reads as the default. **Flagged as
   inference-from-omission, not a literal "default: stdio" sentence** — whoever builds
   D.4 should confirm directly against `server.py`'s argument parser rather than
   trusting this secondhand.

**B. `github.com/alpacahq/cli`**

1. **Linux/amd64 release asset naming: not found in the README.** Whoever builds D.5
   needs to check the GitHub Releases page's actual asset list directly.
2. **Clock subcommand — the plan's named trap, and `docs/HACKATHON.md`'s old text was
   wrong:** verbatim from two independent fetches — a Quick Start line `# Check if the
   market is open` / `alpaca clock`, and a Commands list: "Trading: `order`,
   `position`, `option`, `locate`, `clock`, `calendar`". **The command is `alpaca
   clock` — no `get` suffix.** `docs/HACKATHON.md`'s superseded assertion of `alpaca
   clock get` is confirmed wrong; `clock` is a top-level command alongside
   `order`/`position`, not a subcommand of anything else.
3. **JSON output:** verbatim — "API commands return JSON on stdout by default. They
   also support CSV output, inline jq filtering, quiet mode, response schemas, and
   request timeouts." **There is no JSON flag to pass — JSON is already the default**;
   `--csv` is the flag that opts OUT of it. The plan's framing ("the exact JSON-output
   flag") assumed a flag exists; the actual fact is the inverse.
4. **Auth env vars — the plan's other named trap, and it does NOT materialize:**
   verbatim — `export ALPACA_API_KEY=PK...` / `export ALPACA_SECRET_KEY=...`, and
   separately, "Credential lookup uses the first complete credential bundle: 1.
   `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`". **Both fetches agree: only the `ALPACA_`
   prefix appears anywhere in this README.** The `APCA_` prefix the plan predicted
   ("Alpaca's own tooling conventionally uses `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`")
   does not appear at all. The CLI reads the exact same env var names this repo
   already uses (`engine/features/clock.py`'s `clock_from_env()`). **Consequence for
   D.3 (not this session's scope): the subprocess `env=` dict does not need to remap
   names for the CLI** — the trap was genuinely worth checking, and on the CLI
   specifically, it turned out not to apply. (Not tested: whether the MCP server side
   uses the same convention — D.0 didn't ask for that fact and D.4 is out of scope
   here.)

**Gate result: both pages fetched successfully. Proceeding to D.1/D.2.**

### 2026-08-30 — four implementation plans, written for the next model

**Account blocker cleared.** New paper account `PA3IAZI74E5R`, **options Level 3**
(covered calls, CSPs, long calls/puts, spreads, straddles, multi-leg). Keys going to
Railway. Phase A only needs Level 2, so we have headroom.

**This commit is docs only. No code changed. Suite still 792 passed, 9 skipped.**
The user is near their session limit and is handing execution over — these four plans
are written to be executed without access to that conversation.

| Plan | Priority |
|---|---|
| [`docs/PLAN_AGGRESSIVE_PROFILE.md`](docs/PLAN_AGGRESSIVE_PROFILE.md) | 1 — changes outcomes; delta band frozen after Mon open |
| [`docs/PLAN_EXIT_AGENT.md`](docs/PLAN_EXIT_AGENT.md) | 2 — ratchet ships without any LLM |
| [`docs/PLAN_ALPACA_MCP.md`](docs/PLAN_ALPACA_MCP.md) | 3 — **eligibility**, starts with a blocking spec-verification gate |
| [`docs/PLAN_CANDLE_PATTERNS.md`](docs/PLAN_CANDLE_PATTERNS.md) | 4 — greenfield |

#### Things I measured that you should not re-derive

```
best_strategy({})  ->  rsi_mean_reversion, score 0.60, tradable=True
blind_weight_fraction:  vol_regime_switch 0.400 · breakout 0.350 · sma/rsi 0.150 · momentum 0.000
```

- **An empty feature dict is tradable at 0.60**, not 0.50 — `not_a_trend_break` reads
  `trend_regime != "downtrend"` and the missing-value sentinel `"unknown"` satisfies that
  as a genuine TRUE. **Raising `MIN_FIT_TO_TRADE` does not fix it.** A data outage
  currently spends 5 LLM calls and can originate a trade.
- **`MIN_FIT_TO_TRADE` has a hard floor of 0.41.** `vol_regime_switch`'s blind fraction is
  exactly 0.400 — at a floor of 0.40 it clears the trade gate on direction-blind checks
  alone.
- **`fit.py:93` claims a test asserts this. That test does not exist.** There is no
  `test_fit.py` anywhere in the repo. CLAUDE.md §4.2 case, and it is the only thing that
  would catch a collision between the aggressive-profile and candlestick workstreams.
  **Write it first.**
- **`engine/features/clock.py::AlpacaClock` already calls `/v2/clock` and is unwired** —
  `clock_from_env()` has zero non-test callers. The early-close/halt awareness
  `HACKATHON.md` §5 promised from the Alpaca CLI has been sitting in the repo the whole
  time. The CLI is the eligibility artifact; wiring that clock is the actual upgrade.
- **At a 1% premium cap on $100k, any contract priced over $10.00 floors to zero
  contracts and HOLDs** (`qty = floor(budget / (ask × 100))`, never rounds up). The cap
  was silently refusing whole price bands, and because the sizer emits a HOLD rather than
  a veto, that refusal never reached the Refusal Ledger either.

#### Corrections to our own docs, made in this commit

- **`README.md:63-64` claimed the Alpaca MCP server and CLI in the present tense**, in the
  judge-facing file, and neither is shipped. I wrote that last session describing intent as
  fact. Now marked planned. **If you catch me doing that again, fix it the same way.**
- **`HACKATHON.md` §5 said the market-hours gate is `pandas_market_calendars` in
  `daily_cron.main`. It is not there.** Real path:
  `market_calendar.is_us_market_open` → `scanner/engine.py:86` → `scheduler.py:317`.
  Corrected — do not go hunting for the code the old text described.
- **`HACKATHON.md` §5's two-session MCP table is deleted.** It proposed an `execution`
  session holding the `trading` toolset, which `mcp_server/tools.py:9-19` correctly says
  would violate the architecture rule. Read-only-only is also the stronger claim: a
  capability boundary with no exception beats one with a carve-out.
- **`HACKATHON.md` §3 and §8's "do not raise the caps"** now record the user's override,
  dated.
- **`OPTIONS_PLAYBOOK.md`** carries a superseded-pending-implementation banner rather than
  contradicting prose.

#### The one design decision worth arguing about

The user asked for the LLM to have "tools and harnesses to execute" an option close. The
plan gives it **monotone authority: it can only close EARLIER, never hold longer, never
move the trail, never place an order.** Deterministic code still executes.

The consequence people get backwards: **the fail-safe on error/timeout is `TRAIL`, not
`CLOSE`.** Closing on an API timeout means an Anthropic outage caused a trade. Because the
agent can only ever close earlier, fail-open is structurally impossible — the trail, hard
stop, time stop and expiry sweep run whatever it says. Do not "harden" this by flipping it.

#### Open, in the order they bite

- [ ] **Nothing from these plans is live.** Monday's open runs the current conservative
      caps and the fixed +60% take-profit.
- [ ] **The delta band is frozen after Monday's open** (`HACKATHON.md` §8). Pre-open or
      accept a mid-contest change.
- [ ] **The reconciler must tick once before the first options pass** —
      `_cold_boot_fallback` leaves `options_trading_level` as `None` and
      `options_level_insufficient` vetoes every entry. Every cold start, not just Monday.
- [ ] Alpaca MCP/CLI not started. Eligibility.
- [ ] No UI renders the contract funnel or the trim rows yet; both endpoints exist.

### 2026-08-30 — `750100a9`+`1c04b194`+`c410f1c0`+`7684c21b`+`0de733c6` The Refusal Ledger, end to end

Sunday's block: made the ledger actually work for OPTIONS, and gave an open
option a price-based way out. Five commits, all on `main`, all `ID:MODEL1REAL`.

**`750100a9` — option ghosts are marked on the CONTRACT.** The hero feature had
never once included an options refusal. Three independent reasons: `ghost_eval`
marked every ghost through the *stock* bars endpoint keyed on `row.symbol` (the
underlying — a different question, and an OCC symbol through that endpoint 400s
with "invalid symbol"); `_ghost_pnl` had no multiplier so every options ghost
was 100x too small; and status only went "final" when the horizon's LAST trading
day happened to print a bar, which for a sparse option strike may never happen.
New `engine/prices/option_alpaca.py` + `get_option_price_provider()`.

**VERIFIED LIVE, and this one is worth knowing about:** `/v1beta1/options/bars`
with `end` inside the 15-minute delay window returns **403 "OPRA agreement is
not signed"**. The message is a lie — nothing is unsigned. Same keys, same
contract, seconds apart:

```
end=2026-08-30T23:59:59Z -> 403 "OPRA agreement is not signed"
end=2026-08-29T00:00:00Z -> 200, bars returned
```

The evaluator always marks up to `today`, so unclamped this 403s on every single
pass. `end` is clamped to `now-20min`. If you see that error anywhere else in
this codebase, check the request window before you check the account.

Also verified live: option daily bars are **sparse**. A 3-contract SPY request
over 10 days returned bars for 2 contracts on 3 days total. Do not write code
that assumes a dense series.

**`1c04b194` — trims are attributed; the contract funnel is persisted.** Both
risk engines were discarding `trim_d.veto_rule` and keeping only an anonymous
`trimmed:10->4` flag. `RiskDecision.trim_rules` now carries the name. And
`select_contract`'s funnel — where MOST refusals happen — was `logger.info`'d
and dropped, because a HOLD writes `proposal = None` and there was nowhere to
put it. **That is the mechanical cause of "it just says HOLD with no
explanation" on an options pass.** Now in `reasoning.contract_funnel` (JSONB,
zero schema change), on the approved path too. `run_council` returns `reasoning`.

**`c410f1c0` — options finally have a price exit.** Before this an open option
had only the calendar time stop, a signal exit, and the `dte<=2` sweep. **None
is a price rule.** Running unattended Mon-Thu, a call up 90% Tuesday and back to
zero Thursday hits none of them. This is not fixable with a bracket: Alpaca
cannot bracket a single-leg option at all (`OrderClass` allows only simple/mleg
for us_option), so the protection every equity entry gets from the broker is
structurally unavailable and has to live in our sweep. `engine/options/exits.py`,
+60% / -50% on the PREMIUM, checked BEFORE the time stop. Env-tunable
(`OPTIONS_TAKE_PROFIT_PCT` / `OPTIONS_STOP_LOSS_PCT`) while the premium caps
stay code-level — an exit threshold cannot increase max loss beyond premium
already paid; a cap can.

**`7684c21b` — `docs/OPTIONS_PLAYBOOK.md`.** Every rule the options side plays
by, derived from the code. Writing it immediately caught that `selection.py`'s
own module docstring had drifted from its constants on FIVE numbers (DTE window,
risk guardrail, both delta bands, volume floor, spread ceiling) — all changed in
an earlier commit with the prose left behind. Corrected. **Read §5 (traps)
before touching options code.**

**`0de733c6` — trims surface on `GET /api/v1/risk/vetoes`** as a separate
`trims[]` list, never summed into `totalVetoes`.

**Verified:** full suite **792 passed, 9 skipped** (was 757 at session start).
Every new test reverted-checked — break the fix, confirm the test fails, restore.
ruff clean on all touched files; the 2 pre-existing B008 in `insights.py`
confirmed unchanged via `git stash`.

**Still open, and the first two are blockers:**
- [ ] **Fresh Alpaca paper account, $100k, `options_trading_level >= 2`.** User
      action. Gates everything. A new account can sit at level 0 until the
      options agreement is accepted and approval is not instant.
- [ ] **The reconciler must tick once before the first options pass.**
      `postgres_context._cold_boot_fallback` does not set
      `options_trading_level`, so it defaults to `None` and
      `options_level_insufficient` vetoes every entry. Hard ordering constraint
      every cold start, not just Monday.
- [ ] **P0.5: Alpaca's own MCP server / CLI — this is ELIGIBILITY, not polish.**
      Not started. `docs/HACKATHON.md` §5 has the design.
- [ ] UI: nothing renders the contract funnel or the trim rows yet. The data and
      the endpoints exist. Highest demo-value-per-hour item remaining.
- [ ] Ghost marks for options are untested against a REAL refused option — there
      has never been one. The code path is verified; the end-to-end is not.

### 2026-08-29 — `add7346a` docs: model handover protocol + hackathon brief + MCP correction

Process change, not a code change. Two models alternate on this repo across
5-hour limits on separate accounts and cannot see each other's
conversations; the user was re-explaining context at every handover.

- **Commit identity trailer is now mandatory**: `ID:MODEL1REAL` (Opus) or
  `ID:MODEL2OFF` (Sonnet / anything else). Unsure ⇒ MODEL2OFF.
- **`CLAUDE.md` rewritten** around handover (§0) and a new engineering
  standard (§4) derived entirely from bugs that shipped past a green suite
  here: revert-test your tests, don't trust docstrings, measure against the
  live API, watch for one threshold in two authoritative places, and state
  what you verified vs. what you believe.
- **`docs/HACKATHON.md` (new)** — deadline, the four hard eligibility
  requirements, the Refusal Ledger positioning and why the obvious framing is
  already claimed by five competitors, and a do-not-do list.
- **`README.md` (new, root)** — judge-facing. Honest limitations section.
- **MCP record corrected** in `apps/mcp_server/README.md`. Its argument that
  Alpaca's MCP server would violate propose/dispose rested on a false premise:
  `ALPACA_TOOLSETS` allows mounting market-data tools with no `trading`
  toolset, which strengthens the boundary rather than weakening it. Keep our
  server; it just doesn't satisfy the requirement alone.
- Documented `ALLOW_OPTIONS`/`ALLOW_SHORTS` + the three tunable options floors
  in `docs/README.md`; marked `docs/OPTIONS_PLAN.md` partly superseded (it
  still said "proposal, not built" and cited a nonexistent `OPTIONS_ENABLED`).

**Verified:** 757 passed, 9 skipped; all cross-doc links resolve.
**Left open:** unchanged from the entry below — fresh paper account +
options level ≥ 2, reconciler tick ordering, Alpaca MCP/CLI integration,
ghost-marking options against option bars.

### 2026-08-29 — `0b824cbb`+`64979a8c`+`56a06779` fix(options): the three blockers that made options unreachable

Repositioning for the Alpaca AI Trading Agents hackathon (deadline Sep 4,
11:00 ET). Options are a hard requirement there, and the audit found the
options track was fully built but had **never once produced a tradeable
proposal**. Three independent blockers, each of which alone was fatal.

**1. `0b824cbb` — the premium was the underlying's share price.**
`risk_officer.py` passed `context.last_price` (~$229 for NVDA) as
`RiskProposal.last_price`. `max_premium_pct` computes
`premium_per_contract = last_price * multiplier`, so a $229 stock became a
$22,900 "premium" and every options proposal was vetoed at "68.71% of
equity (cap 1.00%)" — real premium was 0.96%. For $100k equity this vetoed
any underlying above ~$10. The executor's own `_option_risk_proposal`
already documents the exact trap and does it right; the council-side node
did the equivalent wrong thing with nothing pinning it. Same class of bug
on the trim path's `estimated_notional` (multiplier dropped, 100x
understated — and that figure feeds `blocked_notional` on the veto ledger).

Also in this commit, the liquidity gate: `ContractQuote.volume` is
populated from the snapshot's LAST TRADE SIZE, because alpaca-py's
`OptionsSnapshot` model drops the `dailyBar` block carrying real volume
(`SNAPSHOT_MAPPING` renames it but the pydantic model has no such field —
verified at runtime). **Measured against the live SPY chain, a floor of 10
rejected 16 of 18 contracts that had already cleared DTE, delta and IV.**
Floor drops to 1; open interest (real, from `/v2/options/contracts`,
populated 90/100 with values 137-722) carries the liquidity judgment. Both
call sites now guard on `> 0` so the gate is switchable off — without that
a floor of 0 still hard-failed on a None volume. Fixed in `selection`,
`rules/illiquid_contract` AND `RiskCaps`; fixing one leaves the others
vetoing a layer later.

Sized for a 4-session window: delta bands now OVERLAP (they were disjoint
at 0.45, so delta-0.50 — at the money, deepest OI, tightest book — needed
conviction > 0.7, which the drafter rarely clears); DTE floor 21 -> 10;
spread cap 8% -> 12%. `RiskCaps.from_env` gains env overrides for the three
DATA-QUALITY floors only — the LOSS limits stay code-level per that
method's own stated principle.

**2. `64979a8c` — the broker was addressed by the underlying.**
An approved options order went to Alpaca as an EQUITY order on the
underlying at the option's premium price, and the
`OptionBracketNotSupportedError` guard silently no-opped because
`OccSymbol.try_parse("AAPL")` is None. The close path was worse: it matched
the held broker position on the underlying, but Alpaca keys option
positions by OCC — so an agent-managed option could **never be closed at
all**, by time-stop, signal, or the expiry sweep.

`runtime._to_proposal_dto` was already correct (`symbol`=underlying,
`occSymbol`=contract). Added `_wire_symbol_for()` as the single decider and
a `wire_symbol` local in `_close_position`. Domain code keeps the
underlying everywhere — the cron's per-(user, symbol, day) dedup, ghost
marking against daily closes, and every UI list read it, and an OCC string
breaks each differently.

**Why no test caught either:** both fixtures set `symbol` to the OCC
string, so the two fields were indistinguishable and the wrong one still
produced the right value. Corrected to the real convention and added seam
assertions on entry and close, each verified to FAIL when the fix is
reverted.

**3. `56a06779` — options were unreachable from every automated path.**
`instrument_preference` had one production caller (`/agent/run`) and no
client ever sent it. The scheduler routes through `daily_cron.main`, which
never passed it, so no scheduled or scanner-triggered pass could produce an
option. Meanwhile `user_watchlist.asset_class` had been persisted, migrated
and UI-toggleable from the start — and never read back.
`_load_user_watchlist` now returns `(symbol, asset_class)` pairs and
`main` takes an optional `instrument_by_symbol` mapping, threaded as a side
mapping keyed by symbol exactly like `scan_context` so no existing
`list[str]` caller breaks. `scheduler._watchlist()` split into
`_watchlist_with_instruments()` + a symbols-only wrapper.

**Also un-vacuumed the capstone test.**
`test_run_council_options_proposal_reaches_evaluate_option_and_is_approved`
had a HOLD escape hatch that returned early, so its approval assertions
never ran — that is precisely what hid blocker 1. Its justification ("the
fit node can legitimately HOLD") was false: `_hash_seed` keys on the symbol
only, so "NVDA" deterministically yields fit 0.787. Hatch removed, plus an
assertion on the premium arithmetic itself. Verified to fail when blocker 1
is reintroduced.

**Verified live (Sat, market closed, real paper keys):**
- Selection funnel now selects on every symbol tried:
  `SPY 4128 -> 2064 calls -> 1843 DTE -> 130 delta -> 3 liquid -> OK`;
  NVDA and AAPL likewise. Before, the volume proxy took this to zero.
- Full council end-to-end against the REAL Alpaca chain:
  `BUY NVDA260909C00225000, 4 @ $2.17 = $868 = 0.868% of $100k,
  15 risk rules passed, approved`.

757 passed, 9 skipped. Ruff clean on touched files.

**Left open:** fresh hackathon paper account (reused accounts are
ineligible); Alpaca's own MCP server / CLI integration (a hard eligibility
requirement — our `apps/mcp_server` exposes us TO Claude, which is the
opposite direction); ghost-marking options against option bars rather than
equity closes; `_cold_boot_fallback` does not set `options_trading_level`,
so the reconciler must tick once before the first options pass or
`options_level_insufficient` vetoes everything.

### 2026-08-28 — `ba2fbbf1` test(broker): cover get_options_trading_level against the real Alpaca account field

Part 3 of the options "actually works" plan: audited the rest of the
options test suite for the same blind-spot pattern the chain-fetch bug
exhibited (idealized mocks masking a real integration seam), rather than
assuming it was the only instance.

**Clean, no changes needed** (read fully, not skimmed):
- `packages/engine/tests/test_options_risk.py` — builds `OptionLegDetails`
  directly (our own internal shape, via `to_risk_proposal`, the one
  sanctioned constructor) and runs it through the real
  `engine.risk.evaluate()`. No Alpaca/broker boundary anywhere in this
  file; its `monkeypatch` calls are all structural spies on the engine's
  OWN dispatch module, not external-shape mocking.
- `packages/engine/tests/test_risk_context_options.py` — either pure
  provider-default logic, or `_parse_positions` reading OUR OWN persisted
  Postgres row shape (plain dicts we designed, not an external API's
  shape a mismatch could sneak into).
- `packages/engine/tests/test_reconciler.py`'s options tests — correctly
  mock at the `BrokerInterface` abstraction (a `MagicMock` broker
  returning this repo's own `broker.types.Position`), which is the RIGHT
  seam for testing `AlpacaBrokerPoller`'s own orchestration logic. The
  actual Alpaca-shape mapping lives one layer down, in `AlpacaBroker`
  itself — correctly out of this file's job to cover.

**One adjacent gap found and fixed, in the same spirit but not literally
one of the three named files**: reading `test_reconciler.py`'s own
`get_options_trading_level` test led to checking whether the underlying
broker method it mocks around had ANY coverage of its own — it had zero,
anywhere in `packages/broker`. Confirmed via the real installed
`alpaca-py` model (`TradeAccount.model_fields`) that
`options_trading_level` is a genuinely real, correctly-named field (also
independently corroborated by `docs/OPTIONS_PLAN.md` §0's own live
account measurement) — so, unlike the chain-fetch bug, this closes a
coverage gap rather than fixing an active bug; the existing
`getattr(acct, "options_trading_level", None)` is the CORRECT choice here
(the field is genuinely absent on an account that never applied for
options approval — a real account state, not a shape mismatch to guard
against).

Had to use a different patching seam than every other test in this file:
`get_options_trading_level` reads `self._client` — already constructed
for real in `AlpacaBroker.__init__` — not a fresh per-call client like
`lookup_asset`/`list_option_contracts`/`list_option_chain_quotes`. The
first attempt patched the `TradingClient` class (this package's usual
convention) and the test made a REAL network call and failed with a real
`401 unauthorized` — caught immediately by actually running the test
rather than assuming the patch took effect. Fixed by swapping
`broker._client` directly with a fake instance.

New tests (`packages/broker/tests/test_alpaca_options.py`, +2): reads a
real value, and `None` on an account with the field genuinely absent.
Verified: full broker suite **45 passed**; `ruff check`/`ruff format
--check`/`mypy` clean.

### 2026-08-28 — `d3d5190b` feat(engine,agents): thread realized_vol_pct into contract selection — iv_outside_plausible_band stage

Part 2 of the options "actually picks up good trades" plan (Part 1 —
the chain-fetch inertness fix — is the three entries below this one).
`engine/options/selection.py`'s own docstring had admitted its
`iv_present` stage only implements half of `docs/OPTIONS_PLAN.md` §2.2
point 4 — null-IV rejection, not "or outside a plausible band vs the
underlying's own realised vol." Buying rich IV into a quiet underlying is
a bad trade even when the direction is right, and the missing input
(`realized_vol_pct`) turned out to already be computed by
`engine.features.quant.compute_quant` — it just wasn't threaded through.

New sixth stage, `iv_realized_vol_band`, appended *after* `iv_present` so
the existing `"no_iv"` reason and every test pinned to it are untouched.
`ContractSelectionInputs.realized_vol_pct: float | None = None` — missing
is a neutral pass (a fact about the analysis environment, not the
contract), unlike `iv_present`'s own stricter handling of a genuinely
missing IV.

**The unit landmine**, stated loudly in the module docstring and pinned
by the FIRST test written for this stage before any boundary case:
`ContractQuote.implied_volatility` is a decimal fraction (`0.28`) while
`realized_vol_pct` is already in percent units (`25.0`, confirmed from
existing fixtures) — the comparison multiplies IV by `100.0` first.
Getting this backwards would make every real contract look ~100x
mispriced and silently re-disable the stage the same way the chain-fetch
bug did, just one filter later — exactly the class of mistake this
session has been hunting all day. Band multipliers (0.3x floor, 3.0x
ceiling) are provisional judgment calls, not derived from data, named as
such in the code — reasonable to ship as-is since this stays paper-only
for the foreseeable future, but flagged for anyone revisiting before real
capital depends on it.

Threaded in `drafter._draft_option_proposal`: `ctx.get("quant",
{}).get("realized_vol_pct")` — **note, called out explicitly because it's
an easy place to get wrong**: this lives under `ctx["quant"]`, a
DIFFERENT dict from `ctx["options_context"]` (which only carries
`days_to_earnings`/`iv_rank`/`atm_iv`/etc.) — the Plan-agent design pass
caught this exact mix-up in my own framing before any code was written.

Tests (`packages/engine/tests/test_options_selection.py`, +6): the unit
consistency check first (`iv=0.25, realized_vol_pct=25.0` → ratio 1.0 →
passes), too-rich rejection, too-cheap rejection, `None` and `<=0`
realized-vol neutral-pass cases. One pre-existing exact-funnel-dict
assertion (`test_funnel_counts_reported_for_a_full_ladder`) updated to
include the new stage key — a real, expected update given a stage was
added, not a masked regression. Verified: full engine suite **292
passed**, `apps/agents` options-drafter suite still green
(`realized_vol_pct` absent in its fixtures → neutral pass, unaffected);
`ruff check`/`mypy` clean on all three touched files (one single-line
pre-existing mypy error in `drafter.py`, confirmed via the diff hunks to
be untouched by this or any of today's other commits).

### 2026-08-28 — `cbe4bec6` fix(agents): drafter's options chain fetch now calls the real endpoint instead of a phantom one

**Layer 3 of 3 — options trading is no longer inert.** This is the
behavior-changing commit: `trading_agents.nodes.drafter._fetch_option_candidates`
now delegates to `engine.options.contracts.fetch_option_candidates`
(layer 2, which calls `broker.alpaca.list_option_chain_quotes` — layer 1,
the correct chain-SNAPSHOT endpoint) instead of the broken direct call to
`list_option_contracts` with a made-up field-mapping adapter
(`_to_contract_quote`, deleted). See the two build-log entries immediately
below for the full root-cause story; short version: the old code called
the wrong Alpaca client entirely and read attribute names that don't
exist on the real response, so every real options run silently HELD with
`no_candidates` forever, regardless of signal quality — invisible across
736 passing tests because none of them exercised a real Alpaca shape.

**The new test that actually proves this** —
`test_drafter_options_path_end_to_end_through_real_alpaca_shapes` in
`apps/agents/tests/test_options_drafter.py` — is the one every other test
in that file (and the ones removed today) could not be: it does **not**
monkeypatch `_fetch_option_candidates`. It patches only the two real
Alpaca SDK client classes (`OptionHistoricalDataClient`, `TradingClient`)
with realistic fixtures built via `model_construct` on the real pydantic
models, and drives the actual, unmocked `_fetch_option_candidates ->
engine.options.contracts.fetch_option_candidates -> broker.alpaca` path.
Asserts a real `BUY` proposal comes out of `drafter_node` with the
correct `occ_symbol`/`contract_type`/`strike`/`bid`/`ask`/
`implied_volatility`/`open_interest`/`volume` — everything that
`OccSymbol.try_parse` and the two merged Alpaca calls were supposed to
produce, genuinely produced. (Note: `delta` isn't asserted here — it's
used only transiently by `select_contract`'s delta-band filter and was
never a field on the persisted `OptionLegDetails`/proposal; confirmed by
reading that dataclass directly rather than assuming.)

Every pre-existing test in this file is unchanged and still passes —
they're valid, focused coverage of `select_contract`'s own logic in
isolation; they just stop being the *only* coverage of the Alpaca
boundary. Verified: full `apps/agents` suite 95→**96** (95 passed + 1
skipped), `ruff check` clean, `mypy` delta zero on the touched region
(the file already carried pre-existing, unrelated mypy debt — confirmed
via the actual diff hunks that none of the reported errors fall inside
lines this commit touched).

**Options trading Phase A can now genuinely reach a real Alpaca account.**
Next up per the approved plan: the realized-vol-vs-IV sanity check
(`docs/OPTIONS_PLAN.md` §2.2's deferred second half), then an audit pass
over the remaining options test files for the same blind-spot pattern.

### 2026-08-28 — `c6ebc324` feat(engine): add options/contracts.fetch_option_candidates — chain fetch + OI merge + ContractQuote mapping

Layer 2 of 3 fixing the options chain-fetch inertness bug (see the layer-1
entry immediately below for the full root cause). `engine/options/
contracts.py`'s own module docstring had promised "chain fetch +
normalise" (`docs/OPTIONS_PLAN.md` §2.1) since this package was first
built, but it never actually landed there — the real (broken) fetch lived
directly in `trading_agents.nodes.drafter` instead. Now it does, correctly:

- New `fetch_option_candidates(underlying_symbol, *, api_key, secret_key,
  now, caps=None)` calls `broker.alpaca.list_option_chain_quotes` (layer
  1) and the existing, unchanged `list_option_contracts` concurrently
  (`asyncio.gather`), merging `open_interest` from the latter into
  candidates from the former by exact OCC symbol string.
- **The merge is necessary, not a nice-to-have**: confirmed by reading
  `engine.options.selection._passes_liquidity` — it hard-fails on
  `open_interest is None` (deliberately: "can't prove liquidity you can't
  see"). Leaving OI unset would silently relocate the exact inertness bug
  layer 1 fixes one stage downstream, under a different rejection reason
  (`no_liquid_contract` instead of `no_candidates`) — same practical
  effect, harder to notice.
- `ContractQuote.volume` is populated from `ChainQuote.last_trade_size` —
  the size of the single last trade, not cumulative daily volume; no
  field on either Alpaca endpoint used here reports the latter. A real
  but documented-imperfect liquidity proxy, flagged as a named follow-up
  (true daily volume would need a third, per-contract bars call — real
  scope creep for this fix).
- Both calls windowed to `RiskCaps.options_min_dte`/`options_max_dte` —
  deliberately the wide, authoritative bound, not `selection.py`'s own
  narrower 21-45 DTE heuristic (that module has its own documented reason
  not to import `RiskCaps` for its selection-only window — this keeps
  both reasons intact rather than merging them into one constant).
- Reused this file's own existing `contract_type_of()` helper for the
  OCC-parsed `str` → `Literal["call","put"]` narrowing — zero new casting
  code needed.

Tests (`packages/engine/tests/test_options_contracts.py`, 7 new): OI
merge-by-symbol correctness, a chain symbol with no metadata match (or an
unparseable/`None` OI value) → `open_interest=None` (fails closed, not a
default), the DTE window's actual date arithmetic reaching both calls
correctly, empty chain → empty tuple, and — the one that pins a
deliberate design choice — a raised exception from the chain call
propagates uncaught here rather than being swallowed a second time (the
drafter's own catch, one layer up, is what turns it into a HOLD).
Verified: full engine suite 280→**287 passed**; `ruff check`/`ruff format
--check`/`mypy` clean on both modified files.

**Still net-new and unwired** — nothing calls this function yet. Layer 3
(next commit) rewires the drafter to actually use it, which is the
behavior-changing commit that flips the feature from inert to live.

### 2026-08-28 — `5b10d0d3` feat(broker): add list_option_chain_quotes wrapping the real Alpaca chain-snapshot endpoint

**Root-cause finding: options trading Phase A is architecturally complete
(736 tests passing) and 100% inert against a real Alpaca account.** The
user asked to make options trading "actually work and pick up good
trades" and to re-check test accuracy — investigating found exactly why
it doesn't, verified against the real code and the installed
`alpaca-py==0.43.4` SDK, not assumed:

- `docs/OPTIONS_PLAN.md` §0 specifies and live-verified (2026-08-26, real
  paper keys) the chain-SNAPSHOT endpoint
  (`/v1beta1/options/snapshots/{underlying}`,
  `OptionHistoricalDataClient.get_option_chain`) — bid/ask/greeks/IV
  bundled per contract.
- What the options Broker/Risk track actually built
  (`list_option_contracts` in `broker/alpaca.py`) wraps a **different**
  Alpaca client entirely: `TradingClient.get_option_contracts`
  (`/v2/options/contracts`), contract *metadata only*. Confirmed directly
  against the real `alpaca.trading.models.OptionContract` class: no bid,
  ask, delta, implied_volatility, or volume field exists on it at all.
- The consumer (`drafter._fetch_option_candidates`/`_to_contract_quote`)
  also passed a bare `str` where the function requires `list[str]`, and
  read attribute names (`occ_symbol`, `bid`, `ask`, `delta`,
  `implied_volatility`) that don't exist on the real model via
  `getattr(..., None)` — every real contract silently became `None` and
  was filtered out. Net effect: `select_contract` always saw zero
  candidates, every options proposal HOLDs with `no_candidates`
  regardless of signal quality, unconditionally.
- **Nothing caught this.** Grepped the whole repo: zero tests exercised
  `list_option_contracts`, `_fetch_option_candidates`, or
  `_to_contract_quote` — every options test monkeypatches the chain-fetch
  directly with hand-built, idealized data. This is the concrete shape of
  "tests that aren't accurate": not wrong assertions, a suite that never
  touches the one seam that talks to Alpaca.

A dedicated Plan-agent design pass (Phase 2 of plan mode) independently
re-verified all of the above against the real installed SDK models
(`OptionsSnapshot`/`Quote`/`OptionsGreeks`/`Trade` — confirmed via
`.model_fields`) before any code was written, and caught two things this
session's own framing had wrong: `realized_vol_pct` lives in
`ctx["quant"]`, not `ctx["options_context"]`; and comparing IV (a decimal
fraction) against `realized_vol_pct` (already in percent units) needs an
explicit ×100 conversion or a later stage would silently re-break
everything a third time. Full design in
`C:\Users\amogpatil\.claude\plans\prancy-meandering-rainbow.md`.

**This commit is layer 1 of 3** (net-new, unwired, zero risk to anything
running today): `ChainQuote` NamedTuple + `list_option_chain_quotes()` in
`packages/broker/broker/alpaca.py`, calling the correct
`OptionHistoricalDataClient.get_option_chain`. Maps the real response
using **real attribute access, never `getattr`-with-a-default** — a
future SDK field rename must raise loudly in a test, not silently
degrade a filter stage until every candidate vanishes three layers away.
`contract_type`/`strike`/`expiry` are parsed from the OCC symbol via the
existing `OccSymbol.try_parse()` (confirmed: `OptionsSnapshot` has no
separate fields for these). New `ALPACA_OPTIONS_FEED` env var
(`opra`/`indicative`), fails closed to `INDICATIVE` — the free Basic tier
every account already has.

Tests (`packages/broker/tests/test_alpaca_options_chain.py`, 9 new) build
fixtures from the REAL alpaca-py pydantic models via `model_construct`
(a genuine instance of the real class, not a loose dict) — including the
exact contract `docs/OPTIONS_PLAN.md` §0 measured live
(`AAPL260828P00305000`, δ -0.2790, IV 0.2644). Covers: real-fixture field
mapping, `greeks=None`/`latest_quote=None`/`latest_trade=None` (deep-ITM
per the plan doc's own observed fact), an unparseable OCC key skipped
without sinking the batch, broker failure → `[]`, default/overridden feed
tier, expiry-window pass-through. Verified: full broker suite 34→**43
passed**, `ruff check`/`ruff format --check`/`mypy` clean (4 pre-existing
mypy errors elsewhere in the file, unchanged).

**Left open, by design — layers 2 and 3 land next**: this function has no
caller yet. `open_interest` isn't on this endpoint at all (only on
`list_option_contracts`, which stays unchanged) — merging the two is
`engine/options/contracts.py`'s job next. There is no cumulative
daily-volume field on either endpoint; the plan's chosen proxy
(`latest_trade.size`, a real but imperfect single-trade size) lands with
that same next commit.

### 2026-08-28 — `8e6f98a3`+`a46756ad`+`19c54133`+`0f3728ba`+`7f6aa413`+`d727bdf1` feat(mcp_server): read/propose-only MCP server for the Alpaca hackathon

New workspace member `apps/mcp_server/` — six MCP tools, every one a thin
adapter over an already-existing service-layer function, built for
lablab.ai's "Alpaca AI Trading Agents Hackathon" (required tech: Alpaca's
Trading API, MCP server, CLI). Per the user's explicit decision, this
wraps THIS APP'S OWN safe pipeline rather than Alpaca's own MCP server
(which exposes real order-placement tools directly to an LLM) — the
actual differentiator, not a compliance checkbox.

**Tools**: `run_council_pass` (the centerpiece — runs the real
deterministic council for a symbol and returns the full rationale;
never executes or auto-approves), `list_positions`, `list_recent_decisions`,
`get_scanner_status`, `get_veto_ledger` (the "here's why the risk gate
said no" showcase — surfaces named `veto_rule` identifiers directly),
`list_watchlist`. **Will never build, stated prominently in the code and
README**: `place_order`, `approve_proposal`, `execute_trade`,
`cancel_order`, `close_position` — nothing in this package reaches
`packages/engine/risk` → `packages/broker`. Confirmed by reading every
tool: zero broker imports, zero execution calls.

**A real SDK-version finding, not an assumption**: the design brief
(researched a day earlier) assumed `mcp.server.fastmcp.FastMCP`. The
version `uv add "mcp[cli]"` actually resolves — confirmed independently
in my own environment too, not just the subagent's — is `mcp==2.1.1`,
which no longer ships that module at all; it points at
`mcp.server.mcpserver.MCPServer` as the v2 replacement. Confirmed via a
real client round trip over stdio (`mcp.client.Client` +
`mcp.StdioServerParameters`, `tools/list`/`tools/call`), not just reading
the error message. The MCP ecosystem is moving fast enough that a
day-old web-researched assumption was already stale — worth remembering
next time this package needs touching.

**A real prompt-injection gap caught beyond the brief's own sample
code**: `run_council_pass` calls `run_council()` directly with no
Pydantic model in front of it, unlike the FastAPI route
(`apps/api/app/routers/agent.py`), which validates `symbol` against
`SYMBOL_RE` before it ever reaches a council node's LLM prompt (a
documented channel — `f"Ticker: {state['symbol']}"` — per that route's
own docstring). The brief's sketch code only `.upper()`-normalized the
symbol, which would have silently reopened that hole for this tool
specifically. Fixed with the same regex + a dedicated test
(`test_run_council_pass_rejects_invalid_symbol`).

**One fix outside the new package**: `apps/api/app/` was the one
workspace member missing a `py.typed` marker (`broker`/`engine`/
`trading_agents` all have one) — invisible until `mcp_server` became the
first package to cross-import `app.*` under mypy strict. Verified myself,
independently: removed the marker and re-ran the full-workspace mypy
check — identical 360 errors/77 files with or without it, confirming
it's genuinely isolated, not a source of new noise elsewhere.

Also caught and correctly handled: two of the five wrapped functions
(`decisions_list.list_decisions`, `ghost_service.build_veto_ledger`)
don't self-guard on `USE_POSTGRES` — their routers do instead (one 404s).
An MCP tool has no HTTP layer to raise a 404 through, so both adapters
replicate the guard directly and return an honest empty payload plus
`postgres_backed: false` instead of letting an exception reach an LLM
caller mid-conversation.

**Verified independently after cherry-pick**: full combined suite
(`apps/api`+`apps/agents`+`packages/engine`+`packages/broker`+
`apps/mcp_server`) 725→**736 passed**, 9 skipped (+11 new, 0
regressions); `ruff check`/`ruff format --check`/`mypy` (strict) all
clean on the new package; the `mcp==2.1.1` resolution reproduced
independently, not just trusted from the subagent's report.

**Left open, disclosed**: no `equity_resolver` wired into
`run_council_pass` (an MCP caller has no authenticated session to
resolve real broker equity from — a Postgres-backed run through this
tool sizes against the synthetic-feature equity fixture, not real
equity); the MCP Inspector's web UI was never interactively exercised
(no browser in that sandbox) — substituted a real client-library round
trip instead, which the subagent argued is stronger evidence than a
manual Inspector click-through, and this review agrees.

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
