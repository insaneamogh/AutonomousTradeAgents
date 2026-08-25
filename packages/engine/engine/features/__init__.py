"""Real feature computation — bars → indicators → the council's feature dict.

Engine-side on purpose: agents receive pre-computed features and never
fetch data (PLAN.md §5.3). The agents package calls
``feature_provider_from_env()`` and passes the resulting callable into
``run_council``.
"""

from engine.features.bars import AlpacaDailyBarsProvider, BarsProvider
from engine.features.macro import (
    compute_macro,
    fred_latest,
    reset_fred_cache,
    sector_relative_strength,
)
from engine.features.market_calendar import US_MARKET_HOLIDAYS, is_us_trading_day
from engine.features.provider import (
    DEFAULT_EQUITY_FALLBACK,
    FundamentalsProvider,
    RealFeatureProvider,
    feature_provider_from_env,
)
from engine.features.quant import (
    DEFAULT_LOOKBACK,
    MIN_QUANT_BARS,
    QuantFeatures,
    compute_quant,
    relative_strength_ranks,
)
from engine.features.technicals import (
    MIN_BARS,
    DailyBar,
    InsufficientBarsError,
    compute_technicals,
)

__all__ = [
    "DEFAULT_EQUITY_FALLBACK",
    "DEFAULT_LOOKBACK",
    "MIN_BARS",
    "MIN_QUANT_BARS",
    "US_MARKET_HOLIDAYS",
    "AlpacaDailyBarsProvider",
    "BarsProvider",
    "DailyBar",
    "FundamentalsProvider",
    "InsufficientBarsError",
    "QuantFeatures",
    "RealFeatureProvider",
    "compute_macro",
    "compute_quant",
    "compute_technicals",
    "feature_provider_from_env",
    "fred_latest",
    "is_us_trading_day",
    "relative_strength_ranks",
    "reset_fred_cache",
    "sector_relative_strength",
]
