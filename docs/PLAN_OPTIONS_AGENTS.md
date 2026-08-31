# Plan K — a dedicated options council: orchestrator, judges, guarded tools

**Status:** plan, not built. Written 2026-08-31 by `ID:MODEL1REAL`.
**Deliverable 3 of 3. Much the largest — read all of §0 before writing code.**

---

## 0. Four things to get straight before you start

### 0.1 🚨 "PreToolUse / PostToolUse" hooks do not exist in this runtime

Those are **Claude Code** features — they gate tool calls made by *Claude Code itself*,
configured in `.claude/settings.json`. This application is FastAPI + LangGraph calling
the Anthropic SDK directly. **There is no hook system here and nothing to configure.**

The *concept* is exactly right and is what §3 builds: a deterministic middleware that
every tool call routes through, with a `before` that can refuse and an `after` that can
reject the result. **You are building the equivalent, not enabling a feature.** An
agent that goes looking for a hooks config will waste an hour and find nothing.

### 0.2 Tool use is not implemented at all

`apps/agents/trading_agents/llm.py` has **no `tools=` support**. Verified 2026-08-31:
zero hits for `complete_tools|tool_use|tools=`. `LLM.complete` sends one user message
and reads `msg.content[0].text`. A `tool_use` block would break that line.

`PLAN_EXIT_AGENT.md` §6 already specifies the two commits needed (a block walk, then a
sibling `complete_tools()`). **That work is a prerequisite for this plan.** Do it there,
once, not twice.

### 0.3 More agents means more latency, not less — parallelism is the only fix

You asked for low latency *and* an orchestrator + 3 subagents + 4 judges. Those pull
against each other: that is ~8 LLM calls where there are 5 today.

The answer is **wall-clock hops, not call count**. Run the fan-outs with
`asyncio.gather` and the shape is 4 sequential hops for 8 calls. Sequential, it is 8
hops and roughly twice today's latency. §2.3 has the budget.

**Check first, because it may be free money:** confirm whether the three equity
analysts currently run in parallel or sequentially. `_run_linear` (the asyncio
fallback) is sequential by construction; the LangGraph branch may not be. If they are
sequential today, parallelising them is a latency win that costs nothing and helps
equity too.

### 0.4 The scope you asked for is bigger than 4 days — here is the honest map

You asked for "buy sell long short call put, all use case scenarios". Here is every
structure, what it actually costs to support, and what I recommend.

| Structure | Legs | Max loss | Alpaca level | Work needed here | Verdict |
|---|---|---|---|---|---|
| **Long call** | buy call | premium | 2 | **none — works today** | ✅ ship on this |
| **Long put** | buy put | premium | 2 | **none — works today** | ✅ ship on this |
| **Bull call / bear put spread** (debit) | buy + sell | net debit | 3 | multi-leg everywhere — see below | ⚠️ Phase B |
| **Bull put / bear call spread** (credit) | sell + buy | width − credit | 3 | same, plus margin accounting | ⚠️ Phase B |
| **Cash-secured put** | sell put | strike − credit | 1 | cash-collateral lock, assignment | ❌ not in 4 days |
| **Covered call** | own shares + sell call | shares called away | 1 | share-collateral lock, assignment | ❌ not in 4 days |
| **Naked short call** | sell call | **UNBOUNDED** | 3+ | — | ❌ **never** |

**What "Phase B spreads" actually costs**, so nobody underestimates it:
`OptionLegDetails.action` is a two-value `Literal`; `naked_short_forbidden` rejects
anything that is not `buy_to_open`/`sell_to_close`; sizing, `max_premium_pct`, the
ghost evaluator, `position_manager._close_position` and the OCC wire symbol **all
assume exactly one leg**. Alpaca does support `OrderClass.MLEG` for options, so the
broker end is reachable — but this is a change across the entire options stack, and
it lands on risk code, mid-contest, with the agent live.

**Recommendation: build this plan's architecture on long calls/puts, which work
today.** The orchestrator/judge/guard design is what you are actually buying, and it is
completely independent of how many legs the proposal has. Add spreads behind
`ALLOW_OPTION_SPREADS=0` *after* the architecture is proven, and only if there is time.

