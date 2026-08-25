# Options Trading — Design Plan

**Status:** proposal, not built. Hand to an agent when you want it.
**Prerequisite reading:** `CLAUDE.md` (the prime rule), `PLAN.md` §11 (phase order).

---

## 0. Verified capability (measured 2026-08-26, live paper keys)

Everything below was checked against the account, not assumed:

| Fact | Value |
|---|---|
| `options_trading_level` | **3** (spreads + long/short singles, already approved) |
| `options_buying_power` | $97,569 |
| Contract lookup `/v2/options/contracts` | works |
| Chain snapshot `/v1beta1/options/snapshots/{underlying}` | works |
| Greeks + IV on **near-the-money** strikes | **present** — e.g. `AAPL260828P00305000` δ −0.2790, γ 0.04540, θ −0.4062, ν 0.0942, IV 0.2644 |
| Greeks on **deep-ITM** strikes | **absent** (`null`) — the plan must tolerate this |
| 5-minute option bars `/v1beta1/options/bars` | works |

**Correction to an earlier claim of mine:** I previously said options data required a
paid OPRA subscription. That was wrong. The **free Basic tier does include US options** —
the caveats are quality, not access:

- **Indicative Pricing Feed**, not full OPRA. Derived quotes, not the consolidated tape.
- **15-minute delay** on the most recent data.
- 200 API calls/min; history only since **February 2024**.
- Greeks are missing on some contracts (notably deep ITM and some 0DTE).

That is *good enough to build and paper-trade on*, and **not** good enough to make
short-dated or spread-pricing decisions that depend on a live inside market.
$99/mo Algo Trader Plus removes the delay and gives real OPRA.

---

## 1. Why this is not a small feature

The current system is equity-only in three places that all have to change together.
Adding an options *order type* without these is how you build something that looks
like it works and quietly takes uncapped risk.

### 1.1 The risk engine has no options rules

`packages/engine/engine/risk/` has 14 named deterministic rules. They are calibrated
for **shares**, and several are actively wrong when applied to a premium:

- `max_position_pct: 5.0` — 5% of equity in shares is a position that can draw down.
  5% of equity in long premium is a position that can go to **exactly zero** on a date
  known in advance. Same number, categorically different risk.
- `atr_position_size` sizes from an ATR **stop distance in dollars per share**. An
  option has no meaningful ATR stop; its loss is bounded by the premium and its
  sensitivity is δ/ν/θ, not price distance.
- `pdt_block`, `wash_sale`, `sector_concentration` — need options-aware definitions
  (a call on AAPL is AAPL exposure for concentration; a 0DTE round trip is a day trade).

**The `max_derivative_notional_pct: 20.0` and `lot_sizes` that already exist are
India NSE/BSE F&O**, not US options. Do not reuse them by accident.

### 1.2 Nothing models expiry, assignment, or exercise

Equity positions end when you close them. Options end **on a schedule**, and can end
*for* you:

- Long ITM at expiry → auto-exercised → you wake up owning 100 shares/contract and a
  margin call you did not authorise.
- Short ITM → **assignment**, possibly early for American style.
- The reconciler currently syncs share positions. An option position that vanishes
  overnight because it expired worthless must be reconciled as a realised loss, not as
  a mystery.

### 1.3 The council reasons about the wrong instrument

Analysts read `technicals` computed from **underlying daily bars**. That is genuinely
useful for direction — but a directional view is only one of three inputs to an options
trade. Without IV rank, term structure, and days-to-expiry, the council can be right
about NVDA going up and still lose money buying overpriced IV that crushes after earnings.

---

## 2. Design

Prime rule is unchanged and non-negotiable: **agents propose, deterministic code disposes.**
The LLM never picks a strike, never sizes, never approves. It expresses a *directional
thesis with a conviction and a horizon*; deterministic Python turns that into a contract.

```
Council (unchanged)          Deterministic options layer (new)
─────────────────────        ──────────────────────────────────
direction: BUY/SELL     ──►  1. Should this be an option at all?  (gate)
conviction: 1-5              2. Structure selection               (rules)
horizon: short/mid           3. Contract selection                (chain scan)
underlying technicals        4. Sizing                            (premium-at-risk)
                             5. Options risk rules                (veto)
                                        │
                                        ▼
                                 ApprovalProposal
                                 (you still tap Approve)
```

