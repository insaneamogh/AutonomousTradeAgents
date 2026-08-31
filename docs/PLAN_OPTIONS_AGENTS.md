# Plan K — two arguing options agents with real trade tools, deterministic guards

**Status:** plan, not built. Written 2026-08-31 by `ID:MODEL1REAL`.
**Deliverable 3 of 3.** Scope: **~3 focused days**, hour table in §9.

> Supersedes two earlier revisions of this file (an 8-agent council, then a
> single-agent proposal-only design). Both were wrong about the same thing: they kept
> the agents' output as a *suggestion* consumed by a pipeline. This design gives the
> agents **real tools that open and modify live positions**, with every call passing
> through a deterministic guard that can refuse. That is what was asked for, and §3
> explains why it is safe.

---

## 0. The loop, end to end

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │ 1. DETERMINISTIC PRE-PASS · 0 LLM                                    │
   │    options_fit · chain fetch · funnel · iv_rank · patterns           │
   │    no setup → HOLD, zero tokens                                      │
   └──────────────────────────────────────────────────────────────────────┘
                                   ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ 2. THE ARGUMENT · 2 agents, run in PARALLEL · 1 hop                  │
   │    BULL agent  — argues the trade, names structure + strategy        │
   │    BEAR agent  — argues against it, or for a different structure     │
   └──────────────────────────────────────────────────────────────────────┘
                                   ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ 3. RESOLUTION · deterministic                                        │
   │    agree on direction  → proceed                                     │
   │    disagree            → HOLD. no trade. (§2.3)                      │
   └──────────────────────────────────────────────────────────────────────┘
                                   ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ 4. THE TRADE CALL · winning agent calls open_option_trade(...)       │
   │    guard.before → select_contract → sizing → engine.risk (13 rules)  │
   │    veto → is_error tool_result naming the rule → agent may retry once │
   │    pass → packages/broker → fill → guard.after arms the trail        │
   └──────────────────────────────────────────────────────────────────────┘
                                   ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │ 5. DETERMINISTIC MANAGEMENT · every 30s · 0 LLM                      │
   │    trailing stop + trailing take-profit ratchet (already shipped)    │
   │    hard stop · time stop · DTE≤2 expiry sweep                        │
   └──────────────────────────────────────────────────────────────────────┘
                                   ▼  material change only (§5.2)
   ┌──────────────────────────────────────────────────────────────────────┐
   │ 6. ESCALATION · trail state goes BACK to the agents · 1 hop          │
   │    they call adjust_option_position(...):                            │
   │      SCALE_IN · EXIT_NOW · RAISE_TAKE_PROFIT · TIGHTEN_STOP · HOLD   │
   │    guard.before re-runs the full risk engine on any loosening        │
   └──────────────────────────────────────────────────────────────────────┘
                                   └──────────► back to 5
```

**The protection floor only ever ratchets tighter.** An agent can bank early, tighten a
stop, or (gated) add size. **No agent action can widen or remove a stop.** That single
invariant is what makes handing them live trade tools safe, and §3.3 enforces it in code.

---

## 1. `llm.py` — full tool access

Three commits. Land 1 alone and prove the suite green before 2.

### Commit 1 — block walk (touches every existing council node)

`llm.py:153` is `msg.content[0].text` — it takes block **[0]** and assumes text. A
`tool_use` block breaks it. Replace with a walk:

```python
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]

def _extract_blocks(msg: Any) -> tuple[str, tuple[ToolCall, ...]]:
    texts, calls = [], []
    for block in (msg.content or []):
        t = getattr(block, "type", None)
        if t == "text":
            texts.append(block.text)
        elif t == "tool_use":
            calls.append(ToolCall(block.id, block.name, dict(block.input or {})))
    return "\n".join(texts), tuple(calls)