> **Naked short calls stay forbidden regardless of time.** Unbounded loss, no
> assignment handling in this codebase, on an account being scored. `naked_short_forbidden`
> is defense-in-depth and must not be relaxed.

---

## 1. Why options need their own council

Today `router → technical/fundamental/macro → drafter` is shared; the options path only
changes what the *drafter* does. That is wrong in both directions:

- The equity analysts reason about **the underlying**. Nobody reasons about **IV rank,
  skew, term structure, or whether the chain can actually be filled** — which is most of
  what makes an options trade good or bad.
- Options passes cost the same 5 calls as equity while asking questions those prompts
  were never written for.

So: a **separate graph**, separate prompts, separate model tiers, sharing only the
deterministic layers (`engine.options.selection`, `engine.risk`) — which is exactly what
should be shared.

---

## 2. The architecture

```
                    options_fit          deterministic, ZERO LLM
                        │                most symbols exit here
                        ▼
                  ORCHESTRATOR           Haiku · picks strategy family +
                        │                which subagents are worth running
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼          ← asyncio.gather, ONE hop
   chain_analyst   vol_analyst    flow_analyst
     (Sonnet)        (Haiku)        (Haiku)
        └───────────────┼───────────────┘
                        ▼
                     DRAFTER             Sonnet · ONE concrete proposal
                        │
        ┌───────┬───────┼───────┬───────┐
        ▼       ▼       ▼       ▼        ← asyncio.gather, ONE hop
     thesis   risk  liquidity consistency    4 × Haiku judges
        └───────┴───────┴───────┴───────┘
                        ▼
              DETERMINISTIC AGGREGATION   N-of-M, no LLM
                        ▼
                   engine.risk            13 named options rules — unchanged
                        ▼
                   TOOL GUARD             §3
                        ▼
                 packages/broker
```

### 2.1 The nodes

| Node | Model | Reads | Emits |
|---|---|---|---|
| `options_fit` | **none** | features, chain summary | strategy family or HOLD |
| `orchestrator` | Haiku | fit output, regime, funnel counts | which subagents to run, target structure |
| `chain_analyst` | Sonnet | full funnel, greeks, expiries | structure + strike + expiry + why |
| `vol_analyst` | Haiku | IV, realized vol, IV rank, skew | is vol cheap or rich, and versus what |
| `flow_analyst` | Haiku | OI, volume, spread, quote age | will this actually fill |
| `drafter` | Sonnet | all of the above | one concrete proposal |
| `judge_*` ×4 | Haiku | the proposal + its inputs | `PASS` / `FAIL` + one named reason |

### 2.2 🔑 The judges have MONOTONE authority — they can only refuse

Same principle as the exit agent, and it is what makes adding four LLMs safe:

> **A judge can veto. A judge can never approve.** Aggregation is
> deterministic: the proposal proceeds only if **≥3 of 4 judges PASS**, and then still
> has to clear all 13 risk rules. A judge saying PASS grants nothing that was not
> already going to happen.

Consequences that follow for free, and each needs a test:
- A judge timing out, erroring, or returning garbage counts as **FAIL** (fail-closed —
  this is the opposite of the exit agent's fail-safe, because here refusing costs
  nothing and there is no protection to remove).
- MOCK mode: judges are **skipped entirely**, not stubbed to PASS. A keyless run must
  not silently behave like a 4-judge consensus.
- No judge output ever widens a cap, changes a strike, or edits the proposal. They vote
  on it as drafted; they do not redraft it.

### 2.3 Latency budget — the number to hold yourself to

| | Sequential hops | LLM calls |
|---|---|---|
| Equity council today | 3 | 5 |
| Options council, **parallel fan-out** | **4** | 8 |
| Options council, sequential (wrong) | 8 | 8 |

Non-negotiables to hit the 4:
1. `asyncio.gather` for the 3 subagents, and again for the 4 judges.
2. Haiku everywhere except `chain_analyst` and `drafter`.
3. `options_fit` first — a symbol with no setup costs **zero** LLM calls, and that is
   what keeps the average down across 8 underlyings.
4. `cache_system=True` (already supported) on every prompt — the system blocks are
   long and static, which is exactly the cacheable case.
