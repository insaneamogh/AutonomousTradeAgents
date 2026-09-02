# Scoring and tool-call contract

What decides which symbols cost money, what the agents are allowed to
do with them, and where each decision is enforced.

The one rule everything below serves: **agents propose, deterministic
code disposes.** Every number here is computed in Python. The model
chooses direction, conviction and which tool to call; it never sets a
threshold, never sizes a position, and never reaches a broker.

---

## 1. The two scoring numbers, and why there are two

`StrategyFit` carries both. They answer different questions and using
either for the other's job produces a specific, observed failure.

### `score` — "is this tradable at all?"

Weighted mean of ~9 bounded components, times the Reflection prior,
clamped to 0..1. Gate: `score >= MIN_FIT_TO_TRADE` (0.45).

A mean is the right instrument for a floor. It is forgiving of one bad
component, it degrades gracefully when data is missing (absent inputs
score `NEUTRAL` = 0.5), and it answers "on balance, is there a case
here?" — which is exactly what a tradability gate should ask.

### `conviction` — "which of these should we look at first?"

```
sum(weight * max(0, component_score - 0.5) / 0.5) / sum(weight)
```

Only positive evidence counts, weighted by how far above neutral it sits.
A neutral or negative check contributes nothing, because it is not
support.

**Why a second number exists.** A mean is a central statistic and central
statistics compress. Measured across 300 symbols: every symbol clearing
the floor scored between **0.6075 and 0.6107** — 18 distinct values
inside a 0.3% band. Sorting candidates by that is decided by the
tie-break, not by quality.

The mean also cannot see a distinction that matters. Two setups, nine
components each, identical mean of 0.55:

| | components | mean | conviction |
|---|---|---|---|
| A | all ~0.55 | 0.55 | ~0.10 |
| B | four at 1.0, five at 0.19 | 0.55 | ~0.44 |

A is a shrug. B is a thesis with specific support and specific
objections. Ranking B first is the point.

**A design note worth keeping:** the first version of `conviction`
counted the weighted *share* of components over a 0.6 line. With ~9
components that can take ~9 values, and it measured out at **two distinct
values across a hundred scenarios** — coarser than the number it was
meant to improve on. "Count things over a threshold" is the obvious
design and it is worse than measuring how far over they are.

**Measured dispersion:**

| Dataset | `score` | `conviction` | ratio |
|---|---|---|---|
| synthetic (300) | 0.0032 | 0.0179 | 5.6× |
| eval archetypes (100) | 0.1946 | 0.3892 | 2.0× |

**Unverified on live features.** Both datasets are synthetic and both are
inadequate in opposite ways — the generator's features all derive from
one hash seed, and the archetypes are hand-built and several saturate.
See `docs/RAILWAY_CHECKS.md` §6.

### The rule

| Question | Number | Never use |
|---|---|---|
| May this trade? | `score` vs `MIN_FIT_TO_TRADE` | `conviction` |
| Which first? | `conviction`, then `score`, then symbol | `score` alone |

`conviction` is deliberately **not** wired into `tradable`. Ranking
changes which candidates get attention; it must not change which are
allowed to trade, or a ranking tweak silently becomes a risk change.

---

## 2. The funnel — where money starts being spent

```
watchlist
   |  deterministic, zero LLM cost
   v
best_strategy  ->  score >= 0.45 ?           no -> free HOLD
   |
   v
rank by (conviction, score, symbol)
   |
   v
MAX_LLM_SYMBOLS_PER_HOUR / _PER_DAY          over -> free HOLD, named reason
   |
   v
preflight_can_open      (book/account)       fail -> HOLD, ZERO model calls
preflight_chain_is_tradeable (chain)         fail -> HOLD, ZERO model calls
   |
   v  ===== everything below costs money =====
Bull / Bear debate            2 model calls
   |
   v
resolve()  ->  proceed ?                     no  -> HOLD
   |
   v
trade hop                     1 model call
   |
   v
ToolGuard.before()  ->  the full risk stack  veto -> named refusal
   |
   v
LIMIT order via packages/broker
```

Both pre-flights exist because the deterministic gates used to run
*inside* the tool guard — i.e. after three model calls. The book hit its
premium cap at 15:00 UTC one day and every options pass for the next
three hours paid ~3 Sonnet calls to be told a fact that had nothing to do
with the symbol.

---

## 3. Tool-call contract

### What the agents may call

