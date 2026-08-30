# Plan A — the agentic option exit

**Status:** plan, not built. Written 2026-08-30 by `ID:MODEL1REAL` for the next model to
execute. **Nothing in this document is in the code yet.**

> **How to use this file.** Read §0 before anything else. Implement in the order of §7.
> Every claim marked ✅ was measured this session against the live repo. Every claim marked
> ⚠️ is an assumption *you* must verify before you rely on it.

---

## 0. What is verified, what is assumed, what you must check

### ✅ Verified (measured 2026-08-30 — do not re-derive)

- `manage_positions_for_user` and `sweep_expiring_options_for_user` are called **only** from
  `apps/api/app/services/orders/reconciler_fleet.py:185-202`, on a **30-second** in-process
  asyncio tick (`FleetConfig.interval_seconds = 30.0`, env `RECONCILER_INTERVAL_SECONDS`).
  Started in the FastAPI lifespan **only when `USE_POSTGRES` and `RECONCILER_ENABLED`**.
- **There is no market-hours gate anywhere in `apps/api/app/services/orders/`.** The close
  ladder runs every 30s, 24/7, including weekends.
- `_exit_reason` (`position_manager.py:435-497`) ladder today: premium exit (options) → time
  stop → signal exit. Returns `None` = hold. Its docstring says *"Deterministic reads only."*
- Option marks come from `_option_pl_pct_by_symbol` — **one** `broker.list_positions()` per
  user tick, only when an option is open. Failure returns `{}` and everything holds.
- `Position.unrealized_pl_pct` is Alpaca's `unrealized_plpc * 100` (`broker/alpaca.py:404`).
  A missing value maps to `0.0`, not `None`.
- `agent_decisions.close_reason` is **`String(20)`**.
- `agent_decisions.reasoning` is nullable JSONB. `contract_funnel` and `strategy_fit` already
  live there.
- `apps/agents/trading_agents/llm.py` has **no tool-use support**. `LLM.complete` builds
  `messages=[{"role":"user","content":user}]` and extracts `msg.content[0].text`
  (`llm.py:153`) — a `tool_use` block breaks it. `anthropic==0.109.1` is installed and
  supports tools fine; only the wrapper doesn't.
- **Nothing in this repo uses tool use.** Zero hits for `tool_use|tools=|tool_choice|input_schema`.
- `_mock_response` (`llm.py:311`) branches on `"you are the <role>"` in the **first 120
  chars** of the system prompt. `infer_role_from_system_prompt` (`cost_ledger.py:199`) does
  the same over the first 160. An unmatched role gets a generic `{score, confidence, thesis}`
  and `"unknown"` in the ledger.
- `reflection_agent_run` (`nodes/reflection.py:39-47`) is the **only** precedent for a
  non-council LLM call with its own signature. Copy its shape.
- `drafter.py:180-187` is the precedent for a deterministic post-filter on LLM output.

### ⚠️ Assumed — verify before relying on it

- That an Alpaca paper account reports `unrealized_plpc` on an option position promptly
  enough for a 30s loop. **Check this Monday with one real open contract before enabling the
  ratchet in anger.** If the field lags or sticks, the trail is measuring stale data.
- That `asyncio.wait_for` around the Anthropic call reliably cancels in-flight HTTP. It
  should; confirm the fleet tick does not slip past 30s under a forced timeout.

### 🔍 You must check first

- Run the baseline **before** you touch anything: `792 passed, 9 skipped`. If your number
  differs at the start, something else is wrong and it is not your change.

---

## 1. Why this exists

An open long option has three ways out today: the calendar time stop (5 days on a "short"
horizon), a signal exit, and the `dte ≤ 2` expiry sweep. Plus a fixed +60% take-profit and a
−50% stop added 2026-08-30.

Two problems, and the second is the reason for this whole document:

1. **The fixed +60% take-profit cuts winners short.** The user's stated strategy is *hold the
   winners, cut the losers early*. A hard ceiling at +60% does the exact opposite of the
   first half — it guarantees you exit every large move at the smallest point that qualifies
   as large.
2. **Nothing reasons about *why* a position is up.** +60% because the underlying broke out
   with three days of follow-through is a different position from +60% because IV spiked on
   one gap. The deterministic engine cannot tell those apart. A model can.