```

`LLMResponse` gains `tool_calls: tuple[ToolCall, ...] = ()` and `stop_reason: str | None`.
Behaviour-identical for all five existing nodes (one text block → the same string).

### Commit 2 — `complete_tools()`, a sibling method

**Do not overload `complete()`.** It hardcodes `messages=[{"role":"user",...}]` and is
on the hot path of five nodes. A tool loop needs caller-supplied `messages` so it can
append assistant + `tool_result` turns.

```python
async def complete_tools(
    self, *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str = Model.SONNET,
    max_tokens: int = 2048,
    tool_choice: dict[str, Any] | None = None,
    cache_system: bool = True,
    council_run_id: str | None = None,
    agent_decision_id: str | None = None,
    user_id: str | None = None,
) -> LLMResponse: ...
```

Passes `tools=` / `tool_choice=` to `client.messages.create`, uses `_extract_blocks`,
records to the cost ledger identically.

> 🚨 **MOCK mode must never emit a `tool_use` block.** The loop terminates on "no tool
> calls"; a mock that emits one loops forever. Return a normal text response.

### Commit 3 — the agent loop

Lives in the node, **`max_rounds=3`** (propose → tool result → final). Every round:
append the assistant turn, run each `tool_use` through the guard, append a
`tool_result` block per call — **including denials, as `is_error: true`.**

**A new role needs a branch in BOTH** `_mock_response` (`llm.py:311`, matches
`"you are the <role>"` in the first 120 chars) **and** `infer_role_from_system_prompt`
(`cost_ledger.py:199`). Miss either and you get a silent generic response plus
`"unknown"` in the cost ledger. Neither raises.

---

## 2. The two agents

### 2.1 Roles

| Agent | Model | System prompt starts | Job |
|---|---|---|---|
| **Bull** | Sonnet | `You are the Options Bull Agent` | Argue FOR a trade: direction, structure, strategy, thesis **with a deadline**, conviction |
| **Bear** | Sonnet | `You are the Options Bear Agent` | Argue AGAINST it — or for a different structure. Name the specific risk: IV rank, theta, liquidity, event, trend conflict |

**Both run in parallel** (`asyncio.gather`) on the identical deterministic pre-pass.
They do not see each other's output in round 1 — that keeps it a genuine independent
read rather than one anchoring the other.

### 2.2 What they may decide, and what they may not

**May:** direction (long/short thesis) · strategy family · conviction 0–1 · the thesis
and its timeframe · take-profit and stop-loss *preferences* (bounded, §3.3) · whether
to trade at all.

**May not:** strike · expiry · OCC symbol · quantity · whether risk approves.
`select_contract`'s 6-stage funnel and `options_position_size` own those, deterministically,
and the funnel is the demo. An agent that picks strikes destroys both the safety
property and the story.

### 2.3 Resolution — deterministic, and disagreement means no trade

```
bull.direction == bear.direction   → proceed, conviction = min(bull, bear)
directions differ                  → HOLD, reason "agents_disagree"
either abstains                    → HOLD
conviction gap > 0.4               → HOLD, reason "conviction_divergence"
```

**`min()`, not the average.** Two agents agreeing weakly is a weak trade; taking the
higher number would let one enthusiastic agent drag the delta band toward the money.

*"We only trade when both agents agree, and we size on the less confident one"* is a
real, checkable risk control — and it costs zero extra latency because both already ran.

---

## 3. Tools + guard — the hooks, built here

> 🚨 **`PreToolUse`/`PostToolUse` are Claude Code features**, configured in
> `.claude/settings.json`, gating tool calls made by Claude Code itself. **This runtime
> is FastAPI + LangGraph calling the Anthropic SDK directly — there is no hook system
> and nothing to configure.** The concept is exactly right; you are building the
> equivalent. An agent hunting for a hooks config will lose an hour.

`apps/agents/trading_agents/tools/guard.py`:

```python
@dataclass(frozen=True)
class GuardVerdict:
    allow: bool
    reason: str | None          # named, like a risk veto rule
    payload: dict | None        # after() may NARROW a result, never widen it

class ToolGuard:
    def before(self, tool: str, args: dict, ctx: GuardContext) -> GuardVerdict: ...
    def after(self, tool: str, args: dict, result: object, ctx: GuardContext) -> GuardVerdict: ...
```

### 3.1 The tools

**Read-only** — `get_funnel_counts`, `get_option_snapshot`, `get_underlying_bars`,
`get_iv_rank`, `get_position_snapshot`, `get_entry_thesis`.

**Mutating — two, and only two:**

```python
open_option_trade(
    underlying: str,          # ticker, NOT an OCC symbol
    direction: Literal["long", "short"],
    strategy: str,            # a registered strategy id
    conviction: float,        # 0..1 — selects the delta band
    thesis: str,              # must contain a timeframe
    take_profit_pct: float,   # bounded, §3.3
    stop_loss_pct: float,     # bounded, §3.3
) -> dict

