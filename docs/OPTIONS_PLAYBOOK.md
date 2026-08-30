# The options playbook — every rule the machine plays by

**This document is derived from the code, not from intent.** Every number
below was read out of the module named beside it on 2026-08-30. If you change
a threshold, change it here in the same commit — and if this file and the code
ever disagree, *the code is right and this file is a bug*.

Scope: **Phase A only — long calls and long puts, single leg, US equity
options.** No spreads, no selling to open, no assignment handling. That is not
a simplification for the contest; it is what bounds the loss.

> ⚠️ **Both plans queued alongside this document have now shipped, 2026-08-30.**
> [`PLAN_AGGRESSIVE_PROFILE.md`](PLAN_AGGRESSIVE_PROFILE.md) is implemented:
> `RiskCaps.aggressive_paper()` is a reviewed profile, dispatched via
> `RISK_PROFILE` (default `conservative` — the numbers this file always had;
> `aggressive_paper` is the wider ones now described in §2/§3). And
> [`PLAN_EXIT_AGENT.md`](PLAN_EXIT_AGENT.md)'s A.1+A.2 shipped: the trailing
> ratchet (`options_ratchet_enabled=True` by default) replaces the old flat
> take-profit described in earlier revisions of this section. §2, §3 and §4
> below reflect both, combined, as the code actually runs today.
>
> **Still open:** the LLM exit agent (A.3/A.4 of `PLAN_EXIT_AGENT.md` — a
> monotone-authority model consult that can only close a position EARLIER
> than the trail would, never later, never hold longer, never place an
> order) has **not** shipped yet; nothing in this file describes it because
> nothing in the code does it yet. Per that plan's own build order, it
> lands only after "deploy, watch one session of pure ratchet evidence."

---

## 0. The one-line version

> The agent may buy one call or one put per underlying per day, 10–45 days
> out, near the money, in a contract that actually trades, for no more than
> **1% of equity** (**2.5%** under the `aggressive_paper` profile) and no more
> than **5% across the whole book** (**12%** under `aggressive_paper`) — and it
> must close that position on a target, a stop, a clock, or an expiry,
> whichever comes first.

Everything below is that sentence, enforced. Two profiles exist —
`conservative` (the numbers above without the parenthetical) and
`aggressive_paper` (the parenthetical) — selected by the `RISK_PROFILE` env
var, default `conservative`. See §2 and §4.

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
| 3 | `delta_band` | conviction ≥ 0.7 → &#124;δ&#124; ∈ **[0.35, 0.75]**; else **[0.25, 0.65]** | `no_delta_in_band` |
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
- **The delta bands overlap at 0.35–0.65, deliberately.** They used to be
  disjoint (`[0.45,0.65]` / `[0.25,0.45]`), which made a 0.50-delta contract —
  ATM, the most liquid strike on the board — eligible *only* when LLM
  confidence cleared 0.7. Conviction should move the band, not exclude the
  middle of the chain. Widened once more 2026-08-30, from `[0.40,0.70]` /
  `[0.25,0.55]`, for the contest window (`docs/PLAN_AGGRESSIVE_PROFILE.md`
  §2 — more delta per premium dollar, and the upper strikes it reaches are
  also the more liquid near-ATM ones). **Frozen after this change** per
  §4/`docs/HACKATHON.md` §8 — not touched again once Monday's open happens,
  so funnel counts stay comparable across the contest's trading days.
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
| `max_premium_pct` | one position's premium > **1% of equity** (**2.5%** under `aggressive_paper`) — *trims first* |
| `max_total_premium_pct` | all open premium > **5% of equity** (**12%** under `aggressive_paper`) — blocks, never trims |

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

### Why the caps are 2.5% and 12% (was 1%/5%), and what still bounds them

Shipped 2026-08-30 via `RiskCaps.aggressive_paper()`
(`docs/PLAN_AGGRESSIVE_PROFILE.md`), dispatched by `RISK_PROFILE`
(default `conservative` — the original 1%/5%). The *structure* of the
original argument survives unchanged; only the chosen bound moved, and
one number moved with it on purpose (the stop-loss, §3) while the two
that matter most did **not** move at all.

A long option's maximum loss is the entire premium. So `options_max_premium_pct`
**is** the position-size cap — it is not a proxy for one. That did not change.
What changed is which bound the position-size cap enforces: 1%/5% under
`conservative`, 2.5%/12% under `aggressive_paper` — the entire options book
going to zero costs 5% (or 12%) of equity, by construction, with no
assumption about stops filling or gaps behaving, whichever profile is active.

