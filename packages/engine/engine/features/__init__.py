"""Real feature computation — bars → indicators → the council's feature dict.

Engine-side on purpose: agents receive pre-computed features and never
fetch data (PLAN.md §5.3). The agents package calls
``feature_provider_from_env()`` and passes the resulting callable into
``run_council``.
"""

from engine.features.bars import (
    AlpacaDailyBarsProvider,
    AlpacaIntradayBarsProvider,
    BarsProvider,
    IntradayBar,
    IntradayBarsProvider,
    intraday_provider_from_env,
)
from engine.features.clock import (
    CLOCK_TTL_SECONDS,
    AlpacaClock,
    MarketClock,
    clock_from_env,
)
from engine.features.corporate_actions import (
    AlpacaCorporateActionsProvider,
    CorporateActionFeatures,
    CorporateActionsProvider,
    CorporateEvent,
    compute_corporate_actions,
    corporate_actions_provider_from_env,
    parse_corporate_actions,
)
from engine.features.macro import (
    compute_macro,
    fred_latest,
    reset_fred_cache,
    sector_relative_strength,
)
from engine.features.market_calendar import (
    US_MARKET_HOLIDAYS,
    is_us_market_open,
    is_us_trading_day,
    minutes_until_us_market_open,
    us_market_session_bounds,
)
from engine.features.microstructure import (
    MAX_CREDIBLE_SPREAD_BPS,
    WIDE_SPREAD_BPS,
    AlpacaSnapshotProvider,
    LiquidityFeatures,
    QuoteProvider,
    compute_liquidity,
    snapshot_provider_from_env,
)
from engine.features.news import (
    LOOKBACK_DAYS as NEWS_LOOKBACK_DAYS,
)
from engine.features.news import (
    MAX_HEADLINES,
    AlpacaNewsProvider,
    NewsFeatures,
    NewsItem,
    NewsProvider,
    compute_news,
    news_provider_from_env,
    sanitize_headline,
)
from engine.features.provider import (
    DEFAULT_EQUITY_FALLBACK,
    HOLD_DAYS_BY_HORIZON,
    AlpacaAssetInfoProvider,
    AssetInfoProvider,
    FundamentalsProvider,
    MinimalOptionsContextProvider,
    OptionsContextProvider,
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
    atr_wilder,
    compute_technicals,
    rsi_wilder,
    sma,
)

__all__ = [
    "CLOCK_TTL_SECONDS",
    "DEFAULT_EQUITY_FALLBACK",
    "DEFAULT_LOOKBACK",
    "HOLD_DAYS_BY_HORIZON",
    "MAX_CREDIBLE_SPREAD_BPS",
    "MAX_HEADLINES",
    "MIN_BARS",
    "MIN_QUANT_BARS",
    "NEWS_LOOKBACK_DAYS",
    "US_MARKET_HOLIDAYS",
    "WIDE_SPREAD_BPS",
    "AlpacaAssetInfoProvider",
    "AlpacaClock",
    "AlpacaCorporateActionsProvider",
    "AlpacaDailyBarsProvider",
    "AlpacaIntradayBarsProvider",
    "AlpacaNewsProvider",
    "AlpacaSnapshotProvider",
    "AssetInfoProvider",
    "BarsProvider",
    "CorporateActionFeatures",
    "CorporateActionsProvider",
    "CorporateEvent",
    "DailyBar",
    "FundamentalsProvider",
    "InsufficientBarsError",
    "IntradayBar",
    "IntradayBarsProvider",
    "LiquidityFeatures",
    "MarketClock",
    "MinimalOptionsContextProvider",
    "NewsFeatures",
    "NewsItem",
    "NewsProvider",
    "OptionsContextProvider",
    "QuantFeatures",
    "QuoteProvider",
    "RealFeatureProvider",
    "atr_wilder",
    "clock_from_env",
    "compute_corporate_actions",
    "compute_liquidity",
    "compute_macro",
    "compute_news",
    "compute_quant",
    "compute_technicals",
    "corporate_actions_provider_from_env",
    "feature_provider_from_env",
    "fred_latest",
    "intraday_provider_from_env",
    "is_us_market_open",
    "is_us_trading_day",
    "minutes_until_us_market_open",
    "news_provider_from_env",
    "parse_corporate_actions",
    "relative_strength_ranks",
    "reset_fred_cache",
    "rsi_wilder",
    "sanitize_headline",
    "sector_relative_strength",
    "sma",
    "snapshot_provider_from_env",
    "us_market_session_bounds",
]