5. One timeout per fan-out, not per call: `asyncio.wait_for` around the `gather`.

**Cost:** ~$0.06–0.08/pass against ~$0.04 today. Across 8 option symbols × a few passes
a day this stays in single-digit dollars. Cost is not the constraint; **latency and
correctness are.**

---

## 3. The tool guard — the deterministic gate you asked for

Build `apps/agents/trading_agents/tools/guard.py`. **Every** tool call the options
agents make routes through it. This is the PreToolUse/PostToolUse equivalent from §0.1.

```python
@dataclass(frozen=True)
class GuardVerdict:
    allow: bool
    reason: str | None          # named, like a risk veto rule
    redacted: dict | None       # `after` may narrow a result, never widen it

class ToolGuard(Protocol):
    def before(self, tool: str, args: dict) -> GuardVerdict: ...
    def after(self, tool: str, args: dict, result: object) -> GuardVerdict: ...
```

### `before` — deterministic, runs on every call, no exceptions

1. **Allowlist by name.** Unknown tool → `deny("unknown_tool")`, returned to the model
   as a `tool_result` with `is_error: true`. **Never raise** — an exception aborts the
   pass; a denial teaches the model.
2. **Argument shape + range.** Symbol matches `SYMBOL_RE`, qty is a positive int, OCC
   symbol parses via `OccSymbol.try_parse`, dates are real dates. A malformed arg is a
   denial, not a 500.
3. **No order-placing tool is in the allowlist at all** (§3.1).
4. **Rate + budget.** Per-pass call ceiling (e.g. 12). Exceeding it denies with
   `tool_budget_exhausted` — a model looping on a tool must terminate the pass, not run
   up a bill.

### `after` — the part people skip

1. **Shape validation.** The result matches the declared schema, or the call is treated
   as failed.
2. **Redaction.** A read tool must never return another user's data. Scope every query
   by `user_id` *inside the tool*, and have `after` assert the invariant held.
3. **Size cap.** Truncate huge payloads (a full chain is thousands of rows) before they
   reach the context window.

### 3.1 🚨 What the agents may and may not call

**Allowed (read-only):**
```
get_contract_candidates   get_option_snapshot     get_underlying_bars
get_funnel_counts         get_position_snapshot   get_entry_thesis
get_iv_rank               get_account_summary (read-only, no secrets)
```

**Will never be built here** — copy this docstring, it mirrors
`apps/mcp_server/mcp_server/tools.py:9-19`:

> `place_order`, `place_option_order`, `approve_proposal`, `execute_trade`,
> `cancel_order`, `close_position`, `exercise_option`, `size_position` — anything that
> mutates broker or portfolio state, or that reaches `packages/engine/risk` →
> `packages/broker`'s order surface. The agents' entire authority is a **proposal**,
> consumed by deterministic code that decides independently whether to place anything.

### 3.2 On "give them tools to auto place trades"

You asked for the agents to be able to open trades. **They do — through the pipeline,
not through a tool call.** The agent emits a proposal; `engine.risk` clears it;
[`PLAN_AUTO_APPROVE.md`](PLAN_AUTO_APPROVE.md)'s sweeper executes it with no human tap.
The trade is fully autonomous end to end.

The difference is *where the authority lives*. Putting `place_option_order` in an LLM's
tool list would mean a hallucinated symbol or a mis-parsed qty reaches the broker with
one deterministic layer fewer between it and the account — and it would break the one
architectural claim that distinguishes this entry. **The autonomy you want is already
available without it.**

---

## 4. Options trading rules the agents must encode

Standard practitioner heuristics. **Flagged honestly: these are widely-used rules of
thumb, not results this repo has validated.** Anything that changes a *threshold* must
be backtested (§5) before it is trusted; anything that is a *hard bound* goes in
`engine.risk`, not in a prompt.

### Entry

