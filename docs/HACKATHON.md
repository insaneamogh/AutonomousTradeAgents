# Alpaca AI Trading Agents Hackathon — mission brief

**If you are an AI model picking up work on this repo, read this in full before
writing code.** It is the single source of truth for what we are building and why.
Source: <https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon>

---

## 1. The deadline is the design constraint

| | |
|---|---|
| **Submission deadline** | **Fri Sep 4, 11:00 AM EDT** |
| Event window | Aug 28 – Sep 4, 2026 |
| Prize pool | $6,300 ($2,500 / $1,500 / $1,000 + 2× $500 social) |
| Field | 3,132 participants, 992 teams, 12 submissions at last check |

**Trading sessions remaining is the number that matters**, not calendar days:
Mon Aug 31 · Tue Sep 1 · Wed Sep 2 · Thu Sep 3 · **Fri Sep 4 for 90 minutes only**
(market opens 9:30, deadline 11:00). Call it **4.25 sessions.**

Everything about the plan follows from that: the agent must be live and trading at
Monday's open, and over 4 sessions **P&L is dominated by variance, not skill.** Do not
plan as if there is time to iterate on strategy performance. There is not.

---

## 2. Hard requirements — these are eligibility, not scoring

1. **Autonomous AI trading agent** on Alpaca's Trading API. ✅ we have this.
2. **Must use Alpaca's OWN MCP server or CLI.** ⚠️ See §5 — got wrong once already.
3. **All strategies must incorporate options trading.** ⚠️ Options were built but had
   never once produced a tradeable proposal until 2026-08-29. See §6.
4. **A brand-new Alpaca paper account, balance set to $100,000.** Reused accounts are
   *"not eligible for judging"*. The old account (`PA3RFT091VEB`) is disqualified.
   **The account ID must be in the submission** — judges use it to read our P&L.

### Submission checklist
- [ ] Public GitHub repo
- [ ] Demo application + live URL
- [ ] Video presentation + slide deck
- [ ] **Alpaca paper account ID**
- [ ] One-page write-up: AI logic · risk gates · Alpaca infrastructure
- [ ] (Optional, separate $500 × 2 prize) up to 5 X/LinkedIn posts tagging
      @lablabai and @AlpacaHQ

---

## 3. Judging criteria

| Criterion | Our position |
|---|---|
| **P&L Performance** | Weakest. **Caps overridden by the user 2026-08-30** — 1%/5% → 2.5%/12% for the contest window, on the grounds that this is a paper account with no real capital. See [`PLAN_AGGRESSIVE_PROFILE.md`](PLAN_AGGRESSIVE_PROFILE.md); the −3% daily halt stays fixed and is what makes the wider bound tolerable. |
| **Technology Implementation** | Strong. 21 equity + 13 options named risk rules, walk-forward backtester sharing live risk code, deterministic funnel, per-agent MCP toolset scoping. |
| **Creativity & Originality** | **This is where we win.** The Refusal Ledger is unclaimed in the field. |
| **Presentation & Execution** | Strong — real mobile + desktop product, full audit trail, live counterfactuals. |

---

## 4. Our positioning: "The Refusal Ledger"

### The claim
> Every agent can show you what it bought. **We show you what it refused — and what
> the refusal was worth, in dollars.**

### Why this and not the obvious thing

