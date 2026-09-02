# 1000-symbol options + equity scan

**Status:** plan, with the one enabling code change identified and built
(batched bars). Written 2026-09-02 against measured API limits, not
against a guess at them.

---

## 0. The request, and the one part of it that cannot work as stated

> *"Lambda functions scan 10k+ stocks every 5 mins. Whatever confidence
> beyond a threshold it sends to pure maths python. Pure maths selects
> 30-40 and passes ~20 to the LLM."*

The **shape** is right and is what this plan builds. One number is not
achievable and pretending otherwise would waste a day discovering it:

**You cannot fetch bars for 10,000 symbols every 5 minutes.** Daily bars
are one HTTP request per batch, Alpaca's free tier allows 200 requests a
minute, and 10k symbols every 5 minutes is 2,000 symbols a minute of
fetch traffic sustained through the session. Batched 100-at-a-time that
is 20 requests/minute — survivable — but it is also ~780,000 bar rows per
sweep for a screen whose answer barely changes between two 5-minute
windows on a daily timeframe.

**You do not need to.** Alpaca already computes the wide screen for you,
server-side, for two API calls: `list_most_active_symbols` ranks the
whole market by volume and by trade count. That IS the "scan everything
and tell me what is moving" step, and it costs two requests instead of
ten thousand. The plan below uses it as Tier 1 and spends its real
compute on the ~150 names it returns.

So: **1,000 symbols in the eligible universe, ~150 examined per sweep,
~30-40 through the maths, ~20 to the LLM.** Same funnel, same end state,
achievable inside the rate limits.

---

## 1. The tiers

Each tier uses a *different data source with a different cost profile*.
That is the design: the expensive data is only ever fetched for names a
cheaper tier already liked.

| Tier | What it does | Source | Calls | Survivors |
|---|---|---|---|---|
| 0 | Eligible universe | `list_tradable_assets` | 2/day | ~1,000 |
| 1 | What is moving now | `list_most_active_symbols` | 2/sweep | ~150 |
| 2 | The maths | batched daily bars → `best_strategy` | ~2/sweep | ~30-40 |
| 3 | Options viability | chain pre-flight | ~1 per candidate | ~20 |
| 4 | The debate | Bull/Bear + guarded tool call | 4 LLM calls each | ~5 book slots |

### Tier 0 — the eligible universe (daily)

`list_tradable_assets` returns ~13,400 rows carrying Alpaca's own
`tradable`, `fractionable` and `has_options` verdicts.

- `fractionable` is the quality filter and it is **Alpaca's judgement,
  not a threshold we invented** — it is enabled on a curated, liquid,
  actively-traded subset, measured at ~7,600 of 13,400 on 2026-09-01.
- `has_options` narrows to the options-eligible names.

Intersect and you get roughly **1,000 symbols**, which is where the
number in the title comes from. Refreshed once a day; it changes on the
timescale of listings, not minutes.

### Tier 1 — what is actually moving (every sweep, 2 calls)

`list_most_active_symbols` merges two independent server-side rankings
(share volume, trade count). **`top` is hard-capped at 100 per ranking** —
the endpoint rejects anything larger outright, confirmed live — so after
dedup the pool is ~150-200 names.

Intersecting with Tier 0 gives ~100-150 candidates that are both
eligible and currently active, for two API calls.

This is the tier that replaces "scan 10k every 5 minutes". It is not a
compromise: a name not in the top 100 by volume *or* trade count is not
a name whose 5-minute-fresh signal we needed.

### Tier 2 — the deterministic maths (every sweep, ~2 calls)

For the Tier 1 survivors: daily bars → `compute_technicals` +
`compute_quant` → `best_strategy` → `score` and `conviction`.

Two things make this cheap:

1. **Batched fetching.** `StockBarsRequest` accepts a list of symbols.
   150 symbols is 2 requests at 100 per batch, not 150 requests. This was
   the one missing capability and is the enabling change this plan
   required.
2. **The day cache.** `AlpacaDailyBarsProvider` caches on
   `(symbol, today, lookback)`, so the first sweep of the day pays for
   the bars and every later sweep is a dictionary lookup. Daily bars do
   not change intraday, so this is exact, not an approximation.

Survivors: those clearing `MIN_FIT_TO_TRADE` (0.45). Typically 30-40.
**Zero LLM calls to here.**

### Tier 3 — options viability (per candidate, ~1 call)

`preflight_chain_is_tradeable` fetches the chain and runs the real
`select_contract` across both conviction regimes. A chain that cannot
produce a tradeable contract is refused here — for **zero model calls**,
which is the fix that came out of the CME post-mortem.

Also applies the freshness stage (`fresh_quote`) once
`options_max_quote_age_seconds` is enabled — see §4.

Survivors: ~20.

### Tier 4 — the LLM (capped)

Top ~20 by `conviction`, subject to `MAX_LLM_SYMBOLS_PER_DAY` (20) and
`MAX_LLM_SYMBOLS_PER_HOUR` (4). Bull/Bear debate, then a guarded
`open_option_trade`. `ToolGuard.before()` re-runs the entire risk stack
on every call regardless of what the model asked for.

---

## 2. Cost

Per trading day, 6.5 hours, 5-minute sweeps = 78 sweeps.

