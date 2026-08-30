# Plan C — candlestick pattern recognition

**Status:** plan, not built. Written 2026-08-30 by `ID:MODEL1REAL`. **Greenfield — a
repo-wide search for `engulfing|hammer|doji|marubozu|harami|candlestick|wick|shadow` returns
zero matches.** Nothing exists to build on and nothing exists to break.

---

## 0. Read this before you write a line

### 🚨 Four hard constraints. Violating any of them fails the build or silently no-ops.

**1. `pandas`, `numpy` and `pandas-ta` are FORBIDDEN in this module and its tests.**

They are declared in `packages/engine/pyproject.toml` and installed in `.venv`, and
`pandas-ta` even ships a `cdl_pattern` helper that looks like it does exactly this job. **Do
not use it.** Two independent reasons:

- Every indicator in this codebase is pure stdlib `math` + list comprehensions, by explicit
  policy. `quant.py:30-31`: *"Pure stdlib math throughout. numpy would buy nothing at these
  sizes and would cost a typed boundary."*
- `pytest` runs with `filterwarnings = ["error"]`. `pandas-ta` emits deprecation warnings on
  import under current pandas/numpy, so **importing it fails the test suite at collection** —
  not in your test, in *every* test.

**2. mypy runs `strict = true`.** Fully annotate everything. Return a frozen dataclass with
an `as_dict()`, mirroring `QuantFeatures` — a heterogeneous bare `dict` is painful under
strict and inconsistent with every sibling module.

**3. Emit scored floats and a names tuple. NEVER a bool.**

`fit.py:195` `_num()` maps `bool → None`, and `_ramp(None)` returns `NEUTRAL = 0.5`. So a
boolean pattern flag read through `f.tech()` scores **0.5 forever and can never reach 1.0**.
The insidious part: every test that asserts "the pattern was detected" still passes, because
those check the *block*, not the *fit*. You would ship a feature that does nothing and have
green tests saying otherwise.

**4. Every new `FitComponent` must be `directional=True`.**

`blind_weight_fraction("vol_regime_switch")` is already exactly **0.400** and
[`PLAN_AGGRESSIVE_PROFILE.md`](PLAN_AGGRESSIVE_PROFILE.md) lowers `MIN_FIT_TO_TRADE` to 0.42.
A `directional=False` component pushes a strategy's blind fraction toward the trade floor,
at which point it clears the gate on checks that cannot tell long from short. **This is the
collision point between the two workstreams.** You may be tempted to mark it non-directional
because "a candle doesn't know direction" — it does: `reversal_bull` and `reversal_bear` are
separate fields.

### ✅ Verified (measured 2026-08-30)