| Tool | Who | Effect |
|---|---|---|
| `open_option_trade` | Bull only | Opens a position — **fully guarded** |
| `adjust_option_position` | escalation | Tighten/close/scale — **fully guarded** |
| read-only tools | both | No risk; guard returns allow unconditionally |

### What the guard re-derives, every call

`ToolGuard.before()` does not trust one field of the model's arguments as
a decision input. It re-runs, from scratch: contract selection (the whole
funnel), liquidity and chain depth, the premium caps, position sizing
including the liquidity trim, options trading level, and finally the same
`evaluate()` the executor and risk officer call.

Consequences worth stating plainly:

- **The model cannot size a position.** It may state a direction and a
  conviction; quantity comes from `options_position_size`.
- **The model cannot pick a contract.** It names an underlying; the OCC
  contract comes from `select_contract`.
- **A weaker model degrades SELECTION, never RISK CONTROL.** That was the
  argument for running Haiku here, and it is true — see §4 for why it was
  reverted anyway.

### Refusals are teaching, not errors

A denied call returns `{"is_error": true, "content": {"denied": "<named
rule>"}}` and the loop continues. The model may adjust once. Nothing in
the guard raises into the caller.

Every refusal carries a **named** rule (`illiquid_chain`,
`max_total_premium_pct`, `size_rounds_to_zero`, `stale_quote`,
`no_liquid_contract`, …). An unnamed refusal is a defect even when the
refusal is correct — the Refusal Ledger is this project's differentiator,
and a refusal nobody can count cannot be priced or shown.

---

## 4. Model choice

Default: **Sonnet**. `OPTIONS_AGENT_MODEL=haiku` forces the cheap model.

This ran on Haiku for a few hours on 2026-09-02 on a cost argument that
was sound in isolation and wrong in context. The argument: the guard
re-runs the entire risk stack regardless of which model asked, so a
weaker model costs selection quality and never risk control.

What it missed is **how** a weaker model fails here. The trade hop must
emit a well-formed `open_option_trade` call, and a hop that never emits
one produces no trade, no error and no ledger row — the same observable
as a market with nothing worth trading. On a book capped at five
concurrent positions there is no way to tell "nothing qualified" from
"the model cannot drive the tool loop".

So the fix was both halves: revert to Sonnet, **and** make the failure
visible. `_attempted_trade` now separates:

| Outcome | Meaning | Remedy |
|---|---|---|
| "Agents agreed but chose not to open" | a judgement | trust the desk |
| "Trade hop produced no `open_option_trade` call" | tool-calling failure | change model/prompt |

Denied attempts count as attempts — a denial means the model formed a
well-shaped call and the deterministic guard refused it, which is the
system working.

Cost is bounded by the day/hour symbol caps and the two pre-flights,
which cut spend by debating **fewer** symbols rather than debating them
**worse**. Expected ~$0.80/day against a $3.00 hard ceiling.

---

## 5. Options-specific determinism

An option's value changes second to second, so the deterministic layer
has to be explicit about *when* each number was true.

| Concern | Handled by | Status |
|---|---|---|
| Paying more than we sized for | `OrderType.LIMIT` at the guard price | structural — a limit cannot fill above its limit |
| Selecting on stale greeks | `fresh_quote` stage, runs first | **plumbed, disabled** — see below |
| Contract too thin to exit | 1% of open-interest sizing trim | live |
| Chain with one survivor | `illiquid_chain`, depth ≥ 5 | live |
| Stop while we are down | resting broker stop-limit | live, **never yet placed for real** |

**The freshness stage ships disabled** because the default options feed
is INDICATIVE — derived quotes on a documented ~15-minute delay — so
every quote is ~900s old as a property of the tier. A 300s gate refuses
100% of options trades. Settings are per-feed (INDICATIVE → 1800,
OPRA → 300) and the verification is in `docs/RAILWAY_CHECKS.md` §3.

It fails **closed** on an absent timestamp, which is the opposite of the
fail-open convention used by the pre-flights and the CLI wrapper. The
difference: those degrade to a slower path, this one would degrade to a
wrong price.

---

## 6. Verifying any of this

```bash
# Funnel logic over 100 labelled scenarios — offline, no keys, <1s
.venv/bin/python -m pytest apps/agents/tests/eval -q

# Human-readable scorecard
cd apps/agents && ../../.venv/bin/python -m tests.eval.run_eval
```

The eval suite tests funnel **logic**, not profitability. It is not a
backtest and says so in three places.
