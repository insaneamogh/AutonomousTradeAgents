# Plan B — the aggressive paper profile

**Status:** plan, not built. Written 2026-08-30 by `ID:MODEL1REAL`. **Nothing here is in the
code yet.** This document supersedes `docs/OPTIONS_PLAYBOOK.md` §2's "1% and 5% are not
negotiable" **once it is implemented** — not before.

---

## 0. What is verified, what is assumed, what you must check

### ✅ Verified (measured 2026-08-30 — do not re-derive)

```
best_strategy({})  ->  rsi_mean_reversion, score 0.60, tradable=True
blind_weight_fraction:  vol_regime_switch 0.400 · breakout 0.350
                        sma_crossover 0.150 · rsi_mean_reversion 0.150 · momentum 0.000
MIN_FIT_TO_TRADE = 0.45
```

- **An empty feature dict is "tradable" at 0.60, not 0.50.** `_rsi_mean_reversion`'s
  `not_a_trend_break` component is `_flag(f.trend_regime != "downtrend")`, and the
  missing-value sentinel `"unknown"` satisfies that as a genuine `True` → 1.0, not NEUTRAL.
  **Raising `MIN_FIT_TO_TRADE` does not close this leak.** Only an explicit evidence gate does.
- **`MIN_FIT_TO_TRADE` has a hard floor of 0.41.** `blind_weight_fraction("vol_regime_switch")`
  is exactly **0.400**. At a floor of 0.40 that strategy clears the trade gate on
  direction-blind checks alone — the precise failure that measure exists to prevent.
- **`fit.py:93` claims "The test suite asserts it stays below the floor for all five
  strategies." That test does not exist.** Grep: the only references to
  `blind_weight_fraction` are its own definition and that docstring. There is no
  `test_fit.py` anywhere. This is a CLAUDE.md §4.2 doc-lies-about-code case and it is the
  single most dangerous thing in this workstream.
- `RiskCaps.from_env`'s docstring states: *"a risk cap that can be widened by an env var
  nobody reviews is not a risk cap"* and explicitly names `options_max_premium_pct`,
  `options_max_total_premium_pct`, `max_position_pct`, `daily_drawdown_halt_pct` as the ones
  that stay code-level.
- Sizing is `qty = floor(budget_usd / (ask × multiplier))` (`engine/options/sizing.py`), and
  it **never rounds up** — a budget that cannot afford one contract sizes to zero and the
  pass becomes a HOLD.

### ⚠️ Assumed — verify before relying on it

- That live drafter confidence clusters 0.55–0.65 and that 0.50 is therefore a binding
  constraint on marginal setups. This is inferred from the prompt's own guidance, **not
  measured against live output.** Check `agent_decisions.judge_confidence` on the new account
  after a day of real passes; if the distribution sits well above 0.50, lowering it buys
  nothing and you should say so rather than shipping a no-op.
- That `min_specialist_avg_score = 45.0` binds live. It never binds under MOCK (mock average
  is 60.7).

### 🔍 You must check first

**Write `test_blind_weight_stays_below_the_trade_floor` BEFORE you touch `MIN_FIT_TO_TRADE`.**
It does not exist, the docstring claims it does, and it is the only thing that will catch a
collision between this workstream and [`PLAN_CANDLE_PATTERNS.md`](PLAN_CANDLE_PATTERNS.md).

---

## 1. Why this exists

The user's instruction, verbatim: *"current execution seems very strict and more towards risk
oriented, its a paper account look towards maximizing profits. hold the winners cut the
losers early."*

I argued the opposite position in `docs/OPTIONS_PLAYBOOK.md` §2 on 2026-08-30 — that 1%/5%
were non-negotiable. **The user has overridden that with reasoning: it is a paper account
with no real capital, in a fixed four-session contest that judges P&L.** That is their call
to make and it is a defensible one. This document implements it.

But one of the numbers is not really about risk appetite at all, and that discovery is worth
leading with:

> **At $100k equity, a 1% premium budget is $1,000. Sizing is
> `floor(1000 / (ask × 100))`. So *any contract priced above $10.00* floors to zero
> contracts and the pass becomes a HOLD.**

