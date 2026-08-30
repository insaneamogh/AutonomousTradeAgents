# The options playbook — every rule the machine plays by

**This document is derived from the code, not from intent.** Every number
below was read out of the module named beside it on 2026-08-30. If you change
a threshold, change it here in the same commit — and if this file and the code
ever disagree, *the code is right and this file is a bug*.

Scope: **Phase A only — long calls and long puts, single leg, US equity
options.** No spreads, no selling to open, no assignment handling. That is not
a simplification for the contest; it is what bounds the loss.

> ⚠️ **PARTLY SUPERSEDED — pending implementation, 2026-08-30.** The user has
> decided to loosen the sizing caps and replace the fixed take-profit with a
> trailing ratchet for the contest window. **The numbers below are still what
> the code does today** — this file's own rule is that the code wins — but §2's
> "1% and 5% are not negotiable" and §3's exit table are scheduled to change.
> The reasoning and the new numbers:
> [`PLAN_AGGRESSIVE_PROFILE.md`](PLAN_AGGRESSIVE_PROFILE.md) and
> [`PLAN_EXIT_AGENT.md`](PLAN_EXIT_AGENT.md). Whoever implements those updates
> this file **in the same commit**, per §0 above.

---

## 0. The one-line version

> The agent may buy one call or one put per underlying per day, 10–45 days
> out, near the money, in a contract that actually trades, for no more than
> **1% of equity** and no more than **5% across the whole book** — and it must
> close that position on a target, a stop, a clock, or an expiry, whichever
> comes first.

Everything below is that sentence, enforced.

---

## 1. What it may buy

### 1.1 The decision to look at options at all

An options pass only happens when **both** are true (`strategy_fit_node`):

- `ALLOW_OPTIONS=1` in the environment, and
- the watchlist row for that symbol has `asset_class='option'`.

Neither alone is enough. There is no code path where the agent decides on its
own to switch an equity idea into an options idea — the instrument is an
input, never an LLM output.

### 1.2 Direction

| Thesis | Contract |
|---|---|
| bullish (`direction="long"`) | **CALL** |
| bearish (`direction="short"`) | **PUT** |

A bearish view is expressed by *buying a put*, never by selling a call.
`OptionLegDetails.action` is `buy_to_open` at every single site — this is
load-bearing, because `engine.risk.rules._short.opens_short` must never
mistake a bought put for a short position.

### 1.3 The contract funnel — `engine/options/selection.py`

Six stages, in fixed order. Each records its survivor count; the **first stage
whose count hits zero names the rejection reason**, and the whole funnel is
persisted to the decision row's `reasoning.contract_funnel`.

| # | Stage | Rule | Rejection reason |
|---|---|---|---|
| 1 | `contract_type` | calls for long, puts for short | `no_matching_contract_type` |
| 2 | `dte_window` | **10 ≤ DTE ≤ 45** | `no_expiry_in_window` |
| 3 | `delta_band` | conviction ≥ 0.7 → &#124;δ&#124; ∈ **[0.40, 0.70]**; else **[0.25, 0.55]** | `no_delta_in_band` |
| 4 | `liquidity` | OI ≥ **100**, volume ≥ **1**, relative spread ≤ **12%** | `no_liquid_contract` |
| 5 | `iv_present` | IV must be reported | `no_iv` |
| 6 | `iv_realized_vol_band` | 0.3× ≤ IV/realized vol ≤ 3.0× | `iv_outside_plausible_band` |

Tie-break among survivors: **tightest relative spread, then highest open
interest.** A contract with no computable spread sorts last — never preferred
over one with a verified tight market.

Four of these thresholds deserve their reasoning stated, because they look
arbitrary and are not:

- **DTE floor is 10, not the risk engine's 7.** `expiry.dte()` reads
  `context.now_utc`; `select_contract` reads its own `inputs.now`. A contract
  chosen at exactly 7 DTE can re-enter the risk engine at 6 across a UTC
  boundary and be vetoed by the layer that just selected it. Three days of
  buffer removes that failure class.
- **The delta bands overlap at 0.40–0.55, deliberately.** They used to be
  disjoint (`[0.45,0.65]` / `[0.25,0.45]`), which made a 0.50-delta contract —
  ATM, the most liquid strike on the board — eligible *only* when LLM
  confidence cleared 0.7. Conviction should move the band, not exclude the
  middle of the chain.
- **The volume floor is 1, and it is not a daily-volume floor.** alpaca-py's
  `OptionsSnapshot` model drops the `dailyBar` block, so the only number
  available is the *last trade size* — one print, typically 1–5 lots. Measured
  against the live chain, a floor of 10 rejected **16 of 18** SPY contracts
  that had already cleared DTE, delta and IV. Open interest is the real
  liquidity gate; volume only asserts the contract has traded at all.
