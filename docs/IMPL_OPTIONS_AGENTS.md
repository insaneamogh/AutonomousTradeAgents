# IMPL 2 — two arguing options agents with guarded trade tools

**Implementation spec.** Depends on [`IMPL_LLM_TOOLS.md`](IMPL_LLM_TOOLS.md).
Written 2026-08-31 by `ID:MODEL1REAL`. Est **16h**.
Design rationale lives in [`PLAN_OPTIONS_AGENTS.md`](PLAN_OPTIONS_AGENTS.md) — read §0
there first if you want to know *why* it is two agents and not eight.

---

## 0. Files you will create

```
apps/agents/trading_agents/options/
    __init__.py
    agents.py          Bull + Bear nodes
    resolution.py      deterministic agree/disagree
    prompts.py         both system prompts
    tools/
        __init__.py
        schemas.py     Anthropic tool JSON schemas
        registry.py    name -> handler
        guard.py       ToolGuard: before() / after()
        readonly.py    6 read-only handlers
        trade.py       open_option_trade / adjust_option_position
```

---

## 1. The tool schemas — `tools/schemas.py`

> **The agent never supplies a contract, strike, expiry or quantity.** The guard derives
> them from `select_contract` + `options_position_size`. A hallucinated OCC symbol is
> therefore not a category of bug that can exist.

```python
OPEN_OPTION_TRADE = {
    "name": "open_option_trade",
    "description": (
        "Open a long option position on an underlying. You do NOT choose the "
        "contract, strike, expiry or quantity — the deterministic selector picks "
        "them from your direction and conviction. The trade is placed only if it "
        "clears all 13 risk rules; if it does not you will be told which rule "
        "refused it and may adjust once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "underlying":      {"type": "string", "description": "Ticker, e.g. NVDA. Never an OCC symbol."},
            "direction":       {"type": "string", "enum": ["long", "short"]},
            "strategy":        {"type": "string", "description": "A registered strategy id."},
            "conviction":      {"type": "number", "minimum": 0, "maximum": 1},
            "thesis":          {"type": "string", "description": "Must state a timeframe."},
            "take_profit_pct": {"type": "number", "minimum": 40,  "maximum": 300},
            "stop_loss_pct":   {"type": "number", "minimum": 25,  "maximum": 50},
        },
        "required": ["underlying", "direction", "strategy", "conviction",
                     "thesis", "take_profit_pct", "stop_loss_pct"],
    },
}

ADJUST_OPTION_POSITION = {
    "name": "adjust_option_position",
    "description": (
        "Act on an open option position whose trailing ratchet reported a "
        "material change. Stops and take-profits may only move UP (tighter/"
        "higher). Any request to loosen protection is refused."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "decision_id": {"type": "string"},
            "action": {"type": "string",
                       "enum": ["SCALE_IN", "EXIT_NOW", "RAISE_TAKE_PROFIT",
                                "TIGHTEN_STOP", "HOLD"]},
            "value":  {"type": "number", "description": "New pct for RAISE/TIGHTEN."},
            "reason": {"type": "string"},
        },
        "required": ["decision_id", "action", "reason"],
    },
}
```

Read-only tools: `get_funnel_counts`, `get_option_snapshot`, `get_underlying_bars`,
`get_iv_rank`, `get_position_snapshot`, `get_entry_thesis`. Same pattern, all
`"type": "object"` schemas, all scoped to the caller's `user_id` **inside the handler**.

---

## 2. The guard — `tools/guard.py`

```python
@dataclass(frozen=True)
class GuardVerdict:
    allow: bool
    reason: str | None = None       # named, like a risk veto rule
    payload: dict[str, Any] | None = None   # after() may NARROW, never widen


@dataclass(frozen=True)
class GuardContext:
    user_id: str
    council_run_id: str
    resolved_direction: str | None   # from resolution.py
    resolved_conviction: float | None
    calls_this_pass: int
    caps: RiskCaps


class ToolGuard:
    def before(self, tool: str, args: dict, ctx: GuardContext) -> GuardVerdict: ...
    def after(self, tool: str, args: dict, result: Any, ctx: GuardContext) -> GuardVerdict: ...
```

### 2.1 `before("open_option_trade", …)` — the full stack, in order

```
1.  AUTO_TRADE_ENABLED set                      else deny "auto_trade_disabled"
2.  trading_mode()=="paper" and not LIVE_TRADING_ENABLED
                                                else deny "live_mode_refused"   ← hard-coded
3.  is_us_market_open(now)                      else deny "market_closed"
4.  ctx.calls_this_pass < 1                     else deny "one_open_per_pass"
5.  SYMBOL_RE.match(underlying)                 else deny "malformed_symbol"
6.  strategy in registry                        else deny "unknown_strategy"
7.  direction == ctx.resolved_direction         else deny "direction_contradicts_resolution"
8.  thesis parses a timeframe                   else deny "thesis_without_timeframe"
9.  clamp take_profit_pct to [40,300], stop_loss_pct to [25,50]
10. select_contract(...)   -> None              deny with the NAMED funnel reason
                                                 (no_delta_in_band / no_liquid_contract / …)
11. options_position_size(...) qty < 1          deny "size_rounds_to_zero"
12. engine.risk.evaluate(...)  vetoed           deny with decision.veto_rule
```