The 1% cap was not sizing expensive contracts small. It was **silently refusing whole price
bands** — and because the sizer emits a HOLD with a `notes` string rather than a veto, that
refusal never even appeared in the Refusal Ledger. Raising to 2.5% ($2,500) makes a $12
contract buy 2. This is a bug fix wearing a risk-appetite costume.

---

## 2. The numbers

| Knob | Now | New | Why |
|---|---|---|---|
| `options_max_premium_pct` | 1.0 | **2.5** | See above — the $10.00 contract-price ceiling. Max loss per position becomes 2.5% of equity. |
| `options_max_total_premium_pct` | 5.0 | **12.0** | Whole options book to zero = 12% of equity. **This is the recommended hard ceiling. Do not exceed it.** |
| `min_council_confidence` | 0.50 | **0.42** | Opens the 0.42–0.50 band of marginal setups. ⚠️ verify it actually binds first. |
| `min_specialist_avg_score` | 45.0 | **40.0** | Don't go below 40, or a genuinely bearish analyst set stops mattering at all. |
| `MIN_FIT_TO_TRADE` | 0.45 | **0.42** | ~7% loosening. **Hard floor 0.41** — see §0. |
| delta band | [0.40,0.70] / [0.25,0.55] | **[0.35,0.75] / [0.25,0.65]** | More delta per premium dollar; upper strikes are also the more liquid near-ATM ones. ⚠️ **`HACKATHON.md` §8 freezes `selection.py` after Monday's open — this lands pre-open or not at all.** |
| `options_stop_loss_pct` | 50.0 | **40.0** | "Cut losers early." ~4% adverse move in a 0.5-delta underlying. **Do not go below ~35:** at a permitted 12% relative spread on a delayed mark, a 30% stop is only 2.5× the spread and stops out on quote noise. |
| take-profit | fixed 60.0 | **ratchet: arm +35, giveback 30% of peak, hard ceiling +150** | The fixed +60 *is* the "cuts winners short" behaviour being overridden. See [`PLAN_EXIT_AGENT.md`](PLAN_EXIT_AGENT.md) §3. |
| `max_correlation_cluster` | 3 | **4** | Will start binding once more capital is deployed. |

### Deliberately unchanged: `max_position_pct = 5.0` (equity)

If you want aggression, put it where the loss is **bounded by construction**. A long option's
worst case is the premium paid. An equity position's worst case is the notional. Concentrating
the loosening in the options caps buys more expected return per unit of tail risk — and it is
the version that survives a judge reading the write-up.

### The one that must not move — and the reason is sharper than "it's the only breaker"

**`daily_drawdown_halt_pct = -3.0`.**

*"The entire options book to zero costs 12% of equity"* is only tolerable as a **multi-day**
worst case. What prevents it being a **single-day** worst case is the −3% intraday halt.

**Widening the total-premium cap and holding the halt fixed are one coupled decision, not two
independent ones.** Write it that way in the commit message and in the playbook. It turns "we
got greedier" into "we widened the bound and kept the thing that time-slices it" — which is
both more accurate and more defensible.

Second most load-bearing, named here so nobody quietly moves it later:
`options_max_total_premium_pct` itself. **Ceiling of 12.**

---

## 3. Delivery — a named profile, not an env-widened cap

Two obvious mechanisms, both defective:

- **Editing the defaults** — irreversible without a deploy. If Monday goes badly you cannot
  pull back before Tuesday's open.
- **`AGGRESSIVE_MODE=1` widening the caps via env** — directly contradicts
  `RiskCaps.from_env`'s own documented principle, and those exact caps are listed there as
  the ones that stay code-level. A judge who reads that docstring and then finds
  `OPTIONS_MAX_PREMIUM_PCT` set in Railway has found a **contradiction**, which is worse than
  finding a commit.

### Do this instead

```python
@classmethod
def aggressive_paper(cls, **overrides: object) -> RiskCaps:
    """Paper-account profile for a fixed-window contest. Every number here is
    reviewed, in git, and diffable against the conservative default.

    The env var selects between two REVIEWED profiles; it does not supply a
    number. That distinction is the whole point — `RISK_PROFILE=aggressive_paper`
    cannot express a cap nobody looked at, which is what the paragraph above
    about env-tunable caps is actually protecting against.
    """
```