### 2.1 New package: `packages/engine/engine/options/`

```
options/
  contracts.py    Chain fetch + normalise. OCC symbol parse/build.
  selection.py    Thesis + chain → the one contract. Deterministic.
  greeks.py       Fallback Black-Scholes when the feed omits greeks.
  sizing.py       Premium-at-risk sizing. Replaces ATR for this path.
  rules/          Options-specific risk rules, named like the equity ones.
  expiry.py       DTE math, expiry calendar, auto-exercise projection.
```

### 2.2 Contract selection (the core algorithm)

Deterministic, auditable, no LLM. Inputs: direction, conviction, horizon, chain snapshot,
account state. Output: one contract + why.

Selection criteria, in order:

1. **Expiry window** from the council's horizon — `short` (1–10d hold) maps to
   **21–45 DTE**. Deliberately *not* 0–7 DTE: theta decay dominates, greeks are often
   missing, and the 15-minute data delay is most dangerous where gamma is highest.
2. **Delta band** from conviction — e.g. conviction 3 → δ 0.35–0.50 (roughly ATM);
   conviction 5 → δ 0.55–0.70. Higher conviction buys more directional exposure, never
   more contracts.
3. **Liquidity floor** — reject if `open_interest < 100`, `volume < 10`, or
   **relative spread `(ask-bid)/mid > 8%`**. On the indicative feed this filter is the
   single most important line of code in the module: a wide book will show a fill price
   that never existed.