"AI proposes, deterministic risk gates dispose" is **already claimed publicly** by at
least five competitors: AEGIS-Q ("bounded AI selects… while deterministic code
controls"), BABIL ("separates AI reasoning from execution authority through
deterministic risk gates"), SPY Sentinel ("applies risk gates, refuses to trade when
an edge is not proven"), Vega ("cannot lose more than the premium it pays"), and a
build-in-public LinkedIn post reasoning to the same architecture from first principles.

It is still how we build. It is no longer a differentiator. **Do not lead with it.**

What nobody else has: **ghost P&L** — `apps/agents/trading_agents/jobs/ghost_eval.py`
marks every vetoed/declined/expired proposal against real forward prices, so
`build_veto_ledger` can report per rule: times fired, notional blocked, and
`prevented_loss_usd`. That machinery was already ~80% built and completely orphaned.

### Why it is also the right *risk* call
It makes the entry compelling **whether or not P&L lands positive**. With 4 sessions of
runway that is the single most valuable property a submission can have.

### The demo money-shot
Aggregate ledger → click one row → the exact contract refused, the named rule that
refused it, the thesis behind it, and the dollars that refusal saved or cost.

---

## 5. The MCP mistake — what happened and what to do

**The requirement:** *"projects must utilize either **Alpaca's** MCP server or its CLI
tools."*

**What was built:** `apps/mcp_server/` — a server exposing **our council TO Claude**.
Six read-only tools, 794 lines, 11 tests, a clean propose-only security boundary. It is
good work. It is also **the opposite direction** from the requirement and does not
satisfy it.

**What to do:**
- **Keep it.** "Our agent is itself MCP-addressable" is a real bonus. Don't delete it.
- **Additionally** consume Alpaca's own tooling. Two integration points, both genuinely
  useful rather than decorative:

**A. Alpaca CLI in the market-hours gate.** `github.com/alpacahq/cli`, prebuilt
binaries, env-var auth, JSON out. Explicitly built for *"long-running agent sessions, cron
jobs and CI"*.

⚠️ **Two corrections to what this section used to say** (both verified 2026-08-30):

1. **The gate is not `pandas_market_calendars` in `daily_cron.main`.** The real path is
   `engine/features/market_calendar.py::is_us_market_open` → `engine/scanner/engine.py:86`
   → `apps/api/app/services/council/scheduler.py:317`.
2. **The early-close / halt awareness already exists in Python and is simply unwired.**
   `engine/features/clock.py::AlpacaClock` calls `/v2/clock`; `clock_from_env()` has zero
   non-test callers. So the CLI is the **eligibility artifact**; wiring that clock is the
   **functional upgrade**. Do both, and do not confuse them.

The exact clock subcommand below was never verified — treat it as unknown until the gate in
[`PLAN_ALPACA_MCP.md`](PLAN_ALPACA_MCP.md) §0 confirms it.

**B. Alpaca's MCP server — ONE read-only session. No execution session.**

The two-session design that used to be described here (a `research` session plus an
`execution` session holding the `trading` toolset) **has been deleted**, because
`apps/mcp_server/mcp_server/tools.py:9-19` correctly says mounting execution tools into an
LLM tool loop would violate this codebase's one architectural rule.

Read-only-only is also the **stronger** claim. Two disjoint sessions says *"we mounted
execution tools and trusted ourselves to only use them from the right place"* — a policy,
which is exactly what five competitors already claim. One read-only session says:

> *"There is exactly one Alpaca MCP session in this system and `place_option_order` is not
> in it. Execution never touches MCP at all — it goes `engine.risk` → `packages/broker` →
> Alpaca REST, deterministic Python end to end."*

A capability boundary **with no exception** beats one with a carve-out. Toolset names must
be verified against the README before use — see [`PLAN_ALPACA_MCP.md`](PLAN_ALPACA_MCP.md) §0.

**Do not let MCP work block Monday's open.** Flag-gate every MCP path with the existing
direct-SDK code as the default. The CLI clock gate alone satisfies the requirement.

---

## 6. State of the options track (as of 2026-08-29)

Options were fully built and **had never produced a single tradeable proposal.** Three
independent blockers, each fatal alone, all fixed in commits `0b824cbb`, `64979a8c`,
`56a06779`, `811d4e46`:

1. **Premium units** — `risk_officer` passed the *underlying share price* as the
   per-contract premium. `max_premium_pct` computes `last_price * multiplier`, so a
   $229 stock became a $22,900 "premium" and everything was vetoed at "68.71% of
   equity (cap 1.00%)". Real premium: 0.96%.
2. **Wire symbol** — orders went out as *equity* orders on the underlying at the
   option's price. The close path matched broker positions on the underlying while
   Alpaca keys them by OCC, so an agent-managed option could **never be closed at all**.
3. **Reachability** — no scheduled or scanner-triggered run could produce an option.
   `user_watchlist.asset_class` had been persisted and UI-toggleable from day one and
   was never read back.

Plus the liquidity gate: `volume` came from the snapshot's *last trade size*, not daily
volume (alpaca-py's `OptionsSnapshot` model drops the `dailyBar` block that holds real
volume). Live measurement: a floor of 10 rejected **16 of 18** contracts that had
already cleared DTE, delta and IV.

### Verified working, live, market closed
```
SPY  4128 → 2064 calls → 1843 DTE → 130 delta → 3 liquid → OK
council: BUY NVDA260909C00225000, 4 @ $2.17 = $868 = 0.868% of $100k
         15 risk rules passed, approved
```

**The full rule set — thresholds, vetoes, exits, and the traps — is
[`docs/OPTIONS_PLAYBOOK.md`](OPTIONS_PLAYBOOK.md). Read it before changing any
options number.**

### Still open
- [ ] **Fresh paper account + `options_trading_level ≥ 2`** — a new account may sit at
      level 0 until the options agreement is accepted, and approval is not instant.
      **This gates everything.**
- [ ] **Reconciler must tick once before the first options pass.**
      `postgres_context._cold_boot_fallback` does not set `options_trading_level`, so
      it defaults to `None` and `options_level_insufficient` vetoes every entry.
      `PositionsSnapshot.options_trading_level` is only populated after a reconciler
      tick. **Hard ordering constraint on Monday morning.**
- [ ] Alpaca MCP / CLI integration (§5)
- [ ] Ghost-marking options against **option** bars, not equity closes (§7)
- [ ] Whether Alpaca accepts an options order outside market hours — untested. The
      user must place that test order; models do not execute trades.

---

## 7. The build plan

Full detail lives in the approved plan file; this is the shape.

**P1 — the Refusal Ledger (the differentiator)**
- `ghost_eval` currently marks against **equity daily closes** via
  `engine.prices.get_price_provider`. An options ghost must be marked on the
  **contract**: `/v1beta1/options/bars`, keyed by the ghost's OCC symbol. Also fold in
  the `multiplier` — `_ghost_pnl` is currently 100× too small for options.
- **Surface trims, not just blocks.** `evaluate()` discards the `veto_rule` name on trim
  decisions (`max_premium_pct_trim`, …) and emits only a `trimmed:old->new` flag, so
  "how often did risk *shrink* a trade" is invisible.
- **The Contract Funnel** — highest demo-value-per-hour item. `select_contract`'s
  6-stage funnel is where *most* refusals happen, and every one is currently
  `logger.info`'d and thrown away. Persist `funnel_counts` into
  `runtime._reasoning_block` (JSONB, **zero schema change**) and render it:
  *"4,128 contracts → 2,064 calls → 1,843 in the DTE window → 130 in the delta band →
  3 liquid → we bought 1."*

**P2 — Mon–Thu live operation.** Monitor whether it trades at all. **Zero options fills
by Tuesday close is the emergency signal** — loosen the funnel, never the risk caps.

**P3 — deliverables.** Start Wednesday. Do not leave the video to Friday.

---

## 8. Do not do these

- ~~**Do not raise `options_max_premium_pct` (1%) or `options_max_total_premium_pct`
  (5%) to chase P&L.**~~ **SUPERSEDED 2026-08-30 by an explicit user decision.** The caps
  move to 2.5% / 12% via a reviewed `RiskCaps.aggressive_paper()` profile — never via an
  env var that supplies a number. The bounded-loss argument survives with a wider bound:
  max loss is still the premium, and `daily_drawdown_halt_pct = -3.0` **does not move**,
  which is what keeps a 12% book-to-zero a multi-day worst case rather than a single-day
  one. Widening the cap and freezing the halt are one coupled decision. Full reasoning and
  the numbers: [`PLAN_AGGRESSIVE_PROFILE.md`](PLAN_AGGRESSIVE_PROFILE.md).
- **Do not raise the caps beyond 2.5% / 12%.** That is the reviewed ceiling.
- **Do not change `selection.py` constants after Monday's open.** One reviewed Saturday
  change, then frozen, so funnel counts stay comparable across days.
- **Do not claim `earnings_blackout` is an active control.** It is permanently inert:
  `MinimalOptionsContextProvider` hardcodes `days_to_earnings: None`, and Alpaca
  publishes **no earnings calendar** (`features/corporate_actions.py` says so
  explicitly; the provider docstring suggesting otherwise is wrong). Disclosing a
  named-but-data-gated rule reads as rigour; a fabricated earnings date is a defect.
- **Do not let an ITM long option run to expiry.** Auto-exercise and assignment
  handling **do not exist**. The `dte ≤ 2` expiry sweep is the only protection.
- **Do not overclaim the data feed.** Free tier is a 15-minute-delayed *indicative*
  feed, not consolidated OPRA. Fine for daily-bar decisions; not a basis for claiming
  execution quality.
- **Do not spend time optimising LLM cost.** ~$0.04/pass, ~$10 total. Not a constraint.

---

## 9. Known-good numbers (don't re-derive these)

| Thing | Value | Source |
|---|---|---|
| Full Python suite | 757 passed, 9 skipped | `pytest apps/agents apps/api packages/` |
| Ruff pre-existing errors | 9 | check baseline before blaming your change |
| Council LLM calls/pass | 5 (max 10 with re-ask) | `graph.py` |
| Cost per pass | ~$0.04 | `cost_ledger.py` pricing table |
| Option `open_interest` | real, populated 90/100, values 137–722 | `/v2/options/contracts` |
| Option `volume` | **last-trade-size proxy, not daily volume** | alpaca-py drops `dailyBar` |
| Chain pagination | auto-paginates, not a problem | `alpaca/common/rest.py:368` |
