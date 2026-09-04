# The Refusal Ledger — one-page technical overview

**Autonomous options and equity trading on Alpaca paper.** The differentiator: every
trade the risk engine *refuses* is marked to market against real Alpaca option quotes,
so risk rules stop being policy and become numbers you can audit.

---

## 1. AI logic — agents propose, deterministic code disposes

The architectural rule: **an LLM is never in an execution or risk path.** Models
supply judgement; Python decides.

```
Scanner (10 named triggers, 2-min sweep)      deterministic, free
   ↓  only symbols that fire
strategy_fit (5 strategies × long/short)      deterministic, free — most die here
   ↓  only symbols with a real setup
Bull agent  ⇄  Bear agent                     2 LLM calls, argue independently
   ↓  only on deterministic agreement
Tool call → ToolGuard → Alpaca                guard re-runs the full risk stack
```

**Screening is unlimited; thinking is rationed.** Every symbol is scored in Python for
free. Only survivors reach a paid model call, hard-capped at **20/day, 4/hour, $3.00/day**.

**Two-agent council.** A Bull and a Bear read the same feature dict and answer
independently. A deterministic resolver combines them — it takes the *minimum*
conviction, never the mean — and a disagreement or abstention ends the pass with zero
trade. Total cost of a full session: **~$9.42**.

**Tool use, and why it is safe.** The Bull agent gets 8 tools: six read-only
(`get_option_snapshot`, `get_underlying_bars`, `get_position_snapshot`,
`get_entry_thesis`, `get_funnel_counts`, `get_iv_rank`) and two that move money
(`open_option_trade`, `adjust_option_position`). **The agent cannot place an order.**
It emits a tool call; `ToolGuard.before()` intercepts *every* call and re-derives the
entire risk decision from scratch. A refusal returns a **named rule** the model can
adjust against once — the loop is bounded at 3 rounds. This is why the agents run on
Haiku: a weaker model degrades *selection*, never *risk control*.

## 2. Risk gates — 18 equity + 16 options named rules, first veto wins

Every veto is a named, testable Python function, never a model output.
`forbid_short_phase_0` · `shortable_check` · `short_requires_stop` · `pdt_block` ·
`drawdown_halt` · `single_name_concentration` · `max_correlation_cluster` ·
`options_level_insufficient` · `max_premium_pct` · `max_total_premium_pct` ·
`illiquid_chain` · `size_rounds_to_zero` · `naked_short_forbidden`

- **Six-stage contract funnel** narrows a full chain to one contract
  (`contract_type → dte_window → delta_band → liquidity → iv_present → iv_realized_band`).
  Every stage count is persisted, so a HOLD explains itself instead of going silent.
- **Chain-depth gate.** A chain yielding <5 liquid contracts is refused outright. Added
  after a stop failed to protect a position whose mark sat frozen for 2h16m, then gapped
  26 points in a single print: *a price stop cannot work on a mark that does not print.*
- **Halt coupling, enforced by test.** `book size × stop ≤ declared tolerance`. The −3%
  daily halt blocks new entries but closes nothing, so it never bounded the book's worst
  session. A profile taking a wider tail must now *declare* it
  (`max_tolerated_book_drawdown_pct`) rather than the invariant being loosened.
- **Three exit layers:** trailing ratchet, broker-side resting stop-limit (survives an
  overnight gap even if our server is down), and a DTE≤2 expiry sweep.
- **Phase A is long-only on the option itself.** A bearish thesis is a *long put* —
  bounded loss by construction. Writing options is deliberately out of scope: unbounded
  loss with no assignment handling.

## 3. Alpaca infrastructure

| Surface | Use |
|---|---|
| `/v2/options/contracts` | Chain metadata + open interest. **Paginated** — unpaged returns 100 of 4,674 rows, which silently emptied the liquidity gate on every symbol. |
| `/v1beta1/options/snapshots` | Live bid/ask/IV/greeks, merged with OI to build candidates |
| `/v2/stocks/bars`, `/v1beta1/news`, `/v1beta1/screener` | Features, sentiment, universe screening |
| Trading API | Bracketed equity orders, options `buy_to_open`, resting `STOP_LIMIT` GTC exits |
| **`alpaca` CLI** | `alpaca clock` before every sweep — catches early closes and unscheduled halts a hardcoded calendar misses. `create_subprocess_exec` with an argv list (never a shell string), killed on timeout, returns `None` on any failure. **First link in a CLI → REST → local-calendar chain**, each reporting its own source. |
| MCP | `apps/mcp_server/` exposes our council read-only *to* Claude — a bonus, not the eligibility artifact. The CLI above is. |

A 30-second reconciler fleet converges broker truth into Postgres: fills, external
closes, the drawdown breaker, and **account-switch detection** (swapping API keys
retires the previous account's state instead of silently inheriting its halt and
positions).

---

## What this bought us

| Found by measuring | Fix |
|---|---|
| A stop that could not fire — mark frozen 2h16m, then a 26-point gap | Chain-depth gate, refused for 0 model calls |
| Options council was 84% of a $10 credit burn | Deterministic pre-flights moved *before* the paid debate |
| "15 symbols per sweep" was really 15 every 2 minutes | Hard per-day and per-hour caps |
| A long put's real −$195 loss displayed as **+$195** | P&L sign keyed to broker side, not to the thesis |

**The claim is not "this agent makes money"** — two sessions of live P&L is noise, and
it is currently slightly negative. The claim is that this agent can tell you, in
dollars, **which of its own risk rules to delete.**

*Full findings and caveats: [`docs/SUBMISSION_FINDINGS.md`](SUBMISSION_FINDINGS.md)*
