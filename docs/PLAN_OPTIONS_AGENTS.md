# Plan K — one autonomous options agent, guarded tools, minimum latency

**Status:** plan, not built. Written 2026-08-31 by `ID:MODEL1REAL`.
**Deliverable 3 of 3.** Scope: **~2 focused days**, hour estimates in §7.

> **This replaces an earlier revision of this file that proposed an orchestrator, 3
> subagents and 4 Haiku judges. That design was wrong and is deleted.** §0 explains
> why, because the reasoning is the most useful thing in this document.

---

## 0. Why one agent, not eight

I proposed 8 LLM calls. Then I checked what the deterministic layer already decides:

| Decision | Decided by | LLM involved? |
|---|---|---|
| call vs put | `select_contract` stage 1 | no |
| expiry / DTE window | stage 2 | no |
| strike / delta band | stage 3 | no |
| liquidity (OI, volume, spread) | stage 4 + `illiquid_contract` | no |
| IV present, IV vs realized vol | stages 5–6 | no |
| **direction** | `strategy_fit` — and `drafter.py:179` **downgrades the LLM's verdict to HOLD if it disagrees** | overridden |
| position size | `options_position_size` — floor division | no |
| approve / veto | `engine.risk`, 13 named rules | no |

The LLM currently supplies **three fields**: `verdict`, `confidence`, `rationale`. And
`verdict` is already overruled by the deterministic fit.

So a `vol_analyst` would compute IV rank — **a number**. A `flow_analyst` would judge
OI and spread — **already funnel stage 4**. A `chain_analyst` would pick strike and
expiry — **already stages 2–3**. Four judges would validate a proposal whose contract
was chosen deterministically and whose risk was checked by 13 deterministic rules.

**That architecture re-implements deterministic checks in a slower, less reliable
medium, and dilutes the one claim that makes this entry distinctive.** More LLM calls
would have made the system worse on every axis: latency, cost, reliability, and the
propose/dispose story.

You were right. **One agent.**

---

## 1. What the agent is actually for

Exactly one thing deterministic code cannot do:

> **Decide whether there is a directional thesis worth expressing right now, how
> strongly, and say why — with a timeframe.**

`conviction` is not cosmetic: it selects the delta band (≥0.7 → [0.35,0.75], else
[0.25,0.65]). So the agent's real output is *which part of the chain we shop in*, plus
a human-readable thesis, plus the option to stand down.

**Options are a timing game, so the agent's answer must carry a clock.** "NVDA looks
strong" is not a thesis. "NVDA breaks 190 within 3 weeks on the volume expansion" is —
because theta is always against a long option, and a thesis without a deadline cannot
be checked against one.

### What it must never decide

Strike · expiry · contract · quantity · whether risk approves · when to exit. All of
those are deterministic today and stay that way. The agent proposes a *direction and a
conviction*; the machine does the rest.

---

## 2. The architecture — 1 hop typical, 2 worst case

```
┌─ DETERMINISTIC PRE-PASS · ZERO LLM ────────────────────────────┐
│  options_fit  →  chain fetch  →  funnel counts                 │
│  iv_rank · realized vol · candlestick patterns · liquidity     │
│  no setup → HOLD here, 0 tokens                                │
└────────────────────────────────────────────────────────────────┘
                              ▼
              ┌─ THE OPTIONS AGENT · 1 call ──────┐
              │  Sonnet. Full pre-pass context    │
              │  already in the prompt.           │
              │  Read-only tools available but    │
              │  usually unnecessary.             │
              │  → direction · conviction ·       │
              │    thesis · timeframe             │
              └───────────────────────────────────┘
                              ▼  (only if it called a tool)
                    [tool guard → tool → 2nd call]
                              ▼
┌─ DETERMINISTIC POST-PASS · ZERO LLM ───────────────────────────┐
│  validators (§4) → select_contract → sizing → engine.risk      │
│  → tool guard → packages/broker                                │
└────────────────────────────────────────────────────────────────┘
```

**This is faster than the equity council that exists today** (3 hops). Options get the
lowest latency in the system, which is the correct priority ordering.

### Why tools are pre-empted, not removed

