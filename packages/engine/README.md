# packages/engine

Deterministic trading engine. Pure Python. No LLM. The dull, audit-friendly half of the system.

## Submodules

| Submodule | Responsibility | Phase |
|---|---|---|
| `engine.db` | SQLAlchemy 2.0 async models, Base, session factory | 0 (done) |
| `engine.backtester` | Event-driven simulation with realistic fills, costs, slippage; routes proposals through `engine.risk` via `RiskGate`; **5 reference strategies + walk-forward harness** | **0/1 done** |
| `engine.risk` | Pre-trade veto rules (PDT, drawdown breaker, concentration, correlation, sizing caps, wash-sale informational) | **1 (done — 11 rules incl. informational, 48 tests across the engine; PLAN §6.2 complete)** |
| `engine.sizing` | Volatility-targeted position sizing (ATR-based) | **1 (done — `atr_position_size`, 8 tests)** |
| `engine.reconciler` | Periodic broker poll → `positions_snapshot` → circuit-breaker | **1 (done — Reconciler + Mock/Alpaca pollers)** |

## `engine.risk` — pre-trade veto layer

Public surface:
```python
from engine.risk import evaluate, RiskCaps, RiskProposal, RiskContext, Side, SpecialistScore

decision = evaluate(proposal, context, caps, specialists=[...])
# decision.approved : bool
# decision.veto_rule : 'drawdown_halt_active' | 'pdt_block' | ... | None
# decision.adjusted_qty : int | None   (when a sizing rule trimmed)
```

Rules run in this order (first veto wins). `position_size_cap` may TRIM, and the aggregate-exposure rules that follow see the trimmed qty — "size me to fit your risk policy" is what users expect.

 1. `drawdown_halt`       — account-level circuit breaker (PLAN.md §12 must-have)
 2. `forbid_short_phase_0` — long-only block for v1
 3. `min_council_confidence`
 4. `min_specialist_avg_score`
 5. `pdt_block`           — US Pattern Day Trader rule (regulatory hard line)
 6. `max_open_positions`
 7. `position_size_cap`   — single-trade sizing; may TRIM the proposal qty
 8. `correlation_cap`     — cluster breadth (tighter than sector); post-trim
 9. `sector_concentration`     — checked against the (possibly-trimmed) qty
10. `single_name_concentration` — checked against the (possibly-trimmed) qty
11. `wash_sale`           — INFORMATIONAL flag only (never vetoes); appends `wash_sale_warning` to `informational_flags`

**Stable `veto_rule` names** — every block returns one for audit:
`drawdown_halt_active`, `drawdown_halt_just_tripped`, `forbid_short_phase_0`,
`min_council_confidence`, `min_specialist_avg_score`, `pdt_block`,
`max_open_positions`, `correlation_cap`, `sector_concentration`,
`single_name_concentration`, `max_position_pct`, `max_position_pct_trim`.

**Informational flags** (never block; surface in `RiskDecision.informational_flags`):
`wash_sale_warning`, `sector_unknown`. UI dispatches on the literal — keep the vocabulary closed.

### Tests
```bash
PYTHONPATH=packages/engine:packages/broker uv run pytest packages/engine/tests/test_risk.py -v
# 19 passed (was 14)
```

**PLAN.md §6.2 deterministic risk-rule list is complete.** Real ρ-correlation matrices (vs. cluster-membership proxy) + per-close realized-PnL join for wash-sale (vs. `MockProvider`-fed) are Phase 1.5 polish.

## `engine.backtester` — scaffold

```bash
uv run python -m engine.backtester --smoke
uv run python -m engine.backtester --csv my_bars.csv --symbol AAPL
```

What ships in the Phase 0/1 backtester:
- Daily-bar event loop
- CSV + in-memory bar feeds
- All 5 reference strategies (PLAN.md §11 Phase 1 baseline complete):
  - `SmaCrossover` — fast/slow SMA cross
  - `RsiMeanReversion` — 14-period RSI, oversold/exit thresholds
  - `Momentum` — 12-1 momentum (lookback − skip)
  - `Breakout` — donchian channel (entry/exit windows)
  - `VolRegimeSwitch` — momentum gated by realized-vol regime
- All share helpers in `strategies/_utils.py` (RollingAtr, size_for_entry, make_coid).
  Strategies opt into vol-targeted sizing by passing `sizing_config=AtrSizingConfig(...)`.
- Simulated broker with SEC fee + FINRA TAF + fixed-bps slippage
- Equity curve, max-drawdown, daily Sharpe reporting
- RiskGate routes every proposal through `engine.risk.evaluate`
- **Walk-forward harness** (`engine.backtester.walk_forward`)

### Walk-forward harness

PLAN.md §6.1: "Walk-forward by default — train on rolling N months, test on next M months."

```python
from engine.backtester import walk_forward, SmaCrossover, InMemoryBarFeed

report = walk_forward(
    lambda: SmaCrossover(fast=20, slow=50, qty=10),   # factory: fresh per window
    InMemoryBarFeed(bars),
    train_bars=252,      # warm-up indicators on these (no orders placed)
    test_bars=63,        # full Engine.run() — scored
)

print(report.strategy_name, report.n_windows)
print(report.mean_return_pct, report.pct_winning_windows, report.mean_sharpe)
for w in report.windows:
    print(w.index, w.return_pct, w.trades)
```