- **The spread ceiling is 12%, not 8%.** The free tier serves a 15-minute
  delayed *indicative* book, which reads wider than the one an order would
  actually fill against.

### 1.4 Sizing — `engine/options/sizing.py`

```
budget_usd = equity × options_max_premium_pct / 100
qty        = floor(budget_usd / (ask × 100))
```

If that floors to 0 contracts, the pass is a **HOLD**. It never rounds up to
one contract, and it never borrows from the aggregate budget.

### 1.5 Order type

**Always LIMIT at the ask. Never MARKET** — on a 15-minute-delayed indicative
feed a market order is an invitation to be filled at a price nobody quoted.
The executor forces this regardless of what the drafter wrote.

The wire symbol is the **OCC contract** (`NVDA260918C00250000`); the decision
row's `symbol` stays the **underlying** (`NVDA`). Both are load-bearing and
they are not interchangeable — see §5.

---

## 2. What it may not buy — the deterministic vetoes

13 named options rules, **first-veto-wins**, evaluated in `engine/options/risk.py`.
No LLM output participates in any of them.

| Rule | Fires when |
|---|---|
| `options_malformed_proposal` | option proposal with no leg details — refuse rather than guess |
| `options_disabled` | `ALLOW_OPTIONS` not set (entry only; never blocks a close) |
| `options_level_insufficient` | account `options_trading_level < 2` |
| `naked_short_forbidden` | anything that is not `buy_to_open` |
| `min_dte` / `max_dte` | outside 7–60 DTE at *approval* time, re-checked independently |
| `expiry_day_entry` | opening on the contract's own expiry day |
| `iv_unavailable` | no IV — a contract this system cannot price is not bought |
| `illiquid_contract` | OI / volume / spread floors, re-checked at approval |
| `earnings_blackout` | **permanently inert — see the warning below** |
| `max_premium_pct` | one position's premium > **1% of equity** — *trims first* |
| `max_total_premium_pct` | all open premium > **5% of equity** — blocks, never trims |

Plus the shared equity rules that apply to any order: `pdt_block`,
`daily_drawdown_halt`, `max_open_positions`, `min_council_confidence`,
`min_specialist_avg_score`, `correlation_cap`, `wash_sale` (informational).

**Trim vs. block.** `max_premium_pct` shrinks the order to fit rather than
refusing it, and the rule that did so is now recorded by name
(`max_premium_pct_trim`) in `RiskDecision.trim_rules`. A trim is a *partial*
refusal and is never counted as a block. If trimming rounds below 1 contract,
it becomes a block.

> ⚠️ **`earnings_blackout` is wired and permanently inert.** Alpaca publishes
> no earnings calendar (`features/corporate_actions.py` states this plainly),
> so `days_to_earnings` is always `None` and the rule never fires. It is named
> and disclosed rather than quietly deleted — and rather than fed a fabricated
> date. Do not describe it as an active control anywhere.

### Why 1% and 5% are not negotiable

> **Superseded pending implementation** — see the banner at the top. The
> *structure* of this argument survives the change; only the chosen bound moves,
> and `daily_drawdown_halt_pct = -3.0` does not move at all.

A long option's maximum loss is the entire premium. So `options_max_premium_pct`
**is** the position-size cap — it is not a proxy for one. 1% per position and 5%
across the book means the entire options book going to zero costs 5% of equity,
by construction, with no assumption about stops filling or gaps behaving.

That bound is the capital-preservation claim. Raising these to chase P&L would
trade the claim for a lottery ticket, and it is the reason they are **not**
env-tunable while other thresholds are.

---

## 3. How it gets out

An open option has **four** exits. Whichever fires first wins.

| Exit | Trigger | `close_reason` |
|---|---|---|
| **Take profit** | premium **≥ +60%** | `option_take_profit` |
| **Stop loss** | premium **≤ −50%** | `option_stop_loss` |
| **Time stop** | held ≥ `timeStopDays` (5 on a "short" horizon) | `agent_time` |
| **Expiry sweep** | **DTE ≤ 2** — unconditional | `agent_expiry` |

Plus **signal exit** (`agent_signal`): a later council pass on the same
underlying comes out SELL.

Order matters. The premium exits are checked **before** the time stop: a
contract already at its target must not sit two more sessions waiting on the
calendar.