Tool calls are round trips, and round trips are the thing we are minimising. So:
**pre-load everything the agent normally needs into the prompt** — funnel counts, IV
rank, greeks summary, patterns, recent bars. Tools exist for the exception (it wants
deeper history, or a second expiry) and are **bounded at one round**. In the common
case the agent answers in a single call and never touches a tool.

That gives you "fully autonomous with tools" without paying tool latency on every pass.

---

## 3. The tool guard — your PreToolUse/PostToolUse, built here

> 🚨 **`PreToolUse`/`PostToolUse` are Claude Code features.** They gate tool calls made
> by Claude Code itself, via `.claude/settings.json`. This app is FastAPI + LangGraph
> calling the Anthropic SDK directly — **there is no hook system here and nothing to
> configure.** The concept is right; you are building the equivalent. An agent that
> goes looking for a hooks config will find nothing and lose an hour.

`apps/agents/trading_agents/tools/guard.py`:

```python
@dataclass(frozen=True)
class GuardVerdict:
    allow: bool
    reason: str | None       # named, like a risk veto rule
    payload: dict | None     # `after` may NARROW a result, never widen it

class ToolGuard:
    def before(self, tool: str, args: dict) -> GuardVerdict: ...
    def after(self, tool: str, args: dict, result: object) -> GuardVerdict: ...
```

**`before`** — allowlist by name (unknown → `deny("unknown_tool")`); validate arg shape
and range (`SYMBOL_RE`, positive int qty, `OccSymbol.try_parse`); enforce a per-pass
call ceiling (**4** — with a 1-round loop this is generous, and it stops a looping
model from running up a bill).

**`after`** — assert the result matches its declared schema; assert every row is scoped
to this `user_id`; truncate large payloads before they reach the context window.

**A denial is returned to the model as a `tool_result` with `is_error: true`. Never
raise** — an exception aborts the pass; a denial teaches the model and the pass
continues.

### Allowed tools (all read-only)

```
get_funnel_counts      get_option_snapshot     get_underlying_bars
get_iv_rank            get_entry_thesis        get_position_snapshot
```

### Will never be built here

Copy this docstring; it mirrors `apps/mcp_server/mcp_server/tools.py:9-19`:

> `place_order`, `place_option_order`, `approve_proposal`, `execute_trade`,
> `cancel_order`, `close_position`, `exercise_option`, `size_position` — anything that
> mutates broker or portfolio state or reaches `packages/engine/risk` →
> `packages/broker`'s order surface. The agent's entire authority is a direction and a
> conviction, consumed by deterministic code that decides independently whether to
> place anything.

### On "fully autonomous"

**It already is.** The agent proposes → `engine.risk` clears → the auto-approve sweeper
([`PLAN_AUTO_APPROVE.md`](PLAN_AUTO_APPROVE.md)) executes with no human tap. End-to-end
autonomous, today, without an order tool in the LLM's hands.

Putting `place_option_order` in the tool list would mean a hallucinated OCC symbol or a
mis-parsed quantity reaches the broker with one deterministic layer fewer — and it would
cost the only architectural claim that separates this entry from five competitors making
the same one as prompt policy. **The autonomy is available without the exposure.**

---

## 4. Validation without judges — deterministic, free, instant

Four LLM judges would cost 4 calls and re-check things already checked. Replace them
with validators that run in microseconds. Each returns a **named** reason, like a risk
rule:

| Validator | Fails when | Named reason |
|---|---|---|
| **Direction agreement** | agent's direction ≠ `strategy_fit`'s | `direction_contradicts_fit` — **this already exists** at `drafter.py:179`; keep it |
| **Thesis has a clock** | no timeframe parsed from the thesis | `thesis_without_timeframe` |
| **Conviction is supported** | conviction ≥0.7 but `strategy_fit` score is marginal | `conviction_exceeds_evidence` |
| **Thesis cites a number** | no figure from the feature dict appears | `thesis_without_evidence` |
| **Funnel actually survived** | `select_contract` returned nothing | `no_contract` (already named) |

Then all 13 risk rules, unchanged.

### The one place a second LLM earns its call