**This is not fixable with a broker bracket.** Alpaca cannot bracket a single-leg option —
`OrderClass` allows only `simple`/`mleg` for `us_option`, and `broker.alpaca` raises
`OptionBracketNotSupportedError`. The protection every equity entry gets from the broker is
structurally unavailable here, which is why it has to live in our own sweep.

---

## 2. The architecture question, answered

The repo's one rule is **"agents propose, deterministic code disposes."** A naive reading of
"give the LLM tools to execute the close" violates it.

The resolution is **monotone authority**:

> The deterministic ratchet decides the position stays open. The exit agent's **only** power
> is to override toward closing **earlier**. It cannot hold longer, cannot move the trail,
> cannot widen a cap, cannot place an order.

Why this is not a compromise:

- **Fail-open is structurally impossible.** There is no answer the model can give — or fail
  to give — that removes protection. The trail, the hard stop, the time stop and the expiry
  sweep all run regardless of what it says or whether it says anything.
- **Therefore the fail-safe on error is `TRAIL`, not `CLOSE`.** This is the opposite of the
  obvious instinct and it is correct. Closing on an API timeout means *an Anthropic outage
  caused a trade*. That is not conservative; it is a different unforced action. The position
  is already fully protected without the model.
- **MOCK mode disables the agent entirely.** MOCK is a configured no-LLM mode, not an error.
  Closing every winner at +35% under MOCK would make CI and every keyless run behave nothing
  like production.

The LLM's output is an **input to a deterministic decision**, exactly like the Drafter's
verdict at `drafter.py:180-187`. That is the existing pattern, not a new one.

---

## 3. A.1 — the deterministic ratchet (no LLM)

**Build this first. It ships and is worth shipping on its own, with no LLM anywhere.**

### The state machine

```
peak       = max(peak_persisted, pl)        monotone — never decreases
armed      = peak >= ARM_PCT                once armed, always armed
trail_line = peak * (1 - GIVEBACK_FRAC)     PROPORTIONAL, not point-giveback

1. pl <= -STOP_PCT        -> CLOSE  option_stop_loss     never consults
2. pl >= HARD_TP_PCT      -> CLOSE  option_take_profit   never consults
3. armed and pl <= trail_line
                          -> CLOSE  option_trail_stop    never consults
4. armed                  -> HOLD,  may_consult = True
5. else                   -> HOLD,  may_consult = False
```

**Rule 1 stays first even when rule 3 also fires.** A gap from +50 to −60 satisfies both. A
give-back that goes through zero reads more honestly in the ledger as a stop than as a trail.
Do not reorder these casually.

**Proportional giveback, not percentage-point giveback.** `close if pl <= peak × (1 − 0.30)`:
peak +80 → line +56; peak +200 → line +140. Point-giveback (`peak − 30pp`) gives a big winner
the same absolute leash as a small one, which is backwards for "hold winners". Proportional
also guarantees a trail exit is always a *profitable* exit.

**Giveback is 30%, not 10%, and that is deliberate.** The mark is a 15-minute-delayed
indicative quote on a contract we permit up to a 12% relative spread. The trail is therefore
a delayed trail on a noisy mark. A 10% giveback fires on quote noise. Document this in
`docs/OPTIONS_PLAYBOOK.md` as a disclosed limitation, in the same voice as the other ones.

`unrealized_pl_pct is None` → `HOLD`, `may_consult=False`, peak unchanged. **The existing
"no mark never closes" rule survives intact and must not be weakened.**

### API — `packages/engine/engine/options/exits.py`

**Leave `option_exit_signal` exactly as it is.** All 10 of its tests stay green and the whole
ratchet becomes revertible by one env var, which matters more on Monday than tidiness.

```python
@dataclass(frozen=True)
class RatchetOutcome:
    action: Literal["CLOSE", "HOLD"]
    reason: str | None          # option_stop_loss | option_take_profit | option_trail_stop
    detail: str                 # the arithmetic, for the notification and the audit row
    pnl_pct: float | None
    peak_pl_pct: float
    trail_line_pct: float | None
    armed: bool
    may_consult: bool
    peak_advanced: bool         # caller persists ONLY when True


def option_ratchet_signal(
    *,
    unrealized_pl_pct: float | None,
    peak_pl_pct: float | None,
    arm_pct: float,
    giveback_frac: float,
    hard_take_profit_pct: float,
    stop_loss_pct: float,
) -> RatchetOutcome: ...
```