adjust_option_position(
    decision_id: str,
    action: Literal["SCALE_IN", "EXIT_NOW", "RAISE_TAKE_PROFIT", "TIGHTEN_STOP", "HOLD"],
    value: float | None,      # new pct for the RAISE/TIGHTEN actions
    reason: str,
) -> dict
```

> **Note the shape of `open_option_trade`.** The agent names *what to express and how
> strongly*. It does **not** pass a contract, a strike or a quantity — the guard derives
> those deterministically. So a hallucinated OCC symbol is not a category of bug that
> can exist, because the agent never supplies one.

### 3.2 `guard.before` for `open_option_trade` — the whole risk stack, inline

1. Validate args: `SYMBOL_RE`, `strategy` in the registry, `0 ≤ conviction ≤ 1`,
   thesis non-empty and parses a timeframe, TP/SL inside §3.3's bands.
2. Confirm the two agents resolved to this direction (§2.3). A tool call that
   contradicts the resolution is denied `direction_contradicts_resolution`.
3. `select_contract(...)` → no survivor ⇒ deny with the **named funnel rejection**
   (`no_delta_in_band`, `no_liquid_contract`, …).
4. `options_position_size(...)` → qty < 1 ⇒ deny `size_rounds_to_zero`.
5. **`engine.risk.evaluate(...)` — all 13 options rules.** Veto ⇒ deny with the rule name.
6. Per-pass ceiling: **one `open_option_trade` per symbol per pass.**
7. `AUTO_TRADE_ENABLED` **and** paper mode **and** market open, else deny.

**A denial returns a `tool_result` with `is_error: true` and the named reason.** The
agent sees *"denied: illiquid_contract"* and may adapt once (different strategy, lower
conviction). It never sees a stack trace, and the pass never aborts.

Only after all seven pass does the guard call `packages/broker`. **The agent's tool call
is a request; the guard is the gate. The order still routes `engine.risk` →
`packages/broker`, exactly as before — the call site moved, the invariant did not.**

### 3.3 🔒 The ratchet invariant — the one rule that makes this safe

```
STOP:         may only move UP (tighter). Never down, never removed. Never widened.
TAKE PROFIT:  may only move UP.
EXIT_NOW:     always allowed — de-risking is never blocked.
SCALE_IN:     full engine.risk re-check + counts against max_total_premium_pct
              + hard cap of 2 adds per position.