Phase 0/1 is "rolling test with warm-up" — train slice fills indicator buffers, no parameter optimization yet. Phase 2 will add grid search on the train slice.

**Sharpe convention:** `mean_sharpe` is the mean of per-window Sharpes, NOT the pooled-returns Sharpe. More honest (one big window can't dominate); docstring on `walk_forward` explains.

Smoke comparison across all 5 strategies:
```bash
uv run python -m engine.backtester --walk-forward
```

Deferred to a later pass:
- Intra-bar simulation (limits, stops, OCO)
- Multi-symbol merged feed
- Parameter-optimization walk-forward (Phase 2)

## `engine.sizing` — ATR vol-targeted position sizing

PLAN.md §6.3: "Never percent-of-account fixed. Volatility-targeted by default."
A 4%-ATR name gets a smaller qty than a 1.5%-ATR name for the same dollar risk.

```python
from engine.sizing import atr_position_size, AtrSizingConfig, SizingInputs

decision = atr_position_size(
    SizingInputs(
        symbol="NVDA",
        last_price=229.04,
        atr_14=6.85,
        account_equity=100_000.0,
        confidence=0.62,        # 0..1 — linearly scales risk dollars
    ),
    config=AtrSizingConfig(
        risk_per_trade_pct=0.5,     # 1R = 0.5% of equity
        stop_atr_mult=2.0,          # stop sits 2× ATR below entry
        target_r_multiple=2.5,      # take-profit = entry + 2.5R
        max_position_pct=5.0,       # mirrors RiskCaps.max_position_pct
        fallback_position_pct=2.0,  # used when ATR is missing / 0 / negative
    ),
)
# decision.qty          : int — clamped + floored to whole shares
# decision.stop_price   : entry − stop_atr_mult × atr
# decision.target_price : entry + stop_atr_mult × atr × R-multiple
# decision.method       : "atr" | "fallback_pct"  (set when ATR isn't ready)
# decision.notes        : audit string for the journal
```

Wired into the **Strategy Selector** node — the LLM emits confidence + bull/bear; the sizer derives qty + stop + target deterministically. The wire DTO carries `stopLoss` + `targetPrice` so the mobile ApprovalCard can render them (Phase 3+).

Wired into the **backtester SMA strategy** as opt-in: pass `sizing_config=AtrSizingConfig(...)` to `SmaCrossover` and qty becomes vol-targeted. Fixed `qty=10` constructor still works for regression-baseline runs.

Fallback path: when `atr_14` is `None` / `0` / negative, the sizer uses `fallback_position_pct` of equity and stamps `method='fallback_pct'` on the decision. Callers can surface this to the user ("sizing relied on fallback — no ATR signal").

Kelly fraction is **not** implemented — PLAN.md §6.3 marks it as opt-in for advanced users; Phase 2 enhancement on top of vol-targeting.

## `engine.reconciler` — periodic broker poll → snapshot → breaker

```python
from engine.db.session import async_session_factory
from engine.reconciler import (
    MockBrokerPoller, AlpacaBrokerPoller,
    Reconciler, ReconcilerConfig,
)

# Phase 0/1 — synthetic state, no real broker:
rec = Reconciler(
    poller=MockBrokerPoller(equity=97_000.0),     # configurable per scenario
    session_factory=async_session_factory(),
    user_id=user_uuid,
    config=ReconcilerConfig(interval_seconds=30.0, halt_threshold_pct=-3.0),
)
rec.start()          # spawns the asyncio task
# ... rec.tick() runs once every 30s ...
await rec.stop()     # drains cleanly
```

Per tick:
1. `poller.get_account_state()` → equity / cash / buying_power / positions.
2. `write_snapshot()` inserts a `positions_snapshot` row; `daily_pnl_pct` is computed against the first snapshot of the same UTC day.
3. `evaluate_breaker()` flips `circuit_breaker_state` to `halted` if `daily_pnl_pct ≤ halt_threshold_pct`. **Never auto-unhalts** — the user must explicitly acknowledge via the API.

Wired into the FastAPI lifespan: when `USE_POSTGRES=1` and `RECONCILER_ENABLED=1` (the default when Postgres is on), the loop starts on app boot and stops on shutdown. Tune with `RECONCILER_INTERVAL_SECONDS` + `DRAWDOWN_HALT_THRESHOLD_PCT` envs.

The risk side: `PostgresRiskContextProvider` reads the newest snapshot + breaker state + PDT count and builds a `RiskContext`. The agent's risk-officer node picks this provider automatically when `USE_POSTGRES=1`.

Phase 0/1 simplifications (called out, not hidden):
- Daily-P&L window = UTC day. Phase 1 swaps to NY business days (`pandas_market_calendars`).
- PDT lookback = 5 calendar days. Phase 1 swaps to 5 business days.
- `MockBrokerPoller` is the default. `AlpacaBrokerPoller` exists but only wires when paper-trading validation closes (PLAN.md §11 Phase 4).
- One reconciler per process, one user. Multi-user is Phase 3 (after auth).

## Architecture rule
**This is the layer that disposes.** Risk decisions must remain LLM-free. If a future PR adds LLM reasoning *inside* a veto path, that's a regression — push back. The "Opus refinement" mentioned in PLAN.md §5.1 is an additive narration layer for proposals that have already cleared `engine.risk.evaluate`; it explains, never overrides.