Pure function. No I/O, no clock read, no LLM import. Same module-docstring standard as the
existing file — say *why*, not just *what*.

### Caps — `packages/engine/engine/risk/types.py`

```python
options_ratchet_enabled:      bool  = True    # OPTIONS_RATCHET_ENABLED
options_trail_arm_pct:        float = 35.0    # OPTIONS_TRAIL_ARM_PCT
options_trail_giveback_pct:   float = 30.0    # OPTIONS_TRAIL_GIVEBACK_PCT (% of peak gain)
options_hard_take_profit_pct: float = 150.0   # OPTIONS_HARD_TAKE_PROFIT_PCT
```

All four are env-tunable. They are exit thresholds, which `from_env`'s docstring already
licenses — an exit threshold cannot increase maximum loss beyond the premium already paid.
See [`PLAN_AGGRESSIVE_PROFILE.md`](PLAN_AGGRESSIVE_PROFILE.md) §4 for why the *caps* are not
treated the same way.

---

## 4. A.2 — the high-water mark. **Do not add a column.**

Persist to **`agent_decisions.reasoning` JSONB under the key `"option_exit"`**.

Why not a migration, in order of weight:

1. `start.sh` runs `alembic upgrade head` **before uvicorn binds**, inside a 600s
   healthcheck window with `restartPolicyMaxRetries = 3`. A bad migration four days from the
   deadline takes the whole app down, not just this feature.
2. The peak is not one field. It needs `consult_date`, `consults`, `last_consult_at` and a
   consult log beside it — that is five columns, not one.
3. `reasoning` is already the documented home for exactly this shape (`contract_funnel` lives
   there, added with "zero schema change" as an explicit design point).
4. Nothing indexes or aggregates on the peak. Only the process that writes it reads it, and
   it arrives free with the row load.

### Shape

```json
"option_exit": {
  "version": 1,
  "peak_pl_pct": 82.4,
  "armed": true,
  "trail_line_pct": 57.7,
  "consult_date": "2026-09-01",
  "consults": 2,
  "last_consult_at": "2026-09-01T14:31:02Z",
  "log": [
    {"at": "2026-09-01T14:31:02Z", "pl_pct": 61.2, "peak_pct": 82.4,
     "trail_line_pct": 57.7, "action": "TRAIL", "confidence": 0.41,
     "reason": "...", "tools_used": ["get_contract_quote"],
     "degraded": false, "model": "claude-sonnet-4-6"}
  ]
}
```

`log` is capped at **10** entries, oldest dropped.

### 🚨 Write with `jsonb_set`, never a Python read-modify-write

The council owns the other keys in this column. A blind overwrite eats `strategy_fit` and
`contract_funnel` — the contract funnel that took a whole commit to persist.

```sql
UPDATE agent_decisions
   SET reasoning = jsonb_set(COALESCE(reasoning, '{}'::jsonb), '{option_exit}', :payload::jsonb, true)
 WHERE id = :id
```

**`COALESCE` is required** — `reasoning` is nullable and `jsonb_set(NULL, …)` returns NULL,
which would silently blank the column instead of writing to it.

Write **only when `peak_advanced` is true or a consult happened**, never every tick. At 30s
ticks across a session that is the difference between ~10 writes and ~800 per position.

---

## 5. A.3 — the exit agent

### Where it fires

From `manage_positions_for_user`, **after** `_exit_reason` returns `None`:

```python
if reason is None and outcome.may_consult and _consult_gate_open(decision, now, caps):
    verdict = await maybe_consult_exit_agent(...)
    if verdict.action == "CLOSE_NOW":
        reason = "option_agent_close"
```

`_exit_reason` stays `str | None` and stays deterministic. Its docstring says "Deterministic
reads only" and an `await llm.complete()` inside it breaks that contract and every fixture in
`apps/api/tests/test_position_manager.py`. Compute the `RatchetOutcome` once via a small
sibling helper and reuse it — do not compute it twice.

`_CLOSE_REASON_LABEL` gains:
```python
"option_trail_stop":  "trailing stop hit",
"option_agent_close": "exit agent banked the gain",
```