| Rule | Why |
|---|---|
| **Buy when IV rank is low, sell premium when it is high** | The single most-cited options rule. Long premium into high IV is paying for a crush. Needs an `iv_rank` feature — 52-week percentile of IV. **Does not exist yet; build it.** |
| **IV vs realized vol** | Already implemented as the `iv_realized_vol_band` funnel stage (0.3×–3.0×). Keep. |
| **Avoid earnings unless the trade IS the earnings bet** | `earnings_blackout` exists and is **permanently inert** — Alpaca publishes no earnings calendar. Do not claim it works. A third-party calendar would fix it; out of scope now. |
| **30–45 DTE for directional longs** | Theta accelerates inside ~21 DTE. Our window is 10–45; the agents should *prefer* the upper half and say when they do not. |
| **Delta by conviction** | Implemented (0.35–0.75 high / 0.25–0.65 low). |
| **Liquidity: OI ≥ 100, spread ≤ 12% of mid** | Implemented. A wide spread is a guaranteed loss on the round trip. |
| **Never more than N% of premium in one underlying** | `max_premium_pct` 2.5% / `max_total_premium_pct` 12%. Enforced deterministically. |

### Exit — already shipped, do not re-litigate in a prompt

Trailing ratchet (arm +35%, give back 30% of peak), hard stop −40%, hard ceiling +150%,
time stop, `DTE ≤ 2` expiry sweep. All deterministic, all in
[`OPTIONS_PLAYBOOK.md`](OPTIONS_PLAYBOOK.md) §3.

**The agents must not be given exit authority.** The ratchet already owns it.

### The rule that matters most

> **Theta is always against a long option.** Every long-premium trade needs a thesis
> with a *timeframe*, and the agent must state it. "NVDA looks strong" is not a thesis;
> "NVDA breaks 190 within 3 weeks on the pattern + volume" is. The `thesis_judge`
> exists to fail proposals that cannot answer *by when*.

---

## 5. Backtesting the options strategies

`packages/engine/engine/backtester/` exists with 5 **equity** strategies and — the
valuable part — **shares live risk code**, so a backtest is not a parallel
reimplementation that can drift.

Needed:
1. An options fill model. Options fill worse than equities: assume the **ask** on entry
   and the **bid** on exit, never mid. A backtest that fills at mid will look profitable
   and will not be.
2. Historical option bars via `/v1beta1/options/bars` — the same endpoint
   `engine/prices/option_alpaca.py` already uses. **Remember the `end`-clamp:**
   requesting inside the 15-minute delay window returns a 403 whose message
   (`"OPRA agreement is not signed"`) is misleading.
3. Report per strategy: win rate, average win/loss, max drawdown, **and the
   distribution of holding periods** — a strategy that only wins by holding to expiry is
   incompatible with the `DTE ≤ 2` sweep.

**Reality check:** with four sessions left, a backtest cannot validate a strategy to any
statistical standard. Its value here is **catching a strategy that is obviously broken**
— negative expectancy, fills that never happen, holding periods the exit rules forbid.
Treat it as a smoke test, and say so in the write-up rather than implying more.

---

## 6. SSE streaming — replace the 600 ms poll

Today `useCouncilProgress` polls `GET /agent/run/{id}/progress` every 600 ms. The
`ProgressEvent` machinery in `progress.py` is already the right shape — one event per
node transition, summaries extracted deterministically, no LLM in the path.

- FastAPI `StreamingResponse` with `text/event-stream` at
  `GET /api/v1/agent/run/{id}/stream`.
- Client: `EventSource`, falling back to the existing poll when SSE fails. **Keep the
  poll.** Proxies and mobile networks drop long-lived connections, and a demo that
  stalls silently is worse than one that polls.
- `progress.NodeName` is a closed `Literal` — the new options nodes must be added there
  **and** to `NODE_ORDER`, or the theater renders an unnamed lane.
- Stream the fan-outs honestly: 3 subagents in flight means 3 lanes lit at once. That
  visual — parallel agents actually running — is worth more than any diagram.

---

## 7. Charts — show the contract, not just the underlying

Use **TradingView Lightweight Charts** (open source, ~45 KB, no account, no network).
There is no usable TradingView *data* API for us — Alpaca is the data source, and
Alpaca's TradingView integration is a human trading UI that yields nothing programmatic.
⚠️ Verify the library's license on its repo before shipping and honour attribution.

What to render on the decision detail page:
1. **Underlying candles**, with the detected candlestick pattern marked on the bar that
   produced it (that detector shipped and nothing displays it).
