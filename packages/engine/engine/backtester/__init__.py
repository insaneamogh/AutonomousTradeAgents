"""Event-driven backtester.

Per PLAN.md §6.1:
- Event-driven, not vectorized (supports intraday, slippage, realistic fills)
- Walk-forward by default
- Realistic costs (SEC fee, FINRA TAF, spread)
- Slippage models: fixed bps, volume-participation, spread-based
- Survivorship-bias-free universe
- **Same vetoes that fire live** — strategy proposals route through
  ``engine.risk.evaluate`` via the ``RiskGate``. Backtests can't take
  trades that the production agent wouldn't be allowed to.

Phase 0/1 scaffold ships:
- Daily-bar event loop
- CSV + in-memory bar feeds
- SMA-crossover reference strategy
- Simulated broker with SEC + FINRA TAF + fixed-bps slippage
- RiskGate wiring (trims + vetoes recorded on ``BacktestResult``)
- Equity curve / max-drawdown / Sharpe reporting

Deferred:
- Intra-bar simulation (limit + stop)
- Multi-strategy + walk-forward harness
- 4 more reference strategies (RSI mean-rev, momentum, breakout, vol-regime switch)
"""

from engine.backtester.costs import SecFinraTafCosts, fixed_bps_slippage
from engine.backtester.engine import BacktestResult, Engine
from engine.backtester.events import Bar, FillEvent
from engine.backtester.feed import BarFeed, CsvBarFeed, InMemoryBarFeed
from engine.backtester.portfolio import Portfolio, Position
from engine.backtester.risk_gate import GateOutcome, RiskGate, TrimEvent, VetoEvent
from engine.backtester.sim_broker import SimulatedBroker
from engine.backtester.strategies import (
    Breakout,
    Momentum,
    RsiMeanReversion,
    SmaCrossover,
    VolRegimeSwitch,
)
from engine.backtester.strategy import Strategy
from engine.backtester.walk_forward import (
    WalkForwardReport,
    WalkForwardWindow,
    walk_forward,
)

__all__ = [
    "BacktestResult",
    "Bar",
    "BarFeed",
    "Breakout",
    "CsvBarFeed",
    "Engine",
    "FillEvent",
    "GateOutcome",
    "InMemoryBarFeed",
    "Momentum",
    "Portfolio",
    "Position",
    "RiskGate",
    "RsiMeanReversion",
    "SecFinraTafCosts",
    "SimulatedBroker",
    "SmaCrossover",
    "Strategy",
    "TrimEvent",
    "VetoEvent",
    "VolRegimeSwitch",
    "WalkForwardReport",
    "WalkForwardWindow",
    "fixed_bps_slippage",
    "walk_forward",
]