> 🚨 **`close_reason` is `String(20)`.** `option_agent_close` = 18, `option_trail_stop` = 17.
> Both fit. **`option_trailing_stop` is exactly 20 — do not use that name.**

### The consult gate — all five must hold

1. **`is_us_market_open(now)`.** There is no market-hours gate in the orders package today;
   the ladder runs 24/7. This one condition kills ~85% of candidate ticks for free, and a
   consult about a stale weekend mark is worthless anyway.
2. `env_flag("OPTIONS_EXIT_AGENT")` **and** `not llm.mock`.
3. Cooldown: `now - last_consult_at >= OPTIONS_EXIT_AGENT_COOLDOWN_S` (default **900**).
4. **Edge trigger** — one of: first arm, or peak advanced ≥15pp since the last consult, or
   `pl` is within 10pp of `trail_line`. Without this you consult on every quiet tick.
5. Per-day budget `OPTIONS_EXIT_AGENT_MAX_CALLS` (default **6**) for `consult_date == today`,
   **and a per-tick cap of 1**.

**The per-tick cap of 1 is a latency requirement, not a cost one.** The consult runs inline
on the fleet tick under `asyncio.wait_for(..., timeout=20.0)`, and the fleet tick is 30s and
serial across users. Without the cap, three armed positions could add 60s to a 30s loop.
Positions get consulted round-robin across ticks; with a 900s cooldown that is ample.

Ordering consequence to state in the code comment and accept: the expiry sweep runs *after*
the position manager in `ReconcilerFleet.tick()`, so a consult delays the sweep by up to 20s.
At `DTE ≤ 2` granularity that is irrelevant — but say so out loud rather than leaving a
reader to wonder.

Budget: ≤6 Sonnet calls per position per day ≈ $0.06. Five positions × 4 days ≈ $1.20.
Against CLAUDE.md §5's "cost is not a constraint" that is noise. **The gate exists so an
unbounded loop cannot happen, not to save money.** Say that in the docstring so nobody
removes it as a premature optimisation.

### Prompt contract

> 🚨 The system prompt **must begin** `You are the Options Exit Agent`. Both
> `_mock_response` (`llm.py:311`, first 120 chars) and `infer_role_from_system_prompt`
> (`cost_ledger.py:199`, first 160) anchor on that exact phrase, and **both need a new
> branch**. Miss either and you get a silent generic `{score, confidence, thesis}` in mock
> and `"unknown"` in the cost ledger — neither of which raises.

```
You are the Options Exit Agent on a quantitative trading desk.

A long option position is in profit and its trailing ratchet is ARMED. The
deterministic engine has ALREADY decided this position stays open. Your only
power is to close it EARLIER than the trail would. You cannot place, size,
cancel or modify an order, you cannot move the trail, and you cannot extend
the hold — the trailing stop, the hard stop, the time stop and the expiry
sweep all run whatever you answer.

Choose exactly one:
  CLOSE_NOW — bank the gain now; the evidence says the move is done.
  TRAIL     — let the deterministic ratchet keep running.

Default to TRAIL. Choose CLOSE_NOW only for a NAMED, checkable reason you can
point at a number for: delta collapsing toward the strike, IV crushed since
entry, the underlying's trend regime flipping against the position, spread
widening past the point where the gain is realisable, or DTE short enough that
theta now dominates the remaining thesis. "It has gone up a lot" is not a
reason — the trail already handles that.

Return strict JSON ONLY:
{ "action": "CLOSE_NOW" | "TRAIL",
  "confidence": <float 0-1>,
  "reason": "<one sentence citing a specific number you read>" }
```

Mock branch:
```python
elif "you are the options exit agent" in role_line:
    body = {"action": "TRAIL", "confidence": 0.4,
            "reason": "MOCK: no live evidence; deferring to the deterministic trail."}
```
Belt and braces — gate condition 2 already refuses to consult under `llm.mock`.

### Deterministic post-filter — the direct analogue of `drafter.py:180-187`

- `action` not in `{"CLOSE_NOW", "TRAIL"}` → `TRAIL`, logged.
- `confidence` unparseable → `0.0`.
- `CLOSE_NOW` with `confidence < OPTIONS_EXIT_AGENT_MIN_CONFIDENCE` (default **0.55**) →
  downgraded to `TRAIL`, logged with the same *"downgrading rather than flipping"* wording
  the drafter uses.
