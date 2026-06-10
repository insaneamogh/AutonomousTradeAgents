"""Reference strategies — these are baselines, not alpha.

PLAN.md §11 Phase 1 calls for 5: SMA crossover, RSI mean-reversion,
momentum (12-1), breakout (donchian), volatility-regime switch. All five
are shipped. They share helpers in ``_utils`` (RollingAtr, size_for_entry,
make_coid).

Pattern (look at any of them):
  - Dataclass with config + sizing_config + starting_equity + atr_window + confidence
  - Internal state with init=False fields
  - on_bar(bar) → list[OrderRequest]
  - Track _held_qty so SELL emits exactly what BUY filled
  - When sizing_config is set, delegate qty to engine.sizing.atr_position_size
"""

from engine.backtester.strategies.breakout import Breakout
from engine.backtester.strategies.momentum import Momentum
from engine.backtester.strategies.rsi_mean_reversion import RsiMeanReversion
from engine.backtester.strategies.sma_crossover import SmaCrossover
from engine.backtester.strategies.vol_regime_switch import VolRegimeSwitch

__all__ = [
    "Breakout",
    "Momentum",
    "RsiMeanReversion",
    "SmaCrossover",
    "VolRegimeSwitch",
]