| Item | Calls/day |
|---|---|
| Tier 0 universe | 2 |
| Tier 1 most-active | 156 |
| Tier 2 bars (first sweep only, then cached) | ~10 |
| Tier 3 chain pre-flights | ~20 |
| **Alpaca total** | **~190/day** |

Against a 200-requests-per-**minute** limit, that is not close to a
constraint. The binding constraint is the LLM budget, not the data API.

| Item | Cost/day |
|---|---|
| 20 symbols × ~4 Sonnet calls | ~$0.80 |
| Hard ceiling (`MAX_DAILY_LLM_SPEND_USD`) | $3.00 |

**Two days of trading: ~$2 expected, $6 worst case.** Comfortably inside
a $10 budget, on Sonnet, with no model downgrade.

---

## 3. Why ranking 1,000 candidates needs the conviction signal

Tier 2 produces 30-40 survivors and Tier 4 can afford ~20. Something has
to order them, and until 2026-09-02 that ordering did not work.

`score` is a weighted mean of ~9 bounded components — a central
statistic, and central statistics compress. Measured across 300 symbols:
**every passer scored between 0.6075 and 0.6107.** Eighteen distinct
values inside a 0.3% band. Sorting a thousand candidates by that is
decided by the tie-break, not by quality.

`conviction` measures only the positive evidence and how far above
neutral it sits. Measured dispersion, same datasets:

| Dataset | score spread | conviction spread | ratio |
|---|---|---|---|
| synthetic (300 symbols) | 0.0032 | 0.0179 | 5.6× |
| eval archetypes (100) | 0.1946 | 0.3892 | 2.0× |

**Unverified on live features.** Both datasets are synthetic: the
generator derives every feature from one hash seed so its inputs barely
vary, and the archetypes are hand-built and several saturate. Whether
real Alpaca features disperse is the single highest-value measurement
outstanding — see `docs/RAILWAY_CHECKS.md`.

If real features turn out to disperse the *score* adequately, conviction
costs nothing and remains a reasonable secondary. If they do not, this
plan depends on it.

---

## 4. Options-specific correctness

### Quote freshness — why the timing question matters

An option's value moves second to second and the path from "we saw a
price" to "we sent an order" runs through two model calls.

**The obvious version of this problem does not exist.** Every entry goes
out as `OrderType.LIMIT` at the guard-selected price, and a limit order
cannot fill above its limit. Overpaying is structurally impossible.

**The real version is the contract CHOICE.** Selection reads delta, IV
and spread off the same snapshot. A stale snapshot yields stale greeks,
picks the wrong strike, and the limit price then faithfully protects the
price of a contract that should never have been selected.

Now plumbed end to end (`latest_quote.timestamp` → `ChainQuote.quote_ts`
→ `ContractQuote.quote_ts` → a `fresh_quote` stage that runs *first*, so
no later stage reasons about a contract that no longer exists at that
price).

**Ships disabled**, and the reason is the feed:

| Feed | Baseline quote age | Correct setting |
|---|---|---|
| INDICATIVE (default, free) | ~900s (documented 15-min delay) | 1800 |
| OPRA (real-time) | seconds | 300 |

Setting 300 on the indicative feed refuses **100% of options trades**.
Verify the feed and the timestamps before enabling either.

### Sizing must know about liquidity

`options_position_size` is `floor(budget / cost)` plus a liquidity trim
at 1% of open interest. The trim exists because the dollar budget answers
"what can we afford to lose" and says nothing about "can we get back
out" — 167 open interest and 28,000 cost the same and used to size the
same. It trims, never vetoes; refusals belong upstream where they get a
named ledger reason.

### The book is five positions wide

`options_max_total_premium_pct` (7.5) ÷ `options_max_premium_pct` (1.5).
The aggregate is pinned by the halt coupling
(`max_options_book_drawdown_pct ≤ |daily_drawdown_halt_pct|`) and cannot
rise, so per-position size is the only lever on width.

**A 1000-symbol scan does not change this.** Scanning wider finds better
candidates; it does not create room to hold more of them. Expect ~5
concurrent positions turning over on a 5-day time stop, not 20-30
concurrent. Wanting more requires a shorter time stop, which is a
strategy change, not a config one.

---

## 5. What to build, in order

| # | Change | Status |
|---|---|---|
| 1 | Batched bar fetching (`symbol_or_symbols` as a list) | **built** |
| 2 | `conviction` as the rank key | **built** |
| 3 | Quote-freshness plumbing + stage | **built, disabled** |
| 4 | Tier 0/1 wiring: intersect most-active with the eligible universe | not built |
| 5 | Raise `DEFAULT_EQUITY_CANDIDATES` 100 → ~1000 once 1 and 4 land | not built |
| 6 | Measure live score/conviction dispersion | **needs live keys** |

Items 4 and 5 are the remaining work and they are small — the expensive
parts (batching, ranking, the pre-flights) are done. They are listed as
not built because they change what the scanner *does* in production, and
that is not a change to make unverified the night before a submission.

---

## 6. What this plan does not claim

- **No backtest.** Nothing here measures whether the strategy makes
  money. The eval suite (`apps/agents/tests/eval`) tests funnel logic,
  not profitability, and says so.
- **No live verification.** Every measurement quoted is from synthetic
  data or from documented API limits. Nothing was run against a live
  account from this session — there are no credentials in it.
- **Scanning wider is not an edge by itself.** It raises the ceiling on
  candidate quality. Whether the strategy converts better candidates
  into P&L is a separate, unmeasured question.