**One of the two numbers was never really about risk appetite.** At $100k
equity, a 1% premium budget is $1,000, and sizing is
`floor(budget / (ask × 100))` — so any contract priced above $10.00 floored
to zero contracts and the pass silently became a HOLD (never even reaching
the Refusal Ledger, because the sizer emits a HOLD via `.notes`, not a veto).
2.5% ($2,500) makes a $12 contract buy 2. `packages/engine/tests/test_options_sizing.py`'s
`test_the_old_one_percent_cap_floored_a_twelve_dollar_contract_to_zero` /
`test_a_twelve_dollar_contract_sizes_to_at_least_one` pin exactly this.

**What still bounds it, and why 12% is a ceiling, not a step:**
`daily_drawdown_halt_pct = -3.0` **does not move, in any profile.** "The
entire options book to zero costs 12% of equity" is only tolerable as a
**multi-day** worst case; the −3% intraday halt is what keeps it from being
a **single-day** one. Widening the total-premium cap and holding the halt
fixed is one coupled decision, not two independent ones — raising 12% any
further without re-deriving that coupling would trade the capital-preservation
claim for a lottery ticket. That is also why these two numbers are **not**
env-tunable directly (no `OPTIONS_MAX_PREMIUM_PCT` env var exists) while
other thresholds are — only a reviewed profile classmethod can move them,
and `RISK_PROFILE` only ever chooses between two such profiles, never
supplies a number itself. See §4.

---

## 3. How it gets out

An open option has **five** exits. Whichever fires first wins, checked in
this order: stop-loss, hard take-profit backstop, trailing stop, time stop,
expiry sweep (plus signal exit, checked alongside the time stop).

| Exit | Trigger | `close_reason` |
|---|---|---|
| **Stop loss** | premium **≤ −50%** (**≤ −40%** under `aggressive_paper` — "cut losers early") | `option_stop_loss` |
| **Hard take-profit backstop** | premium **≥ +150%** | `option_take_profit` |
| **Trailing stop** | armed (peak ≥ +35%) AND premium retraced to ≤ 70% of peak | `option_trail_stop` |
| **Time stop** | held ≥ `timeStopDays` (5 on a "short" horizon) | `agent_time` |
| **Expiry sweep** | **DTE ≤ 2** — unconditional | `agent_expiry` |

Plus **signal exit** (`agent_signal`): a later council pass on the same
underlying comes out SELL.

**The old fixed +60% take-profit is gone by default** (`RiskCaps.
options_ratchet_enabled = True`), replaced by a trailing ratchet
(`engine.options.exits.option_ratchet_signal`) that arms once the position's
peak gain reaches **+35%** and then gives back **30% of the peak** before
closing — proportionally, not as a flat point count: a peak of +80% draws the
line at +56%, a peak of +200% draws it at +140%. A hard +150% ceiling remains
as a backstop for a single-tick gap the trail somehow never caught. The whole
ratchet reverts to the old flat +60%/−50% behavior by flipping
`OPTIONS_RATCHET_ENABLED=0` — see §4.

**Stop wins over trail on a gap through zero.** A peak of +50% gapping to
−60% satisfies both "past the stop" and "below the trail line" — the stop is
checked first and wins, so the audit row reads `option_stop_loss`, which is
the more honest label for a loss that happened to pass through a level the
trail formula would also have caught.

**The high-water mark is persisted per position**, in
`agent_decisions.reasoning->option_exit->peak_pl_pct`, written via
`jsonb_set` (never a whole-column overwrite — see §5 traps) only on the
ticks where the peak actually advances.

Order matters. The premium exits are checked **before** the time stop: a
contract already at a stop/backstop/trail level must not sit two more
sessions waiting on the calendar.

**Why these live in our own sweep and not at the broker.** Alpaca cannot
bracket a single-leg option — `OrderClass` allows only `simple`/`mleg` for
`us_option`, and `broker.alpaca` raises `OptionBracketNotSupportedError` if you
try. The broker-side stop/target that protects every equity entry is
structurally unavailable here. Every equity entry gets a real OCO at the
broker; every option entry gets this loop instead. That is a genuine
difference in protection and it should be stated as one.

**The measure is the premium, not the underlying.** On a 0.5-delta call, a 50%
(or, under `aggressive_paper`, 40%) premium stop is roughly a 5% (4%) adverse
move in the stock. The leverage is the instrument's whole point and is why a
percentage that would be absurd on shares is ordinary here.

**The stop is tight; the ceiling is wide, on purpose.** A long option that has
not worked bleeds theta every day it sits, so the loss side stays tight (50%,
or **40%** under `aggressive_paper` — "cut losers early") while the trail —
not a fixed ceiling — is now the mechanism that decides when a winner is
done. **Do not go below ~35 on the stop, in either profile:** at the
permitted 12% relative spread on a delayed mark, a 30% stop is only 2.5× the
spread and starts stopping out on quote noise rather than a real adverse
move — `aggressive_paper`'s 40% already accounts for this floor.