**Optional, risk-proportional, off by default.** A single Haiku sanity check that runs
**only** when the trade is both high-conviction and large (say ≥1.5% of equity). Not on
every pass — on the few that matter.

Same monotone rule as the exit agent: **it can only refuse.** A `PASS` grants nothing;
a timeout, an error or malformed output counts as **FAIL** (fail-closed — refusing an
options entry costs nothing, so the safe default is the strict one). Ship it behind
`OPTIONS_SANITY_JUDGE=0`.

---

## 5. Latency budget — the number to hold yourself to

| Path | LLM hops | Wall clock |
|---|---|---|
| No setup (most symbols) | **0** | ~0 |
| Normal options pass | **1** | one Sonnet call |
| Agent used a tool | 2 | + one tool + one call |
| Large high-conviction trade w/ sanity judge on | 2–3 | + one Haiku call |
| *(equity council today, for comparison)* | *3* | — |

Non-negotiables:
1. `options_fit` **first** — a symbol with no setup costs zero tokens. This is what
   keeps the average down across 8 underlyings, and it already exists.
2. `cache_system=True` (already supported). The system prompt is long and static —
   textbook cacheable.
3. Pre-load the pre-pass context so the tool loop is the exception.
4. `max_rounds=1` on the tool loop. Hard cap.
5. **Check for free latency first:** confirm whether today's three equity analysts run
   in parallel or sequentially. `_run_linear` is sequential by construction; the
   LangGraph branch may not be. If they are sequential, `asyncio.gather` is a win that
   costs nothing and helps equity too.

**Cost:** ~$0.01–0.02 per options pass against ~$0.04 for an equity pass. Fewer calls
than today, not more.

---

## 6. Options scope — what ships, what does not

Single-leg **long calls and long puts** work today, end to end. Build on them.

| Structure | Max loss | Work needed | Verdict |
|---|---|---|---|
| Long call / long put | premium | **none** | ✅ ship |
| Vertical spreads (debit/credit) | defined | multi-leg across sizing, risk, ghost eval, close path — `OptionLegDetails.action` is a 2-value `Literal` and single-leg is load-bearing throughout | ⚠️ flag-gated, only after the agent is proven |
| Cash-secured put / covered call | defined | collateral locking + assignment handling, neither exists | ❌ not now |
| Naked short call | **unbounded** | — | ❌ **never** |

`naked_short_forbidden` stays. Unbounded loss with no assignment handling, on a live
scored account, is not a trade-off worth making for a contest.

### Rules the agent must encode in its prompt

- **Long premium wants low IV rank.** Buying into high IV is paying for a crush.
  `iv_rank` (52-week IV percentile) **does not exist yet — build it**, ~1h, and it is
  the single most valuable new feature here.
- **Prefer the upper half of the 10–45 DTE window** for directional longs; theta
  accelerates inside ~21 DTE. State it when going shorter.
- **Every thesis carries a timeframe.** Enforced by a validator (§4), not by hope.
- Exits are **not the agent's business** — the ratchet, stop, time stop and expiry sweep
  own them and already shipped.

Standard practitioner heuristics, stated honestly as such — not results this repo has
validated. Anything that moves a *threshold* needs a backtest; anything that is a *hard
bound* lives in `engine.risk`, never in a prompt.

---

## 7. Scope — ~2 focused days

| Task | Est. |
|---|---|
| `llm.py`: block walk + `complete_tools()` (spec in [`PLAN_EXIT_AGENT.md`](PLAN_EXIT_AGENT.md) §6 — **build once, there**) | 2h |
| `tools/guard.py` + 6 read-only tools + tests | 3h |
| `iv_rank` feature | 1h |
| Options agent node, prompt, mock branch, cost-ledger role | 3h |
| Deterministic validators (§4) | 2h |
| SSE stream + node lanes | 3h |
| Charts: contract + funnel | 4h |
| Optional sanity judge (flag off) | 1h |
| **Total** | **~19h** |

Cut order if it slips: charts → sanity judge → SSE → `iv_rank`. **Never cut** the guard
or the validators.