- Bars are `list[DailyBar]` — a frozen dataclass `(day, open, high, low, close, volume)` at
  `packages/engine/engine/features/technicals.py:30`, **oldest → newest**, ~220 per pass
  (320 calendar-day lookback ending *yesterday* — never today's half-formed candle).
- **`RealFeatureProvider.__call__` (`provider.py:233-278`) discards `bars` when it returns.**
  The feature dict has no `bars` key. Nothing in `apps/agents`, nothing in `fit.py`, can
  reach OHLC arrays. **Anything bar-derived must be computed inside the provider.**
- `compute_technicals` already produces `atr_14` and `trend_regime`. `compute_quant` never
  raises — every field independently degrades to `None`.
- `technical_analyst.py:28-38` and `:44-60` enumerate fixed tuples of feature keys rendered
  via `render_features(..., label_width=25)`, which renders a missing key as literal `n/a`.

### 🔍 Check first

Baseline `792 passed, 9 skipped` before you start.

---

## 1. `packages/engine/engine/features/patterns.py`

```python
@dataclass(frozen=True)
class PatternBlock:
    reversal_bull: float        # 0..1, ATR-normalised and trend-context-gated
    reversal_bear: float
    continuation_bull: float
    continuation_bear: float
    indecision: float           # doji family — direction-neutral
    compression: float          # inside bar / NR7 — a coil
    expansion: float            # outside bar / wide-range
    names: tuple[str, ...]      # every pattern scoring >= 0.35, strongest first
    top_pattern: str | None
    top_pattern_score: float

    def as_dict(self) -> dict[str, Any]: ...


def detect_patterns(
    bars: list[DailyBar], *, atr: float, trend_regime: str
) -> PatternBlock: ...
```

**`atr` and `trend_regime` are parameters, not recomputed.** `compute_technicals` already
produced both. Recomputing them here creates the "same number in two places" trap that
CLAUDE.md §4.4 exists because of — and that `options_min_volume` already demonstrated the
hard way.

Guards: `atr <= 0` or `len(bars) < 7` → the all-zero block with `names=()`. **Never raise.**
A pattern failure must not take down the whole feature pass for a symbol.

### Scoring — `quality × magnitude × context`, all in 0..1

Per-bar primitives, all expressed **in ATR units**:

```
body  = |close - open|
upper = high - max(open, close)
lower = min(open, close) - low
rng   = high - low
body_atr = body / atr        rng_atr = rng / atr
```

- **quality** — how cleanly the geometry holds, as a *ramp*, not a threshold. A hammer:
  `_ramp(lower / max(body, eps), low=2.0, high=3.0)`. Ramps mean a marginal pattern scores
  marginally rather than flipping a switch.
- **magnitude** — `_ramp(rng_atr, low=0.5, high=1.5)`. **This is the ATR normalisation and it
  is the point of the whole design.** Below half an ATR of range the pattern scores ~0
  regardless of how perfect the geometry is. A "hammer" on a 0.1%-range day is noise wearing
  a costume.
- **context** — the trend gate. Reversal patterns score `1.0` in the counter-trend regime
  they belong in (hammer ← `downtrend`), `0.4` in `choppy`/`unknown`, `0.15` in the wrong
  regime. Continuation patterns: `1.0` aligned, `0.3` otherwise. `indecision` and
  `compression` are `1.0` always — they are direction-neutral by definition.

**Aggregate with `max`, never `sum`.** Summing lets three weak patterns outscore one clean
one, which is exactly backwards.

### The pattern set

| Family | Patterns |
|---|---|
| Single-bar | `hammer`, `shooting_star`, `doji`, `marubozu_bull`, `marubozu_bear` |
| Two-bar | `bullish_engulfing`, `bearish_engulfing`, `bullish_harami`, `bearish_harami`, `piercing_line`, `dark_cloud_cover` |
| Three-bar | `morning_star`, `evening_star`, `three_white_soldiers`, `three_black_crows` |
| Range | `inside_bar`, `outside_bar`, `nr7` (narrowest range in 7) |

`nr7` is worth keeping even though it is not a classical candlestick pattern — a genuine
compression signal, and it pairs naturally with the breakout strategy.

---

## 2. Provider wiring

`packages/engine/engine/features/provider.py`, immediately after `compute_technicals`:

```python
patterns = detect_patterns(
    bars,
    atr=float(technicals["atr_14"]),
    trend_regime=str(technicals["trend_regime"]),
)
```

and `"patterns": patterns.as_dict()` into the returned feature dict.

> 🚨 **Add `"patterns"` to `_SNAPSHOT_BLOCKS` in `apps/agents/trading_agents/runtime.py`.**
> That tuple decides what lands in `reasoning.feature_snapshot`. Miss it and the block
> computes, feeds the fit, changes decisions — and never appears in the audit row, so nobody
> can check the machine's homework on it. Easiest thing in this plan to forget.

---

## 3. Fit integration — exactly two strategies

`_Features` gains `self._patterns = _block(features, "patterns")` and:

```python
def pattern(self, key: str) -> float | None:
    return _num(self._patterns.get(key))
```

**`rsi_mean_reversion`** — new component `candle_reversal_confirms`, weight **0.15**,
`directional=True`, scored off `reversal_bull` / `reversal_bear` by direction. A
mean-reversion entry is precisely where a reversal candle carries information.

**`breakout`** — new component `candle_confirms_break`, weight **0.10**, `directional=True`,
scored off `continuation_bull` / `continuation_bear` combined with `expansion`. A Donchian
break on a marubozu is a break; the same break on a doji is a probe.

**Do NOT add to `momentum`** (a 252-day trailing return does not care about one candle),
**`sma_crossover`**, or **`vol_regime_switch`**. Adding it to all five multiplies the
behaviour change by 2.5× for no additional signal, and `vol_regime_switch` at blind 0.400 is
one careless `directional=False` away from breaking the floor invariant.

### Understand the renormalisation before you ship it

`_weighted` divides by the weight total, so renormalisation is automatic — but that means
`rsi_mean_reversion`'s total goes 1.00 → 1.15 and **every existing component's effective
weight drops ~13% for every symbol**, not just ones with patterns. That is a real behaviour
change to the whole strategy, not an additive feature. It is acceptable, and it must be
*known*, which is what `test_absent_patterns_block_barely_moves_the_fit` is for.

Blind fractions after the change: `rsi_mean_reversion` 0.150 → ~0.130, `breakout` 0.350 →
~0.318. **Both improve**, which gives [`PLAN_AGGRESSIVE_PROFILE.md`](PLAN_AGGRESSIVE_PROFILE.md)
headroom on the floor. Confirm this with the test rather than trusting the arithmetic here.

**When the block is absent** (MOCK provider, thin history): `_num(None) → None →
_ramp(None) → NEUTRAL 0.5`. The component contributes a neutral 0.5 at 0.15 weight and
barely moves the mean. **That degradation is correct and automatic — do not special-case it.**

---

## 4. The technical analyst prompt

`apps/agents/trading_agents/nodes/technical_analyst.py`:

```python
PATTERN_FEATURES = (
    "top_pattern", "top_pattern_score", "reversal_bull", "reversal_bear",
    "continuation_bull", "continuation_bear", "indecision", "compression", "expansion",
)
...
patterns = ctx.get("patterns")
if patterns:
    body += "\nCandlestick patterns:\n" + render_features(patterns, PATTERN_FEATURES, label_width=25)
```

> ⚠️ **`names` is a tuple — do not put it through `render_features`**, which expects scalars.
> Render it separately as a comma join, or omit it entirely (`top_pattern` carries the
> headline).

`apps/agents/trading_agents/prompts/technical_analyst.py` — add to the "computed
deterministically upstream, treat as ground truth" block:

> - Candlestick pattern scores are 0–1, already **ATR-normalised** (a pattern on a range
>   smaller than half the average true range scores ~0) and already **trend-context-gated**
>   (a hammer scores high in a downtrend and near zero in an uptrend). **Do not re-apply the
>   trend yourself — that would double-count it.** `top_pattern` names the strongest
>   formation on the most recent bars. High `compression` with everything else low means a
>   coil: that is a setup, not a direction.

The existing prompt enumerates and explains every quant field it passes, so a new block
without matching prose is inconsistent with the file's own standard.

---

## 5. The chart — TradingView Lightweight Charts

**Use [Lightweight Charts](https://github.com/tradingview/lightweight-charts)** — TradingView's
open-source library. ~45KB, no account, no network calls, no data vendor.

⚠️ **Verify the license on the repo before shipping** (Apache-2.0 at time of writing) and
honour its attribution requirement. I did not open the LICENSE file this session — do not
take my word for it in a submission.

**Do NOT wire Alpaca's TradingView broker integration.** That is a link letting a *human*
trade an Alpaca account from TradingView's UI. It exposes no programmatic bars endpoint, adds
nothing our agent can consume, and we already have Alpaca bars.

Scope: in the desktop tree (`apps/mobile/src/desktop/`), render the underlying's daily
candles with **detected patterns marked on the bars that produced them**, plus entry and exit
points for any decision on that symbol. Paired with the Contract Funnel view this is the
strongest single visual in the demo — it shows the machine's actual reasoning on the actual
price action.

This is the **last** thing to build in this workstream. The feature is the pattern detection;
the chart is presentation.

---

## 6. Tests

New file `packages/engine/tests/test_features_patterns.py`, with a `_bar(o, h, l, c, day)`
helper. **One positive AND one negative test per pattern.** A bar that nearly qualifies but
fails one condition must score 0 and must not be named. "Hammer detected" proves nothing if a
doji also detects as a hammer.

### Revert-check matrix (CLAUDE.md §4.1 — break it, confirm the test fails, restore)

| Test | Break this to make it fail |
|---|---|
| **`test_a_micro_range_hammer_scores_zero`** | Delete the ATR magnitude ramp. Perfect hammer geometry with `rng = 0.05 × atr` — if it still passes, the ATR normalisation is doing nothing. |
| **`test_hammer_in_an_uptrend_is_heavily_discounted`** | Delete the trend-context multiplier. Identical bars, `trend_regime="uptrend"` vs `"downtrend"`, assert strictly lower. |
| `test_three_weak_patterns_do_not_outscore_one_clean_one` | Change the aggregate from `max` to `sum` |
| `test_zero_atr_returns_an_empty_block_without_raising` | Remove the guard |
| `test_fewer_than_seven_bars_does_not_raise` | Remove the length guard |

New file `apps/agents/tests/test_fit_patterns.py`:

| Test | Purpose |
|---|---|
| **`test_every_pattern_component_is_directional`** | Iterate the components of all five strategies; assert every `candle_*` has `directional is True`. The §0 constraint, enforced. |
| **`test_blind_weight_stays_below_the_trade_floor`** | `assert blind_weight_fraction(sid) < MIN_FIT_TO_TRADE` for all five. **This test does not exist today despite `fit.py:93` claiming it does.** Write it in whichever of B or C lands first — it is the only thing that catches a B↔C collision. |
| `test_absent_patterns_block_barely_moves_the_fit` | Same feature dict with and without `patterns`; assert \|Δfit\| ≤ 0.03. Guards against C silently retuning every symbol. |
| `test_patterns_reach_the_audit_row` | `"patterns"` is in `_SNAPSHOT_BLOCKS` |

Baseline: **792 passed, 9 skipped**; 9 pre-existing ruff errors. `git stash` and re-run before
blaming your change.

---

## 7. Where you are most likely to go wrong

1. **Reaching for `pandas-ta`.** It is installed, it has `cdl_pattern`, and importing it
   fails the entire suite at collection. Re-read §0.
2. **Returning bools.** They score NEUTRAL 0.5 forever via `_num`, and every "was it
   detected" test still passes. Invisible failure.
3. **Adding the component to all five strategies.** 2.5× the behaviour change for no extra
   signal.
4. **Setting `directional=False`** because "a candle doesn't know direction." It does —
   `reversal_bull` and `reversal_bear` are separate fields.
5. **Recomputing ATR inside `patterns.py`** instead of taking it as a parameter. Two
   implementations of one number; CLAUDE.md §4.4.
6. **Forgetting `_SNAPSHOT_BLOCKS`** in `runtime.py`. Computes, feeds the fit, never reaches
   the audit row.
7. **Putting `names` through `render_features`.** It is a tuple; the renderer expects scalars.
8. **Skipping the negative tests.** They are where the value is.
9. **Building the chart first** because it is the fun part. It is presentation; the detector
   is the feature.

---

*Related: [`PLAN_AGGRESSIVE_PROFILE.md`](PLAN_AGGRESSIVE_PROFILE.md) · [`HACKATHON.md`](HACKATHON.md) · [`../CLAUDE.md`](../CLAUDE.md)*