- Any exception, timeout, or `None` from the parser → `TRAIL`.

---

## 6. A.4 — the read-only tool harness

### Commit 1: the block walk (lands on the shared path, must be its own commit)

Replace `text = msg.content[0].text if msg.content else ""` (`llm.py:153`) with a walk that
returns `(text, tool_calls)`. `LLMResponse` gains `tool_calls: tuple[ToolCall, ...] = ()` and
`stop_reason: str | None = None`.

Behaviour-identical for every existing call — one text block yields the same string — and
strictly more robust against a leading non-text block. **Full suite green before commit 2.**
This touches the hot path of all five council nodes; it deserves to be independently
revertible.

### Commit 2: `complete_tools()` — a sibling method, **not** an overload of `complete()`

`complete()` hardcodes `messages=[{"role":"user","content":user}]`. A tool loop needs
caller-supplied `messages` so it can append `assistant` and `tool_result` turns. **Do not
refactor `complete()`'s signature four days from the deadline** — five nodes depend on it.

```python
async def complete_tools(
    self, *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str = Model.SONNET,
    max_tokens: int = 1024,
    tool_choice: dict[str, Any] | None = None,
    cache_system: bool = True,
    council_run_id: str | None = None,
    user_id: str | None = None,
) -> LLMResponse: ...
```

> 🚨 **MOCK mode must return a normal response and never emit a `tool_use` block.** A mock
> that emits one builds an infinite loop, because the loop terminates on "no tool calls".

**The loop lives in the node, bounded at `max_rounds=2`.** Enough for "look at one or two
things, then decide"; caps worst-case latency at 3 API calls inside a 20s budget.

### The four tools

| Tool | Returns | Cost |
|---|---|---|
| `get_position_snapshot` | current `pl_pct`, `peak_pl_pct`, `trail_line_pct`, days held, DTE, entry premium, qty | **zero** — all in hand |
| `get_entry_thesis` | `bull_case`, `bear_case`, `selected_strategy`, fit components, the scan trigger that woke it | **zero** — on the decision row |
| `get_contract_quote` | bid/ask/mid, relative spread, delta, IV for this OCC | one chain call scoped to the single expiry, cached per tick |
| `get_underlying_bars` | last N (5–60) daily OHLC + `rsi_14`, `atr_14`, `trend_regime` | one bars fetch, provider-cached |

**Do not implement a generic "fetch the whole chain" tool.** It is a heavy call and the model
will reach for it.

Dispatcher: an explicit allowlist dict. An unknown name returns
`{"error": "unknown tool"}` as a `tool_result` with `is_error: True` — **never** an
exception, which would abort the tick.

### The boundary docstring — copy this in spirit from `apps/mcp_server/mcp_server/tools.py:9-19`

> **WILL NEVER BUILD HERE:** `place_order`, `close_position`, `cancel_order`,
> `size_position`, or anything reaching `packages/engine/risk` → `packages/broker`'s order
> surface. Every tool in this module is a market-data or stored-state read. The exit agent's
> entire authority is a two-valued verdict consumed by deterministic code that decides,
> independently, whether to place anything.

---

## 7. Build order

```
1. A.1  ratchet + RiskCaps knobs + tests          no LLM, no migration, ships alone
2. A.2  jsonb_set HWM persistence + tests         still no LLM
3. --- deploy, watch one session of pure ratchet evidence ---
4. llm.py block walk                              own commit, full suite green
5. A.3  exit agent node + prompt + mock + cost_ledger role + post-filter
6. A.4  complete_tools + the four tools + the 2-round loop
7. A.5  ledger tie-in (only if 1-6 are solid by Tuesday)
```

Ship A.3 with `OPTIONS_EXIT_AGENT=0`. Flip to `1` mid-morning after one clean deterministic
tick. **Never deploy into a live session.**

If step 6 slips, **ship the agent single-turn** with the same facts inlined into the user
message. The tools buy "the agent chose what to look at" — a Technology-Implementation point,
not a P&L one. The verdict, the post-filter, the monotone authority and the audit row all
survive unchanged.

---

## 8. A.5 — the Refusal Ledger tie-in (the demo prize)