2. **The contract itself** — entry premium, current mark, the trailing-ratchet line, the
   stop. *This* is the picture a judge has not seen anywhere else: not "NVDA went up"
   but "the contract we bought went from $2.17 to $3.40 and the ratchet armed here".
3. **The funnel**, as a stepped bar: 4,128 → 2,064 → 1,843 → 130 → 3 → 1.

---

## 8. Build order

```
0. llm.py block walk + complete_tools()      ← PLAN_EXIT_AGENT.md §6, prerequisite
1. tools/guard.py + the read-only tool set   ← no agents yet; unit-testable alone
2. options_fit + orchestrator + 3 subagents  ← parallel from the first commit
3. drafter (options variant)
4. 4 Haiku judges + deterministic N-of-M
5. SSE stream + the new node lanes
6. Charts + the funnel view
7. iv_rank feature + backtest smoke          ← cut first if time runs out
8. Spreads behind ALLOW_OPTION_SPREADS=0     ← cut second; needs §0.4's full stack change
```

**Ship 1–4 behind `USE_OPTIONS_COUNCIL=0`.** The existing shared council keeps running
until the new one is proven on a live pass. Never deploy a new agent graph into a live
session.

---

## 9. Tests — the revert-check matrix

| Test | Break this to make it fail |
|---|---|
| **`test_no_order_tool_in_the_allowlist`** | Add `place_option_order`. **The security claim, as a unit test.** |
| **`test_judge_failure_counts_as_fail_not_pass`** | Make the `except` return PASS — fail-closed is the whole design |
| `test_three_of_four_judges_required` | Accept a 2-of-4 vote |
| `test_judges_cannot_edit_the_proposal` | Let a judge's output mutate strike/qty |
| `test_judges_skipped_entirely_in_mock_mode` | Stub them to PASS |
| `test_unknown_tool_denies_and_does_not_raise` | Raise instead of returning `is_error` |
| `test_tool_budget_terminates_a_looping_agent` | Remove the per-pass ceiling |
| `test_after_guard_rejects_a_cross_user_result` | Drop the `user_id` assertion |
| `test_subagents_run_concurrently` | Replace `gather` with sequential awaits — assert wall-clock, not call count |
| `test_options_fit_holds_without_spending_a_call` | Let a no-setup symbol reach the orchestrator |
| `test_naked_short_still_forbidden` | Add `sell_to_open` to `_ALLOWED_ACTIONS` |

**Baseline: 969 passed, 11 skipped** (Python) + 28 (jest). `git stash` and re-run before
blaming your change.

---

## 10. Where you will go wrong

1. **Looking for a PreToolUse hook config.** It does not exist in this runtime. §0.1.
2. **Building `complete_tools()` twice** — once here and once in `PLAN_EXIT_AGENT.md`.
   Do it once, there.
3. **Running the subagents sequentially.** Doubles latency and silently defeats the
   whole design. Assert concurrency in a test.
4. **Letting a judge approve.** They vote to refuse; they never grant. Monotone.
5. **Judges failing open.** A timeout is a FAIL here.
6. **Putting an order tool in the allowlist** because "the agent should place trades".
   It already can — through the pipeline. §3.2.
7. **Relaxing `naked_short_forbidden`** to reach "short options". Unbounded loss, no
   assignment handling.
8. **Shipping spreads without the full §0.4 stack change.** Single-leg assumptions are
   load-bearing in sizing, risk, ghost eval and the close path.
9. **Deleting the SSE fallback poll.**
10. **Believing a 4-day backtest validates anything.** It catches broken; it does not
    prove good.
11. **Deploying the new graph into a live session.** Flag-gate, watch one pass, then
    flip.

---

*Related: [`PLAN_CLI_SURFACE.md`](PLAN_CLI_SURFACE.md) · [`PLAN_MCP_DEMO.md`](PLAN_MCP_DEMO.md) ·
[`PLAN_EXIT_AGENT.md`](PLAN_EXIT_AGENT.md) · [`PLAN_AUTO_APPROVE.md`](PLAN_AUTO_APPROVE.md) ·
[`OPTIONS_PLAYBOOK.md`](OPTIONS_PLAYBOOK.md) · [`../CLAUDE.md`](../CLAUDE.md)*