`from_env` reads `RISK_PROFILE` (default `conservative`) and dispatches.

**Amend the `from_env` docstring** to draw the profile-vs-number distinction explicitly.
Leaving it as-is means the code and its own documentation disagree, which is the §4.2 sin
this repo has already been bitten by twice.

This gets all three properties: numbers visible in git, a judge sees a reasoned commit rather
than a silent widening, and rollback is a 30-second Railway variable change with no deploy.

> On the git-log worry generally: **visibility is an argument *for* the code change, not
> against it.** What you do not want a judge to find is a *silent* widening. A commit titled
> `feat(risk): aggressive paper profile for the contest window`, whose body explains the
> sizing-floor arithmetic and the halt coupling, is a better artifact than no commit at all.

---

## 4. The empty-features leak

`best_strategy({})` returns `rsi_mean_reversion` at **0.60**, tradable. A symbol whose feature
provider returned nothing spends five LLM calls and can produce a proposal. **A data outage
currently increases spend and can originate a trade.**

### The fix — guard in `best_strategy` ONLY

> 🚨 Not in `score_strategy`, not in `rank_strategies`. `blind_weight_fraction` calls
> `_SCORERS[sid](_Features({}), "long")` **deliberately** and must keep working on an empty
> dict. An implementer who "helpfully" moves the guard up a level breaks that invariant and
> the test that depends on it.

```python
def _has_usable_features(features: Mapping[str, Any]) -> tuple[bool, str]:
    """(usable, why_not). An empty/near-empty feature dict must not be 'tradable'.

    Measured 2026-08-30: best_strategy({}) returns rsi_mean_reversion at 0.60 —
    not 0.50 — because `not_a_trend_break` reads `trend_regime != "downtrend"`
    and the missing-value sentinel "unknown" satisfies that as a genuine TRUE.
    Raising MIN_FIT_TO_TRADE does not fix this; only an explicit evidence gate does.
    """
```

Require **all three**:
1. `technicals` block non-empty, **and**
2. `trend_regime not in ("", "unknown")`, **and**
3. ≥3 non-`None` values among the quant keys the scorers actually read:
   `price_zscore_20`, `atr_zscore`, `donchian_pct`, `sharpe`, `ret_21d_pct`, `ret_63d_pct`,
   `ret_252d_pct`, `realized_vol_pct`, `corr_benchmark`.

On failure return `(None, ranked)` and add `"usable_features": False, "unusable_reason": <str>`
to the fit block.

**Then extend `strategy_fit_node`'s rationale branch.** It currently only handles
`ranked == []`. Without the new case the audit row will say *"best was rsi_mean_reversion at
0.60"* while returning HOLD — which reads as a bug to anyone looking at the decision, and
will cost someone an hour.

---

## 5. Doc consistency — same commit, not a follow-up

`docs/OPTIONS_PLAYBOOK.md` §0 states its own rule: *"If you change a threshold, change it
here in the same commit."*

- **§0 one-liner** — new numbers.
- **§2 "Why 1% and 5% are not negotiable" → rewrite, do not delete.** New heading:
  *"Why the caps are 2.5% and 12%, and what still bounds them."* The bounded-loss argument
  survives verbatim in structure — max loss is still the premium, the caps are still the
  position-size cap, the arithmetic is still exact. What changed is the chosen bound and why:
  a fixed four-session paper window with a −3% daily halt, on an account with no real capital.
- **§3 exits table** — replace the +60% row with the ratchet; add `option_trail_stop` and
  `option_agent_close`.
- **§4 tunables** — add the four ratchet knobs and the `RISK_PROFILE` row.
- **New §7** — the exit agent: monotone authority, choice set, fail-safe, tool list, and the
  fact that it cannot place an order.
- **`docs/HACKATHON.md` §3 and §8** currently say *"Do not chase this by raising the caps."*
  Those are now contradicted by the code. **Rewrite them with the user's override recorded
  and dated.** Leaving them is exactly the doc-disagrees-with-code failure this repo has
  already shipped twice.

---

## 6. What happens to the Refusal Ledger claim