```

Bounds on what an agent may even ask for:

| Field | Band | Enforced by |
|---|---|---|
| `stop_loss_pct` | 25–50 | guard, clamped |
| `take_profit_pct` | 40–300 | guard, clamped |
| trailing arm | fixed 35% | **caps, not agent-settable** |
| trailing giveback | fixed 30% of peak | **caps, not agent-settable** |

> **An agent asking to loosen a stop is denied, logged, and the position keeps its
> existing protection.** There is no code path — none — by which any model output
> reduces the protection on an open position. Write that as a test (§10) and keep it
> passing.

### 3.4 `guard.after`

Persist the audit row (tool name, args, verdict, named reason, latency) · assert result
rows are scoped to this `user_id` · truncate large payloads before they re-enter context ·
on a successful open, **arm the trailing manager** and stamp `approval_mode='auto'`.

---

## 4. Autonomy switch — off by default, and it is yours to flip

```
AUTO_TRADE_ENABLED=0     # master switch for the mutating tools
```

Plus the existing two-key `auto_approve_consent` per broker connection. **Ship it off.**
Enabling autonomous order placement is the account owner's decision, not a deploy's —
and it is the moment this stops being a code change.

Hard-coded, not configurable: **paper only.** `trading_mode() == "paper"` **and**
`not LIVE_TRADING_ENABLED`, checked in `before`, regardless of any flag. In paper the
blast radius is a number on a dashboard; in live it is money placed by a loop.

---

## 5. The management loop — deterministic first, agents second

### 5.1 What runs without any LLM (already shipped — do not rebuild)

The trailing ratchet from [`PLAN_EXIT_AGENT.md`](PLAN_EXIT_AGENT.md), on the 30s
reconciler tick: peak tracking, trail line = `peak × (1 − 0.30)`, hard stop −40%, hard
ceiling +150%, time stop, `DTE ≤ 2` expiry sweep. **This is the safety net and it never
waits for a model.**

### 5.2 When the agents get called back

Escalate **only on a material change**, never on a quiet tick:

- the ratchet just **armed** (crossed +35%), or
- peak advanced **≥15pp** since the last escalation, or
- price is **within 10pp** of the trail line, or
- **DTE ≤ 5** and still open.

Plus: market open · cooldown ≥900s · ≤4 escalations per position per day · **1 per
tick** (bounds the fleet tick's latency).

The agents receive: entry premium, current, peak, trail line, DTE, days held, the
original thesis and its deadline. Then they call `adjust_option_position`.

**Fail-safe is `HOLD`** — on error, timeout, malformed output or MOCK mode, nothing
changes and the deterministic ratchet keeps running. An Anthropic outage must never
move a position.

### 5.3 Why this is the interesting half

Any entry can be lucky. *"The agents argued, opened, watched it run +80%, and then
argued again about whether to bank it or let the trail work"* — that is a system
managing a position, and it is visible in the audit trail. Nobody else in the field
will show that.

---

## 6. The UI — make the argument and the management visible

### 6.1 The live debate (SSE)

Today `useCouncilProgress` polls every 600 ms. `progress.py`'s `ProgressEvent` is
already the right shape.

- `GET /api/v1/agent/run/{id}/stream` — FastAPI `StreamingResponse`,
  `text/event-stream`; client `EventSource`. **Keep the poll as a fallback** — proxies
  and mobile drop long-lived connections, and a demo that stalls silently is worse.
- Render Bull and Bear as **two columns filling in simultaneously** (they run in
  parallel — show that), then the resolution banner: *agreed / disagreed → no trade*,
  then the tool call and the guard's verdict.
- **Stream guard denials too.** *"Agent asked to open NVDA calls → denied:
  `illiquid_contract`"* is the propose/dispose story happening on screen in real time.
  It is the single most persuasive thing this product can show.
- `progress.NodeName` is a closed `Literal` — add the new nodes there **and** to
  `NODE_ORDER`, or the theater renders an unnamed lane.

### 6.2 The position card

Per open option: entry premium · current mark · **peak** · the trail line as a moving
floor · hard stop · DTE · and each agent escalation with what they decided and why.

The trail line rising underneath a live position is the clearest possible picture of
"deterministic protection that only tightens".

### 6.3 Charts

**TradingView Lightweight Charts** (open source, ~45 KB, no account, no network). There
is no usable TradingView *data* API here — Alpaca is the data source, and Alpaca's
TradingView integration is a human trading UI that yields nothing programmatic.
⚠️ Verify the licence on the repo and honour attribution.

Priority: **the contract's own premium curve** with entry, peak, trail line and exit
marked — *"$2.17 → $3.40, ratchet armed here, banked here"*. Then the funnel
(4,128 → 2,064 → 1,843 → 130 → 3 → 1). Then underlying candles with the detected
pattern marked (that detector shipped and nothing displays it).

---

## 7. Options scope

Single-leg **long calls and long puts** work end to end today. Build on them.

| Structure | Max loss | Work | Verdict |
|---|---|---|---|
| Long call / long put | premium | **none** | ✅ ship |
| Vertical spreads | defined | multi-leg across sizing, risk, ghost eval, close path — `OptionLegDetails.action` is a 2-value `Literal`, single-leg is load-bearing throughout | ⚠️ flag-gated, after |
| CSP / covered call | defined | collateral locking + assignment, neither exists | ❌ not now |
| Naked short call | **unbounded** | — | ❌ **never** |

`naked_short_forbidden` stays. A "short" thesis is expressed by **buying a put**.

**Rules for the prompts:** long premium wants **low IV rank** (build `iv_rank`, ~1h —
the most valuable new feature here); prefer the upper half of the 10–45 DTE window
(theta accelerates inside ~21 DTE); **every thesis carries a deadline** — validated, not
hoped for. Standard practitioner heuristics, stated as such — thresholds need a
backtest, hard bounds live in `engine.risk`, never in a prompt.

---

## 8. Latency

| Path | LLM hops |
|---|---|
| No setup (most symbols) | **0** |
| Agents disagree → HOLD | **1** (both parallel) |
| Open a trade | **2** (argue → tool call) |
| Escalation | **1** |
| *(equity council today)* | *3* |

`options_fit` first · `asyncio.gather` for the pair · `cache_system=True` · pre-load the
pre-pass so read-tools are the exception · `max_rounds=3` hard cap · one escalation per
tick.

**Free win to check first:** are today's three equity analysts parallel or sequential?
`_run_linear` is sequential by construction; the LangGraph branch may not be. If
sequential, `asyncio.gather` helps equity too and costs nothing.

---

## 9. Scope — ~3 focused days

| Task | Est. |
|---|---|
| `llm.py` commits 1–3 (block walk, `complete_tools`, loop) | 4h |
| `tools/guard.py` + 6 read-only tools | 3h |
| `open_option_trade` + the full `before` stack | 4h |
| `adjust_option_position` + ratchet invariant | 3h |
| Bull/Bear agents, prompts, mock branches, ledger roles | 4h |
| Deterministic resolution + validators | 2h |
| Escalation wiring into the 30s tick | 3h |
| SSE + debate UI | 4h |
| Position card + charts | 5h |
| `iv_rank` | 1h |
| **Total** | **~33h** |

Cut order: charts → `iv_rank` → SSE (poll still works) → escalation loop (the ratchet
alone still protects). **Never cut** the guard, the risk stack in `before`, or the
ratchet invariant.

Ship behind `USE_OPTIONS_AGENT=0` **and** `AUTO_TRADE_ENABLED=0`. Prove one pass in
dry-run (guard evaluates, logs the verdict, does **not** place) before either flips.
**Never deploy a new agent graph into a live session.**

---

## 10. Tests — revert-check matrix

| Test | Break this to make it fail |
|---|---|
| **`test_agent_cannot_widen_a_stop`** | Let `adjust_option_position` lower a stop. **The single most important test in this plan.** |
| **`test_open_trade_runs_the_full_risk_engine`** | Skip `evaluate()` in `before` |
| **`test_risk_veto_returns_is_error_not_an_exception`** | Raise instead |
| `test_agents_disagreeing_means_no_trade` | Proceed on divergence |
| `test_conviction_is_the_min_not_the_mean` | Use the average |
| `test_agent_cannot_supply_a_contract_or_qty` | Accept `occ_symbol` in the tool schema |
| `test_never_trades_in_live_mode` | Make the paper check configurable |
| `test_disabled_without_AUTO_TRADE_ENABLED` | Default it on |
| `test_scale_in_recheck_counts_against_total_premium` | Skip the aggregate cap |
| `test_escalation_failure_holds` | Return EXIT on a timeout |
| `test_mock_mode_emits_no_tool_use` | Make the mock emit one — infinite loop |
| `test_tool_loop_bounded_at_three_rounds` | Raise `max_rounds` |
| `test_naked_short_still_forbidden` | Add `sell_to_open` to `_ALLOWED_ACTIONS` |

**Baseline: 969 passed, 11 skipped** (Python) + 28 (jest). `git stash` and re-run before
blaming your change.

---

## 11. Where you will go wrong

1. **Hunting for a PreToolUse hook config.** Does not exist here. §3.
2. **Letting the agent pass a strike, an OCC symbol or a qty.** The guard derives them.
   A hallucinated contract then isn't a possible bug.
3. **Skipping `engine.risk` inside `before`** because "the agent already decided". The
   agent decided *what to express*. Risk decides *whether*.
4. **Raising a tool denial as an exception.** It aborts the pass. Return `is_error`.
5. **Letting any agent action loosen protection.** §3.3.
6. **Escalating on every tick.** Material changes only, cooldown, daily cap, one/tick.
7. **Failing open on escalation.** A timeout means HOLD, not EXIT.
8. **A mock that emits `tool_use`.** Infinite loop.
9. **Overloading `complete()`** instead of adding `complete_tools()`. Five nodes ride
   that path.
10. **Turning `AUTO_TRADE_ENABLED` on yourself.** The account owner flips it.
11. **Deleting the SSE fallback poll.**
12. **Deploying into a live session.**

---

*Related: [`PLAN_CLI_SURFACE.md`](PLAN_CLI_SURFACE.md) · [`PLAN_MCP_DEMO.md`](PLAN_MCP_DEMO.md) ·
[`PLAN_EXIT_AGENT.md`](PLAN_EXIT_AGENT.md) · [`PLAN_AUTO_APPROVE.md`](PLAN_AUTO_APPROVE.md) ·
[`OPTIONS_PLAYBOOK.md`](OPTIONS_PLAYBOOK.md) · [`../CLAUDE.md`](../CLAUDE.md)*