4. **IV sanity** — reject if IV is `null` (can't price it) or outside a plausible band
   vs the underlying's own realised vol. Buying 90% IV into earnings is a different
   trade from the one the council proposed.
5. Tie-break on tightest relative spread, then highest OI.

**Every rejection carries a named reason** (`no_liquid_contract`, `iv_unavailable`,
`spread_too_wide`, `no_expiry_in_window`) so the Veto Ledger explains itself exactly
like the equity path does.

### 2.3 Structure selection

Start deliberately narrow. Level 3 permits spreads; that is not a reason to ship them first.

| Phase | Structures | Rationale |
|---|---|---|
| **A** | Long call / long put only | Loss is bounded by premium paid. No assignment risk. Simplest thing that is honestly risk-checkable. |
| **B** | Vertical debit spreads | Still bounded, cheaper, but needs multi-leg orders + per-leg fills in the reconciler. |
| **C** | Cash-secured puts / covered calls | Assignment machinery + collateral checks required first. |

**Never without a separate, explicit decision:** naked short calls. Unbounded loss is
incompatible with a system whose selling point is deterministic risk disposal.

### 2.4 Sizing — replaces ATR entirely on this path

Long premium is a **total-loss-possible** instrument. Size on premium at risk:

```
max_premium = min(
    account_equity * OPTIONS_MAX_PREMIUM_PCT,   # default 1.0%, hard cap 2.0%
    OPTIONS_MAX_PREMIUM_ABS,                    # absolute per-trade dollar cap
)
contracts = floor(max_premium / (ask * 100))
```

Reject if `contracts < 1` — do not round up into a bigger position than the rule allows.

Two properties this must have:
- The **whole premium is the risk number**. No stop-loss assumption. An option can gap
  through any stop, and on a 15-min-delayed feed a stop is a suggestion.
- **Portfolio-level greek caps**, not just per-trade: total portfolio δ (as
  underlying-equivalent notional) and total θ/day both capped.

### 2.5 New risk rules (named, deterministic, in `options/rules/`)

| Rule | Default | Blocks |
|---|---|---|
| `options_disabled` | on | Master switch, default OFF |
| `options_level_insufficient` | — | Broker level < required for the structure |
| `max_premium_pct` | 1.0% equity | Oversized premium |
| `max_total_premium_pct` | 5.0% equity | All open long premium combined |
| `min_dte` | 7 | 0DTE/weekly gamma risk |
| `max_dte` | 60 | Capital parked too long |
| `illiquid_contract` | see §2.2 | OI/volume/spread floor |
| `iv_unavailable` | — | No IV → cannot price |
| `earnings_blackout` | ±2 days | IV crush around a known event |
| `portfolio_delta_cap` | 30% equity | Aggregate directional exposure |
| `portfolio_theta_cap` | 0.2%/day | Aggregate time decay burn |
| `naked_short_forbidden` | on | Undefined-risk structures |
| `expiry_day_entry` | on | No new positions expiring today |

### 2.6 Expiry & assignment (`expiry.py` + reconciler work)

Non-negotiable before any options position is opened:

- **Daily DTE sweep.** At T-2 days, an open long option is force-surfaced for a decision
  (close / roll / let expire). Silence must not be a decision.
- **Auto-exercise projection.** Long ITM approaching expiry → warn *before* the broker
  exercises. Alpaca auto-exercises ITM longs; that converts a $500 option into a $30,000
  share position overnight. The app must never let that surprise the user.
- **Reconciler**: a vanished option position resolves to expired-worthless (realised
  loss = premium) rather than an unexplained gap.
- Explicit exercise endpoint (`POST /v2/positions/{symbol}/exercise`) stays **manual-only**.

### 2.7 What the council gains

Add an `options_context` block to the feature dict — **only when options are enabled**,
and only real values (omit the key rather than synthesise, exactly as `fundamentals`
does today):

- IV rank / IV percentile vs the underlying's own 52-week IV history
- ATM IV vs realised vol (rich/cheap)
- Term structure slope (front vs back month)
- Days to next earnings

The analysts stay directional. This block exists so the **deterministic** layer can
refuse an otherwise-fine direction when the options are priced badly.

---

## 3. Order execution notes (from the API, verified)

- **Only `market` and `limit`**; **`day` only**; no extended hours; no fractional/notional.
- **Use limit orders.** On a 15-minute-delayed indicative feed, a market order into a
  wide options book is how you donate money. Limit at mid or better, with a
  configurable max slippage from mid.
- Multi-leg (Phase B) uses `order_class: mleg` with a `legs[]` array.
- Check `options_buying_power` (a distinct field from equity buying power) pre-trade.
- OCC symbol format: `AAPL260828P00305000` = underlying + YYMMDD + C/P + strike×1000,
  8 digits. Parse and build it in `contracts.py`; never string-concatenate at call sites.

---

## 4. Phasing

| Phase | Deliverable | Gate to proceed |
|---|---|---|
| **0** | `engine/options/` skeleton, OCC parse/build, chain fetch, greeks fallback, **tests only — no trading** | Chain + greeks read reliably for 20 symbols |
| **1** | Selection + sizing + risk rules, wired into a **backtest/dry-run only** | Rules demonstrably veto: illiquid, no-IV, oversized, 0DTE |
| **2** | Paper execution, long call/put only, behind `OPTIONS_ENABLED=0` default | 2 weeks paper with expiry sweep working |
| **3** | Expiry/assignment automation + UI (chain view, greeks on pick detail, expiry countdown) | — |
| **4** | Vertical spreads | Phase 3 stable |

**Do not start Phase 2 before the expiry sweep from §2.6 exists.** Opening a position
you have no automated plan to close is the failure mode that actually costs money.

---

## 5. Honest assessment

**Cost to build properly: ~2 weeks.** Not because any single piece is hard — because
options risk is genuinely a different domain, and the parts that make this system worth
showing (named deterministic vetoes, a full audit row, ghost P&L) all have to be
re-derived for a new instrument.

**Cheap versions to refuse:** placing options orders through the existing equity path
with `max_position_pct` doing the risk checking. It will run. It will look fine on a
demo. It is a 5%-of-equity position that can be worth zero on a known date with no rule
that understands that.

**For the current demo:** options stay out, and that is the stronger position to argue —
*"we deliberately don't trade instruments we can't risk-check yet"* is a maturity signal.
This document is the answer to "what would it take?", which is a better answer than a
half-wired options path.

---

## 6. Data subscription decision

Free Basic tier is sufficient for **Phases 0–2** (build, backtest, paper). Buy Algo
Trader Plus ($99/mo) before Phase 3 if any of these become true:

- You want DTE < 7 (the delay is disqualifying near expiry).
- You want spreads (leg pricing needs a live inside market).
- The indicative feed's fills diverge materially from paper fills in Phase 2 — **measure
  this in Phase 2 rather than assuming either way.**