Only after all twelve does the handler call `packages/broker`.

> **The order still routes `engine.risk` → `packages/broker`.** The call site moved
> inside a guard; the invariant did not move. Step 12 is the same `evaluate()` the
> executor runs — **do not reimplement any risk logic here** (CLAUDE.md §4.4).

### 2.2 🔒 `before("adjust_option_position", …)` — the ratchet invariant

```python
ACTION_RULES = {
    "EXIT_NOW":          "always_allow",     # de-risking is never blocked
    "TIGHTEN_STOP":      "must_increase",    # new stop pct > current
    "RAISE_TAKE_PROFIT": "must_increase",    # new TP pct > current
    "SCALE_IN":          "full_risk_recheck",
    "HOLD":              "always_allow",
}
```

- `TIGHTEN_STOP` / `RAISE_TAKE_PROFIT` with a value **≤ current** → deny
  `"cannot_loosen_protection"`. Log it. Position keeps its existing protection.
- `SCALE_IN` → re-run `evaluate()` in full, count against `options_max_total_premium_pct`,
  and cap at **2 adds per position** (`adds_this_position < 2` else
  `"scale_in_cap_reached"`).
- **There is no action that widens a stop.** Not by argument, not by omission, not by a
  `None` that falls through to a default. Assert it in a test.

### 2.3 `after(...)`

Persist an audit row per call (`tool`, `args`, `allow`, `reason`, latency_ms) into
`reasoning.tool_log` via `jsonb_set` — **never a whole-column overwrite**, the council
owns the other keys in that column. Assert every returned row is scoped to
`ctx.user_id`. Truncate payloads > 8 KB before they re-enter the context window. On a
successful open: arm the trailing manager and stamp `approval_mode='auto'`.

### 2.4 Dispatch never raises

```python
async def dispatch(call: ToolCall) -> dict[str, Any]:
    handler = REGISTRY.get(call.name)
    if handler is None:
        return {"is_error": True, "content": {"denied": "unknown_tool"}}
    v = guard.before(call.name, call.input, ctx)
    if not v.allow:
        return {"is_error": True, "content": {"denied": v.reason}}
    try:
        result = await handler(call.input, ctx)
    except Exception as exc:
        logger.exception("tool %s failed", call.name)
        return {"is_error": True, "content": {"denied": "tool_failed"}}
    after = guard.after(call.name, call.input, result, ctx)
    if not after.allow:
        return {"is_error": True, "content": {"denied": after.reason}}
    return {"is_error": False, "content": after.payload or result}
```

A denial **teaches the model and the pass continues**. An exception would abort it.

---

## 3. The agents — `options/agents.py`

### 3.1 Prompts must begin with these exact strings

```
You are the Options Bull Agent …
You are the Options Bear Agent …
```

Both `_mock_response` and `infer_role_from_system_prompt` anchor on that phrase
(IMPL_LLM_TOOLS §4). Add a branch in **both**, plus a test each.

### 3.2 Bull prompt — the shape

```
You are the Options Bull Agent on a quantitative desk.

You are given a COMPLETE deterministic pre-pass: strategy fit, candlestick
patterns, IV rank, realized vol, the option-chain funnel counts, and
liquidity. You do not need to fetch anything in the common case.

Argue FOR a trade if one is there. Decide:
  direction   long (call) or short (put) — a bearish view is expressed by
              BUYING A PUT. You never sell to open.
  strategy    a registered strategy id
  conviction  0-1. This selects the delta band, so be honest: high
              conviction shops closer to the money and costs more premium.
  thesis      ONE sentence, and it MUST contain a timeframe. Theta is
              always against a long option, so a thesis with no deadline
              cannot be checked. "NVDA looks strong" is not a thesis.
              "NVDA breaks 190 within 3 weeks on the volume expansion" is.

If there is no trade, say so. Standing down is a valid, common answer.

You do NOT choose the strike, expiry, contract or quantity.
```

Bear prompt is the mirror: name the **specific** risk — IV rank too high (paying for a
crush), theta vs the timeframe, liquidity, trend conflict, event risk — or argue for a
different direction.

### 3.3 Both run in parallel

```python
bull_task = asyncio.create_task(run_bull(state, llm))
bear_task = asyncio.create_task(run_bear(state, llm))
bull, bear = await asyncio.gather(bull_task, bear_task)
```

**One hop, not two.** Assert concurrency in a test (wall-clock, not call count).
Neither sees the other's output — anchoring would make the second opinion worthless.

---

## 4. Resolution — `options/resolution.py`

