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

1. **`eslint` and `jest` are declared but non-functional, repo-wide.**
   `pnpm lint`'s JS half fails outright — root `package.json` pins
   `eslint@^9` (flat-config only) but no `eslint.config.js` exists anywhere.
   `pnpm --filter @app/mobile test` fails the same way — the script runs
   `jest`, but `jest` isn't installed as a dependency anywhere in the
   workspace and there's no `jest.config.*`. Pre-existing, unrelated to
   anything touched this session; means JS lint/test have been giving zero
   signal on every recent change, this session's included.
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

**Scope decisions locked this build:** instruments = US stocks/ETFs only (options/futures out); exits = Alpaca **bracket** (broker-enforced stop/target) **+** agent early-exit/time-stop for `exit_mode=agent`; entries always human-approved; Zerodha stays dark for v1.

## Entries

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
