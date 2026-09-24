"""Trade each thesis in the instrument that matches its horizon.

docs/PLAN_PLATFORM.md Phase 5, the operator's decision of 2026-09-23. What
it fixes: every entry went through a long option regardless of what the
strategy predicted, so a 60-trading-day momentum thesis was bought as a
~20-DTE contract and held for two days (strategies/horizon.py). An option
charges theta for every day the thesis has not yet played out; stock does
not.

The rule is one comparison and no model:

  horizon >= 20 trading days  -> equity. The thesis outlives any contract
                                 the desk would hold (options_max_dte is
                                 45-60 calendar days), and an edgeless
                                 signal costs ~0.1% per trade in stock
                                 against 5-8% of premium through an
                                 option (tests/eval/option_backtest).
  horizon <  20 trading days  -> option, still subject to every options
                                 gate (expected_move_below_breakeven,
                                 horizon_exceeds_contract, ...).
  a SHORT thesis              -> option (a put), whatever the horizon,
                                 unless equity shorting is enabled. Equity
                                 shorts stay off by design; the options
                                 rules then decide, and a horizon no
                                 contract can hold is refused by name.

Debit spreads are NOT a route. They were measured before being built and
lose more than a single leg under this desk's costs (8c3f25235).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from trading_agents.strategies.horizon import strategy_horizon_days

EQUITY_MIN_HORIZON_DAYS = 20
"""Trading days. Every trend strategy in horizon.py sits at or above it
(sma_crossover 20, breakout 20, momentum 60, the unknown-strategy default
20); rsi_mean_reversion (5) and vol_regime_switch (15) sit below."""


@dataclass(frozen=True)
class InstrumentRoute:
    instrument: Literal["equity", "option"]
    reason: str
    horizon_days: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def route_instrument(
    *,
    strategy_id: str | None,
    direction: str | None,
    requested: Literal["equity", "option"],
    allow_equity_shorts: bool = False,
) -> InstrumentRoute:
    horizon = strategy_horizon_days(strategy_id)
    if requested == "equity":
        return InstrumentRoute("equity", "requested_equity", horizon)
    if horizon < EQUITY_MIN_HORIZON_DAYS:
        return InstrumentRoute("option", f"horizon_{horizon}d_fits_an_option", horizon)
    if direction == "short" and not allow_equity_shorts:
        return InstrumentRoute("option", "short_thesis_needs_a_put", horizon)
    return InstrumentRoute("equity", f"horizon_{horizon}d_outlives_an_option", horizon)