```python
@dataclass(frozen=True)
class Resolution:
    proceed: bool
    direction: str | None
    conviction: float
    reason: str          # named: agreed | agents_disagree | conviction_divergence | abstained


def resolve(bull: AgentView, bear: AgentView) -> Resolution:
    if bull.direction is None or bear.direction is None:
        return Resolution(False, None, 0.0, "abstained")
    if bull.direction != bear.direction:
        return Resolution(False, None, 0.0, "agents_disagree")
    if abs(bull.conviction - bear.conviction) > 0.4:
        return Resolution(False, None, 0.0, "conviction_divergence")
    return Resolution(True, bull.direction, min(bull.conviction, bear.conviction), "agreed")
```

> **`min`, not the mean.** Two agents agreeing weakly is a weak trade. Averaging lets one
> enthusiastic agent drag the delta band toward the money. This is a real risk control
> and it costs nothing — both already ran.

Only the **Bull** agent gets the trade tool, and only when `resolve().proceed` is True.

---

## 5. The escalation loop

### 5.1 Trigger — in `manage_positions_for_user`, after `_exit_reason` returns None

Escalate only on a **material change**:
- ratchet just **armed** (crossed +35%), or
- peak advanced **≥15pp** since last escalation, or
- price within **10pp** of the trail line, or
- **DTE ≤ 5** and still open

Plus: market open · cooldown ≥900s · ≤4 per position per day · **1 per fleet tick**
(bounds the 30s tick's latency).

### 5.2 What the agents receive

`entry_premium`, `current_pct`, `peak_pct`, `trail_line_pct`, `dte`, `days_held`, the
original `thesis` and its parsed deadline, and whether the deadline has passed.

### 5.3 Fail-safe is `HOLD`

Error, timeout, malformed output, MOCK mode → **nothing changes**, the deterministic
ratchet keeps running. An Anthropic outage must never move a position.

---

## 6. Config

| Var | Default | Meaning |
|---|---|---|
| `USE_OPTIONS_AGENT` | `0` | Route options through the new graph |
| `AUTO_TRADE_ENABLED` | `0` | Master switch for the two mutating tools |
| `OPTIONS_AGENT_MAX_ROUNDS` | `3` | Tool-loop cap |
| `OPTIONS_ESCALATION_COOLDOWN_S` | `900` | |
| `OPTIONS_ESCALATION_MAX_PER_DAY` | `4` | |

**Paper-only is hard-coded, not configurable.** Ship both flags off; the account owner
flips them. Prove one **dry-run** pass first (guard evaluates, logs the verdict, does
**not** place) before `AUTO_TRADE_ENABLED=1`.

---

## 7. Tests — revert-check matrix

`apps/agents/tests/test_options_agents.py`, `apps/agents/tests/test_tool_guard.py`

| Test | Break this to make it fail |
|---|---|
| **`test_agent_cannot_widen_a_stop`** | Let `TIGHTEN_STOP` accept a lower value. **The single most important test in this repo.** |
| **`test_open_trade_runs_the_full_risk_engine`** | Skip `evaluate()` in `before` |
| **`test_risk_veto_returns_is_error_not_an_exception`** | Raise instead |
| `test_agent_cannot_supply_a_contract_or_qty` | Add `occ_symbol`/`qty` to the schema |
| `test_agents_disagreeing_means_no_trade` | Proceed on divergence |
| `test_conviction_is_the_min_not_the_mean` | Use the average |
| `test_never_trades_in_live_mode` | Make the paper check read a flag |
| `test_disabled_without_auto_trade_enabled` | Default it on |
| `test_scale_in_counts_against_total_premium` | Skip the aggregate cap |
| `test_scale_in_capped_at_two_adds` | Remove the counter |
| `test_exit_now_always_allowed` | Gate it behind a risk check |
| `test_escalation_failure_holds` | Return EXIT on timeout |
| `test_unknown_tool_denies_and_does_not_raise` | Raise |
| `test_agents_run_concurrently` | Sequential awaits — assert wall-clock |
| `test_tool_log_preserves_other_reasoning_keys` | Whole-column overwrite kills `contract_funnel` |
| `test_bull_role_resolves_in_mock_and_cost_ledger` | Remove either branch |
| `test_naked_short_still_forbidden` | Add `sell_to_open` to `_ALLOWED_ACTIONS` |

**Baseline: 969 passed, 11 skipped.**

---

## 8. Where you will go wrong

1. **Hunting for a `PreToolUse` hook config.** Claude Code feature; does not exist in
   this FastAPI/LangGraph runtime. §2 *is* the equivalent.
2. **Letting the agent pass a strike, OCC symbol or qty.** The guard derives them.
3. **Skipping `evaluate()`** because "the agent already decided". It decided *what to
   express*; risk decides *whether*.
4. **Raising on a denial.** Aborts the pass. Return `is_error`.
5. **Any path that loosens protection.** §2.2.
6. **Running Bull and Bear sequentially.** Doubles latency; assert concurrency.
7. **Escalating every tick.** Material changes only.
8. **Failing open on escalation.** Timeout ⇒ HOLD.
9. **Whole-column `reasoning` writes.** Use `jsonb_set` + `COALESCE`.
10. **Flipping `AUTO_TRADE_ENABLED` yourself.**

---

*Next: [`IMPL_CONTRACT_FUNNEL_UI.md`](IMPL_CONTRACT_FUNNEL_UI.md)*
