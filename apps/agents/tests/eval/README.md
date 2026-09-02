# Deterministic funnel eval suite

100 labelled scenarios through the **real** funnel code. No network, no
credentials, no LLM calls, under a second.

```bash
# assertions (runs in CI)
.venv/bin/python -m pytest apps/agents/tests/eval -q

# scorecard
cd apps/agents && ../../.venv/bin/python -m tests.eval.run_eval
```

## What it answers

**"Does the deterministic layer actually fire?"** Yes — measured
2026-09-02: 40 of 100 cases are refused before any model call, every one
with a named reason. All 10 no-edge cases, all 10 thin-evidence cases and
all 10 illiquid-chain cases are refused for free.

## What it does not answer

**It is not a backtest.** No real bars, no measured P&L, no
survivorship-bias handling. It tests funnel LOGIC, not profitability.
Anyone quoting these numbers as strategy performance is misreading them.

## The finding that matters

Passing scores cluster inside a **0.3% band** (0.6075–0.6107 across 300
synthetic symbols; 18 distinct values). Two consequences:

1. `MIN_LLM_SCORE` cannot work as a quality dial — there is a cliff
   between 0.60 and 0.65 with nothing inside it. It is a switch.
2. Ranking the paid loop by score is near-arbitrary, because candidates
   tie to three decimals. Still better than watchlist order (deterministic,
   position-independent) but not yet "the best setups get the budget".

**Unverified:** measured on synthetic features, which are low-variance by
construction. Whether real Alpaca features spread the score out needs
live keys and has not been checked. That is the single highest-value
measurement to run next.

## Layout

| File | Role |
|---|---|
| `scenarios.py` | The 100 cases: 10 archetypes × 10 variations, each with a written justification |
| `funnel.py` | Runs one scenario through the real production functions |
| `test_funnel_eval.py` | Assertions, split into **contract** (must hold) and **characterisation** (measured tripwires) |
| `run_eval.py` | Human-readable scorecard |

Archetypes include the three shapes that have actually cost money here:
`thin_evidence` (the empty-dict scoring bug), `illiquid_chain` (the CME
-$1,200 loss), and `thin_open_interest` (sizing with no liquidity input).