**Why these live in our own sweep and not at the broker.** Alpaca cannot
bracket a single-leg option — `OrderClass` allows only `simple`/`mleg` for
`us_option`, and `broker.alpaca` raises `OptionBracketNotSupportedError` if you
try. The broker-side stop/target that protects every equity entry is
structurally unavailable here. Every equity entry gets a real OCO at the
broker; every option entry gets this loop instead. That is a genuine
difference in protection and it should be stated as one.

**The measure is the premium, not the underlying.** On a 0.5-delta call, a 50%
premium stop is roughly a 5% adverse move in the stock. The leverage is the
instrument's whole point and is why a percentage that would be absurd on
shares is ordinary here.

**The asymmetry (+60/−50) is deliberate.** A long option that has not worked
bleeds theta every day it sits, so the loss side has to be tighter than the
gain side is wide.

**No mark means hold.** If the broker does not report a price, no premium exit
fires — a missing price must never close a position. The time stop and the
expiry sweep still run, so nothing is left unmanaged.

**The expiry sweep is not optional.** There is no auto-exercise or assignment
handling in this system. An ITM long call left to expire becomes a share
position the account may not be able to carry. `DTE ≤ 2` force-closes,
automatically, without asking.

---

## 4. What is tunable, and what is not

The split is not arbitrary. **A cap bounds how much capital can ever be at
risk, so an env var that widens it is not a cap.** An exit threshold only
decides when to realize a position whose size those caps already bounded — it
cannot increase maximum loss beyond the premium already paid.

**Env-tunable** (`RiskCaps.from_env`, malformed input keeps the default and logs):

| Var | Default |
|---|---|
| `ALLOW_OPTIONS` | off |
| `OPTIONS_MIN_OPEN_INTEREST` | 100 |
| `OPTIONS_MIN_VOLUME` | 1 |
| `OPTIONS_MAX_SPREAD_PCT` | 12.0 |
| `OPTIONS_TAKE_PROFIT_PCT` | 60.0 |
| `OPTIONS_STOP_LOSS_PCT` | 50.0 |

**Code-level only, by design:** `options_max_premium_pct` (1%),
`options_max_total_premium_pct` (5%), `max_position_pct`,
`daily_drawdown_halt_pct`. Changing one requires a reviewed commit.

**Frozen for the contest:** `selection.py`'s constants. One reviewed change,
then no more, so funnel counts stay comparable across days.

---

## 5. Traps — read before touching options code

1. **`symbol` is the underlying; `occSymbol` is the contract.** Do not
   "simplify" them into one field. `symbol` is what the one-decision-per-
   symbol-per-day dedup keys on, what `ghost_eval` marks equities against, and
   what the UI renders. The OCC string belongs on the wire and nowhere else.
   Alpaca keys option *positions* by OCC, so the close path must match on OCC
   or an option can never be closed at all.
2. **Premium units.** `max_premium_pct` computes `last_price × multiplier`, so
   `last_price` for an option must be the **per-contract premium**, never the
   underlying's share price. Passing the underlying turned a $229 stock into a
   $22,900 "premium" and vetoed 100% of options proposals for weeks behind a
   green test suite.
3. **The same threshold lives in two places.** The liquidity floors exist in
   `selection.py` (a heuristic) *and* in `RiskCaps` (the authoritative veto).
   Loosening one leaves the other rejecting a layer later. Grep before you edit.
4. **The reconciler must tick before the first options pass.**
   `postgres_context._cold_boot_fallback` does not set
   `options_trading_level`, so it defaults to `None` and
   `options_level_insufficient` vetoes every entry.
   `PositionsSnapshot.options_trading_level` is only populated after a
   reconciler tick. **This is a hard ordering constraint at every cold start.**
5. **A fresh Alpaca account may be options level 0** until the options
   agreement is accepted, and approval is not instant. Check it the day
   before, not the morning of.

---

## 6. Honest limitations

- **Paper trading.** Hypothetical results, no real fills.
- **15-minute-delayed indicative feed**, not consolidated OPRA. Fine for
  daily-bar decisions; not a basis for any claim about execution quality.
- **No assignment or exercise handling.** The `DTE ≤ 2` sweep is the only
  protection, and it depends on the sweep actually running.
- **`earnings_blackout` never fires.** See §2.
- **Premium exits depend on our loop being alive.** Unlike an equity bracket,
  which survives our downtime at the broker, an unreached stop is an unenforced
  stop. This is the strongest argument for the expiry sweep being unconditional.

---

*Related: [`CLAUDE.md`](../CLAUDE.md) · [`docs/HACKATHON.md`](HACKATHON.md) ·
[`docs/OPTIONS_PLAN.md`](OPTIONS_PLAN.md) (older design doc, partly superseded)*