**No mark means hold, and the peak is left exactly alone.** If the broker does
not report a price, no premium exit fires and the persisted peak does not
move — a missing price must never close a position or manufacture a data
point in either direction. The time stop and the expiry sweep still run, so
nothing is left unmanaged.

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

**Profile, not a number** (`RiskCaps.from_env`):

| Var | Default | What it does |
|---|---|---|
| `RISK_PROFILE` | `conservative` | Picks the BASE profile before any of the env vars below apply on top: `conservative` (bare `RiskCaps()`) or `aggressive_paper` (`RiskCaps.aggressive_paper()` — the wider premium caps and confidence floors, tighter stop, see §2/§3). An unrecognised value falls back to `conservative` and logs a warning — same fail-to-default contract as every var below. This is a choice between two REVIEWED, in-git profiles; it cannot express a cap nobody looked at, which is the whole point given the next paragraph. |

**Env-tunable** (malformed input keeps the default and logs; the "default"
below is whichever profile's own value — only `OPTIONS_STOP_LOSS_PCT` differs
by profile today):

| Var | Default |
|---|---|
| `ALLOW_OPTIONS` | off |
| `OPTIONS_MIN_OPEN_INTEREST` | 100 |
| `OPTIONS_MIN_VOLUME` | 1 |
| `OPTIONS_MAX_SPREAD_PCT` | 12.0 |
| `OPTIONS_TAKE_PROFIT_PCT` | 60.0 — read only when the ratchet is disabled |
| `OPTIONS_STOP_LOSS_PCT` | 50.0 under `conservative` / 40.0 under `aggressive_paper` — read by BOTH the ratchet's stop and the legacy flat exit |
| `OPTIONS_RATCHET_ENABLED` | **on** — the one flag here that fails OPEN, not closed |
| `OPTIONS_TRAIL_ARM_PCT` | 35.0 |
| `OPTIONS_TRAIL_GIVEBACK_PCT` | 30.0 (percent OF THE PEAK, not percentage points) |
| `OPTIONS_HARD_TAKE_PROFIT_PCT` | 150.0 |

`OPTIONS_RATCHET_ENABLED` is the one exception to "unset means off" among the
flags in this table: it defaults to **True**, because the ratchet is the
intended behavior, not an opt-in. Set it to an explicit falsy value
(`0`/`false`) to revert every open option to the flat `OPTIONS_TAKE_PROFIT_PCT`
/`OPTIONS_STOP_LOSS_PCT` behavior this whole ratchet replaced — that single
flag is the entire revert path, by design.

**Code-level only, by design:** `options_max_premium_pct` (1% /
`aggressive_paper` 2.5%), `options_max_total_premium_pct` (5% /
`aggressive_paper` 12%), `max_position_pct` (5%, same in both profiles),
`daily_drawdown_halt_pct` (-3.0, same in both profiles — see §2's "what
still bounds it"). Changing one requires a reviewed commit — either a new
number in an existing profile classmethod, or a new profile entirely. No
env var supplies any of these four as a raw number, in either profile.

**Frozen for the contest:** `selection.py`'s constants (the DTE window and
the delta bands). One reviewed widening of the delta bands landed alongside
this profile, 2026-08-30 — no more after Monday's open, so funnel counts
stay comparable across days.

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
- **The trail's 30% giveback is sized for a noisy, delayed mark, not tuned for
  return.** The broker P&L the trail reads is itself derived from a
  15-minute-delayed indicative quote on a contract we permit up to a 12%
  relative spread. A tighter giveback (10%, say) would look more aggressive
  about "locking in gains faster" but would in practice fire on quote noise
  as often as on a real reversal — the position closing and reopening cost
  (spread + missed continuation) on a false trigger is worse than the
  giveback surrendered. 30% was chosen against that noise floor, not against
  a return-optimization backtest.
- **No assignment or exercise handling.** The `DTE ≤ 2` sweep is the only
  protection, and it depends on the sweep actually running.
- **`earnings_blackout` never fires.** See §2.
- **Premium exits depend on our loop being alive.** Unlike an equity bracket,
  which survives our downtime at the broker, an unreached stop is an unenforced
  stop. This is the strongest argument for the expiry sweep being unconditional.
- **The exit-agent LLM consult (`PLAN_EXIT_AGENT.md` A.3/A.4) has not shipped.**
  Only the deterministic ratchet in this section is live. When it does ship,
  its authority is monotone by design — it can only close a position EARLIER
  than the trail would, never later, never bigger, never by itself on error
  or timeout (the fail-safe is to keep trailing, not to close).

---

*Related: [`CLAUDE.md`](../CLAUDE.md) · [`docs/HACKATHON.md`](HACKATHON.md) ·
[`docs/OPTIONS_PLAN.md`](OPTIONS_PLAN.md) (older design doc, partly superseded)*