Ship behind `USE_OPTIONS_AGENT=0`; the existing shared council keeps running until the
new path is proven on one live pass. **Never deploy a new agent graph into a live
session.**

---

## 8. SSE and charts

**SSE.** Today `useCouncilProgress` polls every 600 ms. `progress.py`'s `ProgressEvent`
is already the right shape. Add `GET /api/v1/agent/run/{id}/stream` as a FastAPI
`StreamingResponse` with `text/event-stream`; client uses `EventSource`. **Keep the poll
as a fallback** — proxies and mobile networks drop long-lived connections, and a demo
that stalls silently is worse than one that polls. `progress.NodeName` is a closed
`Literal`; add the new nodes there **and** to `NODE_ORDER` or the theater renders an
unnamed lane.

**Charts.** TradingView **Lightweight Charts** (open source, ~45 KB, no account, no
network). There is no usable TradingView *data* API for us — Alpaca is the data source,
and Alpaca's TradingView integration is a human trading UI that yields nothing
programmatic. ⚠️ Verify the library's licence on its repo and honour attribution.

Render, in priority order:
1. **The contract itself** — entry premium, current mark, the ratchet line, the stop.
   *"The contract we bought went $2.17 → $3.40 and the ratchet armed here."* Nobody
   else will show this.
2. **The funnel**, stepped: 4,128 → 2,064 → 1,843 → 130 → 3 → 1.
3. Underlying candles with the detected pattern marked — that detector shipped and
   nothing displays it.

---

## 9. Tests — revert-check matrix

| Test | Break this to make it fail |
|---|---|
| **`test_no_order_tool_in_the_allowlist`** | Add `place_option_order`. The security claim, as a unit test. |
| **`test_unknown_tool_denies_and_does_not_raise`** | Raise instead of returning `is_error` |
| `test_tool_budget_terminates_a_looping_agent` | Remove the per-pass ceiling |
| `test_after_guard_rejects_a_cross_user_result` | Drop the `user_id` assertion |
| `test_direction_contradicting_fit_is_downgraded` | Let the agent's direction win |
| `test_thesis_without_a_timeframe_is_rejected` | Skip the validator |
| `test_options_fit_holds_without_spending_a_call` | Let a no-setup symbol reach the agent |
| `test_tool_loop_bounded_at_one_round` | Raise `max_rounds` |
| `test_sanity_judge_failure_counts_as_fail` | Make the `except` return PASS |
| `test_sanity_judge_only_runs_on_large_high_conviction` | Run it every pass |
| `test_naked_short_still_forbidden` | Add `sell_to_open` to `_ALLOWED_ACTIONS` |

**Baseline: 969 passed, 11 skipped** (Python) + 28 (jest). `git stash` and re-run before
blaming your change.

---

## 10. Where you will go wrong

1. **Looking for a PreToolUse hook config.** Does not exist here. §3.
2. **Adding analyst agents back** because more agents feels more capable. Re-read §0 —
   they re-implement deterministic checks in a worse medium.
3. **Building `complete_tools()` twice.** Once, in `PLAN_EXIT_AGENT.md` §6.
4. **Putting an order tool in the allowlist** because the agent "should be autonomous".
   It already is. §3.
5. **Letting the sanity judge approve**, or fail open. It refuses only, and a timeout
   is a FAIL.
6. **Letting the agent pick strike or expiry.** `select_contract` owns that, and the
   funnel is the demo.
7. **Relaxing `naked_short_forbidden`.**
8. **Making tool calls the normal path.** Pre-load context; tools are the exception.
9. **Deleting the SSE fallback poll.**
10. **Deploying the new graph into a live session.**

---

*Related: [`PLAN_CLI_SURFACE.md`](PLAN_CLI_SURFACE.md) · [`PLAN_MCP_DEMO.md`](PLAN_MCP_DEMO.md) ·
[`PLAN_EXIT_AGENT.md`](PLAN_EXIT_AGENT.md) · [`PLAN_AUTO_APPROVE.md`](PLAN_AUTO_APPROVE.md) ·
[`OPTIONS_PLAYBOOK.md`](OPTIONS_PLAYBOOK.md) · [`../CLAUDE.md`](../CLAUDE.md)*