The claim is *"we measure, in dollars, what our refusals were worth."* **That claim is about
measurement, not about the caps being small.** Nothing in `ghost_eval` or `build_veto_ledger`
depends on a threshold's value. Three things actually change:

1. **The mix shifts** — fewer `max_premium_pct` blocks, proportionally more
   `illiquid_contract` and funnel-stage rejections. That is *more* interesting, not less: it
   moves the ledger's mass off "we were too small to trade" and onto "we could not price or
   fill this."
2. **The magnitudes get larger**, because ghost P&L marks against a bigger notional. Better
   for the demo.
3. **The one thing that would make it dishonest is claiming a bound you no longer enforce.**

So: stamp `reasoning["risk_profile"] = "aggressive_paper"` on every decision, surface it on
the ledger view, and state the caps in force alongside the numbers.

> *"Here is what we refused, under these caps, which we chose and disclosed."*

That is a **stronger** claim than the old one, because it makes the caps a variable the
ledger reports rather than a boast the README makes.

---

## 7. Tests and the revert-check matrix

| Test | Break this to make it fail |
|---|---|
| **`test_empty_features_are_not_tradable`** | Remove the `_has_usable_features` guard from `best_strategy` → returns `rsi_mean_reversion @ 0.60`. **Must fail loudest — it currently passes trivially with no code at all.** |
| **`test_blind_weight_stays_below_the_trade_floor`** | Set `MIN_FIT_TO_TRADE = 0.40` → `vol_regime_switch` at exactly 0.400 fails. **Write this FIRST. It does not exist despite `fit.py:93` claiming it does.** |
| `test_blind_weight_fraction_still_works_on_an_empty_dict` | Move the guard into `score_strategy` |
| `test_aggressive_profile_leaves_the_drawdown_halt_alone` | Change `daily_drawdown_halt_pct` in `aggressive_paper()` |
| `test_aggressive_profile_leaves_max_position_pct_alone` | Loosen the equity cap too |
| `test_risk_profile_env_selects_the_profile` | Ignore `RISK_PROFILE` |
| `test_unknown_risk_profile_falls_back_to_conservative` | Raise, or silently pick aggressive |
| `test_a_twelve_dollar_contract_sizes_to_at_least_one` | Revert `options_max_premium_pct` to 1.0 → floors to 0 and HOLDs. **This is the test that documents the real reason for the change.** |

An unknown `RISK_PROFILE` value **must fall back to conservative and log a warning** — the
same fail-to-default contract `_env_int` / `_env_float` already use. A typo in a Railway
variable must never silently select the aggressive profile.

Baseline: **792 passed, 9 skipped**; 9 pre-existing ruff errors. `git stash` and re-run before
blaming your change.

---

## 8. Where you are most likely to go wrong

1. **Setting `MIN_FIT_TO_TRADE` to 0.40 or below.** `vol_regime_switch`'s blind fraction is
   exactly 0.400. Floor is 0.41; the plan says 0.42 for margin.
2. **Not writing the blind-weight test first**, then landing [`PLAN_CANDLE_PATTERNS.md`](PLAN_CANDLE_PATTERNS.md)
   with a `directional=False` component, and never finding out.
3. **Putting the empty-features guard in the wrong function** and breaking
   `blind_weight_fraction`.
4. **Shipping `AGGRESSIVE_MODE=1` that widens caps by env** — read §3 again.
5. **Forgetting `strategy_fit_node`'s rationale branch**, leaving an audit row that names a
   winner while returning HOLD.
6. **Changing the delta band after Monday's open.** `HACKATHON.md` §8 freezes `selection.py`
   so funnel counts stay comparable across days. Pre-open or not at all.
7. **Not updating `OPTIONS_PLAYBOOK.md` in the same commit.** Its §0 makes that a rule, and
   this repo has shipped that exact failure twice already.

---

*Related: [`OPTIONS_PLAYBOOK.md`](OPTIONS_PLAYBOOK.md) · [`PLAN_EXIT_AGENT.md`](PLAN_EXIT_AGENT.md) · [`PLAN_CANDLE_PATTERNS.md`](PLAN_CANDLE_PATTERNS.md) · [`HACKATHON.md`](HACKATHON.md)*