**A `TRAIL` verdict is a refusal to bank a profit** — and unlike most refusals it has a
directly measurable counterfactual: the position was worth `pl_pct` at the consult, and it
was actually realised at `realized_pnl`.

`ghost_eval` can mark these the same way it marks vetoed entries, giving `build_veto_ledger`
a new rule row `exit_agent_held` with times fired, notional held, and a dollar figure — which
for a held winner is a gain *captured*, signed the other way from a prevented loss.

*"We show you what it refused — including refusing to take a profit, and what that refusal
was worth."* Nobody in the field has that. It is the highest-value demo item in this plan and
the first thing to cut if A slips.

Also stamp `reasoning["risk_profile"]` (see [`PLAN_AGGRESSIVE_PROFILE.md`](PLAN_AGGRESSIVE_PROFILE.md))
so the ledger can slice refusals by which cap set produced them.

---

## 9. Tests — and the revert-check matrix

Per CLAUDE.md §4.1: **revert your fix and confirm the new test fails, then restore.** A test
you cannot make fail is not a test.

| Test | Break this to make it fail |
|---|---|
| **`test_exit_agent_timeout_falls_back_to_trail`** | Make the `except` return `CLOSE_NOW`. **The single most important test here — it pins the fail-safe.** |
| `test_trail_does_not_fire_before_arming` | Make `armed` unconditional |
| `test_ratchet_closes_on_a_peak_retracement` | Make `trail_line` read `pl` instead of `peak` |
| `test_peak_is_monotone_across_ticks` | Let `peak` take `pl` directly |
| `test_stop_wins_over_trail_on_a_gap_through_zero` | Reorder rules 1 and 3 |
| `test_no_mark_holds_and_leaves_the_peak_alone` | Treat `None` as `0.0` |
| `test_low_confidence_close_is_downgraded_to_trail` | Remove the post-filter |
| `test_unknown_action_becomes_trail` | Pass the model's string through |
| `test_peak_write_preserves_the_council_reasoning_keys` | Replace `jsonb_set` with a whole-column overwrite → `contract_funnel` and `strategy_fit` vanish |
| `test_jsonb_set_writes_into_a_null_reasoning_column` | Drop the `COALESCE` |

Non-revert coverage required:
- Consult gate: market closed → no consult; cooldown not elapsed → no consult; budget
  exhausted → no consult; `llm.mock` → no consult; not armed → no consult.
- Per-tick cap: two armed positions, one tick, exactly one consult.
- **Both** `_mock_response` and `infer_role_from_system_prompt` resolve the new role. Assert
  it explicitly — missing either fails silently, which is why it needs a test rather than a
  glance.
- `close_reason` strings all fit in 20 chars. A one-line test that costs nothing and catches
  a truncation that would otherwise only appear in production.

Baseline: **792 passed, 9 skipped**; 9 pre-existing ruff errors. `git stash` and re-run
before attributing any failure to your change.

---

## 10. Where you are most likely to go wrong

1. **Making the fail-safe `CLOSE`.** It feels safer. It isn't — it lets an Anthropic outage
   trigger trades, and the position is already fully protected without the model. Re-read §2.
2. **Calling the LLM inside `_exit_reason`.** It breaks the "deterministic reads only"
   contract and every fixture in `test_position_manager.py`.
3. **Read-modify-writing `reasoning`.** You will silently destroy `contract_funnel`. Use
   `jsonb_set` with `COALESCE`.
4. **Forgetting one of the two role branches** (`_mock_response`, `infer_role_from_system_prompt`).
   Both fail silently.
5. **Making the mock emit a `tool_use` block.** Infinite loop.
6. **Naming it `option_trailing_stop`** — exactly 20 chars, will truncate or error on insert.
7. **Refactoring `complete()` to take `messages`** instead of adding `complete_tools()`. Five
   council nodes are on that path.
8. **Dropping the per-tick consult cap** as an over-engineering. It is a latency bound on a
   30s serial loop, not a cost optimisation.

---

*Related: [`OPTIONS_PLAYBOOK.md`](OPTIONS_PLAYBOOK.md) · [`PLAN_AGGRESSIVE_PROFILE.md`](PLAN_AGGRESSIVE_PROFILE.md) · [`HACKATHON.md`](HACKATHON.md) · [`../CLAUDE.md`](../CLAUDE.md)*
