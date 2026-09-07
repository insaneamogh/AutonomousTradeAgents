# Why the book loses money, and the gate that is missing

**Measured 2026-09-07 on 21 recorded option positions, 8,080 price points.**
Reproduce with `python -m tests.eval.entry_quality` and
`python -m tests.eval.exit_replay`.

## What it is not

The exit ladder. `exit_replay` sweeps 16 ladder configurations against the
real premium paths; **every one loses**, from -12.7% to -36% per trade. The
plan's own hypothesis — tighten the stop 40 → 30 — is *worse* than the live
setting in both bounds. Exits are not the cause and tuning them is not the fix.

## What it is

### 1. Positions rarely go green

```
median MFE (best ever)   +2.9%
median MAE (worst ever) -20.6%
8 of 21 never traded above entry, not once
```

An exit can only capture a gain that existed. That ~7:1 asymmetry is exactly
why no ladder helps.

### 2. No detectable directional edge

Underlying move over the holding period vs the thesis: **6 right, 11 wrong,
1 flat**. 6/17 = 35%.

Stated honestly: this is **not** evidence of a negative edge. At n=17,
P(≤6 correct | true 50/50) = 0.17, and the 95% Wilson interval is 17%–59%.
The claim is *no detectable edge* — and a system with no edge that pays
theta and spread loses by construction.

### 3. The real finding: right is not enough

Among the six correct theses:

| underlying move | won |
|---|---|
| ≥ 2% | **2 of 2** (+43.4%, +22.3%) |
| < 2% | **1 of 4** (the winner made +2.1%) |

```
GILD155 call   underlying +0.21%  ->  option -23.5%
GILD150 call   underlying +0.64%  ->  option -13.8%
AMD     put    underlying -1.45%  ->  option -10.1%
```

Correct, and still down double digits. Theta and the spread outrun the delta
gain on a small move.

## The diagnosis

**We use signals that predict DIRECTION to trade an instrument that requires
MAGNITUDE AND SPEED.**

All five strategies — `sma_crossover`, `rsi_mean_reversion`, `momentum`,
`breakout`, `vol_regime_switch` — score *which way*. Not one estimates *how
far, how fast*. Nothing downstream compares an expected move against the move
the chosen contract needs merely to break even. A trending name that drifts
0.5% is a correct call and a losing trade, and the pipeline cannot tell those
apart.

This is a **missing gate, not a mis-tuned one**.

## The fix: `expected_move_below_breakeven`

A deterministic, named veto in `engine/options/rules/`, in the shape every
other rule already has.

1. **Required move.** For the selected contract over the intended horizon,
   the underlying move needed to recover premium decay plus the round-trip
   spread. Derivable from data already on `OptionLegDetails` — `delta`,
   `implied_volatility`, `bid`/`ask`, `expiry`.
2. **Expected move.** What the name actually does in that window: ATR-based,
   or the IV-implied expected move (`spot × IV × √(days/365)`). Both inputs
   already exist in the feature block.
3. **Refuse** when required > expected × margin. Contract selection can then
   retry a different strike rather than the pass dying — a nearer-the-money
   contract needs a smaller move.

Ship it the way Phase 1 shipped: named rule, first-veto-wins, revert-checked,
and replayed against these same 21 paths before it goes live. The replay
should show it refusing GILD155, GILD150 and AMD while keeping NVDA225 and
CDNS.

## Order of work

1. `expected_move_below_breakeven` — the missing gate
2. Reflection loop (Phase 2.2) — so per-strategy edge becomes measurable at all
3. Only then revisit exits, if there is ever an edge for them to protect

**Not** the exit ladder. That question is closed until something upstream
produces positions worth protecting.
